"""
Collecting settlement payments.

When a monetary case settles we create a payment request for the
respondent:
  • Razorpay Payment Link — if RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are
    set. Razorpay's signed webhook (payment_link.paid) marks the case
    paid automatically.
  • Manual — otherwise: the payee's UPI / bank details (from the
    organisation's settings) are shown to the respondent; the claimant
    confirms receipt.

Implements Razorpay's documented Payment Links API; not exercised
against a live account in this repository's tests (stubbed there).
"""
import base64
import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

RAZORPAY_API = "https://api.razorpay.com/v1"
PAYMENT_WINDOW_DAYS = 14   # matches the settlement agreement's payment clause


def provider() -> str:
    return "razorpay" if settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET else "manual"


def _payee(case: dict) -> dict:
    if not case.get("org_id"):
        return {}
    from app.services.cosmos_service import cosmos_service
    org = cosmos_service.get_org(case["org_id"]) or {}
    return org.get("payee") or {}


def _razorpay_link(case: dict, amount: float) -> dict:
    auth = base64.b64encode(f"{settings.RAZORPAY_KEY_ID}:{settings.RAZORPAY_KEY_SECRET}".encode()).decode()
    body = {
        "amount": int(round(amount * 100)),           # paise
        "currency": "INR",
        "accept_partial": False,
        "reference_id": case["id"],
        "description": f"Settlement payment — case {case['id'][:8].upper()}",
        "customer": {"name": case["respondent_name"], "email": case["respondent_email"],
                     **({"contact": case["respondent_phone"]} if case.get("respondent_phone") else {})},
        "notify": {"email": True, "sms": bool(case.get("respondent_phone"))},
        "reminder_enable": True,
    }
    with httpx.Client(timeout=15) as client:
        r = client.post(f"{RAZORPAY_API}/payment_links", json=body,
                        headers={"Authorization": f"Basic {auth}"})
    r.raise_for_status()
    data = r.json()
    return {"link_id": data["id"], "link_url": data["short_url"]}


def create_payment_request(case: dict) -> dict | None:
    """Create (once) the payment request for a settled monetary case."""
    amount = case.get("settled_amount") or 0
    if case.get("track") == "non_monetary" or amount <= 0 or case.get("payment"):
        return case.get("payment")
    due = (datetime.now(timezone.utc) + timedelta(days=PAYMENT_WINDOW_DAYS)).isoformat()
    payment = {"provider": provider(), "status": "pending", "amount": amount, "currency": "INR",
               "reference": case.get("external_reference") or case["id"][:8].upper(), "due_at": due,
               "created_at": datetime.now(timezone.utc).isoformat()}
    if payment["provider"] == "razorpay":
        try:
            payment.update(_razorpay_link(case, amount))
        except Exception as e:
            logger.exception(f"Razorpay link failed — falling back to manual | case_id={case['id']}")
            payment.update({"provider": "manual", "provider_error": str(e)[:200]})
    if payment["provider"] == "manual":
        payee = _payee(case)
        payment["payee"] = {k: payee[k] for k in ("upi_id", "account_name", "account_number", "ifsc", "bank")
                            if payee.get(k)}
    from app.services.cosmos_service import cosmos_service
    cosmos_service.update_case(case["id"], {"payment": payment})
    return payment


def mark_paid(case_id: str, source: str, details: dict | None = None) -> dict:
    """Record that the settlement was paid (gateway webhook or claimant confirmation)."""
    from app.services.cosmos_service import cosmos_service
    now = datetime.now(timezone.utc).isoformat()

    def apply(case):
        payment = case.setdefault("payment", {"provider": "manual"})
        if payment.get("status") == "paid":
            return
        payment.update({"status": "paid", "paid_at": now, "confirmed_by": source, **(details or {})})
        case["settlement_honored"] = True
        case["payment_confirmed_at"] = now
    case = cosmos_service._mutate_case(case_id, apply)
    if case.get("org_id"):
        from app.services import webhooks
        webhooks.emit(cosmos_service.get_org(case["org_id"]), "case.payment_received",
                      {**webhooks.case_payload(case), "payment": {
                          k: case["payment"].get(k) for k in ("amount", "provider", "paid_at", "confirmed_by")}})
    return case


def verify_razorpay_signature(body: bytes, signature: str | None) -> bool:
    secret = settings.RAZORPAY_WEBHOOK_SECRET
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
