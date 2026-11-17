"""
Human mediators review AI proposals before the parties see them (when an
organisation, or the platform, requires it). Mediators are configured via
MEDIATOR_EMAILS and log in like any user.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.core.dependencies import get_current_user
from app.models.case import CaseStatus
from app.services.blob_service import refresh_case_urls
from app.services.cosmos_service import cosmos_service
from app.core.jurisdictions import money

router = APIRouter()
logger = logging.getLogger(__name__)


def require_mediator(current_user: dict = Depends(get_current_user)) -> dict:
    if (current_user.get("email") or "").lower() not in settings.mediator_emails:
        raise HTTPException(status_code=403, detail="Mediator access only.")
    return current_user


def _pending(case: dict):
    neg = cosmos_service.get_negotiation_by_case(case["id"])
    rnd = cosmos_service.get_round(neg["id"], case.get("current_round", 1)) if neg else None
    if (case.get("status") != CaseStatus.MEDIATOR_REVIEW.value or not rnd
            or not rnd.get("draft_proposal") or rnd.get("proposal_issued_at")):
        return None, None
    return neg, rnd


@router.get("/queue")
async def review_queue(mediator: dict = Depends(require_mediator)):
    items = []
    for case in cosmos_service.get_cases_by_status(CaseStatus.MEDIATOR_REVIEW):
        neg, rnd = _pending(case)
        if not rnd:
            continue
        items.append({
            "case_id": case["id"], "reference": case.get("external_reference"), "track": case.get("track"),
            "claimant_name": case.get("claimant_name"), "respondent_name": case.get("respondent_name"),
            "claim_amount": case.get("claim_amount"), "round": rnd["round_number"],
            "claimant_offer": (rnd.get("claimant") or {}).get("amount") or (rnd.get("claimant") or {}).get("actions"),
            "respondent_offer": (rnd.get("respondent") or {}).get("amount") or (rnd.get("respondent") or {}).get("actions"),
            "draft": rnd["draft_proposal"], "review_due": case.get("current_deadline"),
        })
    items.sort(key=lambda x: x["review_due"] or "")
    return {"queue": items}


@router.get("/cases/{case_id}")
async def review_case(case_id: str, mediator: dict = Depends(require_mediator)):
    case = cosmos_service.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found.")
    neg = cosmos_service.get_negotiation_by_case(case_id)
    return {"case": refresh_case_urls(case), "negotiation": neg}


class Approval(BaseModel):
    amount:    float | None = None
    actions:   list[str] | None = None
    reasoning: str | None = Field(default=None, max_length=4000)
    note:      str | None = Field(default=None, max_length=1000)


@router.post("/cases/{case_id}/approve")
async def approve_proposal(case_id: str, body: Approval, mediator: dict = Depends(require_mediator)):
    case = cosmos_service.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found.")
    neg, rnd = _pending(case)
    if not rnd:
        raise HTTPException(status_code=409, detail="No proposal is awaiting review for this case.")
    draft = rnd["draft_proposal"]
    amount = draft.get("amount") if body.amount is None else body.amount
    actions = draft.get("actions") if body.actions is None else body.actions
    reasoning = (body.reasoning or draft.get("reasoning") or "").strip()

    if case.get("track") != "non_monetary":
        ask = (rnd.get("claimant") or {}).get("amount")
        offer = (rnd.get("respondent") or {}).get("amount")
        if amount is None or amount <= 0:
            raise HTTPException(status_code=400, detail="A positive amount is required.")
        if ask is not None and offer is not None and offer < ask and not (offer <= amount <= ask):
            raise HTTPException(status_code=400,
                                detail=f"The proposal must lie between the offers ({money(offer, case)} – {money(ask, case)}).")
    if len(reasoning) < 40:
        raise HTTPException(status_code=400, detail="Give the parties a reasoned explanation (at least 40 characters).")

    edited = (amount != draft.get("amount") or actions != draft.get("actions")
              or reasoning != (draft.get("reasoning") or "").strip())
    from app.routers.negotiation import issue_proposal
    issue_proposal(case_id, rnd["round_number"], amount, actions, reasoning, review={
        "reviewed_by": mediator["email"], "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "edited": edited, "note": body.note,
    })
    logger.info(f"Mediator approved proposal | case_id={case_id} | by={mediator['email']} | edited={edited}")
    return {"message": "Proposal issued to both parties.", "edited": edited}
