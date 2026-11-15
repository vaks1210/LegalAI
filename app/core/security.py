import secrets
import logging
from datetime import datetime, timedelta, timezone
import jwt
from jwt import PyJWTError as JWTError  # noqa: F401  (re-exported for callers)
from app.config import settings

logger = logging.getLogger(__name__)


def generate_otp(length: int = 6) -> str:
    """Generate a cryptographically random 6-digit numeric OTP."""
    return "".join(secrets.choice("0123456789") for _ in range(length))


def create_access_token(data: dict) -> str:
    """
    Create a signed JWT access token.
    data must contain 'sub' key with the user's email.
    Token expires after JWT_EXPIRE_HOURS (default 72).
    """
    to_encode = data.copy()
    expire    = datetime.now(timezone.utc) + timedelta(
        hours=settings.JWT_EXPIRE_HOURS
    )
    to_encode.update({"exp": expire})
    return jwt.encode(
        to_encode,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def decode_token(token: str) -> dict:
    """
    Decode and validate a JWT token.
    Returns the payload dict.
    Raises jwt.PyJWTError if invalid, expired, or tampered.
    """
    return jwt.decode(
        token,
        settings.JWT_SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
    )


def get_otp_expiry() -> str:
    """Returns ISO timestamp 10 minutes from now."""
    expiry = datetime.now(timezone.utc) + timedelta(
        minutes=settings.OTP_EXPIRE_MINUTES
    )
    return expiry.isoformat()


def is_otp_expired(expires_at: str) -> bool:
    """Returns True if the OTP expiry timestamp has passed."""
    expiry = datetime.fromisoformat(expires_at)
    return datetime.now(timezone.utc) > expiry


def validate_security_settings() -> None:
    """Refuse to start in production with an insecure JWT secret."""
    secret = settings.JWT_SECRET_KEY
    if settings.is_production and (secret in ("", "changeme") or len(secret) < 32):
        raise RuntimeError(
            "JWT_SECRET_KEY must be set to a strong secret (>= 32 chars) in production. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    if secret == "changeme":
        logger.warning("JWT_SECRET_KEY is the insecure default — fine for local dev only.")


# ── Respondent access tokens ─────────────────────────────────
# The respondent has no account. Their portal link carries an HMAC token bound
# to the case and their email, so knowing the case ID and the respondent's
# email address is no longer enough to act on their behalf.

def respondent_token(case_id: str, email: str) -> str:
    import hashlib
    import hmac
    msg = f"respondent|{case_id}|{(email or '').strip().lower()}".encode()
    return hmac.new(settings.JWT_SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()[:40]


def verify_respondent_token(case_id: str, email: str, token: str | None) -> bool:
    import hmac
    if not token:
        return False
    return hmac.compare_digest(respondent_token(case_id, email), token.strip())


def respondent_portal_url(case: dict) -> str:
    token = respondent_token(case["id"], case["respondent_email"])
    return f"{settings.BASE_URL.rstrip('/')}/respond/{case['id']}?t={token}"


def claimant_portal_url() -> str:
    return settings.BASE_URL.rstrip("/")
