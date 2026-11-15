"""B2B: organisations filing disputes in bulk."""
import itertools
from urllib.parse import parse_qs, urlparse

_n = itertools.count()


def _org(client, login, **overrides):
    n = next(_n)
    owner = login(f"owner{n}@acme{n}.test")
    body = {
        "name": f"Acme Rentals {n}", "contact_email": f"legal@acme{n}.test", "accept_terms": True,
        "profile": {"legal_name": f"Acme Rentals Pvt Ltd {n}", "city": "Pune", "state": "Maharashtra",
                    "phone": "02012345678", "address": "1 MG Road", "signatory": "R. Mehta"},
    }
    body.update(overrides)
    r = client.post("/api/orgs", headers=owner, json=body)
    assert r.status_code == 200, r.text
    return r.json(), owner, n


def _rows(n, count=3, start=0, template_fields=True):
    return [{
        "respondent_name": f"Tenant {i}", "respondent_email": f"tenant{i}.{n}@mail.test",
        "reference": f"RENT-{n}-{i}", "claim_amount": 25000 + i * 1000,
        "due_date": "2026-09-30", "incident_date": "2026-04-01",
    } for i in range(start, start + count)]


def test_org_creation_requires_terms_and_lists_membership(client, login):
    owner = login("termless@example.com")
    r = client.post("/api/orgs", headers=owner, json={
        "name": "No Terms", "contact_email": "x@noterms.test", "profile": {"legal_name": "No Terms Ltd"}})
    assert r.status_code == 400
    org, owner, _ = _org(client, login)
    mine = client.get("/api/orgs/mine", headers=owner).json()["organisations"]
    assert any(o["id"] == org["id"] and o["role"] == "owner" for o in mine)
    assert "overdue_invoice" in org["templates"]


def test_bulk_filing_validates_dedupes_and_analyses(client, login):
    org, owner, n = _org(client, login)
    rows = _rows(n, 3) + [
        {**_rows(n, 1)[0]},                                              # duplicate reference
        {"respondent_name": "Bad", "respondent_email": "nope", "reference": "X1", "claim_amount": 100},
        {"respondent_email": "a@b.test", "reference": "X2"},             # missing name
    ]
    r = client.post(f"/api/orgs/{org['id']}/cases/bulk", headers=owner,
                    json={"rows": rows, "template": "unpaid_rent"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["summary"] == {"rows": 6, "created": 3, "duplicates": 1, "errors": 2}
    assert {e["row"] for e in out["errors"]} == {5, 6}

    listed = client.get(f"/api/orgs/{org['id']}/cases", headers=owner).json()
    assert listed["total"] == 3
    assert all(c["status"] == "ANALYZED" for c in listed["cases"])

    case = client.get(f"/api/orgs/{org['id']}/cases/{out['created'][0]['case_id']}", headers=owner).json()
    assert case["claimant_name"] == org["profile"]["legal_name"]
    assert "Rs. 25,000" in case["dispute_text"] and "RENT-" in case["dispute_text"]
    assert case["intake_data"]["dispute_category"] == "unpaid_rent"
    assert case["documents_data"]["demand_letter_url"]

    # Re-uploading the same file is idempotent.
    again = client.post(f"/api/orgs/{org['id']}/cases/bulk", headers=owner,
                        json={"rows": _rows(n, 3), "template": "unpaid_rent"}).json()
    assert again["summary"]["created"] == 0 and again["summary"]["duplicates"] == 3


def test_csv_upload_with_auto_invite(client, login):
    org, owner, n = _org(client, login)
    csv_text = ("respondent_name,respondent_email,reference,claim_amount,incident_date,due_date\n"
                f"Ravi Traders,ravi.{n}@shop.test,INV-{n}-1,\"48,500\",15/05/2026,2026-06-15\n"
                f"Meena Stores,meena.{n}@shop.test,INV-{n}-2,12000,2026-05-20,2026-06-20\n")
    r = client.post(f"/api/orgs/{org['id']}/cases/bulk-csv", headers=owner,
                    files={"file": ("invoices.csv", csv_text.encode(), "text/csv")},
                    data={"template": "overdue_invoice", "auto_invite": "true"})
    assert r.status_code == 200, r.text
    assert r.json()["summary"]["created"] == 2
    cases = client.get(f"/api/orgs/{org['id']}/cases", headers=owner).json()["cases"]
    assert {c["status"] for c in cases} == {"INVITE_SENT"}
    assert {c["claim_amount"] for c in cases} == {48500, 12000}
    mails = client.get("/api/dev/outbox", params={"email": f"ravi.{n}@shop.test"}).json()["emails"]
    assert any("/respond/" in link for m in mails for link in m["links"])


def test_api_key_auth_and_revocation(client, login):
    org, owner, n = _org(client, login)
    other, other_owner, _ = _org(client, login)
    key = client.post(f"/api/orgs/{org['id']}/api-keys", headers=owner, json={"name": "billing"}).json()
    assert key["api_key"].startswith("lai_")
    assert "hash" not in str(client.get(f"/api/orgs/{org['id']}", headers=owner).json()["api_keys"])

    h = {"X-API-Key": key["api_key"]}
    r = client.post(f"/api/orgs/{org['id']}/cases/bulk", headers=h,
                    json={"rows": _rows(n, 1), "template": "overdue_invoice"})
    assert r.status_code == 200 and r.json()["summary"]["created"] == 1
    assert client.get(f"/api/orgs/{org['id']}/stats", headers=h).status_code == 200
    # A key can't administer the org, and can't touch another org.
    assert client.post(f"/api/orgs/{org['id']}/api-keys", headers=h, json={"name": "x"}).status_code == 403
    assert client.get(f"/api/orgs/{other['id']}/cases", headers=h).status_code == 401

    client.delete(f"/api/orgs/{org['id']}/api-keys/{key['id']}", headers=owner)
    assert client.get(f"/api/orgs/{org['id']}/cases", headers=h).status_code == 401


def test_roles_and_member_access_to_org_cases(client, login):
    org, owner, n = _org(client, login)
    outsider = login(f"outsider{n}@else.test")
    assert client.get(f"/api/orgs/{org['id']}", headers=outsider).status_code == 404

    agent_email = f"agent{n}@acme{n}.test"
    client.post(f"/api/orgs/{org['id']}/members", headers=owner, json={"email": agent_email, "role": "agent"})
    agent = login(agent_email)
    assert client.post(f"/api/orgs/{org['id']}/api-keys", headers=agent, json={"name": "x"}).status_code == 403
    out = client.post(f"/api/orgs/{org['id']}/cases/bulk", headers=agent,
                      json={"rows": _rows(n, 1), "template": "loan_default"}).json()
    case_id = out["created"][0]["case_id"]

    # The agent works the case through the normal claimant endpoints.
    assert client.get(f"/api/cases/{case_id}", headers=agent).status_code == 200
    assert client.post(f"/api/cases/{case_id}/send-invite", headers=agent).status_code == 200
    assert client.get(f"/api/cases/{case_id}", headers=outsider).status_code == 403
    r = client.post("/api/negotiation/submit-claimant-offer", headers=agent, json={
        "case_id": case_id, "round_number": 1, "offer_amount": 24000})
    assert r.status_code == 200, r.text

    # The last owner cannot be removed.
    r = client.delete(f"/api/orgs/{org['id']}/members/owner{n}@acme{n}.test", headers=owner)
    assert r.status_code == 400


def test_plan_quota_limits_monthly_filing(client, login):
    org, owner, n = _org(client, login)
    out = client.post(f"/api/orgs/{org['id']}/cases/bulk", headers=owner,
                      json={"rows": _rows(n, 28), "template": "overdue_invoice"}).json()
    assert out["summary"]["created"] == 25
    assert sum("limit" in e["error"] for e in out["errors"]) == 3


def test_stats_track_recovery(client, login):
    org, owner, n = _org(client, login)
    out = client.post(f"/api/orgs/{org['id']}/cases/bulk", headers=owner,
                      json={"rows": _rows(n, 2), "template": "unpaid_rent", "auto_invite": True}).json()
    case_id, resp_email = out["created"][0]["case_id"], f"tenant0.{n}@mail.test"
    mails = client.get("/api/dev/outbox", params={"email": resp_email}).json()["emails"]
    token = parse_qs(urlparse(next(l for m in mails for l in m["links"] if "/respond/" in l)).query)["t"][0]
    client.post("/api/respondent/accept-in-full", json={"case_id": case_id, "token": token})

    stats = client.get(f"/api/orgs/{org['id']}/stats", headers=owner).json()
    assert stats["total_cases"] == 2 and stats["by_status"]["SETTLED"] == 1
    assert stats["amount_claimed"] == 25000 + 26000
    assert stats["amount_settled"] == 25000
    assert stats["settlement_rate"] == 1.0
    assert stats["usage"]["this_month"] == 2 and stats["usage"]["monthly_limit"] == 25
