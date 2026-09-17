"""GET /plates/{plate}/history (API.md §4). Two layers: observed (AI) and confirmed (human)."""
from fastapi import APIRouter, Depends, status

from app.database.supabase import supabase
from app.utils.auth import api_error, require_role
from app.utils.db import PLATE_RE, normalise_plate

router = APIRouter(prefix="/plates", tags=["Plates"])


def plate_status(plate: str) -> dict:
    rows = supabase.table("plate_status").select("*").eq("plate", plate).limit(1).execute().data or []
    return rows[0] if rows else {"plate": plate, "status": "clean", "confirmed_count": 0, "top_tier_confirmed": 0, "observed_count": 0}


@router.get("/{plate}/history")
def plate_history(plate: str, current_user=Depends(require_role("officer", "admin"))):
    plate = normalise_plate(plate) or ""
    if not PLATE_RE.match(plate):
        raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Not a valid Indian plate")
    history = supabase.table("plate_history").select("*").eq("plate", plate).order("created_at", desc=True).execute().data or []
    if current_user["role"] != "admin":
        # reviewers see a plate's history only after deciding one of its cases (docs §2.7 item 27)
        case_ids = [h["case_id"] for h in history if h.get("case_id")]
        mine = supabase.table("cases").select("id").in_("id", case_ids).eq("finalized_by", current_user["id"]).limit(1).execute().data if case_ids else []
        if not mine:
            raise api_error(status.HTTP_403_FORBIDDEN, "History is visible only after you finalize a case on this plate")
    escalations = supabase.table("escalations").select("id, status, threshold_hit, created_at, approved_at").eq("plate", plate).order("created_at", desc=True).execute().data or []
    return {"success": True, "data": {
        "plate": plate,
        "status": plate_status(plate).get("status"),
        "confirmed": [h for h in history if h.get("layer") == "confirmed"],
        "observed": [h for h in history if h.get("layer") == "observed"],
        "escalations": escalations,
    }}
