import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status

from app.database.supabase import supabase
from app.routes.cases import corrected_plate, sign_paths
from app.schemas.rbac import DeleteVideoRequest, UploadCompleteRequest, UploadInitV3Request, WithdrawRequest
from app.services.storage_service import StorageService
from app.services.video_service import MAX_VIDEO_BYTES, VideoService
from app.utils.audit import write_audit
from app.utils.auth import api_error, get_current_user, require_role
from app.utils.db import PLATE_RE, normalise_plate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/videos", tags=["Videos"])

ALLOWED_VIDEO_TYPES = {
    "video/mp4",
    "video/mpeg",
    "video/quicktime",
    "video/x-msvideo",
    "video/webm",
}
VEHICLE_TYPES = {"two_wheeler", "four_wheeler"}
TIER_PRIORITY = {"top": 3, "middle": 2, "minor": 1}
USER_STATUS = {"uploading": "uploading", "unprocessed": "queued", "processing": "analysing",
               "failed": "could_not_process", "withdrawn": "withdrawn"}
OPEN_CASE = ("pending_review", "in_review", "second_opinion", "reopened")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _check_size_and_quota(user_id: str, size_bytes: int):
    if size_bytes > MAX_VIDEO_BYTES:
        raise api_error(status.HTTP_413_CONTENT_TOO_LARGE, "Video exceeds the 200 MB limit")
    if VideoService.uploads_today(user_id) >= VideoService.max_uploads_per_day():
        raise api_error(status.HTTP_429_TOO_MANY_REQUESTS, "Daily upload quota reached")


def _uploader_plate(plate: Optional[str], claimed: Optional[str], subject_resolved: bool) -> Optional[str]:
    """docs §0: the uploader reads a plate only on the subject vehicle, only when they typed it and the
    AI resolved exactly that plate. Everything else is null — a partial mask still leaks state + RTO district."""
    return plate if subject_resolved and claimed and normalise_plate(plate) == claimed else None


def _citizen_safe(video: dict) -> dict:
    """Storage locations and internal failure text never leave the backend for an uploader (blob_url is a
    public URL of the original clip and would bypass signing). error_category stays: it drives user_status."""
    return {k: v for k, v in video.items() if k not in ("blob_url", "local_path", "storage_path", "error_reason")}


def _get_video(video_id: str) -> Optional[dict]:
    res = supabase.table("videos").select("*").eq("id", video_id).is_("deleted_at", "null").limit(1).execute()
    return res.data[0] if res.data else None


def user_status(video: dict, cases: list) -> str:
    """Plain words for the uploader (docs §2.6 item 20)."""
    s = video.get("status")
    if s == "processed":
        return "awaiting_review" if any(c.get("status") in OPEN_CASE for c in cases) else "decided"
    return USER_STATUS.get(s, s)


def _validate_claim(body: UploadInitV3Request) -> dict:
    """declared_violation must be allowed for the vehicle type; plate must be Indian format. Returns the videos columns."""
    plate = normalise_plate(body.claimed_plate)
    if plate and not PLATE_RE.match(plate):
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "claimed_plate is not a valid Indian registration (e.g. MH12AB1234)")
    priority = 0
    if body.declared_violation:
        rows = supabase.table("violation_policy").select("violation_type, tier, two_wheeler, four_wheeler").eq("violation_type", body.declared_violation).limit(1).execute().data or []
        pol = next((r for r in rows if r.get("violation_type") == body.declared_violation), None)
        if not pol or not pol.get(body.vehicle_type):
            raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, f"declared_violation '{body.declared_violation}' is not reportable for a {body.vehicle_type}")
        priority = TIER_PRIORITY.get(pol.get("tier"), 0)
    return {
        "declared_violation": body.declared_violation, "claimed_plate": plate, "note": body.note.strip(),
        "recording_at": body.recording_at.isoformat() if body.recording_at else None, "location_text": body.location_text, "consent_at": _now(), "priority": priority,
    }


# ==============================================================
# POST /videos/upload/init
# ==============================================================
@router.post("/upload/init", status_code=status.HTTP_201_CREATED)
def init_video_upload(body: UploadInitV3Request, current_user=Depends(get_current_user)):
    if body.content_type not in ALLOWED_VIDEO_TYPES:
        raise api_error(status.HTTP_400_BAD_REQUEST, f"Invalid file type: {body.content_type}")
    _check_size_and_quota(current_user["id"], body.size_bytes)
    claim = _validate_claim(body)
    try:
        signed = StorageService.create_signed_upload_url(body.filename)
        record = VideoService.create_video_record(
            filename=signed["storage_path"],
            original_name=body.filename,
            user_id=current_user["id"],
            vehicle_type=body.vehicle_type,
            status="uploading",
            file_size=body.size_bytes,
            storage_path=signed["storage_path"],
            **claim,
        )
    except Exception as e:
        logger.exception("Failed to init video upload")
        raise api_error(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Failed to init video upload: {e}")
    return {
        "success": True,
        "data": {
            "video_id": record["id"],
            "storage_path": signed["storage_path"],
            "upload_url": signed["upload_url"],
            "priority": claim["priority"],
        },
    }


# ==============================================================
# POST /videos/upload/complete
# ==============================================================
@router.post("/upload/complete")
def complete_video_upload(body: UploadCompleteRequest, current_user=Depends(get_current_user)):
    video = _get_video(body.video_id)
    if not video or video.get("uploaded_by") != current_user["id"]:
        raise api_error(status.HTTP_404_NOT_FOUND, "Video not found")
    if video.get("status") != "uploading":
        return {"success": True, "data": _citizen_safe(video)}  # idempotent: already completed

    storage_path = video.get("local_path")
    size = StorageService.object_size(storage_path) if storage_path else 0
    if size <= 0:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Uploaded object not found in storage")

    try:
        res = (
            supabase.table("videos")
            .update({
                "blob_url": StorageService.get_public_url(storage_path),
                "file_size": size,
                "status": "unprocessed",
            })
            .eq("id", video["id"])
            .execute()
        )
    except Exception as e:
        logger.exception("Failed to complete video upload")
        raise api_error(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Failed to complete video upload: {e}")
    return {"success": True, "message": "Video upload completed", "data": _citizen_safe(res.data[0] if res.data else video)}


# ==============================================================
# POST /videos/upload  (legacy single-request multipart upload)
# ==============================================================
@router.post("/upload", status_code=status.HTTP_201_CREATED)
def upload_video(
    file: UploadFile = File(...),
    vehicle_type: str = Form("two_wheeler"),
    current_user=Depends(get_current_user),
):
    if file.content_type not in ALLOWED_VIDEO_TYPES:
        raise api_error(status.HTTP_400_BAD_REQUEST, f"Invalid file type: {file.content_type}")
    if vehicle_type not in VEHICLE_TYPES:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "vehicle_type must be two_wheeler or four_wheeler")

    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    if size <= 0:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Empty file")
    _check_size_and_quota(current_user["id"], size)

    try:
        stored = StorageService.upload_video(file)
        record = VideoService.create_video_record(
            filename=stored["filename"],
            original_name=file.filename,
            user_id=current_user["id"],
            vehicle_type=vehicle_type,
            status="unprocessed",
            file_size=size,
            blob_url=StorageService.get_public_url(stored["filename"]),
            storage_path=stored["filename"],
        )
    except Exception as e:
        logger.exception("Video upload failed")
        raise api_error(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Video upload failed: {e}")

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


# ==============================================================
# GET /videos  (role-scoped list)
# ==============================================================
@router.get("")
def list_videos(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user=Depends(get_current_user),
):
    q = supabase.table("videos").select("*").is_("deleted_at", "null")
    if current_user["role"] == "citizen":
        q = q.eq("uploaded_by", current_user["id"])
    if status_filter:
        q = q.eq("status", status_filter)
    rows = q.order("uploaded_at", desc=True).range(offset, offset + limit - 1).execute().data or []
    ids = [v["id"] for v in rows if v.get("status") == "processed"]
    cases = supabase.table("cases").select("id, video_id, status").in_("video_id", ids).execute().data or [] if ids else []
    out = []
    for v in rows:
        v["user_status"] = user_status(v, [c for c in cases if c.get("video_id") == v["id"]])
        out.append(_citizen_safe(v) if current_user["role"] == "citizen" else v)
    return {"success": True, "data": out}


# ==============================================================
# GET /videos/{id}
# ==============================================================
@router.get("/{video_id}")
def get_video(video_id: str, current_user=Depends(get_current_user)):
    video = _get_video(video_id)
    is_citizen = current_user["role"] == "citizen"
    if not video or (is_citizen and video.get("uploaded_by") != current_user["id"]):
        raise api_error(status.HTTP_404_NOT_FOUND, "Video not found")

    records = supabase.table("vehicle_records").select("*").eq("video_id", video_id).execute().data or []
    cases = supabase.table("cases").select("*").eq("video_id", video_id).order("created_at").execute().data or []
    case_ids = [c["id"] for c in cases]
    findings = supabase.table("findings").select("*").in_("case_id", case_ids).execute().data or [] if case_ids else []
    # docs §2.6 items 24-25: the uploader gets the textual outcome and the redacted detection video, never evidence frames.
    evidence = supabase.table("evidence").select("*").in_("case_id", case_ids).execute().data or [] if case_ids and not is_citizen else []
    corrections = supabase.table("case_corrections").select("case_id, field, corrected_value, created_at").in_("case_id", case_ids).eq("field", "plate").execute().data or [] if case_ids else []
    subject_done = any(c.get("is_subject") and c.get("status") == "finalized" for c in cases)
    by_track = {r.get("track_id"): r for r in records}
    claimed = normalise_plate(video.get("claimed_plate"))
    resolved_tracks = {c.get("track_id") for c in cases if c.get("is_subject") and c.get("identity_status") == "resolved"}
    signed = sign_paths([e["blob_path"] for e in evidence])

    out_cases = []
    for c in cases:
        rec = by_track.get(c.get("track_id"), {})
        plate = corrected_plate([x for x in corrections if x["case_id"] == c["id"]]) or rec.get("plate_text")
        if is_citizen:
            plate = _uploader_plate(plate, claimed, c.get("track_id") in resolved_tracks)
        out_cases.append({
            "id": c["id"], "track_id": c.get("track_id"), "is_subject": c.get("is_subject"), "status": c.get("status"),
            "lane": c.get("lane"), "identity_status": c.get("identity_status"), "vehicle_type": rec.get("vehicle_type"),
            "plate": plate,
            "findings": [{
                "id": f["id"], "violation": f.get("violation"), "ai_result": f.get("ai_result"), "tier": f.get("tier"),
                "decision": f.get("decision"), "rejection_reason": f.get("rejection_reason"),
                "note_public": None if is_citizen else f.get("note"),
                **({} if is_citizen else {"decided_by": f.get("decided_by"), "decided_at": f.get("decided_at")}),
            } for f in findings if f.get("case_id") == c["id"]],
            "evidence": [{"finding_id": e["finding_id"], "frame_index": e.get("frame_index"), "blob_path": e["blob_path"],
                          **signed.get(e["blob_path"], {})} for e in evidence if e.get("case_id") == c["id"]],
            **({} if is_citizen else {"claimed_by": c.get("claimed_by"), "finalized_by": c.get("finalized_by")}),
        })

    if is_citizen:  # no reviewer ids, notes, verdicts or evidence paths for the uploader (API.md §2)
        records = [{**{k: r.get(k) for k in ("track_id", "vehicle_type", "first_seen", "last_seen", "frames_observed")},
                    "plate_text": _uploader_plate(r.get("plate_text"), claimed, r.get("track_id") in resolved_tracks),
                    "corrected_plate": _uploader_plate(r.get("corrected_plate"), claimed, r.get("track_id") in resolved_tracks)}
                   for r in records]
    detection_url = None
    if video.get("detection_video_path") and (not is_citizen or subject_done):
        detection_url = sign_paths([video["detection_video_path"]])[video["detection_video_path"]].get("url")
    video["user_status"] = user_status(video, cases)
    if is_citizen:
        video = _citizen_safe(video)
    return {"success": True, "data": {"video": video, "cases": out_cases, "vehicle_records": records,
                                      "detection_video_url": detection_url}}


# ==============================================================
# POST /videos/{id}/withdraw  (owner)
# ==============================================================
@router.post("/{video_id}/withdraw")
def withdraw_video(video_id: str, body: Optional[WithdrawRequest] = None, current_user=Depends(get_current_user)):
    video = _get_video(video_id)
    if not video or video.get("uploaded_by") != current_user["id"]:
        raise api_error(status.HTTP_404_NOT_FOUND, "Video not found")
    s = video.get("status")
    if s == "withdrawn":
        raise api_error(status.HTTP_409_CONFLICT, "Already withdrawn")
    cases = supabase.table("cases").select("id, status, claimed_by").eq("video_id", video_id).execute().data or [] if s in ("processing", "processed") else []
    if any(c.get("status") == "finalized" for c in cases):
        raise api_error(status.HTTP_409_CONFLICT, "A review on this submission is already decided; it can no longer be withdrawn")
    if any(c.get("status") == "in_review" for c in cases):
        pending = supabase.table("withdrawal_requests").select("id").eq("video_id", video_id).eq("status", "pending").limit(1).execute().data
        if pending:
            raise api_error(status.HTTP_409_CONFLICT, "A withdrawal request is already pending")
        reason = (body.reason or "").strip() if body else ""
        if len(reason) < 5:
            raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "A reason (at least 5 characters) is required: a reviewer has to answer this request")
        req = supabase.table("withdrawal_requests").insert({"video_id": video_id, "requested_by": current_user["id"], "reason": reason}).execute()
        return {"success": True, "data": {"user_status": "awaiting_review", "withdrawal": "requested",
                                          "request": req.data[0] if req.data else None}}
    res = supabase.table("videos").update({"status": "withdrawn", "withdrawn_at": _now()}).eq("id", video_id).execute()
    if cases:
        supabase.table("cases").update({"status": "withdrawn", "claimed_by": None, "claimed_at": None}).eq("video_id", video_id).in_("status", list(OPEN_CASE)).execute()
    row = _citizen_safe(res.data[0] if res.data else {**video, "status": "withdrawn"})
    row["user_status"] = "withdrawn"
    return {"success": True, "data": row}


# ==============================================================
# POST /videos/{id}/requeue  (admin)
# ==============================================================
@router.post("/{video_id}/requeue")
def requeue_video(video_id: str, current_user=Depends(require_role("admin"))):
    video = _get_video(video_id)
    if not video:
        raise api_error(status.HTTP_404_NOT_FOUND, "Video not found")
    if video.get("status") not in ("processed", "failed"):
        raise api_error(status.HTTP_409_CONFLICT, f"Cannot requeue a video in status '{video.get('status')}'")
    # a new run creates a fresh set of cases; reviewers must not lose work already decided or in hand
    cases = supabase.table("cases").select("id, status").eq("video_id", video_id).execute().data or []
    if any(c.get("status") in ("finalized", "in_review") for c in cases):
        raise api_error(status.HTTP_409_CONFLICT, "A case on this submission is decided or under review; it can no longer be requeued")
    res = (
        supabase.table("videos")
        # attempts is NOT reset: the worker derives run_id from (video_id, attempts); a reused run_id collides in persist_run_result
        .update({"status": "unprocessed", "error_reason": None, "error_category": None, "claimed_at": None})
        .eq("id", video_id)
        .execute()
    )
    write_audit(current_user, "video.requeue", "video", video_id,
                before={"status": video.get("status"), "attempts": video.get("attempts")},
                after={"status": "unprocessed", "attempts": video.get("attempts")})
    return {"success": True, "data": res.data[0] if res.data else video}


# ==============================================================
# DELETE /videos/{id}  (soft delete)
# ==============================================================
@router.delete("/{video_id}")
def delete_video(video_id: str, body: Optional[DeleteVideoRequest] = None, current_user=Depends(get_current_user)):
    video = _get_video(video_id)
    is_admin = current_user["role"] == "admin"
    if not video or (not is_admin and video.get("uploaded_by") != current_user["id"]):
        raise api_error(status.HTTP_404_NOT_FOUND, "Video not found")
    if not is_admin and video.get("status") not in ("uploading", "unprocessed"):
        raise api_error(status.HTTP_403_FORBIDDEN, "Only videos not yet processed can be deleted by their owner")

    reason = body.reason if body else None
    res = (
        supabase.table("videos")
        .update({"deleted_at": _now(), "deleted_by": current_user["id"], "delete_reason": reason})
        .eq("id", video_id)
        .execute()
    )
    write_audit(current_user, "video.soft_delete", "video", video_id,
                before={"status": video.get("status")}, after={"deleted": True}, reason=reason)
    row = res.data[0] if res.data else video
    return {"success": True, "data": row if is_admin else _citizen_safe(row)}
