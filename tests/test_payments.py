"""Collecting settlement payments (manual payee details and Razorpay links)."""
import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlparse

from tests.test_orgs import _org, _rows


def _settle_one(client, owner, org, n, **extra):
    out = client.post(f"/api/orgs/{org['id']}/cases/bulk", headers=owner,
                      json={"rows": _rows(n, 1), "template": "overdue_invoice", "auto_invite": True, **extra}).json()
    case_id, resp_email = out["created"][0]["case_id"], f"tenant0.{n}@mail.test"
    mails = client.get("/api/dev/outbox", params={"email": resp_email}).json()["emails"]
    token = parse_qs(urlparse(next(l for m in mails for l in m["links"] if "/respond/" in l)).query)["t"][0]
    r = client.post("/api/respondent/accept-in-full", json={"case_id": case_id, "token": token})
    assert r.status_code == 200, r.text
    return case_id, resp_email, token


def test_manual_payment_instructions_and_claimant_confirmation(client, login):
    org, owner, n = _org(client, login)
    client.patch(f"/api/orgs/{org['id']}", headers=owner, json={"payee": {
        "upi_id": "acme@okbank", "account_name": "Acme Pvt Ltd", "account_number": "0012345678", "ifsc": "HDFC0000123"}})
    case_id, resp_email, token = _settle_one(client, owner, org, n)

    case = client.get(f"/api/orgs/{org['id']}/cases/{case_id}", headers=owner).json()
    assert case["payment"]["provider"] == "manual" and case["payment"]["status"] == "pending"
    assert case["payment"]["payee"]["upi_id"] == "acme@okbank"
    view = client.get(f"/api/respondent/case/{case_id}", params={"t": token}).json()["case"]
    assert view["payment"]["amount"] == 25000

    mail = next(m for m in client.get("/api/dev/outbox", params={"email": resp_email}).json()["emails"]
                if m["subject"].startswith("Payment due"))
    assert "Rs. 25,000" in mail["subject"]

    r = client.post(f"/api/cases/{case_id}/confirm-payment", params={"honored": True}, headers=owner)
    assert r.status_code == 200
    case = client.get(f"/api/orgs/{org['id']}/cases/{case_id}", headers=owner).json()
    assert case["payment"]["status"] == "paid" and case["payment"]["confirmed_by"] == "claimant"


def test_razorpay_link_and_signed_webhook(client, login, monkeypatch):
    from app.config import settings
    from app.services import payments
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "secret")
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", "whsecret")
    created = {}

    def fake_link(case, amount):
        created.update(case_id=case["id"], amount=amount)
        return {"link_id": "plink_123", "link_url": "https://rzp.io/i/abc"}
    monkeypatch.setattr(payments, "_razorpay_link", fake_link)

    org, owner, n = _org(client, login)
    case_id, resp_email, _ = _settle_one(client, owner, org, n)
    assert created == {"case_id": case_id, "amount": 25000}
    case = client.get(f"/api/orgs/{org['id']}/cases/{case_id}", headers=owner).json()
    assert case["payment"]["link_url"] == "https://rzp.io/i/abc"
    mail = next(m for m in client.get("/api/dev/outbox", params={"email": resp_email}).json()["emails"]
                if m["subject"].startswith("Payment due"))
    assert "https://rzp.io/i/abc" in mail["links"]

    event = {"event": "payment_link.paid", "payload": {"payment_link": {"entity": {
        "id": "plink_123", "reference_id": case_id, "status": "paid"}}}}
    body = json.dumps(event).encode()
    forged = client.post("/api/payments/razorpay/webhook", content=body,
                         headers={"X-Razorpay-Signature": "0" * 64, "Content-Type": "application/json"})
    assert forged.status_code == 400
    sig = hmac.new(b"whsecret", body, hashlib.sha256).hexdigest()
    ok = client.post("/api/payments/razorpay/webhook", content=body,
                     headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"})
    assert ok.status_code == 200 and ok.json() == {"ok": True}
    case = client.get(f"/api/orgs/{org['id']}/cases/{case_id}", headers=owner).json()
    assert case["payment"]["status"] == "paid" and case["payment"]["confirmed_by"] == "razorpay"
    stats = client.get(f"/api/orgs/{org['id']}/stats", headers=owner).json()
    assert stats["payments_confirmed"] == 1


def test_razorpay_failure_falls_back_to_manual(client, login, monkeypatch):
    from app.config import settings
    from app.services import payments
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "secret")

    def boom(case, amount):
        raise RuntimeError("gateway down")
    monkeypatch.setattr(payments, "_razorpay_link", boom)
    org, owner, n = _org(client, login)
    case_id, _, _ = _settle_one(client, owner, org, n)
    case = client.get(f"/api/orgs/{org['id']}/cases/{case_id}", headers=owner).json()
    assert case["status"] == "SETTLED" and case["payment"]["provider"] == "manual"
