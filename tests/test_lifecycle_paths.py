"""
Non-happy-path lifecycles: repeated rejection → escalation, and the
deadline-expiry worker that runs unattended in production.
"""
import asyncio
import itertools
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from tests.test_e2e_local import _submission

_counter = itertools.count()


def _invited_case(client, login):
    n = next(_counter)
    claimant_email, respondent_email = f"cl{n}@paths.test", f"rs{n}@paths.test"
    headers = login(claimant_email)
    r = client.post("/api/cases/submit", headers=headers, json=_submission(
        claimant_email=claimant_email, respondent_email=respondent_email,
    ))
    case_id = r.json()["case_id"]
    assert client.post(f"/api/cases/{case_id}/send-invite", headers=headers).status_code == 200
    mails = client.get("/api/dev/outbox", params={"email": respondent_email}).json()["emails"]
    link = next(l for e in mails for l in e["links"] if f"/respond/{case_id}" in l)
    token = parse_qs(urlparse(link).query)["t"][0]
    return case_id, headers, respondent_email, token


def _status(client, case_id):
    return client.get(f"/api/cases/{case_id}/status").json()["status"]


def _play_round(client, case_id, headers, respondent_email, token, rnd, decision="REJECT"):
    r = client.post("/api/negotiation/submit-claimant-offer", headers=headers, json={
        "case_id": case_id, "round_number": rnd, "offer_amount": 58000 - rnd * 2000,
    })
    assert r.status_code == 200, r.text
    r = client.post("/api/negotiation/submit-respondent-offer", json={
        "case_id": case_id, "respondent_token": token, "round_number": rnd,
        "offer_amount": 20000 + rnd * 2000,
    })
    assert r.status_code == 200, r.text
    assert _status(client, case_id) == "PROPOSAL_ISSUED"
    r = client.post("/api/negotiation/proposal-response", headers=headers, json={
        "case_id": case_id, "round_number": rnd, "decision": decision, "party": "claimant",
    })
    assert r.status_code == 200, r.text
    r = client.post("/api/respondent/proposal-response", json={
        "case_id": case_id, "email": respondent_email, "token": token, "decision": decision,
    })
    assert r.status_code == 200, r.text
    return r.json()


def test_three_rejected_rounds_escalate_with_court_documents(client, login):
    case_id, headers, resp_email, token = _invited_case(client, login)

    out = _play_round(client, case_id, headers, resp_email, token, 1)
    assert out["outcome"] == "NEXT_ROUND"
    assert _status(client, case_id) == "NEGOTIATION_OPEN"

    out = _play_round(client, case_id, headers, resp_email, token, 2)
    assert out["outcome"] == "NEXT_ROUND"

    out = _play_round(client, case_id, headers, resp_email, token, 3)
    assert out["outcome"] == "ESCALATED"
    assert _status(client, case_id) == "ESCALATED"

    docs = client.get(f"/api/documents/{case_id}/all", headers=headers).json()["documents"]
    assert docs["mediation_certificate"]
    assert docs["court_file"]


def _expire_now(case_id):
    from app.services.cosmos_service import cosmos_service
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    cosmos_service.update_case(case_id, {"current_deadline": past})


def _run_expiry():
    from app.core.expiry_worker import run_expiry_check
    asyncio.run(run_expiry_check())


def test_unanswered_invite_auto_escalates(client, login):
    case_id, *_ = _invited_case(client, login)
    _expire_now(case_id)
    _run_expiry()
    assert _status(client, case_id) == "AUTO_ESCALATED"


def test_undecided_proposal_times_out_into_next_round(client, login):
    case_id, headers, resp_email, token = _invited_case(client, login)
    client.post("/api/negotiation/submit-claimant-offer", headers=headers, json={
        "case_id": case_id, "round_number": 1, "offer_amount": 55000,
    })
    client.post("/api/negotiation/submit-respondent-offer", json={
        "case_id": case_id, "respondent_token": token, "round_number": 1, "offer_amount": 30000,
    })
    assert _status(client, case_id) == "PROPOSAL_ISSUED"

    _expire_now(case_id)
    _run_expiry()
    status = client.get(f"/api/cases/{case_id}/status").json()
    assert status["status"] == "NEGOTIATION_OPEN"
    assert status["current_round"] == 2


def test_unanalysed_case_is_abandoned_after_deadline(client, login):
    headers = login("idle@paths.test")
    r = client.post("/api/cases/submit", headers=headers, json=_submission(
        claimant_email="idle@paths.test", respondent_email="never-invited@paths.test",
    ))
    case_id = r.json()["case_id"]
    assert _status(client, case_id) == "ANALYZED"
    _expire_now(case_id)
    _run_expiry()
    assert _status(client, case_id) == "ABANDONED"


def test_silent_next_round_escalates_after_deadline(client, login):
    case_id, headers, resp_email, token = _invited_case(client, login)
    _play_round(client, case_id, headers, resp_email, token, 1)
    assert _status(client, case_id) == "NEGOTIATION_OPEN"

    # Nobody makes a round-2 offer before the deadline.
    _expire_now(case_id)
    _run_expiry()
    assert _status(client, case_id) == "ESCALATED"
