import html
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from app.models.case import CaseStatus
from app.services.cosmos_service import cosmos_service
from app.core.state_machine import is_expired

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# MAIN EXPIRY CHECK — runs every hour
# ══════════════════════════════════════════════════════════════

async def run_expiry_check():
    """
    Checks all non-terminal cases with a deadline.
    For each expired case applies the correct action.
    """
    cases = cosmos_service.get_cases_needing_expiry_check()
    logger.info(f"Expiry check running | {len(cases)} cases to check")

    expired = [c for c in cases if is_expired(c)]
    logger.info(f"Expiry check | {len(expired)} expired cases found")

    for case in expired:
        await _handle_expired_case(case)

    expired_ids = {c["id"] for c in expired}
    for case in cases:
        if case["id"] not in expired_ids:
            try:
                send_respondent_nudge(case)
            except Exception:
                logger.exception(f"Nudge failed | case_id={case['id']}")


# States where the case is waiting on the respondent, and the label used in
# the reminder. A nudge goes out NUDGE_DAYS before the deadline, once each.
RESPONDENT_ACTION_STATES = {
    CaseStatus.INVITE_SENT.value:            "respond to the dispute",
    CaseStatus.RESPONDENT_VIEWED.value:      "submit your response",
    CaseStatus.WAITING_FOR_RESPONDENT.value: "submit your offer",
}
NUDGE_DAYS = (3, 1)


def send_respondent_nudge(case: dict, now: datetime | None = None) -> str | None:
    """Remind a respondent (email + WhatsApp/SMS) before their window closes."""
    action = RESPONDENT_ACTION_STATES.get(case.get("status"))
    deadline = case.get("current_deadline")
    if not action or not deadline:
        return None
    now = now or datetime.now(timezone.utc)
    due = datetime.fromisoformat(deadline)
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    left = due - now
    if left.total_seconds() <= 0:
        return None
    window = next((d for d in sorted(NUDGE_DAYS) if left <= timedelta(days=d)), None)
    if window is None:
        return None
    key = f"{case['status']}:{deadline}:{window}d"
    claimed = {}

    def claim(c):
        sent = c.setdefault("nudges_sent", [])
        if key in sent:
            return
        sent.append(key)
        del sent[:-20]
        claimed["ok"] = True
    case = cosmos_service._mutate_case(case["id"], claim)
    if not claimed:
        return None

    from app.core.security import respondent_portal_url
    from app.services.email_service import email_service
    from app.services import messaging
    days_left = max(0, left.days)
    email_service.send_case_update(
        to_email=case["respondent_email"], party_name=case["respondent_name"], case_id=case["id"],
        headline=f"Reminder: please {action} by {due.strftime('%d %b %Y')}",
        summary=(f"{case['claimant_name']}'s dispute with you is waiting for you to {action}. "
                 f"The window closes on {due.strftime('%d %B %Y')}. If you do not take part, the claimant "
                 "may take the matter to court with a record of your non-response."),
        portal_url=respondent_portal_url(case), action_label="Open case",
    )
    messaging.messaging_service.notify_respondent(case, "nudge", messaging.nudge_text(case, days_left))
    logger.info(f"Respondent nudged | case_id={case['id']} | window={window}d")
    return key


async def _handle_expired_case(case: dict):
    """Applies the correct expiry action based on current status."""
    case_id = case["id"]
    status  = case["status"]

    try:
        if status == CaseStatus.ANALYZED.value:
            await _expire_analyzed(case_id)

        elif status == CaseStatus.INVITE_SENT.value:
            await _expire_invite_sent(case_id, case)

        elif status == CaseStatus.RESPONDENT_VIEWED.value:
            await _expire_respondent_viewed(case_id, case)

        elif status in {
            CaseStatus.NEGOTIATION_OPEN.value,
            CaseStatus.WAITING_FOR_CLAIMANT.value,
            CaseStatus.WAITING_FOR_RESPONDENT.value,
            CaseStatus.PROOF_REQUESTED.value,
            CaseStatus.PROOF_RESPONSE_PENDING.value,
        }:
            await _expire_round_pending(case_id, case)

        elif status in {
            CaseStatus.PROPOSAL_ISSUED.value,
            CaseStatus.SETTLEMENT_PENDING_CONFIRMATION.value,
        }:
            await _expire_proposal_issued(case_id, case)

        elif status == CaseStatus.SETTLING.value:
            # SETTLING is a transient step inside the settlement handler; if
            # it expires the handler was interrupted — finish the settlement.
            # (It previously issued a payment-breach notice here.)
            from app.routers.negotiation import _handle_settlement
            await _handle_settlement(case_id)

        elif status == CaseStatus.ANALYZING.value:
            await _resume_analysis(case_id, case)

        elif status == CaseStatus.MEDIATOR_REVIEW.value:
            await _resume_mediation(case_id, case)

        elif status == CaseStatus.ESCALATING.value:
            await _resume_escalation(case_id)

    except Exception as e:
        logger.error(f"Expiry handler failed | case_id={case_id} | status={status} | error={e}")


# ══════════════════════════════════════════════════════════════
# EXPIRY HANDLERS PER STATE
# ══════════════════════════════════════════════════════════════

async def _resume_analysis(case_id: str, case: dict):
    """Analysis was interrupted (e.g. server restart) — run it again."""
    logger.warning(f"Resuming interrupted analysis | case_id={case_id}")
    from app.routers.cases import _run_full_pipeline, analyse_new_case
    if not case.get("track"):          # bulk-filed case interrupted before routing
        await analyse_new_case(case_id)
    else:
        await _run_full_pipeline(case_id)


async def _resume_mediation(case_id: str, case: dict):
    """Mediation was interrupted after both offers arrived — run it again."""
    neg = cosmos_service.get_negotiation_by_case(case_id)
    if not neg:
        return
    round_number = case.get("current_round", 1)
    rnd = cosmos_service.get_round(neg["id"], round_number) or {}
    draft = rnd.get("draft_proposal")
    if draft and not rnd.get("proposal_issued_at"):
        # Mediator review SLA passed: release the AI draft (flagged) rather
        # than leave both parties waiting indefinitely.
        logger.warning(f"Mediator review SLA passed — releasing draft | case_id={case_id}")
        from app.routers.negotiation import issue_proposal
        issue_proposal(case_id, round_number, draft.get("amount"), draft.get("actions"), draft.get("reasoning"),
                       review={"released_without_review": True,
                               "released_at": datetime.now(timezone.utc).isoformat()})
        return
    logger.warning(f"Resuming interrupted mediation | case_id={case_id}")
    from app.routers.negotiation import _run_mediation
    await _run_mediation(case_id, neg["id"], round_number)


async def _resume_escalation(case_id: str):
    """Escalation was interrupted — finish it."""
    logger.warning(f"Completing interrupted escalation | case_id={case_id}")
    cosmos_service.transition_case(case_id, CaseStatus.ESCALATED, {
        "escalated_at": datetime.now(timezone.utc).isoformat(),
        "action_required_by": None,
    })


async def _refresh_court_file(case_id: str) -> str:
    """
    Rebuild the court file after auto-escalation so it carries the dated
    service-of-notice record (email, portal views, WhatsApp/SMS notices).
    Falls back to the file generated at analysis time.
    """
    case = cosmos_service.get_case(case_id)
    docs = case.get("documents_data") or {}
    try:
        from app.agents.document_agent import _generate_court_file
        from app.services.blob_service import blob_service
        pdf = await _generate_court_file(case, case.get("intake_data") or {}, case.get("legal_data") or {},
                                         case.get("analytics_data") or {})
        name = f"court_file_escalated_{case_id}.pdf"
        blob_service.upload("pdfs", name, pdf)
        url = blob_service.generate_download_url("pdfs", name)
        cosmos_service.update_case(case_id, {"documents_data": {**docs, "court_file_url": url}})
        return url
    except Exception:
        logger.exception(f"Court file refresh failed | case_id={case_id}")
        return docs.get("court_file_url", "")


async def _expire_analyzed(case_id: str):
    """
    ANALYZED → ABANDONED
    Claimant submitted a case but never sent the invite within 14 days.
    No notification — case quietly closed.
    """
    cosmos_service.transition_case(case_id, CaseStatus.ABANDONED, {
        "abandoned_reason": "Claimant did not send invite within 14 days of analysis"
    })
    logger.info(f"Case abandoned | case_id={case_id} | reason=no_invite_sent")


async def _expire_invite_sent(case_id: str, case: dict):
    """
    INVITE_SENT → AUTO_ESCALATED
    Respondent never opened the email within 7 days.
    Claimant is notified with court file.
    """
    cosmos_service.transition_case(case_id, CaseStatus.AUTO_ESCALATED, {
        "auto_escalation_reason": (
            "Respondent did not open the invite within 7 days. "
            "Non-participation noted in court file."
        )
    })

    court_url = await _refresh_court_file(case_id)

    from app.services.email_service import email_service
    email_service.send(
        to_email=case["claimant_email"],
        subject=f"Case Auto-Escalated — Respondent Did Not Respond — #{case_id[:8].upper()}",
        html_body=f"""
        <div style="font-family:Arial;background:#0F2A4A;padding:24px;border-radius:8px;">
            <h3 style="color:#D97706;">LegalAI Resolver — Case Update</h3>
            <p style="color:#E2E8F0;">Dear {html.escape(case['claimant_name'])},</p>
            <p style="color:#E2E8F0;">
                <b>{html.escape(case['respondent_name'])}</b> did not respond to the mediation
                invite within 7 days. Your case has been automatically escalated.
            </p>
            <p style="color:#E2E8F0;">
                A court-ready case file has been prepared. The respondent's
                non-participation is documented as evidence of bad faith.
            </p>
            {'<a href="' + court_url + '" style="display:block;background:#DC2626;color:#fff;text-align:center;padding:12px;border-radius:6px;text-decoration:none;font-weight:bold;margin-top:16px;">Download Court File</a>' if court_url else ''}
        </div>
        """,
    )
    logger.info(f"Auto-escalated | case_id={case_id} | reason=invite_not_opened")


async def _expire_respondent_viewed(case_id: str, case: dict):
    """
    RESPONDENT_VIEWED → AUTO_ESCALATED
    Respondent opened the invite but did not submit a response within 5 days.
    """
    cosmos_service.transition_case(case_id, CaseStatus.AUTO_ESCALATED, {
        "auto_escalation_reason": (
            "Respondent viewed the case but did not submit a response within 5 days."
        )
    })

    court_url = await _refresh_court_file(case_id)

    from app.services.email_service import email_service
    email_service.send(
        to_email=case["claimant_email"],
        subject=f"Case Auto-Escalated — Respondent Did Not Engage — #{case_id[:8].upper()}",
        html_body=f"""
        <div style="font-family:Arial;background:#0F2A4A;padding:24px;border-radius:8px;">
            <h3 style="color:#D97706;">LegalAI Resolver — Case Update</h3>
            <p style="color:#E2E8F0;">Dear {html.escape(case['claimant_name'])},</p>
            <p style="color:#E2E8F0;">
                <b>{html.escape(case['respondent_name'])}</b> viewed your case but did not
                submit a response within 5 days. Your case has been escalated.
                Their deliberate non-engagement is documented in the court file.
            </p>
            {'<a href="' + court_url + '" style="display:block;background:#DC2626;color:#fff;text-align:center;padding:12px;border-radius:6px;text-decoration:none;font-weight:bold;margin-top:16px;">Download Court File</a>' if court_url else ''}
        </div>
        """,
    )
    logger.info(f"Auto-escalated | case_id={case_id} | reason=no_response_after_view")


async def _expire_round_pending(case_id: str, case: dict):
    """
    Waiting/proof state expired (3 days).
    If both sides have already submitted numbers, trigger mediation.
    Otherwise auto-escalate for non-participation or stale proof exchange.
    """
    neg = cosmos_service.get_negotiation_by_case(case_id)
    if not neg:
        logger.warning(f"Negotiation wait state expired but no negotiation found | case_id={case_id}")
        return

    round_number  = case.get("current_round", 1)
    current_round = cosmos_service.get_round(neg["id"], round_number)

    if not current_round:
        return

    # WAITING_FOR_* is also the status while one party has decided on an
    # issued proposal and the other hasn't. That is the decision phase, not
    # the offer phase: time it out like an unanswered proposal. (Re-running
    # mediation here changed nothing and left the case waiting forever.)
    if current_round.get("proposal_issued_at"):
        await _expire_proposal_issued(case_id, case)
        return

    claimant_submitted = bool((current_round.get("claimant") or {}).get("submitted_at"))
    respondent_submitted = bool((current_round.get("respondent") or {}).get("submitted_at"))
    if claimant_submitted and respondent_submitted:
        from app.routers.negotiation import _run_mediation
        await _run_mediation(case_id, neg["id"], round_number)
        return

    from app.routers.negotiation import _handle_escalation
    await _handle_escalation(case_id)


async def _expire_proposal_issued(case_id: str, case: dict):
    """
    Proposal/settlement confirmation expired (2 days) — one or both parties did not decide.
    Treat missing decision as REJECT.
    """
    neg = cosmos_service.get_negotiation_by_case(case_id)
    if not neg:
        return

    round_number  = case.get("current_round", 1)
    current_round = cosmos_service.get_round(neg["id"], round_number)

    if not current_round:
        return

    from app.models.negotiation import ProposalDecision
    now     = datetime.now(timezone.utc).isoformat()
    updates = {}

    claimant_state = current_round.get("claimant") or {}
    respondent_state = current_round.get("respondent") or {}

    if claimant_state.get("decision") == ProposalDecision.PENDING.value:
        claimant_state["decision"] = ProposalDecision.REJECT.value
        claimant_state["decided_at"] = now
        updates["claimant"] = claimant_state
        logger.info(f"Auto-rejected claimant | case_id={case_id} | reason=timeout")

    if respondent_state.get("decision") == ProposalDecision.PENDING.value:
        respondent_state["decision"] = ProposalDecision.REJECT.value
        respondent_state["decided_at"] = now
        updates["respondent"] = respondent_state
        logger.info(f"Auto-rejected respondent | case_id={case_id} | reason=timeout")

    if updates:
        cosmos_service.update_round_in_negotiation(neg["id"], round_number, updates)

    # Now process the outcome (both are now non-PENDING)
    max_rounds = case.get("max_rounds", 3)
    if round_number >= max_rounds:
        from app.routers.negotiation import _handle_escalation
        await _handle_escalation(case_id)
    else:
        from app.routers.negotiation import _start_next_round
        await _start_next_round(case_id, round_number + 1)


# ══════════════════════════════════════════════════════════════
# STARTUP INTEGRATION
# ══════════════════════════════════════════════════════════════

async def start_expiry_worker():
    """
    Runs expiry check every hour.
    Call this from app startup in main.py.
    """
    logger.info("Expiry worker starting — runs every 60 minutes")
    while True:
        try:
            # Blocking DB/PDF/email work — keep it off the request event loop.
            await asyncio.to_thread(lambda: asyncio.run(run_expiry_check()))
            from app.services.payments import check_payment_schedules
            await asyncio.to_thread(check_payment_schedules)
        except Exception as e:
            logger.error(f"Expiry worker error: {e}")
        await asyncio.sleep(3600)
