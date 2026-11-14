"""
Shared validation for evidence uploads (claimant multipart and respondent
base64). Content type is derived from the extension — never trusted from the
client — so an upload cannot be served back as HTML/JS.
"""
import os
import re

from fastapi import HTTPException

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

CONTENT_TYPES = {
    ".pdf":  "application/pdf",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".doc":  "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt":  "text/plain; charset=utf-8",
}


def safe_filename(name: str | None, default: str = "evidence") -> str:
    base = os.path.basename((name or "").replace("\\", "/"))
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return (base or default)[:120]


def validate_upload(filename: str | None, content: bytes) -> tuple[str, str]:
    """Returns (safe_filename, content_type) or raises HTTP 400/413."""
    name = safe_filename(filename)
    ext = os.path.splitext(name)[1].lower()
    if ext not in CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported evidence file type. Allowed: {', '.join(sorted(CONTENT_TYPES))}",
        )
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large. Maximum size is 10 MB.")
    return name, CONTENT_TYPES[ext]
