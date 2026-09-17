"""Cases and findings (API.md §3). A case = one vehicle track in one run; AI columns are immutable."""
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, status

from app.database.supabase import supabase
from app.schemas.rbac import CorrectionRequest, FindingDecisionRequest, WithdrawalAnswerRequest
from app.services import storage_service
from app.utils.audit import write_audit
from app.utils.auth import api_error, require_role
from app.utils.db import PLATE_RE, normalise_plate, rpc

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Cases"])
reviewer = require_role("officer", "admin")

CASE_STATUSES = ("pending_review", "in_review", "second_opinion", "finalized", "reopened", "withdrawn")
CASE_FIELDS = "id, video_id, run_id, track_id, is_subject, lane, status, priority, identity_status, cycle, claimed_by, claimed_at, finalized_at, created_at"


def _first(rows):
    return rows[0] if rows else None


def get_case(case_id: str) -> dict:
    case = _first(supabase.table("cases").select("*").eq("id", case_id).limit(1).execute().data)
    if not case:
        raise api_error(status.HTTP_404_NOT_FOUND, "Case not found")
    return case


def require_claimer(case: dict, user: dict) -> None:
    if case.get("status") != "in_review" or case.get("claimed_by") != user["id"]:
        raise api_error(status.HTTP_409_CONFLICT, "Case is not in review by you")


def sign_paths(paths: list) -> dict:
    """path -> {url, expires_at}; url None (with reason) when Azure is not configured so reviewers still see the record."""
    out = {}
    for p in paths:
        try:
            out[p] = storage_service.sign_azure_blob(p)
        except storage_service.AzureNotConfigured as e:
            out[p] = {"url": None, "expires_at": None, "error": str(e)}
    return out


def _text(value):
    """case_corrections columns are text; an evidence selection arrives as a list and is stored as JSON."""
    return json.dumps(value) if isinstance(value, list) else value


def corrected_plate(corrections: list) -> Optional[str]:
    plates = [c for c in corrections if c.get("field") == "plate" and c.get("corrected_value")]
    return max(plates, key=lambda c: c.get("created_at") or "")["corrected_value"] if plates else None


def enrich_cases(cases: list) -> list:
    """List shape: plate {ai, confidence, claimed, corrected}, vehicle_type, findings_count, allegation_answer."""
    if not cases:
        return cases
    ids = [c["id"] for c in cases]
    rec_ids = [c["vehicle_record_id"] for c in cases if c.get("vehicle_record_id")]
    video_ids = list({c["video_id"] for c in cases})
    records = {r["id"]: r for r in (supabase.table("vehicle_records").select("id, plate_text, plate_confidence, vehicle_type")
                                    .in_("id", rec_ids).execute().data or [])} if rec_ids else {}
    videos = {v["id"]: v for v in (supabase.table("videos").select("id, claimed_plate, allegation_answer, declared_violation")
                                   .in_("id", video_ids).execute().data or [])}
    corrections = supabase.table("case_corrections").select("case_id, field, corrected_value, created_at").in_("case_id", ids).eq("field", "plate").execute().data or []
    findings = supabase.table("findings").select("id, case_id").in_("case_id", ids).execute().data or []
    for c in cases:
        rec = records.get(c.get("vehicle_record_id"), {})
        vid = videos.get(c["video_id"], {})
        c["vehicle_type"] = rec.get("vehicle_type")
        c["plate"] = {
            "ai": rec.get("plate_text"),
            "confidence": rec.get("plate_confidence"),
            "claimed": vid.get("claimed_plate") if c.get("is_subject") else None,
            "corrected": corrected_plate([x for x in corrections if x["case_id"] == c["id"]]),
        }
        c["findings_count"] = sum(1 for f in findings if f.get("case_id") == c["id"])
        c["allegation_answer"] = vid.get("allegation_answer") if c.get("is_subject") else None
        c["declared_violation"] = vid.get("declared_violation") if c.get("is_subject") else None
    return cases


# ==============================================================
# GET /cases
# ==============================================================
@router.get("/cases")
def list_cases(
    lane: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user=Depends(reviewer),
):
    if lane and lane not in ("normal", "not_supported"):
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "lane must be normal or not_supported")
    if status_filter and status_filter not in CASE_STATUSES:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid status")
    q = supabase.table("cases").select(CASE_FIELDS + ", vehicle_record_id")
    if lane:
        q = q.eq("lane", lane)
    if status_filter:
        q = q.eq("status", status_filter)
    else:
        q = q.neq("status", "withdrawn")
    rows = q.order("priority", desc=True).order("created_at", desc=False).range(offset, offset + limit - 1).execute().data or []
    return {"success": True, "data": enrich_cases(rows)}


# ==============================================================
# GET /cases/mine
# ==============================================================
@router.get("/cases/mine")
def my_cases(current_user=Depends(reviewer)):
    rows = (supabase.table("cases").select(CASE_FIELDS + ", vehicle_record_id")
            .eq("claimed_by", current_user["id"]).eq("status", "in_review").order("claimed_at", desc=False).execute().data or [])
    now = datetime.now(timezone.utc)
    for c in rows:
        try:  # ponytail: idle = time since claim; switch to last decision time if reviewers ask
            claimed = datetime.fromisoformat(str(c["claimed_at"]).replace("Z", "+00:00"))
            c["idle_hours"] = round((now - claimed).total_seconds() / 3600, 1)
        except Exception:
            c["idle_hours"] = None
    return {"success": True, "data": enrich_cases(rows)}


# ==============================================================
# GET /rejection-reasons
# ==============================================================
@router.get("/rejection-reasons")
def rejection_reasons(current_user=Depends(reviewer)):
    rows = supabase.table("rejection_reasons").select("code, label, sort").order("sort").execute().data or []
    return {"success": True, "data": rows}


# ==============================================================
# POST /cases/{id}/claim  /release
# ==============================================================
@router.post("/cases/{case_id}/claim")
def claim_case(case_id: str, current_user=Depends(reviewer)):
    get_case(case_id)
    row = rpc(supabase, "claim_case", {"p_case_id": case_id, "p_reviewer": current_user["id"]})
    return {"success": True, "data": row}


@router.post("/cases/{case_id}/release")
def release_case(case_id: str, current_user=Depends(reviewer)):
    case = get_case(case_id)
    is_admin = current_user["role"] == "admin"
    row = rpc(supabase, "release_case", {"p_case_id": case_id, "p_actor": current_user["id"], "p_is_admin": is_admin})
    if is_admin and case.get("claimed_by") != current_user["id"]:
        write_audit(current_user, "case.release", "case", case_id,
                    before={"claimed_by": case.get("claimed_by"), "status": case.get("status")},
                    after={"claimed_by": None})
    return {"success": True, "data": row}


# ==============================================================
# GET /cases/{id}
# ==============================================================
@router.get("/cases/{case_id}")
def case_detail(case_id: str, current_user=Depends(reviewer)):
    case = enrich_cases([get_case(case_id)])[0]   # same plate / vehicle_type / allegation_answer shape as the list
    video = _first(supabase.table("videos").select("*").eq("id", case["video_id"]).limit(1).execute().data) or {}
    record = _first(supabase.table("vehicle_records").select("*").eq("id", case.get("vehicle_record_id")).limit(1).execute().data) if case.get("vehicle_record_id") else None
    observations = (supabase.table("plate_observations").select("*").eq("video_id", case["video_id"])
                    .eq("run_id", case["run_id"]).eq("track_id", case["track_id"]).execute().data or [])
    findings = supabase.table("findings").select("*").eq("case_id", case_id).order("created_at").execute().data or []
    evidence = supabase.table("evidence").select("*").eq("case_id", case_id).order("frame_index").execute().data or []
    corrections = supabase.table("case_corrections").select("*").eq("case_id", case_id).order("created_at").execute().data or []
    decisions = supabase.table("finding_decisions").select("*").eq("case_id", case_id).order("created_at").execute().data or []

    signed = sign_paths([e["blob_path"] for e in evidence])
    by_finding: dict = {}
    for e in evidence:
        by_finding.setdefault(e["finding_id"], []).append({**e, **signed.get(e["blob_path"], {})})

    summary = video.get("summary") or {}
    plate = corrected_plate(corrections) or normalise_plate((record or {}).get("plate_text"))
    history = None
    if plate and (current_user["role"] == "admin" or case.get("status") == "finalized"):
        rows = supabase.table("plate_history").select("*").eq("plate", plate).order("created_at", desc=True).execute().data or []
        history = {"confirmed": [h for h in rows if h.get("layer") == "confirmed"],
                   "observed": [h for h in rows if h.get("layer") == "observed"]}

    return {"success": True, "data": {
        **case,
        "uploader_claim": {"declared_violation": video.get("declared_violation"), "note": video.get("note"),
                           "claimed_plate": video.get("claimed_plate"), "vehicle_type": video.get("vehicle_type")},
        "allegation": summary.get("allegation"),
        "ai": {
            "track": record,
            "plate_observations": observations,
            "resolved_plate": {"text": (record or {}).get("plate_text"), "confidence": (record or {}).get("plate_confidence")},
            "findings": findings,
            "pipeline_version": summary.get("pipeline_version"),
            "model_versions": summary.get("model_versions"),
            "vlm_calls": [v for v in (summary.get("vlm_calls") or []) if v.get("track_id") == case["track_id"]],
        },
        "evidence": by_finding,
        "corrections": corrections,
        "decisions": decisions,
        "plate_of_record": plate,
        "plate_history": history,
        # A pending withdrawal request is answered from this screen; without it the reviewer
        # has no way to see the uploader asked, and POST /cases/{id}/withdrawal is unreachable.
        "withdrawal_request": (supabase.table("withdrawal_requests").select("*")
                               .eq("video_id", case["video_id"]).eq("status", "pending")
                               .limit(1).execute().data or [None])[0],
    }}


# ==============================================================
# POST /cases/{id}/findings/{fid}/decision
# ==============================================================
@router.post("/cases/{case_id}/findings/{finding_id}/decision")
def decide_finding(case_id: str, finding_id: str, body: FindingDecisionRequest, current_user=Depends(reviewer)):
    case = get_case(case_id)
    require_claimer(case, current_user)
    if body.decision == "rejected" and not body.rejection_reason:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "rejection_reason is required when rejecting")
    row = rpc(supabase, "decide_finding", {
        "p_case_id": case_id, "p_finding_id": finding_id, "p_reviewer": current_user["id"],
        "p_decision": body.decision, "p_rejection_reason": body.rejection_reason, "p_note": body.note,
    })
    return {"success": True, "data": row}


# ==============================================================
# POST /cases/{id}/corrections
# ==============================================================
@router.post("/cases/{case_id}/corrections")
def add_correction(case_id: str, body: CorrectionRequest, current_user=Depends(reviewer)):
    case = get_case(case_id)
    require_claimer(case, current_user)
    corrected = body.corrected_value
    if isinstance(corrected, list) and body.field != "evidence":
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, f"corrected_value must be a single value for a {body.field} correction")
    if body.field == "plate":
        if not body.reason:
            raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "reason is required for a plate correction")
        corrected = normalise_plate(corrected)
        if corrected and not PLATE_RE.match(corrected):
            raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "corrected_value is not a valid Indian plate")
    if body.field == "vehicle_type" and corrected not in ("two_wheeler", "four_wheeler"):
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "corrected_value must be two_wheeler or four_wheeler")
    if body.field in ("violation_label", "evidence"):
        if not body.finding_id:
            raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, f"finding_id is required for a {body.field} correction")
        owned = supabase.table("findings").select("id").eq("id", body.finding_id).eq("case_id", case_id).limit(1).execute().data
        if not owned:
            raise api_error(status.HTTP_404_NOT_FOUND, "Finding not found on this case")
    row = {
        "case_id": case_id, "finding_id": body.finding_id if body.field in ("violation_label", "evidence") else None,
        "field": body.field, "ai_value": _text(body.ai_value), "corrected_value": _text(corrected), "reason": body.reason,
        "reviewer_id": current_user["id"],
    }
    res = supabase.table("case_corrections").insert(row).execute()
    return {"success": True, "data": res.data[0] if res.data else row}


# ==============================================================
# POST /cases/{id}/finalize
# ==============================================================
@router.post("/cases/{case_id}/finalize")
def finalize_case(case_id: str, current_user=Depends(reviewer)):
    case = get_case(case_id)
    require_claimer(case, current_user)
    pending = supabase.table("findings").select("id").eq("case_id", case_id).eq("decision", "pending").limit(1).execute().data
    if pending:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Every finding needs a decision before finalizing")
    row = rpc(supabase, "finalize_case", {"p_case_id": case_id, "p_reviewer": current_user["id"]})
    return {"success": True, "data": row}


# ==============================================================
# POST /cases/{id}/withdrawal  — claimer (or admin) answers the uploader's pending withdrawal request
# ==============================================================
@router.post("/cases/{case_id}/withdrawal")
def answer_withdrawal(case_id: str, body: WithdrawalAnswerRequest, current_user=Depends(reviewer)):
    case = get_case(case_id)
    if current_user["role"] != "admin":
        require_claimer(case, current_user)
    pending = supabase.table("withdrawal_requests").select("*").eq("video_id", case["video_id"]).eq("status", "pending").limit(1).execute().data or []
    if not pending:
        raise api_error(status.HTTP_404_NOT_FOUND, "No pending withdrawal request on this submission")
    new_status = "accepted" if body.accept else "declined"
    now = datetime.now(timezone.utc).isoformat()
    if body.accept:
        supabase.table("videos").update({"status": "withdrawn", "withdrawn_at": now}).eq("id", case["video_id"]).execute()
        supabase.table("cases").update({"status": "withdrawn", "claimed_by": None, "claimed_at": None}) \
            .eq("video_id", case["video_id"]).in_("status", ["pending_review", "in_review", "second_opinion", "reopened"]).execute()
    res = supabase.table("withdrawal_requests").update({
        "status": new_status, "decided_by": current_user["id"], "decided_at": now, "reason": body.reason,
    }).eq("id", pending[0]["id"]).execute()   # trigger notifies the uploader
    return {"success": True, "data": res.data[0] if res.data else {**pending[0], "status": new_status}}
