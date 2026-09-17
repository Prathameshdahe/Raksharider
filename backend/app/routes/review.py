import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, status

from app.database.supabase import supabase
from app.schemas.rbac import ReviewDecisionRequest
from app.utils.audit import write_audit
from app.utils.auth import api_error, require_role

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/review", tags=["Review"])

VIDEO_FIELDS = "id, uploaded_at, vehicle_type, uploaded_by, blob_url"
reviewer = require_role("officer", "admin")


def _attach_videos_and_violations(records: list) -> list:
    if not records:
        return records
    video_ids = list({r["video_id"] for r in records if r.get("video_id")})
    record_ids = [r["id"] for r in records]
    videos = {v["id"]: v for v in (supabase.table("videos").select(VIDEO_FIELDS).in_("id", video_ids).execute().data or [])}
    violations = supabase.table("violations").select("*").in_("vehicle_record_id", record_ids).execute().data or []
    by_record: dict = {}
    for v in violations:
        by_record.setdefault(v["vehicle_record_id"], []).append(v)
    for r in records:
        r["video"] = videos.get(r.get("video_id"))
        r["violations"] = by_record.get(r["id"], [])
    return records


# ==============================================================
# GET /review/queue
# ==============================================================
@router.get("/queue")
def review_queue(
    status_filter: str = Query(default="needs_review", alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user=Depends(reviewer),
):
    if status_filter not in ("clear", "needs_review", "confirmed", "rejected"):
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid status")
    res = (
        supabase.table("vehicle_records")
        .select("*")
        .eq("review_status", status_filter)
        .order("created_at", desc=False)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return {"success": True, "data": _attach_videos_and_violations(res.data or [])}


# ==============================================================
# GET /review/records/{id}
# ==============================================================
@router.get("/records/{record_id}")
def review_record(record_id: str, current_user=Depends(reviewer)):
    res = supabase.table("vehicle_records").select("*").eq("id", record_id).limit(1).execute()
    if not res.data:
        raise api_error(status.HTTP_404_NOT_FOUND, "Record not found")
    record = _attach_videos_and_violations(res.data)[0]
    record["audit"] = (
        supabase.table("audit_log").select("*")
        .eq("entity", "vehicle_record").eq("entity_id", record_id)
        .order("created_at", desc=True).execute().data or []
    )
    return {"success": True, "data": record}


# ==============================================================
# POST /review/records/{id}/decision
# ==============================================================
@router.post("/records/{record_id}/decision")
def review_decision(record_id: str, body: ReviewDecisionRequest, current_user=Depends(reviewer)):
    res = supabase.table("vehicle_records").select("*").eq("id", record_id).limit(1).execute()
    if not res.data:
        raise api_error(status.HTTP_404_NOT_FOUND, "Record not found")
    record = res.data[0]

    already_decided = record.get("review_status") in ("confirmed", "rejected")
    is_override = already_decided and body.override and current_user["role"] == "admin"
    if already_decided and not is_override:
        raise api_error(status.HTTP_409_CONFLICT, "Record already decided")

    violations = supabase.table("violations").select("*").eq("vehicle_record_id", record_id).execute().data or []
    existing_types = {v["violation_type"] for v in violations}
    to_confirm = set(body.violations if body.violations is not None else (record.get("review_violations") or existing_types))
    unknown = to_confirm - existing_types
    if unknown:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown violation types for this record: {sorted(unknown)}")
    if body.decision == "rejected":
        to_confirm = set()

    plate = (body.corrected_plate or record.get("plate_text") or "").strip().upper() or None
    now = datetime.now(timezone.utc).isoformat()
    review_meta = {"reviewed_by": current_user["id"], "reviewed_at": now, "review_reason": body.reason}

    deltas = {}
    if to_confirm and plate:
        policy = supabase.table("violation_policy").select("violation_type, trust_delta").in_("violation_type", sorted(to_confirm)).execute().data or []
        deltas = {p["violation_type"]: p["trust_delta"] for p in policy}

    # ---- write phase: violations -> ledger -> vehicle_records (LAST: fires the audit/notify trigger)
    touched = []       # (violation_id, previous_row_fields) for rollback
    ledger_ids = []
    try:
        for v in violations:
            new_status = "confirmed" if v["violation_type"] in to_confirm else "rejected"
            prev = {k: v.get(k) for k in ("status", "reviewed_by", "reviewed_at", "review_reason")}
            supabase.table("violations").update({"status": new_status, **review_meta}).eq("id", v["id"]).execute()
            touched.append((v["id"], prev))

        if to_confirm and plate:
            rows = [
                {
                    "plate": plate,
                    "vehicle_record_id": record_id,
                    "violation_type": t,
                    "delta": deltas[t],
                    "reason": body.reason,
                    "actor_id": current_user["id"],
                }
                for t in sorted(to_confirm) if t in deltas
            ]
            if rows:
                inserted = supabase.table("score_ledger").insert(rows).execute().data or []
                ledger_ids = [r["id"] for r in inserted if "id" in r]

        update = {
            "review_status": body.decision,
            "corrected_plate": body.corrected_plate,
            "corrected_vehicle_type": body.corrected_vehicle_type,
            **review_meta,
        }
        upd = supabase.table("vehicle_records").update(update).eq("id", record_id).execute()
        if not upd.data:
            raise RuntimeError("vehicle_records update returned no row")
    except Exception as e:
        logger.exception("Review decision failed; rolling back")
        try:
            if ledger_ids:
                supabase.table("score_ledger").delete().in_("id", ledger_ids).execute()
            for vid, prev in touched:
                supabase.table("violations").update(prev).eq("id", vid).execute()
        except Exception:
            logger.exception("Rollback of review decision failed")
        raise api_error(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Review decision failed: {e}")

    if is_override:
        try:
            write_audit(current_user, "review.override", "vehicle_record", record_id,
                        before={"review_status": record.get("review_status")},
                        after={"review_status": body.decision}, reason=body.reason)
        except Exception:
            logger.exception("Failed to write review.override audit row")

    return {"success": True, "data": upd.data[0]}
