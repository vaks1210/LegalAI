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
