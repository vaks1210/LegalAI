"""
Serves files stored by the local blob backend. URLs are HMAC-signed and
time-limited (the local equivalent of Azure SAS URLs), so no login is needed
to follow a download link — exactly like the Azure deployment.
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.services.blob_service import blob_service, verify_local_signature

router = APIRouter()


@router.get("/{container_key}/{blob_name:path}", include_in_schema=False)
async def download_local_file(container_key: str, blob_name: str, expires: int, sig: str):
    if not blob_service.local:
        raise HTTPException(status_code=404, detail="Not found")
    if not verify_local_signature(container_key, blob_name, expires, sig):
        raise HTTPException(status_code=403, detail="Download link is invalid or has expired.")
    backend = blob_service.backend
    try:
        if not backend.exists(container_key, blob_name):
            raise HTTPException(status_code=404, detail="File not found")
        path = backend.path_for(container_key, blob_name)
    except (KeyError, ValueError):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        path,
        media_type=backend.content_type(container_key, blob_name),
        filename=path.name,
    )
