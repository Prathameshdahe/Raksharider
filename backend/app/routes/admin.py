import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, status

from app.database.supabase import supabase
from app.routes.evidence import sign_or_503
from app.schemas.rbac import PauseRequest, ReasonRequest, ReassignRequest, RoleChangeV3Request, SettingsRequest
from app.utils.audit import write_audit
from app.utils.auth import api_error, require_role

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin"])
admin = require_role("admin")

VIDEO_STATUSES = ("uploading", "unprocessed", "processing", "processed", "failed")


def _setting(key: str):
    res = supabase.table("system_settings").select("value").eq("key", key).limit(1).execute()
    return res.data[0]["value"] if res.data else None


# ==============================================================
# GET /admin/users
# ==============================================================
@router.get("/users")
def list_users(role: Optional[str] = None, current_user=Depends(admin)):
    q = supabase.table("profiles").select("id, email, full_name, role, requested_role, role_requested_at, badge_number, "
                                          "verified_via, last_seen_at, deactivated_at, created_at")
    if role:
        q = q.eq("role", role)
    res = q.order("created_at", desc=True).execute()
    return {"success": True, "data": res.data or []}


# ==============================================================
# PATCH /admin/users/{id}/role
# ==============================================================
@router.patch("/users/{user_id}/role")
def change_role(user_id: str, body: RoleChangeV3Request, current_user=Depends(admin)):
    if user_id == current_user["id"]:
        raise api_error(status.HTTP_400_BAD_REQUEST, "You cannot change your own role")
    if body.role == "officer" and not (body.verified_via or "").strip():
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "verified_via is required when promoting to officer (where was the badge checked?)")
    res = supabase.table("profiles").select("id, role, requested_role").eq("id", user_id).limit(1).execute()
    if not res.data:
        raise api_error(status.HTTP_404_NOT_FOUND, "User not found")
    target = res.data[0]
    update = {"role": body.role, "verified_via": body.verified_via}
    if target.get("requested_role") == body.role:
        update.update({"requested_role": None, "role_requested_at": None})
    # trg_guard_profile_role audits + notifies on role change.
    upd = supabase.table("profiles").update(update).eq("id", user_id).execute()
    return {"success": True, "data": upd.data[0] if upd.data else {**target, **update}}


# ==============================================================
# GET /admin/queue
# ==============================================================
@router.get("/queue")
def queue_health(current_user=Depends(admin)):
    counts = {}
    for s in VIDEO_STATUSES:
        r = supabase.table("videos").select("id", count="exact").eq("status", s).is_("deleted_at", "null").execute()
        counts[s] = r.count if r.count is not None else len(r.data or [])
    oldest = (
        supabase.table("videos").select("uploaded_at").eq("status", "unprocessed").is_("deleted_at", "null")
        .order("uploaded_at", desc=False).limit(1).execute().data or []
    )
    processing = supabase.table("videos").select("*").eq("status", "processing").is_("deleted_at", "null").execute().data or []
    failed = (
        supabase.table("videos").select("*").eq("status", "failed").is_("deleted_at", "null")
        .order("uploaded_at", desc=True).limit(20).execute().data or []
    )
    return {
        "success": True,
        "data": {
            "counts": counts,
            "oldest_unprocessed_at": oldest[0]["uploaded_at"] if oldest else None,
            "processing": processing,
            "failed": failed,
            "paused": _setting("worker_paused") is True,
            "worker_last_seen": _setting("worker_last_seen"),
        },
    }


# ==============================================================
# POST /admin/queue/pause
# ==============================================================
@router.post("/queue/pause")
def pause_queue(body: PauseRequest, current_user=Depends(admin)):
    before = _setting("worker_paused")
    supabase.table("system_settings").upsert({
        "key": "worker_paused",
        "value": body.paused,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": current_user["id"],
    }).execute()
    write_audit(current_user, "queue.pause", "system", "worker_paused",
                before={"paused": before}, after={"paused": body.paused})
    return {"success": True, "data": {"paused": body.paused}}


# ==============================================================
# POST /admin/queue/retry-failed
# ==============================================================
@router.post("/queue/retry-failed")
def retry_failed(current_user=Depends(admin)):
    res = (
        supabase.table("videos")
        .update({"status": "unprocessed", "error_reason": None, "error_category": None, "claimed_at": None})  # attempts kept: fresh run_id
        .eq("status", "failed").is_("deleted_at", "null")
        .execute()
    )
    ids = [r["id"] for r in (res.data or [])]
    write_audit(current_user, "queue.retry_failed", "system", "videos",
                before={"failed": len(ids)}, after={"requeued_ids": ids})
    return {"success": True, "data": {"count": len(ids)}}


# ==============================================================
# GET /admin/audit
# ==============================================================
@router.get("/audit")
def list_audit(
    entity: Optional[str] = None,
    entity_id: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=500),
    current_user=Depends(admin),
):
    q = supabase.table("audit_log").select("*")
    if entity:
        q = q.eq("entity", entity)
    if entity_id:
        q = q.eq("entity_id", entity_id)
    res = q.order("created_at", desc=True).limit(limit).execute()
    return {"success": True, "data": res.data or []}


# ==============================================================
# v3: live board, quality, escalations, settings, case admin (API.md §3, §4, §6)
# ==============================================================
SETTING_KEYS = ("max_uploads_per_day", "escalation_threshold_any", "escalation_threshold_top", "retention_days",
                "worker_paused", "worker_lease_minutes")
OPEN_CASE_STATUSES = ("pending_review", "second_opinion", "reopened")


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(ts) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _n(res) -> int:
    return res.count if res.count is not None else len(res.data or [])


@router.get("/live")
def live_board(current_user=Depends(admin)):
    now = datetime.now(timezone.utc)
    today = _iso(now.replace(hour=0, minute=0, second=0, microsecond=0))
    online = supabase.table("profiles").select("id", count="exact").gte("last_seen_at", _iso(now - timedelta(minutes=5))).execute()
    uploads = supabase.table("videos").select("id", count="exact").gte("uploaded_at", today).is_("deleted_at", "null").execute()
    queued = supabase.table("videos").select("id, priority").eq("status", "unprocessed").is_("deleted_at", "null").execute().data or []
    processing = supabase.table("videos").select("id, claimed_at, attempts").eq("status", "processing").is_("deleted_at", "null").execute().data or []
    failed = supabase.table("videos").select("id", count="exact").eq("status", "failed").gte("uploaded_at", today).is_("deleted_at", "null").execute()
    open_cases = supabase.table("cases").select("id, created_at, status").in_("status", list(OPEN_CASE_STATUSES)).order("created_at").execute().data or []
    claimed = supabase.table("cases").select("id, claimed_by").eq("status", "in_review").execute().data or []
    by_reviewer: dict = {}
    for c in claimed:
        by_reviewer[c["claimed_by"]] = by_reviewer.get(c["claimed_by"], 0) + 1
    names = {p["id"]: p.get("full_name") for p in (supabase.table("profiles").select("id, full_name").in_("id", list(by_reviewer)).execute().data or [])} if by_reviewer else {}
    oldest = _parse(open_cases[0]["created_at"]) if open_cases else None
    return {"success": True, "data": {
        "users_online": _n(online),
        "uploads_today": _n(uploads),
        "queue": {"by_priority": {p: sum(1 for v in queued if (v.get("priority") or 0) == p) for p in (3, 2, 1, 0)}, "depth": len(queued)},
        "processing": [{"video_id": v["id"], "attempts": v.get("attempts"),
                        "elapsed_s": int((now - _parse(v["claimed_at"])).total_seconds()) if _parse(v.get("claimed_at")) else None}
                       for v in processing],
        "worker_last_seen": _setting("worker_last_seen"),
        "failed_today": _n(failed),
        "review": {
            "backlog": len(open_cases),
            "oldest_waiting_s": int((now - oldest).total_seconds()) if oldest else None,
            "claimed": len(claimed),
            "reviewers_active": [{"id": rid, "name": names.get(rid), "open_claims": n} for rid, n in by_reviewer.items()],
        },
    }}


@router.get("/quality")
def quality(days: int = Query(default=30, ge=1, le=365), current_user=Depends(admin)):
    since = _iso(datetime.now(timezone.utc) - timedelta(days=days))
    decisions = supabase.table("finding_decisions").select("finding_id, decision, rejection_reason").gte("created_at", since).execute().data or []
    fids = list({d["finding_id"] for d in decisions})
    violations = {f["id"]: f["violation"] for f in (supabase.table("findings").select("id, violation").in_("id", fids).execute().data or [])} if fids else {}
    rejections: dict = {}
    agreement: dict = {}
    for d in decisions:
        if d["decision"] == "rejected":
            key = d.get("rejection_reason") or "other"
            rejections[key] = rejections.get(key, 0) + 1
        v = violations.get(d["finding_id"], "unknown")
        agreement.setdefault(v, {"confirmed": 0, "rejected": 0, "inconclusive": 0})
        agreement[v][d["decision"]] = agreement[v].get(d["decision"], 0) + 1
    finalized = supabase.table("cases").select("id, claimed_at, finalized_at").eq("status", "finalized").gte("finalized_at", since).execute().data or []
    n_fix = _n(supabase.table("case_corrections").select("id", count="exact").eq("field", "plate").gte("created_at", since).execute())
    processed = supabase.table("videos").select("id, allegation_answer").eq("status", "processed").gte("processed_at", since).execute().data or []
    n_ns = sum(1 for v in processed if v.get("allegation_answer") in ("not_supported", "unobservable"))
    durations = [(_parse(c["finalized_at"]) - _parse(c["claimed_at"])).total_seconds()
                 for c in finalized if _parse(c.get("finalized_at")) and _parse(c.get("claimed_at"))]
    return {"success": True, "data": {
        "days": days,
        "rejections_by_reason": rejections,
        "agreement_by_violation": agreement,
        "plate_misread_rate": round(n_fix / len(finalized), 3) if finalized else 0.0,
        "not_supported_rate": round(n_ns / len(processed), 3) if processed else 0.0,
        "avg_review_seconds": round(sum(durations) / len(durations)) if durations else None,
        "decisions": len(decisions), "finalized_cases": len(finalized),
    }}


@router.get("/escalations")
def list_escalations(status_filter: Optional[str] = Query(default=None, alias="status"), current_user=Depends(admin)):
    q = supabase.table("escalations").select("*")
    if status_filter:
        if status_filter not in ("pending_approval", "approved", "dismissed"):
            raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid status")
        q = q.eq("status", status_filter)
    return {"success": True, "data": q.order("created_at", desc=True).execute().data or []}


def _pending_escalation(esc_id: str) -> dict:
    rows = supabase.table("escalations").select("*").eq("id", esc_id).limit(1).execute().data or []
    if not rows:
        raise api_error(status.HTTP_404_NOT_FOUND, "Escalation not found")
    if rows[0].get("status") != "pending_approval":
        raise api_error(status.HTTP_409_CONFLICT, f"Escalation already {rows[0].get('status')}")
    return rows[0]


def build_escalation_package(plate: str) -> dict:
    """Plate, every confirmed case with evidence (signed 7 days), decisions + reasons, reviewer badge numbers."""
    generated = _iso(datetime.now(timezone.utc))
    history = supabase.table("plate_history").select("*").eq("plate", plate).eq("layer", "confirmed").execute().data or []
    case_ids = list({h["case_id"] for h in history if h.get("case_id")})
    if not case_ids:
        return {"plate": plate, "cases": [], "generated_at": generated}
    cases = supabase.table("cases").select("*").in_("id", case_ids).execute().data or []
    findings = supabase.table("findings").select("*").in_("case_id", case_ids).eq("decision", "confirmed").execute().data or []
    decisions = supabase.table("finding_decisions").select("*").in_("case_id", case_ids).execute().data or []
    evidence = supabase.table("evidence").select("*").in_("case_id", case_ids).execute().data or []
    videos = {v["id"]: v for v in (supabase.table("videos").select("id, detection_video_path, uploaded_at, recording_at, location_text")
                                   .in_("id", list({c["video_id"] for c in cases})).execute().data or [])}
    reviewer_ids = list({c["finalized_by"] for c in cases if c.get("finalized_by")})
    badges = {p["id"]: p.get("badge_number") for p in (supabase.table("profiles").select("id, badge_number").in_("id", reviewer_ids).execute().data or [])} if reviewer_ids else {}
    week = 7 * 24 * 60
    out = []
    for c in cases:
        vid = videos.get(c["video_id"], {})
        out.append({
            "case_id": c["id"], "video_id": c["video_id"], "track_id": c["track_id"], "finalized_at": c.get("finalized_at"),
            "recording_at": vid.get("recording_at"), "location_text": vid.get("location_text"),
            "reviewer_badge": badges.get(c.get("finalized_by")),
            "detection_video": sign_or_503(vid["detection_video_path"], week) if vid.get("detection_video_path") else None,
            "findings": [{
                **{k: f.get(k) for k in ("id", "violation", "ai_result", "tier", "confidence", "decision", "rejection_reason", "note")},
                "decisions": [{k: d.get(k) for k in ("decision", "rejection_reason", "note", "created_at", "cycle")}
                              for d in decisions if d["finding_id"] == f["id"]],
                "evidence": [{"blob_path": e["blob_path"], "frame_index": e.get("frame_index"), "sha256": e.get("sha256"),
                              **sign_or_503(e["blob_path"], week)} for e in evidence if e["finding_id"] == f["id"]],
            } for f in findings if f["case_id"] == c["id"]],
        })
    return {"plate": plate, "cases": out, "generated_at": generated}


@router.post("/escalations/{esc_id}/approve")
def approve_escalation(esc_id: str, current_user=Depends(admin)):
    esc = _pending_escalation(esc_id)
    package = build_escalation_package(esc["plate"])
    update = {"status": "approved", "approved_by": current_user["id"], "approved_at": _iso(datetime.now(timezone.utc)), "package": package}
    res = supabase.table("escalations").update(update).eq("id", esc_id).execute()
    write_audit(current_user, "escalation.approve", "escalation", esc_id,
                before={"status": "pending_approval"}, after={"status": "approved", "plate": esc["plate"], "cases": len(package["cases"])})
    return {"success": True, "data": res.data[0] if res.data else {**esc, **update}}


@router.post("/escalations/{esc_id}/dismiss")
def dismiss_escalation(esc_id: str, body: ReasonRequest, current_user=Depends(admin)):
    esc = _pending_escalation(esc_id)
    update = {"status": "dismissed", "dismissed_by": current_user["id"], "dismissed_reason": body.reason}
    res = supabase.table("escalations").update(update).eq("id", esc_id).execute()
    write_audit(current_user, "escalation.dismiss", "escalation", esc_id,
                before={"status": "pending_approval"}, after={"status": "dismissed"}, reason=body.reason)
    return {"success": True, "data": res.data[0] if res.data else {**esc, **update}}


@router.get("/settings")
def get_settings(current_user=Depends(admin)):
    rows = supabase.table("system_settings").select("key, value, updated_at").in_("key", list(SETTING_KEYS)).execute().data or []
    return {"success": True, "data": {r["key"]: r["value"] for r in rows}}


@router.put("/settings")
def put_settings(body: SettingsRequest, current_user=Depends(admin)):
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if not changes:
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "No settings provided")
    before = {r["key"]: r["value"] for r in (supabase.table("system_settings").select("key, value").in_("key", list(changes)).execute().data or [])}
    now = _iso(datetime.now(timezone.utc))
    supabase.table("system_settings").upsert([
        {"key": k, "value": v, "updated_at": now, "updated_by": current_user["id"]} for k, v in changes.items()
    ]).execute()
    write_audit(current_user, "settings.update", "system", "settings", before=before, after=changes)
    return {"success": True, "data": changes}


def _case(case_id: str) -> dict:
    rows = supabase.table("cases").select("*").eq("id", case_id).limit(1).execute().data or []
    if not rows:
        raise api_error(status.HTTP_404_NOT_FOUND, "Case not found")
    return rows[0]


@router.post("/cases/{case_id}/reopen")
def reopen_case(case_id: str, body: ReasonRequest, current_user=Depends(admin)):
    case = _case(case_id)
    if case.get("status") != "finalized":
        raise api_error(status.HTTP_409_CONFLICT, "Only a finalized case can be reopened")
    update = {"status": "reopened", "cycle": (case.get("cycle") or 1) + 1, "reopened_by": current_user["id"],
              "reopen_reason": body.reason, "claimed_by": None, "claimed_at": None, "previous_status": None}
    res = supabase.table("cases").update(update).eq("id", case_id).execute()
    # new cycle: findings go back to pending; the original decisions stay in finding_decisions
    supabase.table("findings").update({"decision": "pending", "decided_by": None, "decided_at": None,
                                       "rejection_reason": None, "note": None}).eq("case_id", case_id).execute()
    write_audit(current_user, "case.reopen", "case", case_id,
                before={"status": "finalized", "cycle": case.get("cycle")}, after={"status": "reopened", "cycle": update["cycle"]},
                reason=body.reason)
    return {"success": True, "data": res.data[0] if res.data else {**case, **update}}


@router.post("/cases/{case_id}/reassign")
def reassign_case(case_id: str, body: ReassignRequest, current_user=Depends(admin)):
    case = _case(case_id)
    if case.get("status") in ("finalized", "withdrawn"):
        raise api_error(status.HTTP_409_CONFLICT, f"Cannot reassign a {case.get('status')} case")
    target = supabase.table("profiles").select("id, role").eq("id", body.reviewer_id).limit(1).execute().data or []
    if not target or target[0].get("role") not in ("officer", "admin"):
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "reviewer_id must be an officer or admin")
    prev = case.get("previous_status") if case.get("status") == "in_review" else case.get("status")
    update = {"status": "in_review", "previous_status": prev, "claimed_by": body.reviewer_id, "claimed_at": _iso(datetime.now(timezone.utc))}
    res = supabase.table("cases").update(update).eq("id", case_id).execute()
    write_audit(current_user, "case.reassign", "case", case_id,
                before={"claimed_by": case.get("claimed_by"), "status": case.get("status")},
                after={"claimed_by": body.reviewer_id, "status": "in_review"})
    return {"success": True, "data": res.data[0] if res.data else {**case, **update}}
