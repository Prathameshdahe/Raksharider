import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.database.supabase import supabase

logger = logging.getLogger(__name__)

MAX_VIDEO_BYTES = 200 * 1024 * 1024
DEFAULT_MAX_UPLOADS_PER_DAY = 10


class VideoService:

    @staticmethod
    def create_video_record(
        filename: str,
        original_name: str,
        user_id: str,
        vehicle_type: str,
        status: str,
        file_size: Optional[int] = None,
        blob_url: Optional[str] = None,
        storage_path: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Insert one videos row. storage_path is kept in local_path (legacy column name)."""
        data = {
            "filename": filename,
            "original_name": original_name,
            "local_path": storage_path,
            "blob_url": blob_url,
            "status": status,
            "uploaded_by": user_id,
            "vehicle_type": vehicle_type,
            "file_size": file_size,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            **extra,
        }
        response = supabase.table("videos").insert(data).execute()
        if not response.data:
            raise RuntimeError("Video insert returned no data")
        record = response.data[0]
        logger.info(f"Video record created: {record['id']} ({status})")
        return record

    @staticmethod
    def max_uploads_per_day() -> int:
        try:
            res = supabase.table("system_settings").select("value").eq("key", "max_uploads_per_day").limit(1).execute()
            if res.data:
                return int(res.data[0]["value"])
        except Exception as e:
            logger.warning(f"max_uploads_per_day lookup failed, using default: {e}")
        return DEFAULT_MAX_UPLOADS_PER_DAY

    @staticmethod
    def uploads_today(user_id: str) -> int:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        res = (
            supabase.table("videos")
            .select("id", count="exact")
            .eq("uploaded_by", user_id)
            .gte("uploaded_at", start)
            .is_("deleted_at", "null")
            .execute()
        )
        return res.count if res.count is not None else len(res.data or [])
