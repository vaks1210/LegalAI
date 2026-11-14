import hashlib
import hmac
import logging
import os
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from azure.storage.blob import (
    BlobServiceClient,
    generate_blob_sas,
    BlobSasPermissions,
    ContentSettings,
)
from datetime import datetime, timedelta, timezone
from app.config import settings

logger = logging.getLogger(__name__)

CONTAINERS = {
    "evidence": "evidence-uploads",
    "pdfs":     "generated-pdfs",
    "signed":   "signed-agreements",
}


def _sign(container_key: str, blob_name: str, expires: int) -> str:
    msg = f"{container_key}|{blob_name}|{expires}".encode()
    return hmac.new(settings.JWT_SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()


def verify_local_signature(container_key: str, blob_name: str, expires: int, sig: str) -> bool:
    if expires < time.time():
        return False
    return hmac.compare_digest(_sign(container_key, blob_name, expires), sig)


class LocalBlobBackend:
    """Stores files on disk; download URLs are HMAC-signed and time-limited."""

    def __init__(self, root: str):
        self.root = Path(root).resolve()
        for key in CONTAINERS:
            (self.root / key).mkdir(parents=True, exist_ok=True)

    def path_for(self, container_key: str, blob_name: str) -> Path:
        if container_key not in CONTAINERS:
            raise KeyError(container_key)
        base = (self.root / container_key).resolve()
        path = (base / blob_name).resolve()
        if base not in path.parents:
            raise ValueError("Invalid blob name")
        return path

    def upload(self, container_key, blob_name, data, content_type=None) -> str:
        path = self.path_for(container_key, blob_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if content_type:
            path.with_name(path.name + ".ctype").write_text(content_type)
        return self.url(container_key, blob_name, expiry_hours=72)

    def download(self, container_key, blob_name) -> bytes:
        return self.path_for(container_key, blob_name).read_bytes()

    def content_type(self, container_key, blob_name) -> str:
        meta = self.path_for(container_key, blob_name)
        meta = meta.with_name(meta.name + ".ctype")
        return meta.read_text() if meta.exists() else "application/pdf"

    def exists(self, container_key, blob_name) -> bool:
        try:
            return self.path_for(container_key, blob_name).is_file()
        except (KeyError, ValueError):
            return False

    def delete(self, container_key, blob_name) -> None:
        path = self.path_for(container_key, blob_name)
        path.unlink(missing_ok=True)
        path.with_name(path.name + ".ctype").unlink(missing_ok=True)

    def url(self, container_key, blob_name, expiry_hours=72) -> str:
        expires = int(time.time() + expiry_hours * 3600)
        sig     = _sign(container_key, blob_name, expires)
        base    = settings.BASE_URL.rstrip("/")
        return (
            f"{base}/api/files/{container_key}/{quote(blob_name)}"
            f"?expires={expires}&sig={sig}"
        )


class BlobService:
    def __init__(self, local_root: str | None = None):
        self.local = local_root is not None or not settings.use_azure_blob
        if self.local:
            self.backend = LocalBlobBackend(
                local_root or os.path.join(settings.LOCAL_DATA_DIR, "files")
            )
            self.client = None
        else:
            self.backend = None
            self.client = BlobServiceClient.from_connection_string(
                settings.AZURE_STORAGE_CONNECTION_STRING
            )

    def ping(self) -> str:
        if self.local:
            return "local_disk"
        next(iter(self.client.list_containers(results_per_page=1)), None)
        return "azure_blob"

    def refresh_url(self, url: str | None, expiry_hours: int = 72) -> str | None:
        """
        Download URLs stored on a case expire (SAS / signed links). Given a
        previously issued URL, return a freshly signed one for the same file.
        Unknown URL shapes are returned unchanged.
        """
        if not url:
            return url
        try:
            path = unquote(urlparse(url).path)
            if self.local:
                prefix = "/api/files/"
                if not path.startswith(prefix):
                    return url
                container_key, blob_name = path[len(prefix):].split("/", 1)
            else:
                container, blob_name = path.lstrip("/").split("/", 1)
                reverse = {v: k for k, v in CONTAINERS.items()}
                container_key = reverse.get(container)
                if not container_key:
                    return url
            return self.generate_download_url(container_key, blob_name, expiry_hours)
        except Exception:
            return url

    def upload(
        self,
        container_key: str,
        blob_name: str,
        data: bytes,
        content_type: str = "application/pdf",
    ) -> str:
        """Upload bytes. Returns permanent blob URL."""
        if self.local:
            return self.backend.upload(container_key, blob_name, data, content_type)
        container   = CONTAINERS[container_key]
        blob_client = self.client.get_blob_client(
            container=container, blob=blob_name
        )
        blob_client.upload_blob(
            data,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )
        logger.info(f"Blob uploaded | container={container} | blob={blob_name}")
        return blob_client.url

    def download(self, container_key: str, blob_name: str) -> bytes:
        if self.local:
            return self.backend.download(container_key, blob_name)
        container   = CONTAINERS[container_key]
        blob_client = self.client.get_blob_client(
            container=container, blob=blob_name
        )
        return blob_client.download_blob().readall()

    def generate_download_url(
        self,
        container_key: str,
        blob_name: str,
        expiry_hours: int = 72,
    ) -> str:
        """Generate a time-limited SAS URL for secure PDF download."""
        if self.local:
            return self.backend.url(container_key, blob_name, expiry_hours)
        container    = CONTAINERS[container_key]
        account_name = settings.AZURE_STORAGE_ACCOUNT_NAME

        # Extract account key from connection string
        conn_parts  = dict(
            part.split("=", 1)
            for part in settings.AZURE_STORAGE_CONNECTION_STRING.split(";")
            if "=" in part
        )
        account_key = conn_parts.get("AccountKey", "")

        sas_token = generate_blob_sas(
            account_name=account_name,
            container_name=container,
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(hours=expiry_hours),
        )
        return (
            f"https://{account_name}.blob.core.windows.net"
            f"/{container}/{blob_name}?{sas_token}"
        )

    def blob_exists(self, container_key: str, blob_name: str) -> bool:
        if self.local:
            return self.backend.exists(container_key, blob_name)
        container   = CONTAINERS[container_key]
        blob_client = self.client.get_blob_client(
            container=container, blob=blob_name
        )
        try:
            blob_client.get_blob_properties()
            return True
        except Exception:
            return False

    def delete(self, container_key: str, blob_name: str):
        if self.local:
            return self.backend.delete(container_key, blob_name)
        container   = CONTAINERS[container_key]
        blob_client = self.client.get_blob_client(
            container=container, blob=blob_name
        )
        blob_client.delete_blob()


blob_service = BlobService()

_TOP_LEVEL_URL_FIELDS = ("settlement_url", "breach_notice_url", "court_file_url")


def refresh_case_urls(case: dict | None) -> dict | None:
    """Return a copy of `case` whose stored document links are freshly signed."""
    if not case:
        return case
    case = dict(case)
    for field in _TOP_LEVEL_URL_FIELDS:
        if case.get(field):
            case[field] = blob_service.refresh_url(case[field])
    if isinstance(case.get("documents_data"), dict):
        docs = dict(case["documents_data"])
        for k, v in docs.items():
            if k.endswith("_url") and isinstance(v, str):
                docs[k] = blob_service.refresh_url(v)
        case["documents_data"] = docs
    if isinstance(case.get("evidence_files"), list):
        case["evidence_files"] = [
            {**f, "url": blob_service.refresh_url(f.get("url"))} if isinstance(f, dict) else f
            for f in case["evidence_files"]
        ]
    return case
