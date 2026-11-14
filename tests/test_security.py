from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.rate_limiter import RateLimiterMiddleware


def test_otp_locks_after_too_many_wrong_attempts(client):
    email = "bruteforce@example.com"
    r = client.post("/api/auth/request-otp", json={"email": email})
    real = r.json()["dev_otp"]
    wrong = "000000" if real != "000000" else "111111"

    for _ in range(5):
        r = client.post("/api/auth/verify-otp", json={"email": email, "otp": wrong})
        assert r.status_code == 400
    assert "Too many" in r.json()["detail"]

    # Even the correct code is now rejected — a new OTP must be requested.
    r = client.post("/api/auth/verify-otp", json={"email": email, "otp": real})
    assert r.status_code == 400


def test_invalid_email_rejected(client):
    assert client.post("/api/auth/request-otp", json={"email": "not-an-email"}).status_code == 400


def test_signed_file_links_reject_tampering(client):
    from app.services.blob_service import blob_service
    url = blob_service.upload("pdfs", "probe/test.pdf", b"%PDF-1.4 test")
    path_and_query = url.split("http://testserver", 1)[1]
    assert client.get(path_and_query).status_code == 200

    tampered = path_and_query.replace("sig=", "sig=0")
    assert client.get(tampered).status_code == 403

    other_file = path_and_query.replace("test.pdf", "other.pdf")
    assert client.get(other_file).status_code == 403


def test_rate_limiter_returns_429_not_500():
    app = FastAPI()
    app.add_middleware(RateLimiterMiddleware, rules={"/ping": (2, 60)})

    @app.get("/ping")
    def ping():
        return {"ok": True}

    c = TestClient(app)
    assert c.get("/ping").status_code == 200
    assert c.get("/ping").status_code == 200
    r = c.get("/ping")
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_otp_is_numeric_and_six_digits():
    from app.core.security import generate_otp
    codes = {generate_otp() for _ in range(50)}
    assert all(len(c) == 6 and c.isdigit() for c in codes)
    assert len(codes) > 40


def _case_for_uploads(client, login):
    from tests.test_e2e_local import _submission
    headers = login("uploader@example.com")
    r = client.post("/api/cases/submit", headers=headers, json=_submission(
        claimant_email="uploader@example.com", respondent_email="up-resp@example.com",
    ))
    return r.json()["case_id"], headers


def test_upload_content_type_comes_from_extension_not_client(client, login):
    case_id, headers = _case_for_uploads(client, login)
    r = client.post(
        f"/api/cases/{case_id}/evidence-upload", headers=headers,
        files={"file": ("../../evil.txt", b"<script>alert(1)</script>", "text/html")},
    )
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["content_type"].startswith("text/plain")
    assert "/" not in rec["filename"] and ".." not in rec["filename"]
    served = client.get(rec["url"].split("http://testserver", 1)[1])
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("text/plain")


def test_upload_rejects_bad_type_and_oversize(client, login):
    from app.core.uploads import MAX_UPLOAD_BYTES
    case_id, headers = _case_for_uploads(client, login)
    r = client.post(f"/api/cases/{case_id}/evidence-upload", headers=headers,
                    files={"file": ("payload.html", b"<html></html>", "text/html")})
    assert r.status_code == 400
    r = client.post(f"/api/cases/{case_id}/evidence-upload", headers=headers,
                    files={"file": ("big.pdf", b"0" * (MAX_UPLOAD_BYTES + 1), "application/pdf")})
    assert r.status_code == 413


XSS = 'Deposit not returned <img src=x onerror="alert(1)"> and <b>unclosed tag, plus Rs 5,000 & interest?'


def test_markup_in_dispute_is_escaped_in_email_and_does_not_break_pdfs(client, login):
    from tests.test_e2e_local import DISPUTE, _submission
    headers = login("markup@example.com")
    r = client.post("/api/cases/submit", headers=headers, json=_submission(
        claimant_email="markup@example.com", respondent_email="markup-resp@example.com",
        claimant_name="Asha <script>x</script>", dispute_text=XSS + " " + DISPUTE,
    ))
    case_id = r.json()["case_id"]
    case = client.get(f"/api/cases/{case_id}", headers=headers).json()
    # PDF generation used to crash on unbalanced markup in user text.
    assert case["documents_data"]["demand_letter_url"]
    assert not case.get("document_agent_error")

    assert client.post(f"/api/cases/{case_id}/send-invite", headers=headers).status_code == 200
    from app.services.email_service import email_service
    invite = email_service.list_outbox("markup-resp@example.com")[0]["html"]
    assert "<img src=x" not in invite and "&lt;img" in invite
    assert "<script>" not in invite
