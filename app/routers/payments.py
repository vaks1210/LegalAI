"""Payment gateway callbacks."""
import json
import logging

from fastapi import APIRouter, HTTPException, Request

from app.services import payments
from app.services.cosmos_service import cosmos_service

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/razorpay/webhook", include_in_schema=False)
async def razorpay_webhook(request: Request):
    body = await request.body()
    if not payments.verify_razorpay_signature(body, request.headers.get("X-Razorpay-Signature")):
        raise HTTPException(status_code=400, detail="Invalid signature")
    event = json.loads(body or b"{}")
    if event.get("event") != "payment_link.paid":
        return {"ignored": event.get("event")}
    link = (((event.get("payload") or {}).get("payment_link") or {}).get("entity") or {})
    reference = link.get("reference_id") or ""
    case_id = reference.rsplit("-", 1)[0] if reference.count("-") > 4 else reference   # "<case uuid>-<instalment>"
    case = cosmos_service.get_case(case_id)
    plan = ((case or {}).get("payment") or {}).get("plan") or []
    if not case or not any(i.get("link_id") == link.get("id") for i in plan):
        logger.warning(f"Razorpay webhook for unknown link | link={link.get('id')}")
        return {"ignored": "unknown payment link"}
    payments.mark_paid(case["id"], "razorpay", {"gateway_status": link.get("status")}, link_id=link.get("id"))
    return {"ok": True}
