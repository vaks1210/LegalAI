"""
Outgoing webhooks: tell an organisation's systems when its cases change.

Payloads are signed with the org's webhook secret:
    X-LegalAI-Signature: sha256=<hex HMAC-SHA256(secret, raw body)>
Delivery runs on a small thread pool (never blocks the request or the
case pipeline), retries with backoff, and the last outcomes are kept on
the organisation for debugging.

The URL is customer-supplied, so it is validated against SSRF: only
http(s), and never to private, loopback, link-local or reserved
addresses (e.g. the cloud metadata endpoint).
"""
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="webhook")
MAX_ATTEMPTS = 3
LOG_SIZE = 20


def validate_webhook_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    allowed = ("https",) if settings.is_production else ("https", "http")
    if parsed.scheme not in allowed or not parsed.hostname:
        raise ValueError("Webhook URL must be an https:// URL" if settings.is_production
                         else "Webhook URL must be an http(s):// URL")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError("Webhook host does not resolve")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise ValueError("Webhook URL must point to a public internet address")
    return parsed.geturl()


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(url: str, body: bytes, headers: dict) -> int:
    with httpx.Client(timeout=5, follow_redirects=False) as client:
        return client.post(url, content=body, headers=headers).status_code


def _record(org_id: str, entry: dict) -> None:
    from app.services.cosmos_service import cosmos_service

    def apply(org):
        log = org.setdefault("webhook_log", [])
        log.insert(0, entry)
        del log[LOG_SIZE:]
    try:
        cosmos_service.mutate_org(org_id, apply)
    except Exception:
        logger.exception(f"Could not record webhook delivery | org_id={org_id}")


def _deliver(org_id: str, url: str, secret: str, event: str, data: dict) -> None:
    delivery_id = uuid.uuid4().hex
    body = json.dumps({
        "id": delivery_id, "event": event,
        "created_at": datetime.now(timezone.utc).isoformat(), "data": data,
    }, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json", "User-Agent": "LegalAI-Webhooks/1",
        "X-LegalAI-Event": event, "X-LegalAI-Delivery": delivery_id,
        "X-LegalAI-Signature": sign(secret, body),
    }
    status, error = None, None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            validate_webhook_url(url)          # re-check: DNS may have changed
            status = _post(url, body, headers)
            if 200 <= status < 300:
                break
            error = f"HTTP {status}"
        except Exception as e:                 # network error or blocked URL
            error = str(e)[:200]
        if attempt < MAX_ATTEMPTS:
            time.sleep(0.5 * 2 ** (attempt - 1))
    ok = status is not None and 200 <= status < 300
    _record(org_id, {"id": delivery_id, "event": event, "case_id": data.get("case_id"),
                     "ok": ok, "status": status, "error": None if ok else error,
                     "attempts": attempt, "at": datetime.now(timezone.utc).isoformat()})


def emit(org: dict | None, event: str, data: dict, wait: bool = False) -> None:
    hook = (org or {}).get("webhook") or {}
    if not hook.get("url") or not hook.get("secret") or hook.get("disabled"):
        return
    future = _pool.submit(_deliver, org["id"], hook["url"], hook["secret"], event, data)
    if wait:
        future.result()


def case_payload(case: dict, previous_status: str | None = None) -> dict:
    return {
        "case_id": case["id"], "reference": case.get("external_reference"),
        "status": case.get("status"), "previous_status": previous_status,
        "respondent_name": case.get("respondent_name"), "claim_amount": case.get("claim_amount"),
        "settled_amount": case.get("settled_amount"), "current_round": case.get("current_round"),
        "updated_at": case.get("updated_at"),
    }


def on_case_transition(case: dict, previous_status: str) -> None:
    """Called by the data layer after every status change of an org case."""
    if not case.get("org_id") or case.get("status") == previous_status:
        return
    try:
        from app.services.cosmos_service import cosmos_service
        emit(cosmos_service.get_org(case["org_id"]), "case.status_changed",
             case_payload(case, previous_status))
    except Exception:
        logger.exception(f"Webhook emit failed | case_id={case.get('id')}")
