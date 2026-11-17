"""Jurisdiction packs: currency, law, forums and documents follow the case's governing law."""
import io
import json

import pytest
from pypdf import PdfReader

from app.core.jurisdictions import JURISDICTIONS, localize_prompt, money, normalize_jurisdiction
from tests.test_llm_path import fake_llm  # noqa: F401  (fixture)
from tests.test_orgs import _org, _rows

INDIAN_MARKERS = ("Indian Contract Act", "Rs.", "India", "BNS", "Lok Adalat", "Information Technology Act", "laws of India",
                  "Consumer Protection Act, 2019", "Limitation Act, 1963")


@pytest.mark.parametrize("amount,currency,expected", [
    (36000, "INR", "Rs. 36,000"), (1250, "GBP", "£1,250"), (800, "SGD", "S$800"),
    (5000, "AED", "AED 5,000"), (300, "USD", "$300"), (99.5, "GBP", "£99.50"), (10, "XYZ", "XYZ 10"),
])
def test_money(amount, currency, expected):
    assert money(amount, currency) == expected


def test_money_from_case_and_aliases():
    assert money(10, {"jurisdiction": "SG"}) == "S$10"
    assert money(10, {"currency": "AED", "jurisdiction": "AE"}) == "AED 10"
    assert normalize_jurisdiction("uk") == "GB" and normalize_jurisdiction("UAE") == "AE"
    assert normalize_jurisdiction("FR") is None


def test_every_pack_is_complete():
    for code, j in JURISDICTIONS.items():
        assert j.currency and j.governing_law and j.esign_law and j.notice_days > 0
        assert set(j.extra["court"]) >= {"cost_min", "filing_fee", "lawyer_fees", "miscellaneous", "months"}
        if code != "IN":
            assert j.forum_for("monetary_civil", 1) and j.laws_for("contract_breach") and j.llm_context


def test_prompt_override_only_for_foreign_cases():
    assert localize_prompt("BASE", {"jurisdiction": "IN"}) == "BASE"
    out = localize_prompt("BASE", {"jurisdiction": "SG"})
    assert out.endswith("BASE") and "Singapore" in out and "never cite Indian law" in out


def _uk_org(client, login):
    org, owner, n = _org(client, login, jurisdiction="GB")
    assert org["settings"]["jurisdiction"] == "GB"
    return org, owner, n


def test_uk_case_end_to_end_has_no_indian_law_or_rupees(client, login):
    org, owner, n = _uk_org(client, login)
    row = {**_rows(n, 1)[0], "claim_amount": 3200, "respondent_phone": "07700 900123"}
    out = client.post(f"/api/orgs/{org['id']}/cases/bulk", headers=owner,
                      json={"rows": [row], "template": "overdue_invoice", "auto_invite": True}).json()
    assert out["created"], out
    case_id = out["created"][0]["case_id"]
    case = client.get(f"/api/orgs/{org['id']}/cases/{case_id}", headers=owner).json()

    assert case["jurisdiction"] == "GB" and case["currency"] == "GBP"
    assert "£3,200" in case["dispute_text"] and "Rs." not in case["dispute_text"]
    legal = case["legal_data"]
    blob = json.dumps(legal)
    assert not any(m in blob for m in INDIAN_MARKERS), blob
    assert "County Court — small claims track" in legal["forum_name"]
    assert legal["legal_notice_notice_period_days"] == 30 and legal["limitation_period_years"] == 6
    assert case["analytics_data"]["time_to_resolution_months"] == 12
    assert not any(m in json.dumps(case["intake_data"]) for m in INDIAN_MARKERS)
    for key in ("demand_letter_url", "court_file_url"):
        pdf = client.get(case["documents_data"][key].split("http://testserver", 1)[1]).content
        doc = " ".join("\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages).split())
        assert not any(m in doc for m in INDIAN_MARKERS), (key, doc)

    wa = client.get("/api/dev/outbox", params={"email": "+447700900123"}).json()["emails"]
    assert wa and "£3,200" in wa[0]["text"]

    # Respondent accepts: agreement and payment request are in pounds under English law.
    from urllib.parse import parse_qs, urlparse
    token = parse_qs(urlparse(wa[0]["links"][0]).query)["t"][0]
    view = client.get(f"/api/respondent/case/{case_id}", params={"t": token}).json()
    assert view["jurisdiction"]["currency"] == "GBP" and view["jurisdiction"]["locale"] == "en-GB"
    r = client.post("/api/respondent/accept-in-full", json={"case_id": case_id, "token": token})
    assert r.status_code == 200, r.text
    case = client.get(f"/api/orgs/{org['id']}/cases/{case_id}", headers=owner).json()
    pdf = client.get(case["settlement_url"].split("http://testserver", 1)[1]).content
    text = " ".join("\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages).split())
    assert "the laws of England and Wales" in text and "Electronic Communications Act 2000" in text
    assert "GBP 3,200" in text
    assert not any(m in text for m in ("Indian Contract Act", "Rs.", "laws of India", "Information Technology Act")), text
    assert case["payment"]["currency"] == "GBP"

    stats = client.get(f"/api/orgs/{org['id']}/stats", headers=owner).json()
    assert stats["currency"] == "GBP" and stats["by_currency"]["GBP"]["claimed"] == 3200


def test_row_overrides_org_jurisdiction_and_bad_values_rejected(client, login):
    org, owner, n = _uk_org(client, login)
    rows = [{**_rows(n, 1, start=1)[0], "jurisdiction": "SG", "claim_amount": 900},
            {**_rows(n, 1, start=2)[0], "jurisdiction": "FR"}]
    out = client.post(f"/api/orgs/{org['id']}/cases/bulk", headers=owner,
                      json={"rows": rows, "template": "overdue_invoice"}).json()
    assert len(out["created"]) == 1 and "Unsupported jurisdiction" in out["errors"][0]["error"]
    case = client.get(f"/api/orgs/{org['id']}/cases/{out['created'][0]['case_id']}", headers=owner).json()
    assert case["currency"] == "SGD" and "Small Claims Tribunals" in case["legal_data"]["forum_name"]

    assert client.patch(f"/api/orgs/{org['id']}", headers=owner, json={"jurisdiction": "XX"}).status_code == 400
    assert client.patch(f"/api/orgs/{org['id']}", headers=owner,
                        json={"jurisdiction": "uae"}).json()["settings"]["jurisdiction"] == "AE"


def test_individual_filing_sets_currency_from_jurisdiction(client, login):
    from tests.test_e2e_local import _submission
    headers = login("sg.claimant@example.com")
    body = _submission(claimant_email="sg.claimant@example.com", jurisdiction="SG",
                       claimant_state="Singapore", claimant_city="Singapore")
    r = client.post("/api/cases/submit", headers=headers, json=body)
    assert r.status_code == 200, r.text
    case = client.get(f"/api/cases/{r.json()['case_id']}", headers=headers).json()
    assert case["jurisdiction"] == "SG" and case["currency"] == "SGD"
    bad = client.post("/api/cases/submit", headers=headers, json={**body, "jurisdiction": "Mars"})
    assert bad.status_code == 422


def test_razorpay_is_not_used_for_foreign_currency(client, login, monkeypatch):
    from app.config import settings
    from app.services import payments
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "rzp_test")
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "secret")
    assert payments.provider({"currency": "GBP", "jurisdiction": "GB"}) == "manual"
    assert payments.provider({"currency": "INR", "jurisdiction": "IN"}) == "razorpay"


def test_llm_prompts_carry_the_jurisdiction(client, login, fake_llm):  # noqa: F811
    seen = []

    def responder(system, user):
        seen.append(system)
        return "{}"
    fake_llm(responder)
    org, owner, n = _uk_org(client, login)
    out = client.post(f"/api/orgs/{org['id']}/cases/bulk", headers=owner,
                      json={"rows": [_rows(n, 1, start=3)[0]], "template": "overdue_invoice"}).json()
    assert out["created"]
    overridden = [s for s in seen if s.startswith("JURISDICTION OVERRIDE")]
    assert len(overridden) >= 3 and all("England and Wales" in s for s in overridden)
