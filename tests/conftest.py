"""
Test configuration: force every integration onto its local backend so the
suite runs offline with no Azure / OpenAI / SendGrid credentials, against a
throwaway data directory.
"""
import os
import shutil
import tempfile

_DATA_DIR = tempfile.mkdtemp(prefix="legalai-test-")

# Explicit empty values win over anything in a developer's .env.local
# (python-dotenv does not override variables that are already set).
os.environ.update({
    "ENVIRONMENT": "development",
    "BASE_URL": "http://testserver",
    "LOCAL_DATA_DIR": _DATA_DIR,
    "RATE_LIMIT_ENABLED": "false",
    "JWT_SECRET_KEY": "test-secret-key-that-is-long-enough-1234567890",
    "COSMOS_CONNECTION_STRING": "",
    "AZURE_STORAGE_CONNECTION_STRING": "",
    "AZURE_OPENAI_KEY": "",
    "AZURE_OPENAI_ENDPOINT": "",
    "OPENAI_API_KEY": "",
    "CONTENT_SAFETY_KEY": "",
    "CONTENT_SAFETY_ENDPOINT": "",
    "SENDGRID_API_KEY": "",
    "SENDGRID_FROM_EMAIL": "",
    "APPINSIGHTS_INSTRUMENTATION_KEY": "",
})

import pytest  # noqa: E402


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_DATA_DIR, ignore_errors=True)


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def login(client):
    """Log a user in through the real OTP flow (dev mode returns the code)."""
    def _login(email: str, name: str = "Test User") -> dict:
        r = client.post("/api/auth/request-otp", json={"email": email, "display_name": name})
        assert r.status_code == 200, r.text
        otp = r.json()["dev_otp"]
        r = client.post("/api/auth/verify-otp", json={
            "email": email, "otp": otp, "display_name": name,
            "city": "Mumbai", "state": "Maharashtra", "consent": True,
        })
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}
    return _login
