"""
DPDP data-principal rights: export (GET /api/auth/me/export) and erasure
(DELETE /api/auth/me), including the retention policy for cases that
already reached the respondent.
"""
import itertools
import json

from app.models.case import CaseStatus
from app.services.blob_service import blob_service
from app.services.cosmos_service import cosmos_service
from tests.test_e2e_local import _submission

_counter = itertools.count()


def _emails():
    n = next(_counter)
    return f"me{n}@rights.test", f"other{n}@rights.test"


def _submit(client, headers, claimant, respondent, **overrides):
    r = client.post("/api/cases/submit", headers=headers, json=_submission(
        claimant_email=claimant, respondent_email=respondent, **overrides,
    ))
    assert r.status_code == 200, r.text
    return r.json()["case_id"]


def _upload_evidence(client, headers, case_id):
    r = client.post(
        f"/api/cases/{case_id}/evidence-upload", headers=headers,
        files={"file": ("receipt.pdf", b"%PDF-1.4 deposit receipt", "application/pdf")},
    )
    assert r.status_code == 200, r.text
    return r.json()["blob_name"]


def _status(client, case_id):
    return client.get(f"/api/cases/{case_id}/status").json()["status"]


def _delete(client, headers, email):
    return client.request("DELETE", "/api/auth/me", headers=headers, json={"confirm_email": email})


# ── Export ────────────────────────────────────────────────────

def test_export_requires_auth(client):
    assert client.get("/api/auth/me/export").status_code in (401, 403)


def test_export_contains_profile_and_both_case_views(client, login):
    me, other = _emails()
    my_headers = login(me, "Me Claimant")
    other_headers = login(other, "Other Claimant")

    mine = _submit(client, my_headers, me, other)
    theirs = _submit(client, other_headers, other, me)  # I am the respondent here

    r = client.get("/api/auth/me/export", headers=my_headers)
    assert r.status_code == 200, r.text
    assert "attachment" in r.headers["content-disposition"]
    data = r.json()

    assert data["profile"]["email"] == me
    assert data["profile"]["display_name"] == "Me Claimant"
    assert "otp" not in data["profile"]

    claimant_cases = {c["id"]: c for c in data["cases_as_claimant"]}
    assert set(claimant_cases) == {mine}
    assert claimant_cases[mine]["analytics_data"], "claimant export should include own analysis"

    respondent_cases = {c["id"]: c for c in data["cases_as_respondent"]}
    assert set(respondent_cases) == {theirs}
    leaked = respondent_cases[theirs]
    # The other party's private analysis and contact number must not leak.
    for field in ("analytics_data", "claimant_phone", "claimant_email"):
        assert field not in leaked, field
    assert "claimant_weaknesses" not in json.dumps(leaked)
    assert "evidence_strength_score" not in leaked["intake_data"]


# ── Erasure ───────────────────────────────────────────────────

def test_delete_requires_matching_confirmation(client, login):
    me, _ = _emails()
    headers = login(me)
    r = _delete(client, headers, "someone-else@rights.test")
    assert r.status_code == 400
    assert client.get("/api/auth/me", headers=headers).status_code == 200


def test_delete_erases_profile_otps_and_unsent_cases(client, login):
    me, other = _emails()
    headers = login(me)
    case_id = _submit(client, headers, me, other)
    assert _status(client, case_id) == "ANALYZED"
    evidence_blob = _upload_evidence(client, headers, case_id)

    case = cosmos_service.get_case(case_id)
    pdf_ref = blob_service.parse_url(case["documents_data"]["demand_letter_url"])
    assert pdf_ref and blob_service.blob_exists(*pdf_ref)
    assert blob_service.blob_exists("evidence", evidence_blob)
    assert cosmos_service.get_case_documents_all(case_id)

    # An unused OTP is pending, and the per-email throttle record exists.
    assert client.post("/api/auth/request-otp", json={"email": me}).status_code == 200
    key = me.replace("@", "_").replace(".", "_")
    assert cosmos_service.get_otp(me)
    assert cosmos_service.users.find(lambda d: d["id"] == f"otpreq_{key}")

    r = _delete(client, headers, me.upper())  # confirmation is case-insensitive
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted_cases"] == 1 and body["retained_cases"] == 0
    assert "files_pending_deletion" not in body

    assert cosmos_service.get_user_by_email(me) is None
    assert cosmos_service.get_otp(me) is None
    for doc_id in (f"otp_{key}", f"otpreq_{key}"):
        assert not cosmos_service.users.find(lambda d: d["id"] == doc_id)

    assert cosmos_service.get_case(case_id) is None
    assert cosmos_service.get_case_documents_all(case_id) == []
    assert not blob_service.blob_exists("evidence", evidence_blob)
    assert not blob_service.blob_exists(*pdf_ref)

    # The old token no longer works.
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_delete_covers_criminal_advisory_and_abandoned(client, login):
    me, other = _emails()
    headers = login(me)
    criminal = _submit(client, headers, me, other, dispute_text=(
        "My neighbour physically assaulted me with a rod outside my house last night and "
        "threatened to kill me if I complain. I was injured and went to the hospital."
    ))
    assert _status(client, criminal) == "CRIMINAL_ADVISORY"
    fir_ref = blob_service.parse_url(cosmos_service.get_case(criminal)["documents_data"]["fir_advisory_url"])
    assert fir_ref and blob_service.blob_exists(*fir_ref)
    abandoned = _submit(client, headers, me, other)
    cosmos_service.transition_case(abandoned, CaseStatus.ABANDONED)

    r = _delete(client, headers, me)
    assert r.status_code == 200, r.text
    assert r.json()["deleted_cases"] == 2
    assert cosmos_service.get_case(criminal) is None
    assert cosmos_service.get_case(abandoned) is None
    assert not blob_service.blob_exists(*fir_ref)


def test_delete_retains_sent_cases_with_minimised_contact(client, login):
    me, other = _emails()
    headers = login(me)
    unsent = _submit(client, headers, me, other)
    sent = _submit(client, headers, me, other)
    assert client.post(f"/api/cases/{sent}/send-invite", headers=headers).status_code == 200
    evidence_blob = _upload_evidence(client, headers, sent)

    other_headers = login(other, "Other Claimant")
    as_respondent = _submit(client, other_headers, other, me)
    before_respondent_case = cosmos_service.get_case(as_respondent)

    r = _delete(client, headers, me)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted_cases"] == 1 and body["retained_cases"] == 1
    assert "kept" in body["retention_notice"]

    assert cosmos_service.get_case(unsent) is None

    kept = cosmos_service.get_case(sent)
    assert kept["status"] == "INVITE_SENT"
    assert kept["claimant_name"] == "[erased]"
    assert "claimant_phone" not in kept
    assert kept["claimant_email"] == me
    assert kept["claimant_erased_at"]
    # The retained record keeps its evidence for the other party / legal use.
    assert blob_service.blob_exists("evidence", evidence_blob)

    # Cases where the user was the respondent belong to the claimant: untouched.
    after = cosmos_service.get_case(as_respondent)
    assert after["respondent_email"] == me
    assert after["respondent_name"] == before_respondent_case["respondent_name"]
    assert after["status"] == before_respondent_case["status"]

    # The other party's account is unaffected.
    assert client.get("/api/auth/me", headers=other_headers).status_code == 200


def test_user_can_sign_up_again_after_deletion(client, login):
    me, _ = _emails()
    headers = login(me, "First Name")
    assert _delete(client, headers, me).status_code == 200
    headers = login(me, "Second Name")
    r = client.get("/api/auth/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["display_name"] == "Second Name"


def test_disclaimers_include_retention_notice(client):
    data = client.get("/api/auth/disclaimers").json()
    assert "[erased]" in data["data_retention"]
    assert "delete" in data["dpdp_consent"]
