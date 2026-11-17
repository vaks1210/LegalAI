"""
Evidence of consent. When a party accepts or rejects settlement terms we
record who, how, when, from where, and a fingerprint of the exact terms,
so a settlement can later be shown to rest on informed acceptance of
those terms (and not of some other figure).
"""
import hashlib
import json
from datetime import datetime, timezone

from fastapi import Request


def client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    from app.config import settings
    forwarded = request.headers.get("X-Forwarded-For")
    hops = settings.TRUSTED_PROXY_COUNT
    if forwarded and hops > 0:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if len(parts) >= hops:
            ip = parts[-hops]
            return ip.split(":")[0] if ip.count(":") == 1 else ip
    return request.client.host if request.client else None


def terms_fingerprint(round_doc: dict) -> str:
    """SHA-256 of the terms a party is deciding on (amount, actions, reasoning)."""
    terms = {
        "amount":  round_doc.get("settlement_candidate_amount") or round_doc.get("ai_proposed_amount"),
        "actions": round_doc.get("ai_proposed_actions") or [],
        "text":    round_doc.get("ai_reasoning") or "",
    }
    return hashlib.sha256(json.dumps(terms, sort_keys=True, default=str).encode()).hexdigest()


def decision_record(request: Request | None, actor: str, method: str, round_doc: dict) -> dict:
    return {
        "at":          datetime.now(timezone.utc).isoformat(),
        "actor":       actor,
        "method":      method,
        "ip":          client_ip(request),
        "user_agent":  (request.headers.get("User-Agent", "")[:200] if request else None),
        "terms_sha256": terms_fingerprint(round_doc),
    }
