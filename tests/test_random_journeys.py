"""
Randomised journeys: both parties fire random (valid-looking) actions in
random order, with deadline expiries mixed in. The API must never return a
5xx, and every case must keep moving: after enough expiry passes it ends in
a terminal state rather than getting stuck.
"""
import random

import pytest

from tests.test_lifecycle_paths import _expire_now, _invited_case, _run_expiry

TERMINAL = {"SETTLED", "ESCALATED", "AUTO_ESCALATED", "ABANDONED", "CRIMINAL_ADVISORY"}


def _actions(client, case_id, headers, resp_email, token, rng):
    def status():
        return client.get(f"/api/cases/{case_id}/status").json()

    def rnd():
        return status().get("current_round") or 1

    return [
        lambda: client.post("/api/negotiation/submit-claimant-offer", headers=headers, json={
            "case_id": case_id, "round_number": rnd(), "offer_amount": rng.randint(30000, 60000),
            "requested_proof": rng.choice([[], ["receipts"]])}),
        lambda: client.post("/api/negotiation/submit-respondent-offer", json={
            "case_id": case_id, "respondent_token": token, "round_number": rnd(),
            "offer_amount": rng.randint(10000, 45000)}),
        lambda: client.post("/api/negotiation/proposal-response", headers=headers, json={
            "case_id": case_id, "round_number": rnd(), "party": "claimant",
            "decision": rng.choice(["ACCEPT", "REJECT"])}),
        lambda: client.post("/api/respondent/proposal-response", json={
            "case_id": case_id, "token": token, "decision": rng.choice(["ACCEPT", "REJECT"])}),
        lambda: client.post("/api/respondent/dispute-facts", json={
            "case_id": case_id, "token": token, "counter_claim": "Damage to walls cost Rs 10,000."}),
        lambda: client.get(f"/api/respondent/case/{case_id}", params={"t": token}),
        lambda: client.get(f"/api/negotiation/status/{case_id}", headers=headers),
        lambda: client.get(f"/api/cases/{case_id}/timeline", headers=headers),
        lambda: client.post(f"/api/cases/{case_id}/resend-invite", headers=headers),
        lambda: client.post("/api/respondent/accept-in-full", json={"case_id": case_id, "token": token})
        if rng.random() < 0.15 else client.get("/health"),
        lambda: client.post("/api/respondent/decline", json={"case_id": case_id, "token": token})
        if rng.random() < 0.1 else client.get("/health"),
        lambda: (_expire_now(case_id), _run_expiry()) and client.get("/health"),
    ]


@pytest.mark.parametrize("seed", range(int(__import__("os").environ.get("JOURNEY_SEEDS", "25"))))
def test_random_journey_never_errors_and_always_terminates(client, login, seed):
    rng = random.Random(seed)
    case_id, headers, resp_email, token = _invited_case(client, login)
    actions = _actions(client, case_id, headers, resp_email, token, rng)

    for _ in range(25):
        r = rng.choice(actions)()
        if r is not None:
            assert r.status_code < 500, (seed, r.request.url, r.status_code, r.text[:300])

    # Whatever happened, deadlines must eventually close the case.
    for _ in range(6):
        status = client.get(f"/api/cases/{case_id}/status").json()["status"]
        if status in TERMINAL:
            break
        _expire_now(case_id)
        _run_expiry()
    status = client.get(f"/api/cases/{case_id}/status").json()["status"]
    assert status in TERMINAL, (seed, status)


def _invited_non_monetary_case(client, login, n):
    from urllib.parse import parse_qs, urlparse
    from tests.test_e2e_local import _submission
    cl, rs = f"nmj-cl{n}@journeys.test", f"nmj-rs{n}@journeys.test"
    headers = login(cl)
    r = client.post("/api/cases/submit", headers=headers, json=_submission(
        claimant_email=cl, respondent_email=rs, claim_amount=None,
        dispute_text=("My neighbour plays loud music past midnight and blocks my parking spot. "
                      "I want the noise to stop and the parking kept clear.")))
    case_id = r.json()["case_id"]
    client.post(f"/api/cases/{case_id}/send-invite", headers=headers)
    mails = client.get("/api/dev/outbox", params={"email": rs}).json()["emails"]
    token = parse_qs(urlparse(next(l for e in mails for l in e["links"] if "/respond/" in l)).query)["t"][0]
    return case_id, headers, rs, token


@pytest.mark.parametrize("seed", range(int(__import__("os").environ.get("JOURNEY_SEEDS", "25")) // 2))
def test_random_non_monetary_journey(client, login, seed):
    rng = random.Random(10_000 + seed)
    case_id, headers, resp_email, token = _invited_non_monetary_case(client, login, seed)
    demands = ["Stop loud music after 10 pm", "Keep parking clear", "Written apology"]

    def rnd():
        return client.get(f"/api/cases/{case_id}/status").json().get("current_round") or 1

    actions = [
        lambda: client.post("/api/negotiation/submit-claimant-offer", headers=headers, json={
            "case_id": case_id, "round_number": rnd(), "demands": rng.sample(demands, rng.randint(1, 3))}),
        lambda: client.post("/api/negotiation/submit-respondent-offer", json={
            "case_id": case_id, "respondent_token": token, "round_number": rnd(),
            "commitments": rng.sample(demands, rng.randint(0, 2))}),
        lambda: client.post("/api/negotiation/proposal-response", headers=headers, json={
            "case_id": case_id, "round_number": rnd(), "party": "claimant",
            "decision": rng.choice(["ACCEPT", "REJECT"])}),
        lambda: client.post("/api/respondent/proposal-response", json={
            "case_id": case_id, "token": token, "decision": rng.choice(["ACCEPT", "REJECT"])}),
        lambda: client.get(f"/api/respondent/case/{case_id}", params={"t": token}),
        lambda: client.get(f"/api/negotiation/status/{case_id}", headers=headers),
        lambda: (_expire_now(case_id), _run_expiry()) and client.get("/health"),
    ]
    for _ in range(20):
        r = rng.choice(actions)()
        if r is not None:
            assert r.status_code < 500, (seed, r.request.url, r.status_code, r.text[:300])
    for _ in range(6):
        if client.get(f"/api/cases/{case_id}/status").json()["status"] in TERMINAL:
            break
        _expire_now(case_id)
        _run_expiry()
    status = client.get(f"/api/cases/{case_id}/status").json()["status"]
    assert status in TERMINAL, (seed, status)
    if status == "SETTLED":
        assert client.get(f"/api/documents/{case_id}/settlement", headers=headers).status_code == 200
