"""Human mediator review of AI proposals."""
import io
from urllib.parse import parse_qs, urlparse

import pytest
from pypdf import PdfReader

from tests.test_lifecycle_paths import _expire_now, _run_expiry
from tests.test_orgs import _org, _rows

MEDIATOR = "mediator@legalai.test"


@pytest.fixture
def review_org(client, login, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "MEDIATOR_EMAILS", MEDIATOR)
    org, owner, n = _org(client, login)
    client.patch(f"/api/orgs/{org['id']}", headers=owner, json={"require_mediator_review": True})
    return org, owner, n


def _case_with_offers(client, owner, org, n, i=0):
    out = client.post(f"/api/orgs/{org['id']}/cases/bulk", headers=owner, json={
        "rows": _rows(n, 1, start=i), "template": "overdue_invoice", "auto_invite": True}).json()
    case_id, resp = out["created"][0]["case_id"], f"tenant{i}.{n}@mail.test"
    mails = client.get("/api/dev/outbox", params={"email": resp}).json()["emails"]
    token = parse_qs(urlparse(next(l for m in mails for l in m["links"] if "/respond/" in l)).query)["t"][0]
    client.post("/api/negotiation/submit-claimant-offer", headers=owner, json={
        "case_id": case_id, "round_number": 1, "offer_amount": 25000})
    client.post("/api/negotiation/submit-respondent-offer", json={
        "case_id": case_id, "respondent_token": token, "round_number": 1, "offer_amount": 15000})
    return case_id, resp, token


def test_proposal_waits_for_mediator_and_is_issued_on_approval(client, login, review_org):
    org, owner, n = review_org
    case_id, resp, token = _case_with_offers(client, owner, org, n)
    status = client.get(f"/api/cases/{case_id}/status").json()["status"]
    assert status == "MEDIATOR_REVIEW"
    assert not client.get(f"/api/respondent/case/{case_id}", params={"t": token}).json()["negotiation"]["proposal_pending"]

    assert client.get("/api/mediator/queue", headers=owner).status_code == 403
    mediator = login(MEDIATOR)
    queue = client.get("/api/mediator/queue", headers=mediator).json()["queue"]
    item = next(q for q in queue if q["case_id"] == case_id)
    assert item["draft"]["amount"] and item["claimant_offer"] == 25000

    bad = client.post(f"/api/mediator/cases/{case_id}/approve", headers=mediator, json={"amount": 30000})
    assert bad.status_code == 400
    ok = client.post(f"/api/mediator/cases/{case_id}/approve", headers=mediator, json={
        "amount": 20000, "reasoning": "Both parties have documented positions; Rs. 20,000 fairly splits the remaining gap."})
    assert ok.status_code == 200 and ok.json()["edited"] is True
    assert client.get(f"/api/cases/{case_id}/status").json()["status"] == "PROPOSAL_ISSUED"
    view = client.get(f"/api/respondent/case/{case_id}", params={"t": token}).json()["negotiation"]
    assert view["ai_proposed_amount"] == 20000

    client.post("/api/respondent/proposal-response", json={"case_id": case_id, "token": token, "decision": "ACCEPT"})
    client.post("/api/negotiation/proposal-response", headers=owner, json={
        "case_id": case_id, "round_number": 1, "decision": "ACCEPT", "party": "claimant"})
    case = client.get(f"/api/orgs/{org['id']}/cases/{case_id}", headers=owner).json()
    assert case["status"] == "SETTLED" and case["settled_amount"] == 20000
    pdf = client.get(case["settlement_url"].split("http://testserver", 1)[1]).content
    text = " ".join("\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages).split())
    assert f"approved by mediator {MEDIATOR}" in text


def test_unreviewed_draft_is_released_after_sla(client, login, review_org):
    org, owner, n = review_org
    case_id, resp, token = _case_with_offers(client, owner, org, n, i=1)
    assert client.get(f"/api/cases/{case_id}/status").json()["status"] == "MEDIATOR_REVIEW"
    _expire_now(case_id)
    _run_expiry()
    assert client.get(f"/api/cases/{case_id}/status").json()["status"] == "PROPOSAL_ISSUED"
    from app.services.cosmos_service import cosmos_service
    rnd = cosmos_service.get_negotiation_by_case(case_id)["rounds"][0]
    assert rnd["mediator_review"]["released_without_review"] is True


def test_without_review_setting_proposals_issue_immediately(client, login, monkeypatch):
    org, owner, n = _org(client, login)
    case_id, _, _ = _case_with_offers(client, owner, org, n, i=2)
    assert client.get(f"/api/cases/{case_id}/status").json()["status"] == "PROPOSAL_ISSUED"
