import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.database.supabase import supabase

logger = logging.getLogger(__name__)


class VideoService:

    @staticmethod
    def create_video_record(
        filename: str,
        original_name: str,
        blob_url: str,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Insert a new video row into the 'videos' table with status 'unprocessed'.

        Args:
            filename:      Filename as stored in Supabase (e.g. 'abc123.mp4')
            original_name: The original filename from the client upload
            blob_url:      Public Supabase Storage URL for the uploaded video
            user_id:       Supabase auth user UUID (from JWT)

        Returns:
            dict with id, filename, status, uploaded_at
        """
        try:
            data = {
                "filename": filename,
                "original_name": original_name,
                "blob_url": blob_url,
                "status": "unprocessed",
                "uploaded_by": user_id,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }

            response = supabase.table("videos").insert(data).execute()

            if not response.data:
                raise RuntimeError("Video insert returned no data")

            record = response.data[0]
            logger.info(f"Video record created: {record['id']}")
            return record

        except Exception as e:
            logger.exception(f"Failed to create video record: {str(e)}")
            raise RuntimeError(f"Failed to create video record: {str(e)}")

    @staticmethod
    def create_pending_video_record(
        filename: str,
        original_name: str,
        storage_path: str,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Pre-create a video DB row before the client uploads the file directly
        to storage. blob_url is initially None; call complete_video_upload once
        the client confirms the storage upload succeeded.

        Args:
            filename:      Filename inside the storage bucket (e.g. 'abc123.mp4')
            original_name: Original filename as provided by the client
            storage_path:  Path inside the bucket (stored in local_path for now)
            user_id:       Supabase auth user UUID

        Returns:
            Inserted video row dict
        """
        try:
            data = {
                "filename": filename,
                "original_name": original_name,
                "local_path": storage_path,
                "blob_url": None,
                "status": "unprocessed",
                "uploaded_by": user_id,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }
            response = supabase.table("videos").insert(data).execute()
            if not response.data:
                raise RuntimeError("Video insert returned no data")
            record = response.data[0]
            logger.info(f"Pending video record created: {record['id']}")
            return record
        except Exception as e:
            logger.exception(f"Failed to create pending video record: {str(e)}")
            raise RuntimeError(f"Failed to create pending video record: {str(e)}")

    @staticmethod
    def complete_video_upload(
        video_id: str,
        blob_url: str,
        storage_path: str,
    ) -> Dict[str, Any]:
        """
        Finalize a video record after the client has confirmed the direct
        storage upload. Sets blob_url (and updates local_path to storage_path).

        Args:
            video_id:     UUID of the video row to update
            blob_url:     Public/signed URL of the uploaded video in storage
            storage_path: Path inside the storage bucket

        Returns:
            Updated video row dict
        """
        try:
            response = (
                supabase
                .table("videos")
                .update({"blob_url": blob_url, "local_path": storage_path})
                .eq("id", video_id)
                .execute()
            )
            if not response.data:
                raise RuntimeError(f"No video found with id: {video_id}")
            record = response.data[0]
            logger.info(f"Video {video_id} upload completed — blob_url set")
            return record
        except Exception as e:
            logger.exception(f"Failed to complete video upload: {str(e)}")
            raise RuntimeError(f"Failed to complete video upload: {str(e)}")

    @staticmethod
    def get_unprocessed_videos() -> List[Dict[str, Any]]:
        """
        Fetch all videos where status = 'unprocessed', ordered oldest first.

        Returns:
            List of video row dicts
        """
        try:
            response = (
                supabase
                .table("videos")
                .select("*")
                .eq("status", "unprocessed")
                .order("uploaded_at", desc=False)
                .execute()
            )

            logger.info(
                f"Fetched {len(response.data)} unprocessed video(s)"
            )
            return response.data or []

        except Exception as e:
            logger.exception(f"Failed to fetch unprocessed videos: {str(e)}")
            raise RuntimeError(f"Failed to fetch unprocessed videos: {str(e)}")

    @staticmethod
    def update_video_status(
        video_id: str,
        status: str,
        error_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Update the status of a video record.

        Args:
            video_id:      UUID of the video row
            status:        New status — 'processing' | 'processed' | 'failed'
            error_message: Optional error detail (used when status = 'failed')

        Returns:
            Updated video row dict
        """
        allowed = {"processing", "processed", "failed"}
        if status not in allowed:
            raise ValueError(
                f"Invalid status '{status}'. Must be one of: {allowed}"
            )

        try:
            update_data: Dict[str, Any] = {"status": status}

            if status == "processed":
                update_data["processed_at"] = datetime.now(timezone.utc).isoformat()

            if error_message:
                update_data["error_message"] = error_message

            response = (
                supabase
                .table("videos")
                .update(update_data)
                .eq("id", video_id)
                .execute()
            )

            if not response.data:
                raise RuntimeError(f"No video found with id: {video_id}")

            record = response.data[0]
            logger.info(f"Video {video_id} status → {status}")
            return record

        except Exception as e:
            logger.exception(f"Failed to update video status: {str(e)}")
            raise RuntimeError(f"Failed to update video status: {str(e)}")

    @staticmethod
    def update_video_score(video_id: str, score: float) -> Dict[str, Any]:
        """
        Update the score of a video record.

        Args:
            video_id: UUID of the video row
            score:    Computed score value

        Returns:
            Updated video row dict
        """
        try:
            response = (
                supabase
                .table("videos")
                .update({"score": score})
                .eq("id", video_id)
                .execute()
            )

            if not response.data:
                raise RuntimeError(f"No video found with id: {video_id}")

            record = response.data[0]
            logger.info(f"Video {video_id} score → {score}")
            return record

        except Exception as e:
            logger.exception(f"Failed to update video score: {str(e)}")
            raise RuntimeError(f"Failed to update video score: {str(e)}")
