"""
azure_storage.py — Helper for uploading/downloading files from Azure Blob Storage.

Usage:
    from azure_storage import upload_evidence, upload_video, get_blob_url

Requirements:
    pip install azure-storage-blob python-dotenv

Add to your .env:
    AZURE_STORAGE_CONNECTION_STRING=<your connection string>
    AZURE_EVIDENCE_CONTAINER=evidence
    AZURE_VIDEOS_CONTAINER=videos
"""

import os
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / '.env')
load_dotenv()

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
CONN_STR      = os.getenv('AZURE_STORAGE_CONNECTION_STRING', '')
EVIDENCE_CONT = os.getenv('AZURE_EVIDENCE_CONTAINER', 'evidence')
VIDEOS_CONT   = os.getenv('AZURE_VIDEOS_CONTAINER', 'videos')

# ── Client ────────────────────────────────────────────────────────────────────
_blob_service = None

def _get_client():
    """Lazy-load the Azure BlobServiceClient."""
    global _blob_service
    if _blob_service:
        return _blob_service

    if not CONN_STR or 'REPLACE_WITH' in CONN_STR:
        logger.warning('[azure] AZURE_STORAGE_CONNECTION_STRING not set — uploads disabled')
        return None

    try:
        from azure.storage.blob import BlobServiceClient
        _blob_service = BlobServiceClient.from_connection_string(CONN_STR)
        logger.info('[azure] BlobServiceClient connected: %s', _blob_service.account_name)
        return _blob_service
    except ImportError:
        logger.error('[azure] azure-storage-blob not installed. Run: pip install azure-storage-blob')
        return None
    except Exception as e:
        logger.error('[azure] Connection failed: %s', e)
        return None


def _ensure_container(container_name: str):
    """Create container if it doesn't exist."""
    client = _get_client()
    if not client:
        return False
    try:
        container = client.get_container_client(container_name)
        container.create_container()
        return True
    except Exception:
        return True  # Already exists


def upload_evidence(local_path: str | Path, blob_name: str = None) -> str | None:
    """
    Upload an evidence image to Azure Blob Storage.

    Args:
        local_path: Local file path (e.g. 'evidence/frame_t15.5.jpg')
        blob_name:  Optional blob name (defaults to filename)

    Returns:
        Public URL of the uploaded blob, or None on failure.
    """
    client = _get_client()
    if not client:
        return None

    local_path = Path(local_path)
    if not local_path.exists():
        logger.error('[azure] File not found: %s', local_path)
        return None

    blob_name = blob_name or local_path.name
    _ensure_container(EVIDENCE_CONT)

    try:
        blob_client = client.get_blob_client(container=EVIDENCE_CONT, blob=blob_name)
        with open(local_path, 'rb') as f:
            blob_client.upload_blob(f, overwrite=True)
        url = blob_client.url
        logger.info('[azure] Evidence uploaded: %s', url)
        return url
    except Exception as e:
        logger.error('[azure] upload_evidence failed: %s', e)
        return None


def upload_video(local_path: str | Path, blob_name: str = None) -> str | None:
    """
    Upload a dashcam video to Azure Blob Storage.

    Returns:
        Public URL of the uploaded blob, or None on failure.
    """
    client = _get_client()
    if not client:
        return None

    local_path = Path(local_path)
    if not local_path.exists():
        logger.error('[azure] File not found: %s', local_path)
        return None

    blob_name = blob_name or local_path.name
    _ensure_container(VIDEOS_CONT)

    try:
        blob_client = client.get_blob_client(container=VIDEOS_CONT, blob=blob_name)
        with open(local_path, 'rb') as f:
            blob_client.upload_blob(f, overwrite=True)
        url = blob_client.url
        logger.info('[azure] Video uploaded: %s', url)
        return url
    except Exception as e:
        logger.error('[azure] upload_video failed: %s', e)
        return None


def get_blob_url(blob_name: str, container: str = None) -> str | None:
    """Get the public URL for a blob without downloading it."""
    client = _get_client()
    if not client:
        return None
    container = container or EVIDENCE_CONT
    try:
        blob_client = client.get_blob_client(container=container, blob=blob_name)
        return blob_client.url
    except Exception as e:
        logger.error('[azure] get_blob_url failed: %s', e)
        return None


def upload_all_evidence(evidence_dir: str | Path) -> list[str]:
    """
    Upload all image files in a directory to Azure evidence container.

    Returns:
        List of uploaded public URLs.
    """
    evidence_dir = Path(evidence_dir)
    urls = []
    for pattern in ('*.jpg', '*.jpeg', '*.png'):
        for f in evidence_dir.glob(pattern):
            url = upload_evidence(f)
            if url:
                urls.append(url)
    return urls
