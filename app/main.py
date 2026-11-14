import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.config import settings
from app.core.exceptions import LegalAIException, legalai_exception_handler
from app.core.rate_limiter import RateLimiterMiddleware
from app.core.security_headers import SecurityHeadersMiddleware
from app.core.input_validation import InputValidationMiddleware
from app.routers import auth, cases, negotiation, documents, respondent, files
from app.core.security import validate_security_settings
from app.core.monitoring import MonitoringMiddleware, setup_app_insights

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

validate_security_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info(f"LegalAI Resolver v2.1 starting | env={settings.ENVIRONMENT}")
    logger.info(f"Backends | {settings.backend_summary()}")
    if not settings.is_production and not settings.use_sendgrid:
        logger.warning(
            "DEV MODE: no email provider — login codes are returned in the API "
            "response. Never expose this configuration publicly."
        )
    setup_app_insights()

    from app.core.expiry_worker import start_expiry_worker
    worker = asyncio.create_task(start_expiry_worker())
    logger.info("All systems started")
    yield
    worker.cancel()


app = FastAPI(
    title="LegalAI Resolver",
    version="2.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.ENVIRONMENT == "development" else None,
    redoc_url=None,
)

# ── Middleware (order matters — outermost runs first) ─────────
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(MonitoringMiddleware)
app.add_middleware(InputValidationMiddleware)
if settings.RATE_LIMIT_ENABLED:
    app.add_middleware(
        RateLimiterMiddleware,
        rules={
            "/api/auth/request-otp": (5,  300),
            "/api/auth/verify-otp":  (10, 300),
            "/api/cases/submit":     (3,  3600),
            "/api/":                 (200, 60),
        },
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=(
        ["*"] if settings.ENVIRONMENT == "development"
        else [settings.BASE_URL]
    ),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Exception handlers ────────────────────────────────────────
app.add_exception_handler(LegalAIException, legalai_exception_handler)

# ── Routers ──────────────────────────────────────────────────
app.include_router(auth.router,        prefix="/api/auth",        tags=["auth"])
app.include_router(cases.router,       prefix="/api/cases",       tags=["cases"])
app.include_router(negotiation.router, prefix="/api/negotiation", tags=["negotiation"])
app.include_router(documents.router,   prefix="/api/documents",   tags=["documents"])
app.include_router(respondent.router,  prefix="/api/respondent",  tags=["respondent"])
app.include_router(files.router,       prefix="/api/files",       tags=["files"])

# ── Static / Frontend ─────────────────────────────────────────
app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/", include_in_schema=False)
async def serve_frontend():
    return FileResponse("frontend/index.html")


@app.get("/respond/{case_id}", include_in_schema=False)
async def serve_respondent_portal(case_id: str):
    return FileResponse("frontend/respond.html")


# ── Dev outbox (local email mode only) ─────────────────────────
@app.get("/api/dev/outbox", tags=["dev"])
async def dev_outbox(email: str | None = None, limit: int = 20):
    """
    Emails captured by the local outbox (no SendGrid configured), newest first.
    Lets you follow respondent invite links etc. without real email.
    Disabled in production or when a real email provider is configured.
    """
    from app.services.email_service import email_service
    if settings.is_production or email_service.is_configured():
        raise HTTPException(status_code=404, detail="Not found")
    items = email_service.list_outbox(email, limit=min(limit, 100))
    return {"emails": [{k: v for k, v in e.items() if k != "html"} for e in items]}


# ── Health Check ─────────────────────────────────────────────
@app.get("/health")
async def health_check(deep: bool = False):
    """
    Liveness + backend report. `?deep=true` also makes a live LLM call
    (costs tokens, so it is opt-in).
    """
    from app.services.cosmos_service import cosmos_service
    from app.services.blob_service import blob_service
    from app.services.openai_service import openai_service
    from app.services.content_safety import content_safety_service
    from app.services.email_service import email_service

    results = {}

    try:
        results["database"] = f"ok ({cosmos_service.ping()})"
    except Exception as e:
        results["database"] = f"error: {str(e)[:60]}"

    try:
        results["file_storage"] = f"ok ({blob_service.ping()})"
    except Exception as e:
        results["file_storage"] = f"error: {str(e)[:60]}"

    if not openai_service.is_configured:
        results["llm"] = "ok (offline: rule-based fallbacks)"
    elif deep:
        try:
            openai_service.call(
                system_prompt="Reply with the word OK",
                user_message="ping",
                use_large_model=False,
                json_mode=False,
                max_tokens=5,
            )
            results["llm"] = f"ok ({openai_service.provider})"
        except Exception as e:
            results["llm"] = f"error: {str(e)[:60]}"
    else:
        results["llm"] = f"ok ({openai_service.provider}, not pinged)"

    results["content_safety"] = f"ok ({content_safety_service.mode})"

    if email_service.is_configured():
        results["email"] = "ok (sendgrid)"
    elif settings.is_production:
        results["email"] = "error: sendgrid not configured"
    else:
        results["email"] = "ok (local_outbox)"

    all_ok = all(v.startswith("ok") for v in results.values())

    return {
        "status":      "healthy" if all_ok else "degraded",
        "environment": settings.ENVIRONMENT,
        "base_url":    settings.BASE_URL,
        "backends":    settings.backend_summary(),
        "services":    results,
    }
