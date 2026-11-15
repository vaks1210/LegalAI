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
    case = cosmos_service.get_case(link.get("reference_id") or "")
    if not case or (case.get("payment") or {}).get("link_id") != link.get("id"):
        logger.warning(f"Razorpay webhook for unknown link | link={link.get('id')}")
        return {"ignored": "unknown payment link"}
    payments.mark_paid(case["id"], "razorpay", {"gateway_status": link.get("status")})
    return {"ok": True}
