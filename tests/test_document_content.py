"""Check what generated documents actually SAY, not just that they exist."""
import io
from urllib.parse import parse_qs, urlparse

from pypdf import PdfReader

from tests.test_e2e_local import _submission


def _pdf_text(client, url):
    r = client.get(url.split("http://testserver", 1)[1])
    assert r.status_code == 200
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(r.content)).pages)


def _invite(client, headers, case_id, resp_email):
    client.post(f"/api/cases/{case_id}/send-invite", headers=headers)
    mails = client.get("/api/dev/outbox", params={"email": resp_email}).json()["emails"]
    return parse_qs(urlparse(next(l for e in mails for l in e["links"] if "/respond/" in l)).query)["t"][0]


def test_demand_letter_sent_to_respondent_hides_claimant_private_analysis(client, login):
    headers = login("dl@docs.test")
    r = client.post("/api/cases/submit", headers=headers, json=_submission(
        claimant_email="dl@docs.test", respondent_email="dl-r@docs.test", claimant_phone="9812345678"))
    case = client.get(f"/api/cases/{r.json()['case_id']}", headers=headers).json()
    text = _pdf_text(client, case["documents_data"]["demand_letter_url"])
    for private in ("WEAKNESSES", "MISSING PROOF", "EVIDENCE GAPS", "LIKELY RESPONDENT", "9812345678"):
        assert private not in text, private
    assert "Asha Verma" in text
    assert "Indian Contract Act" in text and "Rs. 60,000" in text

    court = _pdf_text(client, case["documents_data"]["court_file_url"])
    assert "CLAIMANT STRENGTHS" in court  # the claimant-only court file keeps the full record


def test_monetary_settlement_agreement_has_real_terms(client, login):
    headers = login("sa@docs.test")
    r = client.post("/api/cases/submit", headers=headers, json=_submission(
        claimant_email="sa@docs.test", respondent_email="sa-r@docs.test"))
    case_id = r.json()["case_id"]
    token = _invite(client, headers, case_id, "sa-r@docs.test")
    client.post("/api/respondent/accept-in-full", json={"case_id": case_id, "token": token})
    url = client.get(f"/api/documents/{case_id}/settlement", headers=headers).json()["download_url"]
    text = _pdf_text(client, url)
    assert "To be filled" not in text
    assert "Rs. 60,000" in text and "within 14 days" in text
    assert "FULL AND FINAL RELEASE" in text and "GOVERNING LAW" in text
    assert "WEAKNESSES" not in text


def test_non_monetary_settlement_lists_agreed_actions_not_rupees(client, login):
    headers = login("nm@docs.test")
    r = client.post("/api/cases/submit", headers=headers, json=_submission(
        claimant_email="nm@docs.test", respondent_email="nm-r@docs.test", claim_amount=None,
        dispute_text=("My neighbour plays loud music past midnight and blocks my parking spot. "
                      "I want the noise to stop and the parking kept clear.")))
    case_id = r.json()["case_id"]
    token = _invite(client, headers, case_id, "nm-r@docs.test")
    client.post("/api/negotiation/submit-claimant-offer", headers=headers, json={
        "case_id": case_id, "round_number": 1, "demands": ["Stop loud music after 10 pm", "Keep parking clear"]})
    client.post("/api/negotiation/submit-respondent-offer", json={
        "case_id": case_id, "respondent_token": token, "round_number": 1,
        "commitments": ["Stop loud music after 10 pm"]})
    client.post("/api/negotiation/proposal-response", headers=headers, json={
        "case_id": case_id, "round_number": 1, "decision": "ACCEPT", "party": "claimant"})
    client.post("/api/respondent/proposal-response", json={"case_id": case_id, "token": token, "decision": "ACCEPT"})

    url = client.get(f"/api/documents/{case_id}/settlement", headers=headers).json()["download_url"]
    text = " ".join(_pdf_text(client, url).split())
    assert "Stop loud music after 10 pm" in text and "Keep parking clear" in text
    assert "Rs. 0" not in text and "To be filled" not in text


def test_breach_notice_and_certificate_use_the_real_dates():
    import asyncio
    from app.agents.document_agent import _generate_breach_notice_pdf, _generate_mediation_certificate
    case = {"id": "abcd1234-0000", "track": "monetary_civil", "claimant_name": "A", "respondent_name": "B",
            "claimant_email": "a@x.com", "respondent_email": "b@x.com", "claimant_city": "Pune",
            "claimant_state": "Maharashtra", "respondent_type": "individual", "dispute_text": "Deposit.",
            "claim_amount": 60000, "settled_amount": 45000,
            "created_at": "2026-09-03T08:00:00+00:00", "settled_at": "2026-10-01T10:00:00+00:00"}
    breach = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(asyncio.run(_generate_breach_notice_pdf(case)))).pages)
    cert = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(_generate_mediation_certificate(case))).pages)
    assert "01 October 2026" in breach
    assert "03 September 2026" in cert


def test_law_references_read_naturally():
    from app.agents.document_agent import _law_ref
    assert _law_ref({"act": "Indian Contract Act, 1872", "section": "Section 73"}) == "Indian Contract Act, 1872 — Section 73"
    assert _law_ref({"act": "NI Act", "section": "138"}) == "NI Act — Section 138"
    assert _law_ref({"act": "Rent Act", "section": "Deposit / refund provisions"}) == "Rent Act — Deposit / refund provisions"
    assert _law_ref({"act": "CPC", "section": "Order XXXVII"}) == "CPC — Order XXXVII"
    assert _law_ref({"act": "Tort law", "section": ""}) == "Tort law"
