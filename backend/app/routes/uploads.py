import logging

from fastapi import (
    APIRouter,
    UploadFile,
    File,
    HTTPException,
    status,
    Depends,
)
from pydantic import BaseModel

from app.services.storage_service import StorageService
from app.services.video_service import VideoService
from app.utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/videos",
    tags=["Videos"],
)

ALLOWED_VIDEO_TYPES = {
    "video/mp4",
    "video/mpeg",
    "video/quicktime",
    "video/x-msvideo",
    "video/webm",
}


# ==============================================================
# Request models
# ==============================================================
class VideoInitRequest(BaseModel):
    filename: str
    content_type: str


class VideoCompleteRequest(BaseModel):
    video_id: str
    storage_path: str
    blob_url: str


# ==============================================================
# POST /videos/upload/init
# Generate a signed upload URL + pre-create the DB record
# ==============================================================
@router.post("/upload/init", status_code=status.HTTP_201_CREATED)
async def init_video_upload(
    body: VideoInitRequest,
    current_user=Depends(get_current_user),
):
    """
    Step 1 of large-file upload flow.

    - Generates a Supabase signed upload URL so the client can PUT the
      video file directly to storage (bypassing Vercel's payload limit).
    - Pre-creates a videos row in DB with status = 'unprocessed' and
      blob_url = None (finalized by /upload/complete).

    Response:
        {
            "success": true,
            "data": {
                "video_id": "...",
                "storage_path": "...",
                "upload_url": "..."   ← client PUTs the file here directly
            }
        }
    """
    user_id = (
        current_user.get("id")
        if isinstance(current_user, dict)
        else getattr(current_user, "id", None)
    )
    try:
        signed = StorageService.create_signed_upload_url(body.filename)
        record = VideoService.create_pending_video_record(
            filename=body.filename,
            original_name=body.filename,
            storage_path=signed["storage_path"],
            user_id=user_id,
        )
        return {
            "success": True,
            "data": {
                "video_id": record["id"],
                "storage_path": signed["storage_path"],
                "upload_url": signed["upload_url"],
            },
        }
    except Exception as e:
        logger.exception(f"Failed to init video upload: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "success": False,
                "error": "Failed to init video upload",
                "detail": str(e),
            },
        )


# ==============================================================
# POST /videos/upload/complete
# Finalize the DB record after client confirms storage upload
# ==============================================================
@router.post("/upload/complete", status_code=status.HTTP_200_OK)
async def complete_video_upload(
    body: VideoCompleteRequest,
    current_user=Depends(get_current_user),
):
    """
    Step 3 of large-file upload flow (step 2 is the direct PUT to storage).

    - Updates the pre-created videos row with the confirmed blob_url.
    - Status remains 'unprocessed' — the processing queue picks it up via
      GET /videos/unprocessed.

    Request body:
        {
            "video_id": "...",
            "storage_path": "...",
            "blob_url": "..."
        }
    """
    try:
        record = VideoService.complete_video_upload(
            video_id=body.video_id,
            blob_url=body.blob_url,
            storage_path=body.storage_path,
        )
        return {
            "success": True,
            "message": "Video upload completed",
            "data": record,
        }
    except Exception as e:
        logger.exception(f"Failed to complete video upload: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "success": False,
                "error": "Failed to complete video upload",
                "detail": str(e),
            },
        )


# ==============================================================
# POST /videos/upload
# Upload a video — saved locally, status set to 'unprocessed'
# ==============================================================
@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_video(
    file: UploadFile = File(...),
    current_user=Depends(get_current_user),
):
    """
    Upload a video file.

    - Saves the file to local storage (cloud upload is deferred).
    - Creates a record in the 'videos' table with status = 'unprocessed'.
    - Returns the video record ID for use by the processing service.
    """
    user_id = (
        current_user.get("id")
        if isinstance(current_user, dict)
        else getattr(current_user, "id", None)
    )

    # Basic content-type validation
    if file.content_type not in ALLOWED_VIDEO_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "success": False,
                "error": "Invalid file type",
                "detail": (
                    f"Expected a video file, got: {file.content_type}"
                ),
            },
        )

    try:
        logger.info(
            f"Upload request from user {user_id} — file: {file.filename}"
        )

        # Step 1: Save to local filesystem
        saved = StorageService.save_locally(file)

        # Step 2: Persist record to DB
        record = VideoService.create_video_record(
            filename=saved["filename"],
            original_name=file.filename,
            local_path=saved["local_path"],
            user_id=user_id,
        )

        logger.info(
            f"Video record created: {record['id']} — status: unprocessed"
        )

        return {
            "success": True,
            "message": "Video uploaded successfully",
            "data": {
                "video_id": record["id"],
                "filename": record["filename"],
                "original_name": record["original_name"],
                "status": record["status"],
                "uploaded_at": record["uploaded_at"],
            },
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception(f"Video upload failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "success": False,
                "error": "Video upload failed",
                "detail": str(e),
            },
        )


# ==============================================================
# POST /videos/register
# Register a video already uploaded to Supabase Storage
# ==============================================================
class VideoRegisterRequest(BaseModel):
    filename: str
    original_name: str
    blob_url: str


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register_video(
    body: VideoRegisterRequest,
    current_user=Depends(get_current_user),
):
    """
    Register metadata for a video already uploaded directly to
    Supabase Storage (client uploads the file first, then calls
    this endpoint with the resulting public URL).
    """
    user_id = (
        current_user.get("id")
        if isinstance(current_user, dict)
        else getattr(current_user, "id", None)
    )

    try:
        logger.info(
            f"Register request from user {user_id} — blob_url: {body.blob_url}"
        )

        record = VideoService.create_video_record(
            filename=body.filename,
            original_name=body.original_name,
            blob_url=body.blob_url,
            user_id=user_id,
        )

        logger.info(
            f"Video record created: {record['id']} — status: unprocessed"
        )

        return {
            "success": True,
            "message": "Video registered successfully",
            "data": {
                "video_id": record["id"],
                "filename": record["filename"],
                "original_name": record["original_name"],
                "status": record["status"],
                "uploaded_at": record["uploaded_at"],
            },
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception(f"Video registration failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "success": False,
                "error": "Video registration failed",
                "detail": str(e),
            },
        )


# ==============================================================
# GET /videos/unprocessed
# Fetch all videos with status = 'unprocessed'
# ==============================================================
@router.get("/unprocessed")
async def get_unprocessed_videos(
    current_user=Depends(get_current_user),
):
    """
    Return all video records with status = 'unprocessed'.

    Intended to be called by the AI processing service to
    retrieve its work queue.
    """
    try:
        videos = VideoService.get_unprocessed_videos()

        return {
            "success": True,
            "count": len(videos),
            "data": videos,
        }

    except Exception as e:
        logger.exception(f"Failed to fetch unprocessed videos: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "success": False,
                "error": "Failed to fetch unprocessed videos",
                "detail": str(e),
            },
        )


# ==============================================================
# PATCH /videos/{video_id}/status
# Update status of a video (processing | processed | failed)
# ==============================================================
@router.patch("/{video_id}/status")
async def update_video_status(
    video_id: str,
    body: dict,
    current_user=Depends(get_current_user),
):
    """
    Update the processing status of a video.

    Request body:
        {
            "status": "processing" | "processed" | "failed",
            "error_message": "optional — only for failed"
        }
    """
    new_status = body.get("status")
    error_message = body.get("error_message")

    if not new_status:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "success": False,
                "error": "'status' is required in request body",
            },
        )

    try:
        updated = VideoService.update_video_status(
            video_id=video_id,
            status=new_status,
            error_message=error_message,
        )

        return {
            "success": True,
            "message": f"Video status updated to '{new_status}'",
            "data": updated,
        }

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "success": False,
                "error": str(e),
            },
        )

    except Exception as e:
        logger.exception(f"Failed to update video status: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "success": False,
                "error": "Failed to update video status",
                "detail": str(e),
            },
        )


# ==============================================================
# PATCH /videos/{video_id}/score
# Update the computed score of a video
# ==============================================================
@router.patch("/{video_id}/score")
async def update_video_score(
    video_id: str,
    body: dict,
    current_user=Depends(get_current_user),
):
    """
    Update the score of a video.

    Request body:
        { "score": 8.5 }
    """
    score = body.get("score")

    if score is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "success": False,
                "error": "'score' is required in request body",
            },
        )

    try:
        updated = VideoService.update_video_score(
            video_id=video_id,
            score=score,
        )

        return {
            "success": True,
            "message": f"Video score updated to {score}",
            "data": updated,
        }

    except Exception as e:
        logger.exception(f"Failed to update video score: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "success": False,
                "error": "Failed to update video score",
                "detail": str(e),
            },
        )