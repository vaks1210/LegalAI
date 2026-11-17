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
from app.core.jurisdictions import money

logger = logging.getLogger(__name__)

RAZORPAY_API = "https://api.razorpay.com/v1"
PAYMENT_WINDOW_DAYS = 14   # matches the settlement agreement's payment clause


def provider(case: dict | None = None) -> str:
    """Razorpay collects INR only; other currencies use the organisation's payee details."""
    if not (settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET):
        return "manual"
    if case is not None:
        from app.core.jurisdictions import case_jurisdiction
        if (case.get("currency") or "INR") != "INR" or "razorpay" not in case_jurisdiction(case).payment_providers:
            return "manual"
    return "razorpay"


def _payee(case: dict) -> dict:
    if not case.get("org_id"):
        return {}
    from app.services.cosmos_service import cosmos_service
    org = cosmos_service.get_org(case["org_id"]) or {}
    return org.get("payee") or {}


def _razorpay_link(case: dict, amount: float, installment_no: int = 1, total_installments: int = 1) -> dict:
    auth = base64.b64encode(f"{settings.RAZORPAY_KEY_ID}:{settings.RAZORPAY_KEY_SECRET}".encode()).decode()
    label = f" — instalment {installment_no} of {total_installments}" if total_installments > 1 else ""
    body = {
        "amount": int(round(amount * 100)),           # paise
        "currency": "INR",
        "accept_partial": False,
        "reference_id": f"{case['id']}-{installment_no}",
        "description": f"Settlement payment{label} — case {case['id'][:8].upper()}",
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


INSTALMENT_INTERVAL_DAYS = 30
GRACE_DAYS = 7             # an instalment this late defaults the plan (acceleration)
REMINDER_DAYS_BEFORE = 3


def installment_policy(case: dict) -> dict:
    """What the claimant has pre-approved: the respondent may choose within it."""
    if not case.get("org_id"):
        return {"max_installments": 1, "min_installment_amount": 0}
    from app.services.cosmos_service import cosmos_service
    org = cosmos_service.get_org(case["org_id"]) or {}
    pol = (org.get("settings") or {}).get("installments") or {}
    return {"max_installments": max(1, min(int(pol.get("max", 1) or 1), 24)),
            "min_installment_amount": float(pol.get("min_amount", 0) or 0)}


def validate_installments(case: dict, amount: float, n: int | None) -> int:
    """Return the accepted instalment count or raise ValueError with a reason."""
    n = int(n or 1)
    if n == 1:
        return 1
    pol = installment_policy(case)
    if n < 1 or n > pol["max_installments"]:
        raise ValueError(f"Choose between 1 and {pol['max_installments']} instalments.")
    if amount and amount / n < pol["min_installment_amount"]:
        raise ValueError(f"Each instalment must be at least {money(pol['min_installment_amount'], case)}.")
    return n


def build_schedule(total: float, n: int, start: datetime) -> list[dict]:
    base = round(total / n)
    plan = []
    for i in range(n):
        amount = base if i < n - 1 else round(total - base * (n - 1), 2)
        plan.append({"no": i + 1, "amount": amount, "status": "pending",
                     "due_at": (start + timedelta(days=INSTALMENT_INTERVAL_DAYS * i)).isoformat()})
    return plan


def _summarise(payment: dict) -> None:
    """Keep top-level fields (status, next due, link) consistent with the plan."""
    plan = payment.get("plan") or []
    paid = [p for p in plan if p["status"] == "paid"]
    pending = [p for p in plan if p["status"] != "paid"]
    payment["paid_amount"] = round(sum(p["amount"] for p in paid), 2)
    if payment.get("status") == "defaulted" and pending:
        pass
    elif not pending:
        payment["status"] = "paid"
    elif paid:
        payment["status"] = "partially_paid"
    else:
        payment["status"] = "pending"
    nxt = pending[0] if pending else None
    payment["due_at"] = nxt["due_at"] if nxt else None
    payment["next_amount"] = nxt["amount"] if nxt else 0
    payment["link_url"] = nxt.get("link_url") if nxt else None
    payment["link_id"] = nxt.get("link_id") if nxt else None


def create_payment_request(case: dict) -> dict | None:
    """Create (once) the payment schedule for a settled monetary case."""
    amount = case.get("settled_amount") or 0
    if case.get("track") == "non_monetary" or amount <= 0 or case.get("payment"):
        return case.get("payment")
    n = int((case.get("payment_plan_request") or {}).get("installments") or 1)
    start = datetime.now(timezone.utc) + timedelta(days=PAYMENT_WINDOW_DAYS)
    payment = {"provider": provider(case), "amount": amount, "currency": case.get("currency") or "INR",
               "reference": case.get("external_reference") or case["id"][:8].upper(),
               "installments": n, "plan": build_schedule(amount, n, start),
               "created_at": datetime.now(timezone.utc).isoformat()}
    if payment["provider"] == "razorpay":
        try:
            for inst in payment["plan"]:
                inst.update(_razorpay_link(case, inst["amount"], inst["no"], n))
        except Exception as e:
            logger.exception(f"Razorpay link failed — falling back to manual | case_id={case['id']}")
            for inst in payment["plan"]:
                inst.pop("link_id", None), inst.pop("link_url", None)
            payment.update({"provider": "manual", "provider_error": str(e)[:200]})
    if payment["provider"] == "manual":
        payee = _payee(case)
        payment["payee"] = {k: payee[k] for k in ("upi_id", "account_name", "account_number", "ifsc", "bank")
                            if payee.get(k)}
    _summarise(payment)
    from app.services.cosmos_service import cosmos_service
    cosmos_service.update_case(case["id"], {"payment": payment})
    return payment


def mark_paid(case_id: str, source: str, details: dict | None = None,
              installment_no: int | None = None, link_id: str | None = None) -> dict:
    """
    Record a payment: a specific instalment (by number or gateway link), or
    otherwise the next unpaid one. When every instalment is paid the
    settlement is honoured.
    """
    from app.services.cosmos_service import cosmos_service
    now = datetime.now(timezone.utc).isoformat()
    marked = {}

    def apply(case):
        payment = case.setdefault("payment", {"provider": "manual"})
        plan = payment.get("plan")
        if not plan:   # legacy single payment without a plan
            plan = payment["plan"] = [{"no": 1, "amount": payment.get("amount") or case.get("settled_amount") or 0,
                                       "status": payment.get("status", "pending"), "due_at": payment.get("due_at")}]
        target = None
        for inst in plan:
            if inst["status"] == "paid":
                continue
            if (installment_no and inst["no"] == installment_no) or (link_id and inst.get("link_id") == link_id) \
                    or (not installment_no and not link_id):
                target = inst
                break
        if target is None:
            return
        target.update({"status": "paid", "paid_at": now, "confirmed_by": source, **(details or {})})
        marked.update(target)
        _summarise(payment)
        payment["confirmed_by"] = source
        if payment["status"] == "paid":
            payment["paid_at"] = now
            case["settlement_honored"] = True
            case["payment_confirmed_at"] = now
    case = cosmos_service._mutate_case(case_id, apply)
    if marked and case.get("org_id"):
        from app.services import webhooks
        webhooks.emit(cosmos_service.get_org(case["org_id"]), "case.payment_received",
                      {**webhooks.case_payload(case), "payment": {
                          "installment": marked.get("no"), "amount": marked.get("amount"),
                          "paid_amount": case["payment"].get("paid_amount"), "total": case["payment"].get("amount"),
                          "status": case["payment"].get("status"), "provider": case["payment"].get("provider"),
                          "confirmed_by": source}})
    return case


def check_payment_schedules(now: datetime | None = None) -> dict:
    """
    Daily-ish sweep over settled cases with money outstanding:
      • remind the respondent shortly before each instalment falls due
      • if an instalment is more than GRACE_DAYS overdue, the plan defaults:
        the whole balance is due (acceleration), the claimant gets a breach
        notice and organisations a case.payment_defaulted webhook.
    """
    from app.services.cosmos_service import cosmos_service
    from app.services.email_service import email_service
    now = now or datetime.now(timezone.utc)
    stats = {"reminded": 0, "defaulted": 0}
    for case in cosmos_service.get_cases_with_open_payments():
        payment = case.get("payment") or {}
        pending = [p for p in payment.get("plan", []) if p["status"] != "paid"]
        if not pending or payment.get("status") == "defaulted":
            continue
        nxt = pending[0]
        due = datetime.fromisoformat(nxt["due_at"])
        if now > due + timedelta(days=GRACE_DAYS):
            _default_plan(case, nxt, now)
            stats["defaulted"] += 1
        elif now >= due - timedelta(days=REMINDER_DAYS_BEFORE) and not nxt.get("reminded_at"):
            how = f"Pay here: {nxt['link_url']}" if nxt.get("link_url") else \
                  f"Quote reference {payment.get('reference')} when paying."
            label = f"Instalment {nxt['no']} of {len(payment['plan'])}" if len(payment["plan"]) > 1 else "Payment"
            email_service.send_case_update(
                to_email=case["respondent_email"], party_name=case["respondent_name"], case_id=case["id"],
                headline=f"Reminder: {label} of {money(nxt['amount'], case)} due {due.strftime('%d %b %Y')}",
                summary=f"Under your settlement with {case['claimant_name']}, {label.lower()} of "
                        f"{money(nxt['amount'], case)} is due on {due.strftime('%d %B %Y')}. {how} "
                        f"Missing a payment by more than {GRACE_DAYS} days makes the full balance due.",
                portal_url=nxt.get("link_url") or None, action_label="Pay now" if nxt.get("link_url") else None,
            )
            from app.services.messaging import messaging_service, payment_reminder_text
            messaging_service.notify_respondent(
                case, "payment_reminder", payment_reminder_text(case, payment, nxt), link=nxt.get("link_url"))
            no = nxt["no"]

            def mark_reminded(c, no=no):
                for inst in c["payment"]["plan"]:
                    if inst["no"] == no:
                        inst["reminded_at"] = now.isoformat()
            cosmos_service._mutate_case(case["id"], mark_reminded)
            stats["reminded"] += 1
    return stats


def _default_plan(case: dict, missed: dict, now: datetime) -> None:
    from app.services.cosmos_service import cosmos_service
    from app.services.email_service import email_service

    def apply(c):
        p = c["payment"]
        p["status"] = "defaulted"
        p["defaulted_at"] = now.isoformat()
        p["default_reason"] = f"Instalment {missed['no']} more than {GRACE_DAYS} days overdue"
        p["balance_due"] = round(p.get("amount", 0) - p.get("paid_amount", 0), 2)
        c["settlement_honored"] = False
        c["payment_overdue"] = True
    case = cosmos_service._mutate_case(case["id"], apply)
    balance = case["payment"]["balance_due"]
    breach_url = None
    try:
        import asyncio
        from app.agents.document_agent import generate_breach_notice
        breach_url = asyncio.run(generate_breach_notice(case))
    except Exception:
        logger.exception(f"Breach notice failed on default | case_id={case['id']}")
    email_service.send_case_update(
        to_email=case["claimant_email"], party_name=case["claimant_name"], case_id=case["id"],
        headline=f"Payment default: {money(balance, case)} now due",
        summary=f"{case['respondent_name']} missed instalment {missed['no']} by more than {GRACE_DAYS} days. "
                f"Under the settlement agreement the full unpaid balance of {money(balance, case)} is now due. "
                "A Breach of Settlement Notice has been prepared.",
        portal_url=breach_url, action_label="Download Breach Notice" if breach_url else None,
    )
    if case.get("org_id"):
        from app.services import webhooks
        webhooks.emit(cosmos_service.get_org(case["org_id"]), "case.payment_defaulted",
                      {**webhooks.case_payload(case), "balance_due": balance, "missed_installment": missed["no"]})


def verify_razorpay_signature(body: bytes, signature: str | None) -> bool:
    secret = settings.RAZORPAY_WEBHOOK_SECRET
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
