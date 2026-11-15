"""
Exercise the LLM code path (prompt building + post-processing) with a fake
client, so it is tested without network access or API keys.
"""
import json
import types

import pytest

from tests.test_e2e_local import _submission


class FakeCompletions:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.responder(kwargs["messages"][0]["content"], kwargs["messages"][1]["content"])
        msg = types.SimpleNamespace(content=content)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=msg)],
            usage=types.SimpleNamespace(total_tokens=42),
        )


@pytest.fixture
def fake_llm(monkeypatch):
    from app.services.openai_service import openai_service

    def install(responder):
        completions = FakeCompletions(responder)
        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))
        monkeypatch.setattr(openai_service, "client", client)
        monkeypatch.setattr(openai_service, "provider", "fake")
        return completions
    return install


@pytest.mark.parametrize("reply", ["{}", '{"track": null, "proposed_amount": null}'])
def test_full_flow_survives_empty_or_partial_llm_json(client, login, fake_llm, reply):
    calls = fake_llm(lambda system, user: reply)
    email = f"llm{len(reply)}@example.com"
    headers = login(email)
    r = client.post("/api/cases/submit", headers=headers, json=_submission(
        claimant_email=email, respondent_email=f"llm-resp{len(reply)}@example.com",
    ))
    assert r.status_code == 200, r.text
    case_id = r.json()["case_id"]
    assert client.get(f"/api/cases/{case_id}/status").json()["status"] == "ANALYZED"
    case = client.get(f"/api/cases/{case_id}", headers=headers).json()
    assert case["documents_data"]["demand_letter_url"]
    assert len(calls.calls) >= 4  # router + intake + legal + analytics (+ documents)


def test_partial_llm_json_still_routes_and_generates_all_documents(client, login, fake_llm):
    from app.services.cosmos_service import cosmos_service
    fake_llm(lambda s, u: "{}")
    headers = login("probe@example.com")
    r = client.post("/api/cases/submit", headers=headers, json=_submission(
        claimant_email="probe@example.com", respondent_email="probe-resp@example.com"))
    case = cosmos_service.get_case(r.json()["case_id"])
    assert case["track"] == "monetary_civil"
    docs = case["documents_data"]
    assert docs["demand_letter_url"] and docs["court_file_url"] and docs["mediation_certificate_url"]
    assert not docs.get("error")


def test_llm_router_missing_track_still_catches_violence(client, login, fake_llm):
    fake_llm(lambda s, u: "{}")
    headers = login("probe2@example.com")
    r = client.post("/api/cases/submit", headers=headers, json=_submission(
        claimant_email="probe2@example.com", respondent_email="probe2-resp@example.com",
        claim_amount=None,
        dispute_text="My neighbour attacked me with an iron rod and threatened to kill me. I was injured.",
    ))
    assert r.json()["status"] == "CRIMINAL_ADVISORY"


def test_misbehaving_llm_mediator_is_corrected_end_to_end(client, login, fake_llm):
    from urllib.parse import parse_qs, urlparse

    seen = {"mediator": 0, "statements": False}

    def responder(system, user):
        if "Classify the dispute into exactly ONE" in system:
            return json.dumps({"track": "monetary_civil", "confidence": 0.9, "is_criminal": False,
                               "is_monetary": True, "dispute_category": "rental_deposit",
                               "criminal_elements": [], "routing_notes": "deposit"})
        if "PARTY STATEMENTS" in user:
            seen["mediator"] += 1
            seen["statements"] = "Flat returned clean" in user  # parties' words reach the mediator
            return json.dumps({"proposed_amount": 99999,
                               "reasoning": "Per the ZOPA we computed, Rs. 99,999 is fair. " * 3})
        return "{}"

    fake_llm(responder)
    headers = login("llm-med@example.com")
    r = client.post("/api/cases/submit", headers=headers, json=_submission(
        claimant_email="llm-med@example.com", respondent_email="llm-med-resp@example.com"))
    case_id = r.json()["case_id"]
    client.post(f"/api/cases/{case_id}/send-invite", headers=headers)
    mails = client.get("/api/dev/outbox", params={"email": "llm-med-resp@example.com"}).json()["emails"]
    token = parse_qs(urlparse(next(l for e in mails for l in e["links"] if "/respond/" in l)).query)["t"][0]

    client.post("/api/negotiation/submit-claimant-offer", headers=headers, json={
        "case_id": case_id, "round_number": 1, "offer_amount": 50000, "explanation": "Flat returned clean."})
    client.post("/api/negotiation/submit-respondent-offer", json={
        "case_id": case_id, "respondent_token": token, "round_number": 1, "offer_amount": 30000})

    view = client.get(f"/api/respondent/case/{case_id}", params={"t": token}).json()["negotiation"]
    assert seen["mediator"] == 1 and seen["statements"]
    assert 30000 <= view["ai_proposed_amount"] <= 50000
    assert "ZOPA" not in view["ai_reasoning"]


WRONG_TYPES = {
    "track": "monetary_civil", "confidence": "high", "is_criminal": "no", "criminal_elements": "none",
    "dispute_type": ["Deposit"], "severity": 3, "key_facts": "Deposit unpaid.", "key_issues": "deposit",
    "evidence_available": "agreement", "evidence_strength_score": "high",
    "missing_proof_checklist": "receipt", "claimant_strengths": "has agreement",
    "claimant_weaknesses": None, "confirmed_claim_amount": "Rs. 60,000",
    "applicable_laws": "Indian Contract Act s.73", "legal_standing": "pretty good",
    "respondent_defenses": "none", "relief_available": "money", "filing_steps": "go to court",
    "total_statutory_dues": "nil",
    "win_probability": "70%", "zopa_min": "Rs. 30,000", "zopa_max": "lots", "zopa_optimal": None,
    "court_cost_estimate": "about 50k", "time_to_resolution_months": "two years",
    "proposed_amount": "Rs 45,000", "reasoning": 12345, "live_reasoning_log": "step",
}


def test_wrongly_typed_llm_output_does_not_break_the_flow(client, login, fake_llm, caplog):
    import logging
    from urllib.parse import parse_qs, urlparse
    from app.services.cosmos_service import cosmos_service
    caplog.set_level(logging.ERROR)
    fake_llm(lambda s, u: json.dumps(WRONG_TYPES))
    headers = login("types@example.com")
    r = client.post("/api/cases/submit", headers=headers, json=_submission(
        claimant_email="types@example.com", respondent_email="types-resp@example.com"))
    assert r.status_code == 200, r.text
    case_id = r.json()["case_id"]
    # "is_criminal": "no" must not be read as True (it used to force the criminal track).
    assert cosmos_service.get_case(case_id)["status"] == "ANALYZED"
    client.post(f"/api/cases/{case_id}/send-invite", headers=headers)
    mails = client.get("/api/dev/outbox", params={"email": "types-resp@example.com"}).json()["emails"]
    token = parse_qs(urlparse(next(l for e in mails for l in e["links"] if "/respond/" in l)).query)["t"][0]
    client.post("/api/negotiation/submit-claimant-offer", headers=headers, json={
        "case_id": case_id, "round_number": 1, "offer_amount": 50000})
    client.post("/api/negotiation/submit-respondent-offer", json={
        "case_id": case_id, "respondent_token": token, "round_number": 1, "offer_amount": 30000})

    case = cosmos_service.get_case(case_id)
    errors = [rec.getMessage()[:160] for rec in caplog.records
              if rec.levelno >= logging.ERROR and "LLM call failed" not in rec.getMessage()]
    assert not errors, errors
    assert case["status"] == "PROPOSAL_ISSUED", case["status"]
