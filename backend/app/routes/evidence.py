"""GET /evidence/sign?path=  — short-lived read URL for one private blob (API.md §5)."""
import logging

from fastapi import APIRouter, Depends, Query, status

from app.database.supabase import supabase
from app.services import storage_service
from app.utils.auth import api_error, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/evidence", tags=["Evidence"])
SIGN_MINUTES = 15


def sign_or_503(path: str, minutes: int = SIGN_MINUTES) -> dict:
    try:
        return storage_service.sign_azure_blob(path, minutes)
    except storage_service.AzureNotConfigured as e:
        raise api_error(status.HTTP_503_SERVICE_UNAVAILABLE, f"Evidence storage is not configured: {e}")


def video_cases(video_id: str) -> list:
    return supabase.table("cases").select("*").eq("video_id", video_id).execute().data or []


def subject_finalized(cases: list) -> bool:
    return any(c.get("is_subject") and c.get("status") == "finalized" for c in cases)


def citizen_may_see(path: str, video: dict, cases: list) -> bool:
    """Citizens never receive evidence frames (docs §2.6 items 24-25): the run's redacted detection video
    is the only blob they may sign, and only once the subject case is finalized."""
    return path == video.get("detection_video_path") and subject_finalized(cases)


@router.get("/sign")
def sign_evidence(path: str = Query(min_length=3, max_length=512), current_user=Depends(get_current_user)):
    parts = path.split("/")
    if path.startswith("/") or ".." in parts or len(parts) < 3 or not all(parts):
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "path must be {video_id}/{run_id}/...")
    video_id = parts[0]
    rows = supabase.table("videos").select("*").eq("id", video_id).is_("deleted_at", "null").limit(1).execute().data or []
    if not rows:
        raise api_error(status.HTTP_404_NOT_FOUND, "Video not found")
    video = rows[0]
    if current_user["role"] == "citizen":
        if video.get("uploaded_by") != current_user["id"]:
            raise api_error(status.HTTP_403_FORBIDDEN, "Not your submission")
        if not citizen_may_see(path, video, video_cases(video_id)):
            raise api_error(status.HTTP_403_FORBIDDEN, "This evidence is not available to you yet")
    return {"success": True, "data": {"path": path, **sign_or_503(path)}}
