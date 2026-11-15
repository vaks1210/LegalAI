"""
Offline end-to-end test of the full dispute lifecycle:

  login → submit → 4-agent analysis → demand letter PDF → invite respondent
  → respondent views → both parties make offers → AI mediator proposal
  → both accept → SETTLED → settlement agreement PDF

Runs entirely on the local backends (SQLite, local disk, rule-based agents,
email outbox).  Run with:  pytest -q
"""
from urllib.parse import parse_qs, urlparse

CLAIMANT = "claimant@example.com"
RESPONDENT = "landlord@example.com"

DISPUTE = (
    "I rented a 2BHK flat in Andheri, Mumbai from my landlord for 11 months under a "
    "registered leave and licence agreement. I paid a refundable security deposit of "
    "Rs. 60,000. I vacated on 1 August after giving one month written notice and handed "
    "over the keys in good condition. Despite repeated WhatsApp messages and emails the "
    "landlord has refused to return the deposit and has stopped responding."
)


def _submission(**overrides):
    body = {
        "dispute_text": DISPUTE,
        "claimant_name": "Asha Verma",
        "claimant_email": CLAIMANT,
        "claimant_phone": "9876543210",
        "claimant_state": "Maharashtra",
        "claimant_city": "Mumbai",
        "respondent_name": "Rakesh Landlord",
        "respondent_email": RESPONDENT,
        "respondent_type": "individual",
        "claim_amount": 60000,
        "claimant_consent": True,
        "disclaimer_acknowledged": True,
    }
    body.update(overrides)
    return body


def _download(client, url):
    """Follow a signed local download link (strip scheme/host for TestClient)."""
    parsed = urlparse(url)
    return client.get(f"{parsed.path}?{parsed.query}")


def test_health_reports_local_backends(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert body["backends"] == {
        "database": "sqlite",
        "file_storage": "local_disk",
        "llm": "offline",
        "content_safety": "local_rules",
        "email": "local_outbox",
    }


def test_full_dispute_lifecycle(client, login):
    claimant = login(CLAIMANT, "Asha Verma")

    # ── Submit + analysis pipeline (background tasks finish before return) ──
    r = client.post("/api/cases/submit", json=_submission(), headers=claimant)
    assert r.status_code == 200, r.text
    case_id = r.json()["case_id"]

    r = client.get(f"/api/cases/{case_id}/status")
    assert r.json()["status"] == "ANALYZED", r.json()

    case = client.get(f"/api/cases/{case_id}", headers=claimant).json()
    for key in ("intake_data", "legal_data", "analytics_data", "documents_data"):
        assert case.get(key), f"{key} missing"
    analytics = case["analytics_data"]
    assert analytics["zopa_min"] <= analytics["zopa_optimal"] <= analytics["zopa_max"]

    demand_url = case["documents_data"]["demand_letter_url"]
    pdf = _download(client, demand_url)
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF")

    # ── Invite respondent; the email lands in the dev outbox ──
    r = client.post(f"/api/cases/{case_id}/send-invite", headers=claimant)
    assert r.status_code == 200, r.text
    outbox = client.get("/api/dev/outbox", params={"email": RESPONDENT}).json()["emails"]
    invite = next(link for e in outbox for link in e["links"] if f"/respond/{case_id}" in link)
    token = parse_qs(urlparse(invite).query)["t"][0]

    # ── Knowing the case ID + respondent email is no longer enough ──
    r = client.get(f"/api/respondent/case/{case_id}", params={"email": RESPONDENT})
    assert r.status_code == 403
    r = client.get(f"/api/respondent/case/{case_id}", params={"email": RESPONDENT, "t": "0" * 40})
    assert r.status_code == 403

    # ── Respondent view (via the emailed link) hides analytics / ZOPA ──
    r = client.get(f"/api/respondent/case/{case_id}", params={"email": RESPONDENT, "t": token})
    assert r.status_code == 200, r.text
    assert "analytics_data" not in r.json()["case"]
    assert client.get(f"/api/cases/{case_id}/status").json()["status"] == "RESPONDENT_VIEWED"

    # ── Round 1 offers → AI mediator proposal ──
    r = client.post("/api/negotiation/submit-claimant-offer", headers=claimant, json={
        "case_id": case_id, "round_number": 1, "offer_amount": 55000,
        "explanation": "Full deposit minus a small allowance for wear and tear.",
    })
    assert r.status_code == 200, r.text
    forged = client.post("/api/negotiation/submit-respondent-offer", json={
        "case_id": case_id, "round_number": 1, "offer_amount": 1,
    })
    assert forged.status_code == 403
    r = client.post("/api/negotiation/submit-respondent-offer", json={
        "case_id": case_id, "respondent_token": token, "round_number": 1, "offer_amount": 35000,
        "explanation": "Painting and cleaning costs were deducted.",
    })
    assert r.status_code == 200, r.text

    status = client.get(f"/api/cases/{case_id}/status").json()["status"]
    assert status == "PROPOSAL_ISSUED", status

    assert client.get(f"/api/negotiation/status/{case_id}").status_code in (401, 403)
    neg = client.get(f"/api/negotiation/status/{case_id}", headers=claimant).json()
    assert neg["current_round_detail"]["proposed_amount"] > 0

    # ── Unauthenticated request must not be able to decide as claimant ──
    r = client.post("/api/negotiation/proposal-response", json={
        "case_id": case_id, "round_number": 1, "decision": "ACCEPT", "party": "claimant",
    })
    assert r.status_code == 401

    # ── ...nor as the respondent without their link token ──
    r = client.post("/api/negotiation/proposal-response", json={
        "case_id": case_id, "round_number": 1, "decision": "REJECT", "party": "respondent",
    })
    assert r.status_code == 403

    # ── The claimant's proposal email must not contain the respondent's link ──
    claimant_mail = client.get("/api/dev/outbox", params={"email": CLAIMANT}).json()["emails"]
    assert not any(token in link for e in claimant_mail for link in e["links"])

    # ── Both accept → SETTLED with a downloadable agreement ──
    r = client.post("/api/negotiation/proposal-response", headers=claimant, json={
        "case_id": case_id, "round_number": 1, "decision": "ACCEPT", "party": "claimant",
    })
    assert r.status_code == 200, r.text
    r = client.post("/api/respondent/proposal-response", json={
        "case_id": case_id, "email": RESPONDENT, "token": token, "decision": "ACCEPT",
    })
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "SETTLED"

    assert client.get(f"/api/cases/{case_id}/status").json()["status"] == "SETTLED"
    r = client.get(f"/api/documents/{case_id}/settlement", headers=claimant)
    assert r.status_code == 200, r.text
    agreement = _download(client, r.json()["download_url"])
    assert agreement.status_code == 200
    assert agreement.content.startswith(b"%PDF")

    timeline = client.get(f"/api/cases/{case_id}/timeline", headers=claimant).json()
    assert timeline["timeline"][-1]["event"] == "SETTLED"


def test_other_users_cannot_read_case(client, login):
    claimant = login("owner@example.com")
    r = client.post("/api/cases/submit", headers=claimant, json=_submission(
        claimant_email="owner@example.com", respondent_email="other-party@example.com",
    ))
    case_id = r.json()["case_id"]
    intruder = login("intruder@example.com")
    assert client.get(f"/api/cases/{case_id}", headers=intruder).status_code == 403
    assert client.get(f"/api/documents/{case_id}/all", headers=intruder).status_code == 403


def test_criminal_dispute_never_goes_to_mediation(client, login):
    headers = login("victim@example.com")
    r = client.post("/api/cases/submit", headers=headers, json=_submission(
        claimant_email="victim@example.com", respondent_email="accused@example.com",
        claim_amount=None,
        dispute_text=(
            "My neighbour attacked me with an iron rod after an argument about parking and "
            "threatened to kill me if I complain. I was injured and went to hospital."
        ),
    ))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "CRIMINAL_ADVISORY"
    case = client.get(f"/api/cases/{r.json()['case_id']}", headers=headers).json()
    assert case["documents_data"]["fir_advisory_url"]


def test_resend_invite_issues_working_link(client, login):
    claimant = login("resender@example.com")
    r = client.post("/api/cases/submit", headers=claimant, json=_submission(
        claimant_email="resender@example.com", respondent_email="slow-reader@example.com",
    ))
    case_id = r.json()["case_id"]
    # Not allowed before the first invite
    assert client.post(f"/api/cases/{case_id}/resend-invite", headers=claimant).status_code == 409
    assert client.post(f"/api/cases/{case_id}/send-invite", headers=claimant).status_code == 200
    r = client.post(f"/api/cases/{case_id}/resend-invite", headers=claimant)
    assert r.status_code == 200, r.text

    mails = client.get("/api/dev/outbox", params={"email": "slow-reader@example.com"}).json()["emails"]
    links = [l for e in mails for l in e["links"] if f"/respond/{case_id}" in l]
    assert len(links) >= 2
    token = parse_qs(urlparse(links[0]).query)["t"][0]
    r = client.get(f"/api/respondent/case/{case_id}",
                   params={"email": "slow-reader@example.com", "t": token})
    assert r.status_code == 200


def test_respondent_link_token_alone_opens_case_but_wrong_email_is_refused(client, login):
    from urllib.parse import parse_qs, urlparse
    headers = login("direct@example.com")
    r = client.post("/api/cases/submit", headers=headers, json=_submission(
        claimant_email="direct@example.com", respondent_email="direct-resp@example.com",
    ))
    case_id = r.json()["case_id"]
    client.post(f"/api/cases/{case_id}/send-invite", headers=headers)
    mails = client.get("/api/dev/outbox", params={"email": "direct-resp@example.com"}).json()["emails"]
    token = parse_qs(urlparse(next(l for e in mails for l in e["links"] if "/respond/" in l)).query)["t"][0]

    assert client.get(f"/api/respondent/case/{case_id}", params={"t": token}).status_code == 200
    r = client.get(f"/api/respondent/case/{case_id}", params={"t": token, "email": "someone@else.com"})
    assert r.status_code == 403


def test_respondent_view_excludes_claimant_private_analysis(client, login):
    from urllib.parse import parse_qs, urlparse
    headers = login("private@example.com")
    r = client.post("/api/cases/submit", headers=headers, json=_submission(
        claimant_email="private@example.com", respondent_email="private-resp@example.com",
    ))
    case_id = r.json()["case_id"]
    assert client.post(f"/api/cases/{case_id}/strategy", headers=headers).status_code == 200
    client.post(f"/api/cases/{case_id}/send-invite", headers=headers)
    mails = client.get("/api/dev/outbox", params={"email": "private-resp@example.com"}).json()["emails"]
    token = parse_qs(urlparse(next(l for e in mails for l in e["links"] if "/respond/" in l)).query)["t"][0]

    view = client.get(f"/api/respondent/case/{case_id}", params={"t": token}).json()
    case = view["case"]
    for secret in ("analytics_data", "strategy_data", "claimant_phone", "routing", "claimant_email"):
        assert secret not in case, secret
    assert "claimant_weaknesses" not in case["intake_data"]
    assert "missing_proof_checklist" not in case["intake_data"]
    assert "court_file_url" not in case["documents_data"]
    assert "zopa" not in str(view).lower()
    assert case["dispute_text"] and case["claimant_name"]


def test_respondent_account_cannot_fetch_claimant_only_documents(client, login):
    claimant = login("docs-owner@example.com")
    r = client.post("/api/cases/submit", headers=claimant, json=_submission(
        claimant_email="docs-owner@example.com", respondent_email="docs-resp@example.com",
    ))
    case_id = r.json()["case_id"]
    respondent = login("docs-resp@example.com")  # anyone can log in as their own email

    assert client.get(f"/api/documents/{case_id}/court-file", headers=respondent).status_code == 403
    docs = client.get(f"/api/documents/{case_id}/all", headers=respondent).json()["documents"]
    assert "court_file" not in docs and docs["demand_letter"]
    assert client.get(f"/api/documents/{case_id}/court-file", headers=claimant).status_code == 200


def test_my_cases_gives_respondent_only_the_filtered_view(client, login):
    claimant = login("list-owner@example.com")
    client.post("/api/cases/submit", headers=claimant, json=_submission(
        claimant_email="list-owner@example.com", respondent_email="list-resp@example.com",
    ))
    respondent = login("list-resp@example.com")
    cases = client.get("/api/cases/my-cases", headers=respondent).json()["cases"]
    assert cases and all(c.get("role") == "respondent" for c in cases)
    assert all("analytics_data" not in c and "strategy_data" not in c for c in cases)
    own = client.get("/api/cases/my-cases", headers=claimant).json()["cases"]
    assert own[0]["analytics_data"]
