"""Instalment plans: the respondent pays a settlement in pre-approved instalments."""
import io
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from pypdf import PdfReader

from tests.test_orgs import _org, _rows


def _invited(client, owner, org, n, i=0, amount=30000):
    row = {**_rows(n, 1, start=i)[0], "claim_amount": amount}
    out = client.post(f"/api/orgs/{org['id']}/cases/bulk", headers=owner,
                      json={"rows": [row], "template": "loan_default", "auto_invite": True}).json()
    case_id, resp = out["created"][0]["case_id"], f"tenant{i}.{n}@mail.test"
    mails = client.get("/api/dev/outbox", params={"email": resp}).json()["emails"]
    token = parse_qs(urlparse(next(l for m in mails for l in m["links"] if "/respond/" in l)).query)["t"][0]
    return case_id, resp, token


def _policy(client, owner, org, mx=6, minimum=5000):
    r = client.patch(f"/api/orgs/{org['id']}", headers=owner, json={"installments": {"max": mx, "min_amount": minimum}})
    assert r.status_code == 200, r.text


def test_respondent_chooses_plan_within_policy_and_agreement_states_it(client, login):
    org, owner, n = _org(client, login)
    _policy(client, owner, org, mx=3, minimum=5000)
    case_id, resp, token = _invited(client, owner, org, n)

    opts = client.get(f"/api/respondent/case/{case_id}", params={"t": token}).json()["payment_options"]
    assert opts == {"max_installments": 3, "min_installment_amount": 5000}
    too_many = client.post("/api/respondent/accept-in-full", json={"case_id": case_id, "token": token, "installments": 6})
    assert too_many.status_code == 400

    r = client.post("/api/respondent/accept-in-full", json={"case_id": case_id, "token": token, "installments": 3})
    assert r.status_code == 200, r.text
    case = client.get(f"/api/orgs/{org['id']}/cases/{case_id}", headers=owner).json()
    plan = case["payment"]["plan"]
    assert [p["amount"] for p in plan] == [10000, 10000, 10000]
    d0, d1 = (datetime.fromisoformat(plan[i]["due_at"]) for i in (0, 1))
    assert (d1 - d0).days == 30

    pdf = client.get(case["settlement_url"].split("http://testserver", 1)[1]).content
    text = " ".join("\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages).split())
    assert "3 monthly instalments" in text and "entire unpaid balance becomes immediately due" in text
    mail = next(m for m in client.get("/api/dev/outbox", params={"email": resp}).json()["emails"]
                if m["subject"].startswith("Payment plan"))
    assert "3 instalments" in mail["subject"]


def test_minimum_instalment_amount_is_enforced(client, login):
    org, owner, n = _org(client, login)
    _policy(client, owner, org, mx=6, minimum=10000)
    case_id, _, token = _invited(client, owner, org, n, amount=30000)
    r = client.post("/api/respondent/accept-in-full", json={"case_id": case_id, "token": token, "installments": 6})
    assert r.status_code == 400 and "at least" in r.json()["detail"]


def test_partial_payments_then_fully_paid(client, login):
    org, owner, n = _org(client, login)
    _policy(client, owner, org, mx=3)
    case_id, _, token = _invited(client, owner, org, n, amount=25000)
    client.post("/api/respondent/accept-in-full", json={"case_id": case_id, "token": token, "installments": 2})
    r = client.post(f"/api/cases/{case_id}/confirm-payment", params={"honored": True}, headers=owner).json()
    assert r["outcome"] == "PARTIALLY_PAID"
    pay = client.get(f"/api/orgs/{org['id']}/cases/{case_id}", headers=owner).json()["payment"]
    assert pay["status"] == "partially_paid" and pay["paid_amount"] == 12500 and pay["next_amount"] == 12500
    r = client.post(f"/api/cases/{case_id}/confirm-payment", params={"honored": True}, headers=owner).json()
    assert r["outcome"] == "PAYMENT_RECEIVED"
    case = client.get(f"/api/orgs/{org['id']}/cases/{case_id}", headers=owner).json()
    assert case["payment"]["status"] == "paid" and case["settlement_honored"] is True


def test_reminder_before_due_and_default_after_grace(client, login):
    from app.services import payments
    org, owner, n = _org(client, login)
    _policy(client, owner, org, mx=3)
    case_id, resp, token = _invited(client, owner, org, n, i=1, amount=30000)
    client.post("/api/respondent/accept-in-full", json={"case_id": case_id, "token": token, "installments": 3})
    plan = client.get(f"/api/orgs/{org['id']}/cases/{case_id}", headers=owner).json()["payment"]["plan"]
    due = datetime.fromisoformat(plan[0]["due_at"])

    payments.check_payment_schedules(now=due - timedelta(days=2))
    payments.check_payment_schedules(now=due - timedelta(days=1))      # no duplicate reminder
    reminders = [m for m in client.get("/api/dev/outbox", params={"email": resp}).json()["emails"]
                 if m["subject"].startswith("Reminder: Instalment 1 of 3")]
    assert len(reminders) == 1

    stats = payments.check_payment_schedules(now=due + timedelta(days=8))
    assert stats["defaulted"] >= 1
    case = client.get(f"/api/orgs/{org['id']}/cases/{case_id}", headers=owner).json()
    assert case["payment"]["status"] == "defaulted" and case["payment"]["balance_due"] == 30000
    assert case["breach_notice_url"]
    claimant_mail = client.get("/api/dev/outbox", params={"email": org["contact_email"]}).json()["emails"]
    assert any(m["subject"].startswith("Payment default: Rs. 30,000") for m in claimant_mail)


def test_individual_claimants_cannot_offer_plans(client, login):
    from tests.test_lifecycle_paths import _invited_case
    case_id, headers, resp_email, token = _invited_case(client, login)
    r = client.post("/api/respondent/accept-in-full", json={"case_id": case_id, "token": token, "installments": 2})
    assert r.status_code == 400


def test_razorpay_link_per_instalment(client, login, monkeypatch):
    import hashlib, hmac, json
    from app.config import settings
    from app.services import payments
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "secret")
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", "whsecret")
    monkeypatch.setattr(payments, "_razorpay_link",
                        lambda case, amount, no=1, total=1: {"link_id": f"plink_{no}", "link_url": f"https://rzp.io/i/{no}"})
    org, owner, n = _org(client, login)
    _policy(client, owner, org, mx=3)
    case_id, _, token = _invited(client, owner, org, n, i=2, amount=30000)
    client.post("/api/respondent/accept-in-full", json={"case_id": case_id, "token": token, "installments": 3})

    event = {"event": "payment_link.paid", "payload": {"payment_link": {"entity": {
        "id": "plink_2", "reference_id": f"{case_id}-2", "status": "paid"}}}}
    body = json.dumps(event).encode()
    sig = hmac.new(b"whsecret", body, hashlib.sha256).hexdigest()
    assert client.post("/api/payments/razorpay/webhook", content=body,
                       headers={"X-Razorpay-Signature": sig}).json() == {"ok": True}
    plan = client.get(f"/api/orgs/{org['id']}/cases/{case_id}", headers=owner).json()["payment"]["plan"]
    assert [p["status"] for p in plan] == ["pending", "paid", "pending"]
