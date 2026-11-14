import os
from functools import lru_cache
from pydantic_settings import BaseSettings
from pydantic import Field
from dotenv import load_dotenv

# Load .env.local for local development only
load_dotenv(".env.local")


class Settings(BaseSettings):
    # ── App ──────────────────────────────────────────────
    ENVIRONMENT: str = Field(default="development")
    BASE_URL: str    = Field(default="http://localhost:8000")

    # Directory used by the local (no-cloud) backends: SQLite DB, PDFs,
    # evidence uploads and the email outbox.
    LOCAL_DATA_DIR: str = Field(default="./data")
    RATE_LIMIT_ENABLED: bool = Field(default=True)

    # ── OpenAI (non-Azure) — used when Azure OpenAI is not configured ──
    OPENAI_API_KEY:      str = Field(default="")
    OPENAI_BASE_URL:     str = Field(default="")
    OPENAI_MODEL_LARGE:  str = Field(default="gpt-4o")
    OPENAI_MODEL_SMALL:  str = Field(default="gpt-4o-mini")

    # ── Azure OpenAI ─────────────────────────────────────
    AZURE_OPENAI_KEY:               str = Field(default="")
    AZURE_OPENAI_ENDPOINT:          str = Field(default="")
    AZURE_OPENAI_DEPLOYMENT_LARGE:  str = Field(default="gpt-4o-legal")
    AZURE_OPENAI_DEPLOYMENT_SMALL:  str = Field(default="gpt-4o-mini-fast")

    # ── Cosmos DB ────────────────────────────────────────
    COSMOS_CONNECTION_STRING: str = Field(default="")
    COSMOS_DATABASE_NAME:     str = Field(default="legalai-db")

    # ── Blob Storage ─────────────────────────────────────
    AZURE_STORAGE_CONNECTION_STRING: str = Field(default="")
    AZURE_STORAGE_ACCOUNT_NAME:      str = Field(default="legalaiprodstore")

    # ── Communication Services ───────────────────────────
    SENDGRID_API_KEY:    str = Field(default="")
    SENDGRID_FROM_EMAIL: str = Field(default="")

    # ── Content Safety ───────────────────────────────────
    CONTENT_SAFETY_KEY:      str = Field(default="")
    CONTENT_SAFETY_ENDPOINT: str = Field(default="")

    # ── Document Intelligence ────────────────────────────
    DOC_INTELLIGENCE_KEY:      str = Field(default="")
    DOC_INTELLIGENCE_ENDPOINT: str = Field(default="")

    # ── JWT Auth ─────────────────────────────────────────
    JWT_SECRET_KEY:      str = Field(default="changeme")
    JWT_ALGORITHM:       str = Field(default="HS256")
    JWT_EXPIRE_HOURS:    int = Field(default=72)
    OTP_EXPIRE_MINUTES:  int = Field(default=10)

    # ── Monitoring ───────────────────────────────────────
    APPINSIGHTS_INSTRUMENTATION_KEY: str = Field(default="")

    class Config:
        env_file = ".env.local"
        extra    = "ignore"

    # ── Backend detection ────────────────────────────────
    # Each integration falls back to a local implementation when its
    # credentials are missing, so the app runs end-to-end with zero cloud
    # setup (`uvicorn app.main:app`).

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() == "production"

    @property
    def use_cosmos(self) -> bool:
        return bool(self.COSMOS_CONNECTION_STRING.strip())

    @property
    def use_azure_blob(self) -> bool:
        return bool(self.AZURE_STORAGE_CONNECTION_STRING.strip())

    @property
    def llm_provider(self) -> str:
        if self.AZURE_OPENAI_KEY.strip() and self.AZURE_OPENAI_ENDPOINT.strip():
            return "azure_openai"
        if self.OPENAI_API_KEY.strip():
            return "openai"
        return "offline"

    @property
    def use_azure_content_safety(self) -> bool:
        return bool(self.CONTENT_SAFETY_KEY.strip() and self.CONTENT_SAFETY_ENDPOINT.strip())

    @property
    def use_sendgrid(self) -> bool:
        return bool(self.SENDGRID_API_KEY.strip() and self.SENDGRID_FROM_EMAIL.strip())

    def backend_summary(self) -> dict:
        return {
            "database":       "cosmos" if self.use_cosmos else "sqlite",
            "file_storage":   "azure_blob" if self.use_azure_blob else "local_disk",
            "llm":            self.llm_provider,
            "content_safety": "azure" if self.use_azure_content_safety else "local_rules",
            "email":          "sendgrid" if self.use_sendgrid else "local_outbox",
        }


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
