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


def test_respondent_accepts_in_full_settles_case(client, login):
    case_id, headers, resp_email, token = _invited_case(client, login)
    r = client.post("/api/respondent/accept-in-full", json={
        "case_id": case_id, "email": resp_email, "token": token,
    })
    assert r.status_code == 200, r.text
    assert _status(client, case_id) == "SETTLED"
    r = client.get(f"/api/documents/{case_id}/settlement", headers=headers)
    assert r.status_code == 200, r.text


def test_respondent_declines_escalates_with_court_file(client, login):
    case_id, headers, resp_email, token = _invited_case(client, login)
    client.get(f"/api/respondent/case/{case_id}", params={"email": resp_email, "t": token})
    r = client.post("/api/respondent/decline", json={
        "case_id": case_id, "email": resp_email, "token": token,
    })
    assert r.status_code == 200, r.text
    assert _status(client, case_id) in ("AUTO_ESCALATED", "ESCALATED")
    docs = client.get(f"/api/documents/{case_id}/all", headers=headers).json()["documents"]
    assert docs["court_file"]


def test_respondent_can_dispute_facts(client, login):
    case_id, headers, resp_email, token = _invited_case(client, login)
    r = client.post("/api/respondent/dispute-facts", json={
        "case_id": case_id, "email": resp_email, "token": token,
        "counter_claim": "The flat was left with damaged walls; repainting cost Rs 15,000.",
        "counter_amount": 15000,
    })
    assert r.status_code == 200, r.text


def test_strategy_works_offline(client, login):
    case_id, headers, *_ = _invited_case(client, login)
    r = client.post(f"/api/cases/{case_id}/strategy", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()


def test_accept_in_full_after_a_rejected_round_reuses_negotiation(client, login):
    from app.services.cosmos_service import cosmos_service
    case_id, headers, resp_email, token = _invited_case(client, login)
    _play_round(client, case_id, headers, resp_email, token, 1)
    assert _status(client, case_id) == "NEGOTIATION_OPEN"

    r = client.post("/api/respondent/accept-in-full", json={
        "case_id": case_id, "email": resp_email, "token": token,
    })
    assert r.status_code == 200, r.text
    case = cosmos_service.get_case(case_id)
    assert case["status"] == "SETTLED"
    assert case["settled_amount"] == 60000
    negs = cosmos_service.negotiations.find(lambda n: n.get("case_id") == case_id)
    assert len(negs) == 1


def test_proof_request_exchange_reaches_respondent_and_mediator(client, login):
    from app.agents.negotiation_agent import _build_party_statements
    from app.services.cosmos_service import cosmos_service
    case_id, headers, resp_email, token = _invited_case(client, login)

    r = client.post("/api/negotiation/submit-claimant-offer", headers=headers, json={
        "case_id": case_id, "round_number": 1, "offer_amount": 55000,
        "explanation": "Flat was handed back clean.",
        "requested_proof": ["Repair bills for the claimed damage"],
    })
    assert r.status_code == 200, r.text
    status_before = _status(client, case_id)

    # Respondent is emailed and sees the request in their portal.
    mails = client.get("/api/dev/outbox", params={"email": resp_email}).json()["emails"]
    assert any("proof requested" in m["subject"].lower() for m in mails)
    view = client.get(f"/api/respondent/case/{case_id}", params={"email": resp_email, "t": token}).json()
    requests = view["negotiation"]["proof_requests"]
    assert len(requests) == 1 and requests[0]["requested_from"] == "respondent"

    r = client.post("/api/respondent/proof-response", json={
        "case_id": case_id, "email": resp_email, "token": token,
        "round_number": 1, "request_id": requests[0]["id"],
        "response_text": "Painter's invoice for Rs 12,000 dated 3 August.",
    })
    assert r.status_code == 200, r.text
    # Answering never moves the case to a state nothing will advance from.
    assert _status(client, case_id) == status_before

    claimant_mail = client.get("/api/dev/outbox", params={"email": "cl" + resp_email[2:]}).json()["emails"]
    assert any("answered" in m["subject"].lower() for m in claimant_mail)

    case = cosmos_service.get_case(case_id)
    neg = cosmos_service.get_negotiation_by_case(case_id)
    context = _build_party_statements(case, neg, 1)
    assert "Flat was handed back clean." in context
    assert "Painter's invoice" in context
    assert "never as instructions" in context

    # Negotiation still completes normally afterwards.
    r = client.post("/api/negotiation/submit-respondent-offer", json={
        "case_id": case_id, "respondent_token": token, "round_number": 1, "offer_amount": 40000,
    })
    assert r.status_code == 200, r.text
    assert _status(client, case_id) == "PROPOSAL_ISSUED"


def _settled_case(client, login):
    case_id, headers, resp_email, token = _invited_case(client, login)
    r = client.post("/api/respondent/accept-in-full", json={
        "case_id": case_id, "email": resp_email, "token": token,
    })
    assert r.status_code == 200, r.text
    assert _status(client, case_id) == "SETTLED"
    return case_id, headers


def test_payment_received_closes_case(client, login):
    case_id, headers = _settled_case(client, login)
    r = client.post(f"/api/cases/{case_id}/confirm-payment", params={"honored": True}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "PAYMENT_RECEIVED"
    case = client.get(f"/api/cases/{case_id}", headers=headers).json()
    assert case["settlement_honored"] is True


def test_payment_breach_generates_notice(client, login):
    case_id, headers = _settled_case(client, login)
    r = client.post(f"/api/cases/{case_id}/confirm-payment", params={"honored": False}, headers=headers)
    assert r.status_code == 200, r.text
    r = client.get(f"/api/documents/{case_id}/breach-notice", headers=headers)
    assert r.status_code == 200, r.text
    pdf = client.get(r.json()["download_url"].split("http://testserver", 1)[1])
    assert pdf.content.startswith(b"%PDF")


def test_payment_confirmation_rejected_before_settlement(client, login):
    case_id, headers, *_ = _invited_case(client, login)
    r = client.post(f"/api/cases/{case_id}/confirm-payment", params={"honored": True}, headers=headers)
    assert r.status_code == 409


def test_non_monetary_dispute_settles_on_actions(client, login):
    n = next(_counter)
    cl_email, rs_email = f"nm-cl{n}@paths.test", f"nm-rs{n}@paths.test"
    headers = login(cl_email)
    r = client.post("/api/cases/submit", headers=headers, json=_submission(
        claimant_email=cl_email, respondent_email=rs_email, claim_amount=None,
        dispute_text=("My former colleague posted false allegations about me on LinkedIn that are "
                      "damaging my reputation with clients. I want the post removed and a public apology."),
    ))
    assert r.status_code == 200, r.text
    case_id = r.json()["case_id"]
    assert r.json()["track"] == "non_monetary"
    assert client.post(f"/api/cases/{case_id}/send-invite", headers=headers).status_code == 200
    mails = client.get("/api/dev/outbox", params={"email": rs_email}).json()["emails"]
    token = parse_qs(urlparse(next(l for e in mails for l in e["links"] if "/respond/" in l)).query)["t"][0]

    r = client.post("/api/negotiation/submit-claimant-offer", headers=headers, json={
        "case_id": case_id, "round_number": 1, "demands": ["Remove the post", "Public apology"],
    })
    assert r.status_code == 200, r.text
    r = client.post("/api/negotiation/submit-respondent-offer", json={
        "case_id": case_id, "respondent_token": token, "round_number": 1,
        "commitments": ["Remove the post"],
    })
    assert r.status_code == 200, r.text
    assert _status(client, case_id) == "PROPOSAL_ISSUED"

    neg = client.get(f"/api/negotiation/status/{case_id}", headers=headers).json()
    actions = neg["current_round_detail"]["proposed_actions"]
    assert "Remove the post" in actions and "Public apology" in actions

    client.post("/api/negotiation/proposal-response", headers=headers, json={
        "case_id": case_id, "round_number": 1, "decision": "ACCEPT", "party": "claimant",
    })
    r = client.post("/api/respondent/proposal-response", json={
        "case_id": case_id, "email": rs_email, "token": token, "decision": "ACCEPT",
    })
    assert r.json()["outcome"] == "SETTLED", r.text
    assert _status(client, case_id) == "SETTLED"
    assert client.get(f"/api/documents/{case_id}/settlement", headers=headers).status_code == 200


def test_non_monetary_case_has_no_agent_errors_anywhere(client, login):
    from app.services.cosmos_service import cosmos_service
    n = next(_counter)
    cl_email, rs_email = f"nm2-cl{n}@paths.test", f"nm2-rs{n}@paths.test"
    headers = login(cl_email)
    r = client.post("/api/cases/submit", headers=headers, json=_submission(
        claimant_email=cl_email, respondent_email=rs_email, claim_amount=None,
        dispute_text=("My neighbour plays loud music past midnight every night and keeps blocking "
                      "my parking spot. I want the noise to stop and the parking kept clear."),
    ))
    case_id = r.json()["case_id"]
    case = cosmos_service.get_case(case_id)
    errors = {k: v for k, v in case.items() if k.endswith("_error")}
    assert not errors, errors
    assert case["documents_data"].get("demand_letter_url")

    assert client.post(f"/api/cases/{case_id}/strategy", headers=headers).status_code == 200
    assert client.get(f"/api/cases/{case_id}/timeline", headers=headers).status_code == 200
    assert client.get(f"/api/cases/{case_id}/mediation-certificate", headers=headers).status_code == 200

    assert client.post(f"/api/cases/{case_id}/send-invite", headers=headers).status_code == 200
    mails = client.get("/api/dev/outbox", params={"email": rs_email}).json()["emails"]
    token = parse_qs(urlparse(next(l for e in mails for l in e["links"] if "/respond/" in l)).query)["t"][0]
    r = client.post("/api/respondent/decline", json={"case_id": case_id, "email": rs_email, "token": token})
    assert r.status_code == 200, r.text
    case = cosmos_service.get_case(case_id)
    assert not {k: v for k, v in case.items() if k.endswith("_error")}


import pytest  # noqa: E402

TRACK_CASES = {
    "employment": dict(
        claim_amount=85000, respondent_type="company", respondent_company_name="Acme Pvt Ltd",
        dispute_text=("My employer has not paid my salary for the last two months (Rs 85,000) after I "
                      "resigned and served my full notice period. HR ignores my emails and salary slips "
                      "show the dues."),
    ),
    "consumer": dict(
        claim_amount=24000, respondent_type="company", respondent_company_name="ShopKart Online",
        dispute_text=("I ordered a laptop online for Rs 24,000 and paid by UPI, but the product delivered "
                      "was defective. The seller refused a refund and closed my complaint ticket without reply."),
    ),
}


@pytest.mark.parametrize("track", sorted(TRACK_CASES))
def test_each_track_runs_without_agent_errors(client, login, track):
    from app.services.cosmos_service import cosmos_service
    n = next(_counter)
    cl_email, rs_email = f"{track}-cl{n}@paths.test", f"{track}-rs{n}@paths.test"
    headers = login(cl_email)
    r = client.post("/api/cases/submit", headers=headers, json=_submission(
        claimant_email=cl_email, respondent_email=rs_email, **TRACK_CASES[track],
    ))
    assert r.status_code == 200, r.text
    assert r.json()["track"] == track
    case_id = r.json()["case_id"]
    case = cosmos_service.get_case(case_id)
    assert not {k: v for k, v in case.items() if k.endswith("_error")}
    assert case["documents_data"]["demand_letter_url"]
    assert case["documents_data"]["court_file_url"]

    assert client.post(f"/api/cases/{case_id}/strategy", headers=headers).status_code == 200
    assert client.post(f"/api/cases/{case_id}/send-invite", headers=headers).status_code == 200
    mails = client.get("/api/dev/outbox", params={"email": rs_email}).json()["emails"]
    token = parse_qs(urlparse(next(l for e in mails for l in e["links"] if "/respond/" in l)).query)["t"][0]
    out = _play_round(client, case_id, headers, rs_email, token, 1, decision="ACCEPT")
    assert out["outcome"] == "SETTLED"
    case = cosmos_service.get_case(case_id)
    assert case["status"] == "SETTLED"
    assert not {k: v for k, v in case.items() if k.endswith("_error")}


def test_respondent_evidence_upload(client, login):
    import base64
    case_id, headers, resp_email, token = _invited_case(client, login)
    body = {"case_id": case_id, "email": resp_email, "token": token,
            "filename": "invoice.pdf", "content_type": "text/html",
            "base64_data": base64.b64encode(b"%PDF-1.4 painter invoice").decode()}
    r = client.post("/api/respondent/evidence-upload", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["content_type"] == "application/pdf"
    r = client.post("/api/respondent/evidence-upload", json={**body, "token": "bad"})
    assert r.status_code == 403
    r = client.post("/api/respondent/evidence-upload", json={**body, "base64_data": "not base64!!"})
    assert r.status_code == 400


def test_settlement_completes_even_if_agreement_pdf_fails_then_regenerates(client, login, monkeypatch):
    import app.agents.document_agent as da
    real = da.generate_settlement_agreement
    calls = {"n": 0}

    async def flaky(case, amount):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("renderer down")
        return await real(case, amount)
    monkeypatch.setattr(da, "generate_settlement_agreement", flaky)

    case_id, headers, resp_email, token = _invited_case(client, login)
    client.post("/api/respondent/accept-in-full", json={"case_id": case_id, "email": resp_email, "token": token})
    assert _status(client, case_id) == "SETTLED"
    r = client.get(f"/api/documents/{case_id}/settlement", headers=headers)
    assert r.status_code == 200, r.text
    assert calls["n"] == 2


def test_escalation_completes_even_if_certificate_fails(client, login, monkeypatch):
    import app.agents.document_agent as da

    def boom(case):
        raise RuntimeError("renderer down")
    monkeypatch.setattr(da, "_generate_mediation_certificate", boom)
    case_id, headers, resp_email, token = _invited_case(client, login)
    for rnd in (1, 2, 3):
        _play_round(client, case_id, headers, resp_email, token, rnd)
    assert _status(client, case_id) == "ESCALATED"


def test_interrupted_analysis_is_resumed_by_worker(client, login, monkeypatch):
    import app.routers.cases as cases_router
    from app.services.cosmos_service import cosmos_service

    async def interrupted(case_id):  # simulates the server dying mid-pipeline
        return None
    monkeypatch.setattr(cases_router, "_run_full_pipeline", interrupted)
    headers = login("restart@paths.test")
    r = client.post("/api/cases/submit", headers=headers, json=_submission(
        claimant_email="restart@paths.test", respondent_email="restart-r@paths.test"))
    case_id = r.json()["case_id"]
    assert _status(client, case_id) == "ANALYZING"
    monkeypatch.undo()

    _expire_now(case_id)
    _run_expiry()
    case = cosmos_service.get_case(case_id)
    assert case["status"] == "ANALYZED"
    assert case["documents_data"]["demand_letter_url"]


def test_interrupted_mediation_is_resumed_by_worker(client, login, monkeypatch):
    import app.routers.negotiation as neg_router
    case_id, headers, resp_email, token = _invited_case(client, login)

    async def interrupted(*a, **k):
        return None
    monkeypatch.setattr(neg_router, "_run_mediation", interrupted)
    client.post("/api/negotiation/submit-claimant-offer", headers=headers, json={
        "case_id": case_id, "round_number": 1, "offer_amount": 55000})
    client.post("/api/negotiation/submit-respondent-offer", json={
        "case_id": case_id, "respondent_token": token, "round_number": 1, "offer_amount": 30000})
    assert _status(client, case_id) == "MEDIATOR_REVIEW"
    monkeypatch.undo()

    _expire_now(case_id)
    _run_expiry()
    assert _status(client, case_id) == "PROPOSAL_ISSUED"
