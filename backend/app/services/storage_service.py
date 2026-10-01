import logging
import os
import uuid
from pathlib import Path

from app.database.supabase import supabase

logger = logging.getLogger(__name__)

VIDEOS_BUCKET = "videos"
# The worker fetches the original clip from videos.blob_url with plain HTTP and no Supabase key,
# so with a private bucket that URL has to be a signed read link. A queued clip is normally
# processed within minutes; requeue / retry-failed re-sign before handing a row back to the worker.
DOWNLOAD_URL_SECONDS = 7 * 24 * 3600


class StorageService:

    @staticmethod
    def create_signed_upload_url(filename: str) -> dict:
        """
        Unique storage path (uuid + the original extension) and a one-shot signed PUT URL for the
        browser. The original filename is never used as the object name, so re-uploading the same
        file twice cannot collide.
        """
        extension = Path(filename).suffix.lower() or ".mp4"
        unique_filename = f"{uuid.uuid4()}{extension}"
        try:
            response = supabase.storage.from_(VIDEOS_BUCKET).create_signed_upload_url(unique_filename)
        except Exception as e:
            logger.exception("Failed to create signed upload URL")
            raise RuntimeError(f"Failed to create signed upload URL: {e}")
        return {
            "storage_path": unique_filename,
            "upload_url": response["signed_url"],
            "token": response.get("token"),
        }

    @staticmethod
    def object_size(path: str, bucket: str = VIDEOS_BUCKET) -> int:
        """Size in bytes of an object in the bucket; 0 if it does not exist."""
        try:
            info = supabase.storage.from_(bucket).info(path)
            return int((info or {}).get("size") or 0)
        except Exception as e:
            logger.warning(f"Object info failed for {bucket}/{path}: {e}")
            return 0

    @staticmethod
    def signed_download_url(path: str, bucket: str = VIDEOS_BUCKET, seconds: int = DOWNLOAD_URL_SECONDS) -> str:
        """Read-only signed URL the worker can GET without any Supabase key."""
        return supabase.storage.from_(bucket).create_signed_url(path, seconds)["signedURL"]

    @staticmethod
    def compute_sha256(path: str, bucket: str = VIDEOS_BUCKET) -> str:
        """Download object and compute its SHA-256 hex digest; empty string on failure."""
        import hashlib
        try:
            data = supabase.storage.from_(bucket).download(path)
            return hashlib.sha256(data).hexdigest()
        except Exception as e:
            logger.warning(f"Failed to download object for sha256 ({bucket}/{path}): {e}")
            return ""


# ── Azure Blob (evidence frames + detection video live here; DB stores blob paths, never URLs) ──

class AzureNotConfigured(RuntimeError):
    pass


def sign_azure_blob(blob_path: str, minutes: int = 15, container: str | None = None) -> dict:
    """Read-only SAS URL for one blob. Raises AzureNotConfigured when the env / library is missing."""
    from datetime import datetime, timedelta, timezone

    conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if not conn:
        raise AzureNotConfigured("AZURE_STORAGE_CONNECTION_STRING is not set on the backend")
    try:
        from azure.storage.blob import BlobSasPermissions, BlobServiceClient, generate_blob_sas
    except ImportError as e:  # pragma: no cover
        raise AzureNotConfigured("azure-storage-blob is not installed") from e

    container = container or os.getenv("AZURE_EVIDENCE_CONTAINER", "evidence")
    svc = BlobServiceClient.from_connection_string(conn)
    account_key = getattr(getattr(svc, "credential", None), "account_key", None)
    if not account_key:
        raise AzureNotConfigured("AZURE_STORAGE_CONNECTION_STRING does not contain an account key (SAS or AAD strings cannot generate new SAS URLs)")
    expires = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    sas = generate_blob_sas(
        account_name=svc.account_name,
        container_name=container,
        blob_name=blob_path,
        account_key=account_key,
        permission=BlobSasPermissions(read=True),
        expiry=expires,
    )
    return {"url": f"{svc.url.rstrip('/')}/{container}/{blob_path}?{sas}", "expires_at": expires.isoformat()}
