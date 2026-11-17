"""
Phone channels: WhatsApp and SMS.

Most respondents in India read WhatsApp, not legal email, so every
respondent-facing notice (invite, deadline nudges, proposals, payment
requests and reminders) also goes to the respondent's phone when one is on
the case.

Providers, chosen by configuration:
  • WhatsApp — Meta WhatsApp Cloud API. Business-initiated messages must use
    an approved template; register one utility template (default name
    `legalai_update`, two body variables):
        "LegalAI Resolver: {{1}} Open your case: {{2}}"
  • SMS — Twilio Programmable Messaging (in India the sender must be
    DLT-registered with TRAI; Twilio handles STOP opt-outs).
  • Neither configured (development) — messages are written to the local
    outbox next to emails, tagged with their channel.

WhatsApp is tried first, SMS is the fallback. Each attempt is appended to the
case's `channel_log`, which doubles as proof of service. Respondents can opt
out from the portal; organisations can switch either channel off.
"""
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

GRAPH_API = "https://graph.facebook.com/v21.0"
TWILIO_API = "https://api.twilio.com/2010-04-01"
TIMEOUT_SECONDS = 15
SMS_MAX_CHARS = 320
WHATSAPP_PARAM_MAX = 900
CHANNEL_LOG_MAX = 50


def normalize_phone(raw: str | None, default_cc: str | None = None) -> str | None:
    """Return the number in E.164 digits (no '+'), or None if it is unusable."""
    if not raw:
        return None
    raw = str(raw).strip()
    cc = (default_cc or settings.DEFAULT_COUNTRY_CODE or "91").lstrip("+")
    plus = raw.startswith("+") or raw.startswith("00")
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("00"):
        digits = digits[2:]
    if not digits:
        return None
    if plus:
        return digits if 8 <= len(digits) <= 15 else None
    if digits.startswith("0"):
        digits = digits.lstrip("0")
        return cc + digits if 7 <= len(digits) <= 12 else None
    if cc == "91" and len(digits) == 10:
        return "91" + digits if digits[0] in "6789" else None
    if len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
        return digits
    if len(digits) <= 10:
        return cc + digits if len(digits) >= 7 else None
    return digits if len(digits) <= 15 else None


def mask_phone(e164: str | None) -> str:
    if not e164:
        return ""
    return f"+{e164[:2]}******{e164[-2:]}" if len(e164) > 4 else "****"


class MessagingService:
    def __init__(self, outbox_dir: str | None = None):
        self.outbox_dir = outbox_dir or os.path.join(settings.LOCAL_DATA_DIR, "outbox")

    # ── provider detection ───────────────────────────────
    @property
    def whatsapp_configured(self) -> bool:
        return bool(settings.WHATSAPP_TOKEN.strip() and settings.WHATSAPP_PHONE_NUMBER_ID.strip())

    @property
    def sms_configured(self) -> bool:
        return bool(settings.TWILIO_ACCOUNT_SID.strip() and settings.TWILIO_AUTH_TOKEN.strip()
                    and settings.TWILIO_FROM_NUMBER.strip())

    @property
    def mode(self) -> str:
        modes = [m for m, on in (("whatsapp_cloud", self.whatsapp_configured),
                                 ("twilio_sms", self.sms_configured)) if on]
        return "+".join(modes) if modes else "local_outbox"

    # ── transports ───────────────────────────────────────
    def _outbox(self, channel: str, to: str, text: str, link: str | None) -> dict:
        os.makedirs(self.outbox_dir, exist_ok=True)
        stamp = datetime.now(timezone.utc)
        rec = {
            "id": uuid.uuid4().hex[:12], "channel": channel, "to": "+" + to,
            "subject": f"[{channel}] {text[:60]}", "sent_at": stamp.isoformat(),
            "text": text, "html": text, "links": [link] if link else [],
        }
        fname = f"{stamp.strftime('%Y%m%dT%H%M%S%f')}_{rec['id']}.json"
        with open(os.path.join(self.outbox_dir, fname), "w") as f:
            json.dump(rec, f, indent=2)
        logger.info(f"{channel} saved to local outbox | to={mask_phone(to)}")
        return {"ok": True, "provider": "local_outbox", "id": rec["id"]}

    def send_whatsapp(self, to: str, text: str, link: str | None, lang: str | None = None) -> dict:
        registered = {l.strip() for l in settings.WHATSAPP_TEMPLATE_LANGS.split(",") if l.strip()}
        tpl_lang = lang if lang in registered else settings.WHATSAPP_TEMPLATE_LANG
        payload = {
            "messaging_product": "whatsapp", "to": to, "type": "template",
            "template": {
                "name": settings.WHATSAPP_TEMPLATE_NAME,
                "language": {"code": tpl_lang},
                "components": [{"type": "body", "parameters": [
                    {"type": "text", "text": _one_line(text)[:WHATSAPP_PARAM_MAX]},
                    {"type": "text", "text": link or settings.BASE_URL},
                ]}],
            },
        }
        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                r = client.post(f"{GRAPH_API}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages",
                                headers={"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"},
                                json=payload)
            if r.status_code < 300:
                msg_id = ((r.json().get("messages") or [{}])[0]).get("id")
                return {"ok": True, "provider": "whatsapp_cloud", "id": msg_id}
            logger.error(f"WhatsApp failed | to={mask_phone(to)} | status={r.status_code} | body={r.text[:300]}")
            return {"ok": False, "provider": "whatsapp_cloud", "error": f"http {r.status_code}"}
        except Exception as e:
            logger.error(f"WhatsApp failed | to={mask_phone(to)} | error={e}")
            return {"ok": False, "provider": "whatsapp_cloud", "error": str(e)[:120]}

    def send_sms(self, to: str, text: str, link: str | None) -> dict:
        body = _one_line(text)
        if link:
            body = body[: SMS_MAX_CHARS - len(link) - 1] + " " + link
        else:
            body = body[:SMS_MAX_CHARS]
        sid = settings.TWILIO_ACCOUNT_SID
        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                r = client.post(f"{TWILIO_API}/Accounts/{sid}/Messages.json",
                                auth=(sid, settings.TWILIO_AUTH_TOKEN),
                                data={"To": "+" + to, "From": settings.TWILIO_FROM_NUMBER, "Body": body})
            if r.status_code < 300:
                return {"ok": True, "provider": "twilio_sms", "id": r.json().get("sid")}
            logger.error(f"SMS failed | to={mask_phone(to)} | status={r.status_code} | body={r.text[:300]}")
            return {"ok": False, "provider": "twilio_sms", "error": f"http {r.status_code}"}
        except Exception as e:
            logger.error(f"SMS failed | to={mask_phone(to)} | error={e}")
            return {"ok": False, "provider": "twilio_sms", "error": str(e)[:120]}

    # ── policy ───────────────────────────────────────────
    def channels_for(self, case: dict) -> list[str]:
        """Channels allowed for this case, in the order they are tried."""
        if case.get("respondent_channel_opt_out"):
            return []
        prefs = {"whatsapp": True, "sms": True}
        if case.get("org_id"):
            from app.services.cosmos_service import cosmos_service
            org = cosmos_service.get_org(case["org_id"]) or {}
            prefs.update((org.get("settings") or {}).get("channels") or {})
        return [c for c in ("whatsapp", "sms") if prefs.get(c)]

    def notify_respondent(self, case: dict, kind: str, text: str, link: str | None = None) -> dict | None:
        """
        Send `text` (+ link) to the respondent's phone. Never raises: a phone
        failure must not break the email flow it accompanies.
        Returns the delivery record, or None when nothing was attempted.
        """
        try:
            from app.core.jurisdictions import case_jurisdiction
            to = normalize_phone(case.get("respondent_phone"), default_cc=case_jurisdiction(case).phone_cc)
            channels = self.channels_for(case) if to else []
            if not channels:
                return None
            live = self.whatsapp_configured or self.sms_configured
            if not live and settings.is_production:
                return None
            if link is None:
                from app.core.security import respondent_portal_url
                link = respondent_portal_url(case)
            result = None
            for channel in channels:
                if not live:
                    result = {**self._outbox(channel, to, text, link), "channel": channel}
                    break
                if channel == "whatsapp" and self.whatsapp_configured:
                    from app.core.i18n import case_language
                    result = {**self.send_whatsapp(to, text, link, case_language(case)), "channel": channel}
                elif channel == "sms" and self.sms_configured:
                    result = {**self.send_sms(to, text, link), "channel": channel}
                else:
                    continue
                if result["ok"]:
                    break
            if result is None:
                return None
            entry = {"at": datetime.now(timezone.utc).isoformat(), "kind": kind,
                     "to": mask_phone(to), **{k: result.get(k) for k in ("channel", "provider", "ok", "id", "error")
                                              if result.get(k) is not None}}
            from app.services.cosmos_service import cosmos_service

            def log(c):
                c["channel_log"] = (c.get("channel_log") or [])[-(CHANNEL_LOG_MAX - 1):] + [entry]
            cosmos_service._mutate_case(case["id"], log)
            return entry
        except Exception:
            logger.exception(f"Phone notification failed | case_id={case.get('id')} | kind={kind}")
            return None


# ── Message texts (short: they must read well in a WhatsApp preview) ──

def _ref(case: dict) -> str:
    return case["id"][:8].upper()


def _amount(value, case: dict | None = None) -> str:
    from app.core.jurisdictions import money
    try:
        return money(float(value), case or {}) if value else ""
    except (TypeError, ValueError):
        return ""


def _date(value, lang: str) -> str:
    d = datetime.fromisoformat(value) if isinstance(value, str) else value
    return d.strftime("%d %b %Y") if lang == "en" else d.strftime("%d/%m/%Y")


def _msg(case: dict, key: str, **params) -> str:
    from app.core.i18n import case_language, t
    return t(key, case_language(case), ref=_ref(case), claimant=case.get("claimant_name", ""), **params)


def invite_text(case: dict) -> str:
    amt = _amount(case.get("claim_amount"), case)
    return _msg(case, "invite_amount" if amt else "invite", amount=amt)


def nudge_text(case: dict, due) -> str:
    from app.core.i18n import case_language
    return _msg(case, "nudge", date=_date(due, case_language(case)))


def proposal_text(case: dict, round_num: int, amount) -> str:
    amt = _amount(amount, case)
    return _msg(case, "proposal_amount" if amt else "proposal", amount=amt, round=round_num)


def next_round_text(case: dict, round_num: int) -> str:
    return _msg(case, "next_round", round=round_num)


def payment_request_text(case: dict, payment: dict) -> str:
    from app.core.i18n import case_language
    lang, plan = case_language(case), payment.get("plan") or []
    if len(plan) > 1:
        text = _msg(case, "pay_plan", n=len(plan), amount=_amount(plan[0]["amount"], case),
                    date=_date(plan[0]["due_at"], lang))
    else:
        text = _msg(case, "pay_single", amount=_amount(payment["amount"], case), date=_date(payment["due_at"], lang))
    if not payment.get("link_url"):
        text += " " + _msg(case, "quote_ref", reference=payment.get("reference", ""))
    return text


def payment_reminder_text(case: dict, payment: dict, inst: dict) -> str:
    from app.core.i18n import case_language
    lang, plan = case_language(case), payment.get("plan") or []
    date = _date(inst["due_at"], lang)
    if len(plan) > 1:
        text = _msg(case, "reminder_instalment", no=inst["no"], n=len(plan), amount=_amount(inst["amount"], case), date=date)
    else:
        text = _msg(case, "reminder_single", amount=_amount(inst["amount"], case), date=date)
    if not inst.get("link_url"):
        text += " " + _msg(case, "quote_ref", reference=payment.get("reference", ""))
    return text


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


messaging_service = MessagingService()
