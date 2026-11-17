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

    def send_whatsapp(self, to: str, text: str, link: str | None) -> dict:
        payload = {
            "messaging_product": "whatsapp", "to": to, "type": "template",
            "template": {
                "name": settings.WHATSAPP_TEMPLATE_NAME,
                "language": {"code": settings.WHATSAPP_TEMPLATE_LANG},
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
            to = normalize_phone(case.get("respondent_phone"))
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
                    result = {**self.send_whatsapp(to, text, link), "channel": channel}
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


def _amount(value) -> str:
    try:
        return f"Rs. {float(value):,.0f}" if value else ""
    except (TypeError, ValueError):
        return ""


def invite_text(case: dict) -> str:
    amt = _amount(case.get("claim_amount"))
    return (f"{case['claimant_name']} has raised a dispute with you"
            f"{' for ' + amt if amt else ''} (case {_ref(case)}). You can resolve it online, "
            "without going to court. Please respond within 7 days.")


def nudge_text(case: dict, days_left: int) -> str:
    when = "today" if days_left <= 0 else f"in {days_left} day{'s' if days_left != 1 else ''}"
    return (f"Reminder: the response window for case {_ref(case)} with {case['claimant_name']} closes {when}. "
            "If you do not respond, the claimant may take the matter to court with a record of non-response.")


def proposal_text(case: dict, round_num: int, amount) -> str:
    amt = _amount(amount)
    return (f"A settlement proposal{' of ' + amt if amt else ''} has been issued in case {_ref(case)} "
            f"(round {round_num}). Review it and accept or reject.")


def next_round_text(case: dict, round_num: int) -> str:
    return f"Negotiation round {round_num} has opened in case {_ref(case)}. Submit your offer."


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


messaging_service = MessagingService()
