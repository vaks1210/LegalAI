import functools
import html
import inspect
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.core.jurisdictions import money

logger = logging.getLogger(__name__)

SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"
SENDGRID_TIMEOUT_SECONDS = 30


def _escape_fields(*names):
    """HTML-escape the named (user-supplied) arguments before templating."""
    def deco(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            for n in names:
                v = bound.arguments.get(n)
                if isinstance(v, str):
                    bound.arguments[n] = html.escape(v)
            return fn(*bound.args, **bound.kwargs)
        return wrapper
    return deco


class EmailService:
    def __init__(self, outbox_dir: str | None = None):
        self.api_key = settings.SENDGRID_API_KEY.strip()
        self.sender = settings.SENDGRID_FROM_EMAIL.strip()
        self.outbox_dir = outbox_dir or os.path.join(settings.LOCAL_DATA_DIR, "outbox")

    def is_configured(self) -> bool:
        return bool(self.api_key and self.sender)

    @property
    def mode(self) -> str:
        return "sendgrid" if self.is_configured() else "local_outbox"

    def _write_to_outbox(self, to_email: str, subject: str, html_body: str) -> bool:
        """
        Local mode: instead of sending, save the email to LOCAL_DATA_DIR/outbox
        and log it, so every flow (OTP, invites, settlement links) still works.
        """
        os.makedirs(self.outbox_dir, exist_ok=True)
        stamp = datetime.now(timezone.utc)
        record = {
            "id":      uuid.uuid4().hex[:12],
            "to":      to_email,
            "subject": subject,
            "sent_at": stamp.isoformat(),
            "html":    html_body,
            "links":   re.findall(r'href="([^"]+)"', html_body),
        }
        fname = f"{stamp.strftime('%Y%m%dT%H%M%S%f')}_{record['id']}.json"
        with open(os.path.join(self.outbox_dir, fname), "w") as f:
            json.dump(record, f, indent=2)
        logger.info(
            f"Email saved to local outbox | to={to_email} | subject={subject}"
            + (f" | links={record['links']}" if record["links"] else "")
        )
        return True

    def list_outbox(self, to_email: str | None = None, limit: int = 50) -> list:
        if not os.path.isdir(self.outbox_dir):
            return []
        items = []
        for name in sorted(os.listdir(self.outbox_dir), reverse=True):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(self.outbox_dir, name)) as f:
                rec = json.load(f)
            if to_email and rec["to"].lower() != to_email.lower():
                continue
            items.append(rec)
            if len(items) >= limit:
                break
        return items

    def send(self, to_email: str, subject: str, html_body: str) -> bool:
        if not self.is_configured():
            if settings.is_production:
                logger.error(
                    "Email failed | provider=sendgrid | reason=missing_configuration"
                )
                return False
            return self._write_to_outbox(to_email, subject, html_body)

        payload = {
            "personalizations": [{"to": [{"email": to_email}]}],
            "from": {"email": self.sender},
            "subject": subject,
            "content": [{"type": "text/html", "value": html_body}],
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            with httpx.Client(timeout=SENDGRID_TIMEOUT_SECONDS) as client:
                response = client.post(
                    SENDGRID_API_URL,
                    headers=headers,
                    json=payload,
                )

            if response.status_code in (200, 201, 202):
                logger.info(
                    f"Email sent | provider=sendgrid | to={to_email} | subject={subject}"
                )
                return True

            logger.error(
                "Email failed | provider=sendgrid | "
                f"to={to_email} | status={response.status_code} | body={response.text[:500]}"
            )
            return False
        except Exception as e:
            logger.error(
                f"Email failed | provider=sendgrid | to={to_email} | error={e}"
            )
            return False

    @_escape_fields('party_name', 'summary')
    def send_case_update(
        self,
        to_email: str,
        party_name: str,
        case_id: str,
        headline: str,
        summary: str,
        portal_url: str | None = None,
        action_label: str | None = None,
    ) -> bool:
        short_id = case_id[:8].upper()
        action_block = ""
        if portal_url and action_label:
            action_block = f"""
            <a href="{portal_url}"
               style="display:block;background:#06B6D4;color:#0F2A4A;
                      text-align:center;padding:14px;border-radius:6px;
                      font-weight:bold;text-decoration:none;font-size:15px;margin-top:18px;">
                {action_label}
            </a>
            """
        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;
                    background:#0F2A4A;padding:32px;border-radius:8px;">
            <h2 style="color:#06B6D4;">LegalAI Resolver</h2>
            <p style="color:#E2E8F0;font-size:14px;">Dear {party_name},</p>
            <div style="background:#1E3A6E;border-radius:8px;padding:16px;margin:16px 0;">
                <p style="color:#93C5FD;font-size:11px;margin:0 0 6px 0;
                           font-weight:bold;letter-spacing:1px;">CASE UPDATE</p>
                <p style="color:#FFFFFF;font-size:18px;font-weight:bold;margin:0 0 6px 0;">{headline}</p>
                <p style="color:#E2E8F0;font-size:13px;margin:0;line-height:1.6;">{summary}</p>
            </div>
            {action_block}
            <p style="color:#475569;font-size:11px;margin-top:18px;">
                Case Reference: {short_id}
            </p>
        </div>
        """
        return self.send(to_email, f"{headline} - Case #{short_id}", html)

    @_escape_fields('display_name')
    def send_otp(self, to_email: str, otp: str, display_name: str = "") -> bool:
        name = display_name or to_email
        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;
                    background:#0F2A4A;padding:32px;border-radius:8px;">
            <h2 style="color:#06B6D4;margin-bottom:4px;">LegalAI Resolver</h2>
            <p style="color:#93C5FD;font-size:12px;margin-top:0;">
                AI-Powered Pre-Litigation Dispute Resolution
            </p>
            <p style="color:#E2E8F0;font-size:14px;">Hi {name},</p>
            <p style="color:#E2E8F0;font-size:14px;">Your login code is:</p>
            <div style="background:#1E293B;border-radius:8px;padding:24px;
                        text-align:center;margin:20px 0;">
                <span style="font-size:36px;font-weight:bold;color:#06B6D4;
                             letter-spacing:12px;">{otp}</span>
            </div>
            <p style="color:#94A3B8;font-size:12px;">
                This code expires in <b>10 minutes</b>.
                Do not share it with anyone.
            </p>
            <hr style="border-color:#1E293B;margin:20px 0;">
            <p style="color:#475569;font-size:11px;">
                If you did not request this code, ignore this email.
            </p>
        </div>
        """
        return self.send(to_email, "Your LegalAI Login Code", html)

    @_escape_fields('respondent_name', 'claimant_name', 'dispute_summary')
    def send_case_invite(
        self,
        to_email: str,
        respondent_name: str,
        claimant_name: str,
        case_id: str,
        claim_amount,
        dispute_summary: str,
        portal_url: str | None = None,
        language: str | None = None,
        currency: str = "INR",
    ) -> bool:
        from html import escape as _esc
        from app.core.i18n import LANGUAGES, t as _t
        intro = _t("email_intro", language) if language and language != "en" else ""
        rtl = ' dir="rtl"' if LANGUAGES.get(language or "", {}).get("rtl") else ""
        intro_block = (f'<p{rtl} style="color:#fff;background:#1E293B;border-left:4px solid #06B6D4;'
                       f'padding:12px;border-radius:6px;font-size:15px;line-height:1.6;">{_esc(intro)}</p>'
                       if intro else "")
        if not portal_url:
            from app.core.security import respondent_token
            portal_url = (
                f"{settings.BASE_URL.rstrip('/')}/respond/{case_id}"
                f"?t={respondent_token(case_id, to_email)}"
            )
        amount_str = (
            money(claim_amount, currency) if claim_amount else "Non-monetary dispute"
        )
        short_id = case_id[:8].upper()
        short_summary = (
            dispute_summary[:300] + "..."
            if len(dispute_summary) > 300 else dispute_summary
        )

        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;
                    background:#0F2A4A;padding:32px;border-radius:8px;">
            <h2 style="color:#06B6D4;margin-bottom:4px;">LegalAI Resolver</h2>
            {intro_block}
            <div style="background:#DC2626;border-radius:6px;padding:12px 16px;
                        margin:16px 0;">
                <p style="color:#fff;font-weight:bold;margin:0;font-size:14px;">
                    [Action Required] Legal Dispute Notice - Case #{short_id}
                </p>
            </div>
            <p style="color:#E2E8F0;font-size:14px;">Dear {respondent_name},</p>
            <p style="color:#E2E8F0;font-size:14px;">
                {claimant_name} has filed a formal dispute against you through
                LegalAI Resolver. You must respond within <b>7 days</b>.
            </p>
            <table style="width:100%;border-collapse:collapse;margin:16px 0;">
                <tr>
                    <td style="color:#94A3B8;padding:8px 0;font-size:13px;
                               border-bottom:1px solid #1E293B;">Claimant</td>
                    <td style="color:#E2E8F0;padding:8px 0;font-size:13px;
                               border-bottom:1px solid #1E293B;">{claimant_name}</td>
                </tr>
                <tr>
                    <td style="color:#94A3B8;padding:8px 0;font-size:13px;
                               border-bottom:1px solid #1E293B;">Claim Amount</td>
                    <td style="color:#06B6D4;padding:8px 0;font-size:13px;
                               font-weight:bold;border-bottom:1px solid #1E293B;">
                        {amount_str}
                    </td>
                </tr>
                <tr>
                    <td style="color:#94A3B8;padding:8px 0;font-size:13px;">
                        Case Reference
                    </td>
                    <td style="color:#E2E8F0;padding:8px 0;font-size:13px;">
                        {short_id}
                    </td>
                </tr>
            </table>
            <p style="color:#E2E8F0;font-size:13px;background:#1E293B;
                      padding:12px;border-radius:6px;line-height:1.5;">
                {short_summary}
            </p>
            <a href="{portal_url}"
               style="display:block;background:#06B6D4;color:#0F2A4A;
                      text-align:center;padding:14px;border-radius:6px;
                      font-weight:bold;text-decoration:none;
                      margin-top:20px;font-size:15px;">
                View Case &amp; Respond
            </a>
            <p style="color:#475569;font-size:11px;margin-top:20px;line-height:1.5;">
                Non-participation after 7 days results in automatic escalation
                and generation of a court-ready case file noting your
                non-response as evidence of bad faith.
            </p>
        </div>
        """
        return self.send(
            to_email,
            f"[Action Required] Legal Dispute Case #{short_id}",
            html,
        )

    @_escape_fields('party_name', 'reasoning')
    def send_proposal(
        self,
        to_email: str,
        party_name: str,
        case_id: str,
        round_num: int,
        proposed_amount: float,
        reasoning: str,
        portal_url: str,
        currency: str = "INR",
    ) -> bool:
        short_id = case_id[:8].upper()
        short_reason = reasoning[:400] + "..." if len(reasoning) > 400 else reasoning
        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;
                    background:#0F2A4A;padding:32px;border-radius:8px;">
            <h2 style="color:#06B6D4;">LegalAI Resolver</h2>
            <p style="color:#E2E8F0;font-size:14px;">Dear {party_name},</p>
            <p style="color:#E2E8F0;font-size:14px;">
                The AI Mediator has reviewed Round {round_num} offers from
                both parties and issued a settlement proposal.
            </p>
            <div style="background:#1E293B;border-radius:8px;padding:20px;
                        text-align:center;margin:16px 0;">
                <p style="color:#94A3B8;font-size:11px;margin:0 0 4px 0;
                           letter-spacing:2px;">PROPOSED SETTLEMENT</p>
                <p style="color:#06B6D4;font-size:30px;font-weight:bold;margin:0;">
                    {money(proposed_amount, currency)}
                </p>
            </div>
            <div style="background:#1E3A6E;border-radius:6px;padding:14px;margin:16px 0;">
                <p style="color:#93C5FD;font-size:11px;margin:0 0 6px 0;
                           font-weight:bold;">AI MEDIATOR REASONING</p>
                <p style="color:#E2E8F0;font-size:13px;margin:0;
                           line-height:1.5;">{short_reason}</p>
            </div>
            <a href="{portal_url}"
               style="display:block;background:#06B6D4;color:#0F2A4A;
                      text-align:center;padding:14px;border-radius:6px;
                      font-weight:bold;text-decoration:none;font-size:15px;">
                View &amp; Decide - Accept or Reject
            </a>
            <p style="color:#475569;font-size:11px;margin-top:16px;">
                You have 48 hours to respond. Failure to respond will be
                treated as rejection.
            </p>
        </div>
        """
        return self.send(
            to_email,
            f"[Round {round_num}] AI Settlement Proposal - Case #{short_id}",
            html,
        )

    @_escape_fields('party_name')
    def send_settlement_confirmation(
        self,
        to_email: str,
        party_name: str,
        case_id: str,
        settled_amount: float,
        download_url: str,
        currency: str = "INR",
    ) -> bool:
        short_id = case_id[:8].upper()
        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;
                    background:#0F2A4A;padding:32px;border-radius:8px;">
            <h2 style="color:#16A34A;">Settlement Confirmed</h2>
            <p style="color:#E2E8F0;font-size:14px;">Dear {party_name},</p>
            <p style="color:#E2E8F0;font-size:14px;">
                Both parties have accepted the settlement proposal.
                Your dispute has been resolved.
            </p>
            <div style="background:#166534;border-radius:8px;padding:20px;
                        text-align:center;margin:16px 0;">
                <p style="color:#86EFAC;font-size:11px;margin:0 0 4px 0;
                           letter-spacing:2px;">AGREED SETTLEMENT</p>
                <p style="color:#fff;font-size:30px;font-weight:bold;margin:0;">
                    {money(settled_amount, currency)}
                </p>
            </div>
            <p style="color:#E2E8F0;font-size:13px;">
                Please download your Settlement Agreement below.
            </p>
            <a href="{download_url}"
               style="display:block;background:#16A34A;color:#fff;
                      text-align:center;padding:14px;border-radius:6px;
                      font-weight:bold;text-decoration:none;font-size:15px;">
                Download Settlement Agreement
            </a>
            <p style="color:#475569;font-size:11px;margin-top:16px;">
                Case Reference: {short_id} | LegalAI Resolver
            </p>
        </div>
        """
        return self.send(
            to_email,
            f"Settlement Confirmed - Case #{short_id}",
            html,
        )

    @_escape_fields('party_name')
    def send_escalation_notice(
        self,
        to_email: str,
        party_name: str,
        case_id: str,
        download_url: str,
        for_claimant: bool = True,
    ) -> bool:
        short_id = case_id[:8].upper()
        if for_claimant:
            body = ("The AI mediation process has concluded without reaching a settlement. "
                    "A court-ready case file has been prepared for you.")
            label, subject = "Download Court File", f"Mediation Concluded - Court File Ready - Case #{short_id}"
        else:
            body = ("The AI mediation process has concluded without reaching a settlement. "
                    "The claimant may now pursue the matter before the appropriate court or forum. "
                    "A certificate recording the mediation attempt is available below.")
            label, subject = "View Mediation Certificate", f"Mediation Concluded - Case #{short_id}"
        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;
                    background:#0F2A4A;padding:32px;border-radius:8px;">
            <h2 style="color:#DC2626;">Mediation Concluded</h2>
            <p style="color:#E2E8F0;font-size:14px;">Dear {party_name},</p>
            <p style="color:#E2E8F0;font-size:14px;">{body}</p>
            <a href="{download_url}"
               style="display:block;background:#DC2626;color:#fff;
                      text-align:center;padding:14px;border-radius:6px;
                      font-weight:bold;text-decoration:none;font-size:15px;">
                {label}
            </a>
            <p style="color:#475569;font-size:11px;margin-top:16px;">
                Case Reference: {short_id} | LegalAI Resolver
            </p>
        </div>
        """
        return self.send(to_email, subject, html)

    @_escape_fields('party_name')
    def send_next_round_invite(
        self,
        to_email: str,
        party_name: str,
        case_id: str,
        round_num: int,
        portal_url: str,
    ) -> bool:
        short_id = case_id[:8].upper()
        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;
                    background:#0F2A4A;padding:32px;border-radius:8px;">
            <h2 style="color:#D97706;">Round {round_num} - Submit Updated Offer</h2>
            <p style="color:#E2E8F0;font-size:14px;">Dear {party_name},</p>
            <p style="color:#E2E8F0;font-size:14px;">
                Round {round_num} is now open. Please submit your updated offer.
            </p>
            <a href="{portal_url}"
               style="display:block;background:#D97706;color:#fff;
                      text-align:center;padding:14px;border-radius:6px;
                      font-weight:bold;text-decoration:none;font-size:15px;">
                Submit Round {round_num} Offer
            </a>
            <p style="color:#475569;font-size:11px;margin-top:16px;">
                Round {round_num} of 3. After 3 rounds without settlement,
                the case escalates automatically.
            </p>
        </div>
        """
        return self.send(
            to_email,
            f"[Round {round_num}] Submit Updated Offer - Case #{short_id}",
            html,
        )

    @_escape_fields('claimant_name')
    def send_respondent_offer_notification(
        self,
        to_email: str,
        claimant_name: str,
        case_id: str,
        round_num: int,
    ) -> bool:
        short_id = case_id[:8].upper()
        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;
                    background:#0F2A4A;padding:32px;border-radius:8px;">
            <h2 style="color:#06B6D4;">LegalAI Resolver</h2>
            <p style="color:#E2E8F0;font-size:14px;">Dear {claimant_name},</p>
            <p style="color:#E2E8F0;font-size:14px;">
                The respondent has submitted their Round {round_num} offer
                for Case #{short_id}. Please login and submit your offer.
            </p>
            <a href="{settings.BASE_URL}"
               style="display:block;background:#06B6D4;color:#0F2A4A;
                      text-align:center;padding:14px;border-radius:6px;
                      font-weight:bold;text-decoration:none;font-size:15px;">
                Login and Submit Offer
            </a>
        </div>
        """
        return self.send(
            to_email,
            f"[Round {round_num}] Respondent Submitted Offer - Case #{short_id}",
            html,
        )


email_service = EmailService()
