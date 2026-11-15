"""
Organisations (B2B): businesses that file disputes in bulk.

Auth for every /api/orgs/{org_id}/... route: either a logged-in member
(JWT, role owner/admin/agent) or an organisation API key in `X-API-Key`
(for the business's own systems — billing, loan management, ...).
"""
import hashlib
import logging
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.core import b2b
from app.core.access import ADMIN_ROLES, ORG_ROLES, org_role
from app.core.background import in_thread
from app.core.dependencies import get_current_user
from app.core.security import decode_token
from app.models.case import CaseStatus, TERMINAL_STATES
from app.services.cosmos_service import cosmos_service

router = APIRouter()
logger = logging.getLogger(__name__)

API_KEY_PREFIX = "lai_"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


# ─────────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────────

class OrgActor:
    def __init__(self, org: dict, role: str, actor: str, user: dict | None):
        self.org, self.role, self.actor, self.user = org, role, actor, user

    def require(self, roles) -> None:
        if self.role not in roles:
            raise HTTPException(status_code=403, detail="Your role cannot perform this action.")


async def org_actor(
    org_id: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False)),
) -> OrgActor:
    api_key = request.headers.get("X-API-Key")
    if api_key:
        org = cosmos_service.get_org_by_api_key_hash(_hash_key(api_key.strip()))
        if not org or org["id"] != org_id:
            raise HTTPException(status_code=401, detail="Invalid API key for this organisation.")
        return OrgActor(org, "api", f"api_key:{api_key.strip()[:12]}", None)

    if not credentials:
        raise HTTPException(status_code=401, detail="Login or an API key is required.")
    try:
        email = (decode_token(credentials.credentials).get("sub") or "").lower()
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired authentication token.")
    org = cosmos_service.get_org(org_id)
    role = org_role(org, email)
    if not org or role not in ORG_ROLES:
        raise HTTPException(status_code=404, detail="Organisation not found.")
    return OrgActor(org, role, email, cosmos_service.get_user_by_email(email))


CASE_ROLES = ORG_ROLES + ("api",)   # who may file and read cases


# ─────────────────────────────────────────────────────────────
# ORGANISATION + MEMBERS
# ─────────────────────────────────────────────────────────────

class OrgProfile(BaseModel):
    legal_name: str = Field(..., min_length=2, max_length=200)
    address:    str = Field(default="", max_length=500)
    city:       str = Field(default="", max_length=100)
    state:      str = Field(default="", max_length=100)
    phone:      str = Field(default="", max_length=30)
    signatory:  str = Field(default="", max_length=120)


class OrgCreate(BaseModel):
    name:           str = Field(..., min_length=2, max_length=120)
    contact_email:  str
    profile:        OrgProfile
    accept_terms:   bool = False


def _public_org(org: dict) -> dict:
    return {
        "id": org["id"], "name": org["name"], "plan": org.get("plan", b2b.DEFAULT_PLAN),
        "contact_email": org.get("contact_email"), "profile": org.get("profile", {}),
        "members": org.get("members", []),
        "api_keys": [{k: v for k, v in key.items() if k != "hash"} for key in org.get("api_keys", [])],
        "templates": {**{k: {**v, "builtin": True} for k, v in b2b.BUILTIN_TEMPLATES.items()},
                      **(org.get("templates") or {})},
        "settings": org.get("settings", {}),
        "webhook": ({"url": org["webhook"]["url"], "updated_at": org["webhook"].get("updated_at")}
                    if org.get("webhook") else None),
        "created_at": org.get("created_at"),
    }


@router.post("")
async def create_org(body: OrgCreate, current_user: dict = Depends(get_current_user)):
    if not body.accept_terms:
        raise HTTPException(status_code=400, detail=(
            "Accept the terms: you confirm the organisation has a lawful basis to process the "
            "respondents' data (DPDP Act 2023) and that filings are accurate."))
    contact = body.contact_email.strip().lower()
    if "@" not in contact:
        raise HTTPException(status_code=400, detail="Enter a valid contact email.")
    org = cosmos_service.create_org({
        "name": body.name.strip(),
        "contact_email": contact,
        "profile": body.profile.model_dump(),
        "plan": b2b.DEFAULT_PLAN,
        "members": [{"email": current_user["email"].lower(), "role": "owner", "added_at": _now()}],
        "api_keys": [],
        "templates": {},
        "settings": {"auto_invite": False},
        "terms_accepted_at": _now(),
        "terms_accepted_by": current_user["email"].lower(),
    })
    logger.info(f"Organisation created | org_id={org['id']} | by={current_user['email']}")
    return _public_org(org)


@router.get("/mine")
async def my_orgs(current_user: dict = Depends(get_current_user)):
    orgs = cosmos_service.get_orgs_for_member(current_user["email"])
    return {"organisations": [
        {"id": o["id"], "name": o["name"], "plan": o.get("plan", b2b.DEFAULT_PLAN),
         "role": org_role(o, current_user["email"])} for o in orgs]}


@router.get("/{org_id}")
async def get_org(actor: OrgActor = Depends(org_actor)):
    return _public_org(actor.org)


class OrgUpdate(BaseModel):
    name:          str | None = Field(default=None, min_length=2, max_length=120)
    contact_email: str | None = None
    profile:       OrgProfile | None = None
    auto_invite:   bool | None = None


@router.patch("/{org_id}")
async def update_org(body: OrgUpdate, actor: OrgActor = Depends(org_actor)):
    actor.require(ADMIN_ROLES)

    def apply(org):
        if body.name:
            org["name"] = body.name.strip()
        if body.contact_email:
            org["contact_email"] = body.contact_email.strip().lower()
        if body.profile:
            org["profile"] = body.profile.model_dump()
        if body.auto_invite is not None:
            org.setdefault("settings", {})["auto_invite"] = body.auto_invite
    return _public_org(cosmos_service.mutate_org(actor.org["id"], apply))


class MemberIn(BaseModel):
    email: str
    role:  str = "agent"


@router.post("/{org_id}/members")
async def add_member(body: MemberIn, actor: OrgActor = Depends(org_actor)):
    actor.require(ADMIN_ROLES)
    email, role = body.email.strip().lower(), body.role
    if role not in ORG_ROLES or (role == "owner" and actor.role != "owner"):
        raise HTTPException(status_code=400, detail="Role must be admin or agent (only an owner can add owners).")

    def apply(org):
        members = [m for m in org.get("members", []) if m["email"] != email]
        members.append({"email": email, "role": role, "added_at": _now(), "added_by": actor.actor})
        org["members"] = members
    return _public_org(cosmos_service.mutate_org(actor.org["id"], apply))


@router.delete("/{org_id}/members/{email}")
async def remove_member(email: str, actor: OrgActor = Depends(org_actor)):
    actor.require(ADMIN_ROLES)
    email = email.lower()

    def apply(org):
        remaining = [m for m in org.get("members", []) if m["email"] != email]
        if not any(m["role"] == "owner" for m in remaining):
            raise HTTPException(status_code=400, detail="An organisation needs at least one owner.")
        org["members"] = remaining
    return _public_org(cosmos_service.mutate_org(actor.org["id"], apply))


# ─────────────────────────────────────────────────────────────
# API KEYS
# ─────────────────────────────────────────────────────────────

class KeyIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)


@router.post("/{org_id}/api-keys")
async def create_api_key(body: KeyIn, actor: OrgActor = Depends(org_actor)):
    actor.require(ADMIN_ROLES)
    key = API_KEY_PREFIX + secrets.token_urlsafe(32)
    record = {"id": secrets.token_hex(6), "name": body.name, "prefix": key[:12],
              "hash": _hash_key(key), "created_at": _now(), "created_by": actor.actor}
    cosmos_service.mutate_org(actor.org["id"], lambda o: o.setdefault("api_keys", []).append(record))
    return {"api_key": key, "id": record["id"], "prefix": record["prefix"],
            "note": "Store this key now — it is not shown again."}


@router.delete("/{org_id}/api-keys/{key_id}")
async def revoke_api_key(key_id: str, actor: OrgActor = Depends(org_actor)):
    actor.require(ADMIN_ROLES)

    def apply(org):
        for k in org.get("api_keys", []):
            if k["id"] == key_id:
                k["revoked"] = _now()
    cosmos_service.mutate_org(actor.org["id"], apply)
    return {"message": "API key revoked."}


# ─────────────────────────────────────────────────────────────
# TEMPLATES
# ─────────────────────────────────────────────────────────────

class TemplateIn(BaseModel):
    title: str = Field(..., min_length=2, max_length=80)
    text:  str = Field(..., min_length=40, max_length=3000)


@router.put("/{org_id}/templates/{key}")
async def upsert_template(key: str, body: TemplateIn, actor: OrgActor = Depends(org_actor)):
    actor.require(ADMIN_ROLES)
    if not key.replace("_", "").isalnum() or len(key) > 40:
        raise HTTPException(status_code=400, detail="Template key: letters, digits and _ only.")
    if key in b2b.BUILTIN_TEMPLATES:
        raise HTTPException(status_code=400, detail="That key is a built-in template; choose another.")
    cosmos_service.mutate_org(actor.org["id"], lambda o: o.setdefault("templates", {}).__setitem__(
        key, {"title": body.title, "text": body.text}))
    return {"message": "Template saved.", "key": key}


# ─────────────────────────────────────────────────────────────
# BULK FILING
# ─────────────────────────────────────────────────────────────

class BulkRequest(BaseModel):
    rows:        list[dict] = Field(..., min_length=1)
    template:    str | None = None
    auto_invite: bool | None = None


def _file_rows(actor: OrgActor, rows: list[dict], template: str | None,
               auto_invite: bool | None, background_tasks: BackgroundTasks) -> dict:
    actor.require(CASE_ROLES)
    org = actor.org
    if not org.get("contact_email"):
        raise HTTPException(status_code=400, detail="Set the organisation's contact email first.")
    if len(rows) > b2b.MAX_BULK_ROWS:
        raise HTTPException(status_code=413, detail=f"At most {b2b.MAX_BULK_ROWS} rows per upload.")

    existing = cosmos_service.get_cases_by_org(org["id"])
    quota = b2b.remaining_quota(org, existing)
    seen_refs = {c.get("external_reference") for c in existing}
    created, duplicates, errors = [], [], []

    from app.services.content_safety import content_safety_service
    from app.core.exceptions import ContentSafetyViolation
    for idx, row in enumerate(rows, start=1):
        try:
            data = b2b.build_case_data(org, row, template)
            if data["external_reference"] in seen_refs:
                duplicates.append({"row": idx, "reference": data["external_reference"]})
                continue
            if quota is not None and len(created) >= quota:
                errors.append({"row": idx, "error": "Monthly case limit for your plan reached."})
                continue
            content_safety_service.check_text(data["dispute_text"])
        except ContentSafetyViolation:
            errors.append({"row": idx, "error": "Dispute text failed the content safety check."})
            continue
        except ValueError as e:
            errors.append({"row": idx, "error": str(e)})
            continue
        case = cosmos_service.create_case({
            **data, "org_id": org["id"], "filed_by": actor.actor,
            "source": "api" if actor.role == "api" else "bulk",
        })
        cosmos_service.transition_case(case["id"], CaseStatus.ANALYZING)
        seen_refs.add(data["external_reference"])
        created.append({"row": idx, "case_id": case["id"], "reference": data["external_reference"]})

    invite = org.get("settings", {}).get("auto_invite", False) if auto_invite is None else auto_invite
    if created:
        background_tasks.add_task(in_thread(_process_batch), [c["case_id"] for c in created], invite)
    logger.info(f"Bulk filing | org_id={org['id']} | created={len(created)} | "
                f"duplicates={len(duplicates)} | errors={len(errors)}")
    return {
        "created": created, "duplicates": duplicates, "errors": errors,
        "summary": {"rows": len(rows), "created": len(created),
                    "duplicates": len(duplicates), "errors": len(errors)},
        "auto_invite": invite,
    }


async def _process_batch(case_ids: list[str], auto_invite: bool):
    """Analyse filed cases one at a time (bounded LLM load), then invite."""
    from app.routers.cases import analyse_new_case, deliver_invite
    for case_id in case_ids:
        try:
            await analyse_new_case(case_id)
            if auto_invite:
                case = cosmos_service.get_case(case_id)
                if case and case["status"] == CaseStatus.ANALYZED.value:
                    deliver_invite(case)
        except Exception:
            logger.exception(f"Bulk processing failed | case_id={case_id}")


@router.post("/{org_id}/cases/bulk")
async def bulk_file_json(body: BulkRequest, background_tasks: BackgroundTasks,
                         actor: OrgActor = Depends(org_actor)):
    return _file_rows(actor, body.rows, body.template, body.auto_invite, background_tasks)


@router.post("/{org_id}/cases/bulk-csv")
async def bulk_file_csv(background_tasks: BackgroundTasks,
                        file: UploadFile = File(...),
                        template: str | None = Form(default=None),
                        auto_invite: bool | None = Form(default=None),
                        actor: OrgActor = Depends(org_actor)):
    raw = await file.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="CSV too large (max 5 MB).")
    try:
        rows = b2b.parse_csv(raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not rows:
        raise HTTPException(status_code=400, detail="CSV has no data rows.")
    return _file_rows(actor, rows, template, auto_invite, background_tasks)


# ─────────────────────────────────────────────────────────────
# CASES + STATS
# ─────────────────────────────────────────────────────────────

def _case_row(c: dict) -> dict:
    return {
        "id": c["id"], "reference": c.get("external_reference"), "status": c.get("status"),
        "track": c.get("track"), "respondent_name": c.get("respondent_name"),
        "respondent_email": c.get("respondent_email"), "claim_amount": c.get("claim_amount"),
        "settled_amount": c.get("settled_amount"), "current_round": c.get("current_round", 0),
        "created_at": c.get("created_at"), "updated_at": c.get("updated_at"),
        "settled_at": c.get("settled_at"), "source": c.get("source"),
    }


@router.get("/{org_id}/cases")
async def list_org_cases(status: str | None = None, reference: str | None = None, q: str | None = None,
                         limit: int = 50, offset: int = 0, actor: OrgActor = Depends(org_actor)):
    actor.require(CASE_ROLES)
    cases = cosmos_service.get_cases_by_org(actor.org["id"])
    if status:
        wanted = {s.strip().upper() for s in status.split(",")}
        cases = [c for c in cases if c.get("status") in wanted]
    if reference:
        cases = [c for c in cases if c.get("external_reference") == reference]
    if q:
        needle = q.lower()
        cases = [c for c in cases if needle in (c.get("respondent_name") or "").lower()
                 or needle in (c.get("respondent_email") or "").lower()
                 or needle in (c.get("external_reference") or "").lower()]
    limit = max(1, min(limit, 200))
    return {"total": len(cases), "cases": [_case_row(c) for c in cases[offset:offset + limit]]}


def _days_between(a: str | None, b: str | None) -> float | None:
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 86400
    except (TypeError, ValueError):
        return None


@router.get("/{org_id}/stats")
async def org_stats(actor: OrgActor = Depends(org_actor)):
    actor.require(CASE_ROLES)
    cases = cosmos_service.get_cases_by_org(actor.org["id"])
    by_status: dict[str, int] = {}
    for c in cases:
        by_status[c.get("status", "UNKNOWN")] = by_status.get(c.get("status", "UNKNOWN"), 0) + 1
    settled = [c for c in cases if c.get("status") == CaseStatus.SETTLED.value]
    terminal = {s.value for s in TERMINAL_STATES}
    closed = [c for c in cases if c.get("status") in terminal]
    days = [d for d in (_days_between(c.get("created_at"), c.get("settled_at")) for c in settled) if d is not None]
    claimed = sum(c.get("claim_amount") or 0 for c in cases)
    recovered = sum(c.get("settled_amount") or 0 for c in settled)
    paid = [c for c in settled if c.get("settlement_honored") is True]
    plan = b2b.PLANS.get(actor.org.get("plan", b2b.DEFAULT_PLAN), b2b.PLANS[b2b.DEFAULT_PLAN])
    return {
        "total_cases": len(cases),
        "open_cases": len(cases) - len(closed),
        "by_status": by_status,
        "amount_claimed": round(claimed, 2),
        "amount_settled": round(recovered, 2),
        "settlement_rate": round(len(settled) / len(closed), 3) if closed else None,
        "recovery_rate_of_claimed": round(recovered / claimed, 3) if claimed else None,
        "payments_confirmed": len(paid),
        "avg_days_to_settle": round(sum(days) / len(days), 1) if days else None,
        "usage": {"plan": actor.org.get("plan", b2b.DEFAULT_PLAN),
                  "this_month": b2b.cases_this_month(cases),
                  "monthly_limit": plan["monthly_cases"]},
    }


def _org_case(actor: OrgActor, case_id: str) -> dict:
    case = cosmos_service.get_case(case_id)
    if not case or case.get("org_id") != actor.org["id"]:
        raise HTTPException(status_code=404, detail="Case not found in this organisation.")
    return case


@router.get("/{org_id}/cases/{case_id}")
async def get_org_case(case_id: str, actor: OrgActor = Depends(org_actor)):
    actor.require(CASE_ROLES)
    from app.services.blob_service import refresh_case_urls
    return refresh_case_urls(_org_case(actor, case_id))


@router.post("/{org_id}/cases/{case_id}/invite")
async def invite_org_case(case_id: str, actor: OrgActor = Depends(org_actor)):
    actor.require(CASE_ROLES)
    from app.routers.cases import deliver_invite
    case = _org_case(actor, case_id)
    if case["status"] != CaseStatus.ANALYZED.value:
        raise HTTPException(status_code=409, detail=f"Case must be ANALYZED to invite. Current: {case['status']}")
    if not deliver_invite(case):
        raise HTTPException(status_code=503, detail="Failed to send invite email. Please try again.")
    return {"message": f"Invite sent to {case['respondent_email']}", "status": "INVITE_SENT"}


# ─────────────────────────────────────────────────────────────
# WEBHOOKS
# ─────────────────────────────────────────────────────────────

class WebhookIn(BaseModel):
    url: str = Field(..., max_length=500)


@router.put("/{org_id}/webhook")
async def set_webhook(body: WebhookIn, actor: OrgActor = Depends(org_actor)):
    actor.require(ADMIN_ROLES)
    from app.services.webhooks import validate_webhook_url
    try:
        url = validate_webhook_url(body.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    secret = "whsec_" + secrets.token_urlsafe(24)
    cosmos_service.mutate_org(actor.org["id"], lambda o: o.__setitem__(
        "webhook", {"url": url, "secret": secret, "updated_at": _now()}))
    return {"url": url, "secret": secret,
            "note": "Verify each delivery: X-LegalAI-Signature = sha256=HMAC_SHA256(secret, raw body). "
                    "The secret is shown only now."}


@router.delete("/{org_id}/webhook")
async def delete_webhook(actor: OrgActor = Depends(org_actor)):
    actor.require(ADMIN_ROLES)
    cosmos_service.mutate_org(actor.org["id"], lambda o: o.pop("webhook", None))
    return {"message": "Webhook removed."}


@router.post("/{org_id}/webhook/test")
async def test_webhook(actor: OrgActor = Depends(org_actor)):
    actor.require(ADMIN_ROLES)
    from starlette.concurrency import run_in_threadpool
    from app.services import webhooks
    if not (actor.org.get("webhook") or {}).get("url"):
        raise HTTPException(status_code=400, detail="No webhook configured.")
    await run_in_threadpool(webhooks.emit, actor.org, "ping", {"message": "LegalAI webhook test"}, True)
    log = (cosmos_service.get_org(actor.org["id"]) or {}).get("webhook_log", [])
    return {"delivery": log[0] if log else None}


@router.get("/{org_id}/webhook/deliveries")
async def webhook_deliveries(actor: OrgActor = Depends(org_actor)):
    actor.require(ADMIN_ROLES)
    return {"deliveries": actor.org.get("webhook_log", [])}
