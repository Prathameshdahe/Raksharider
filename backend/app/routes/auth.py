import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, status
from app.database.supabase import supabase
from app.schemas.auth import ProfileUpdateRequest
from app.utils.auth import api_error, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/auth",
    tags=["Authentication"]
)

# Sign-up and sign-in happen in the browser through supabase-js, never here. The shared
# service-role client rebinds its Authorization header to whichever session signs in on it,
# so a single login call on this process would run every later request as that user.


PORTAL = {"citizen": "user", "officer": "reviewer", "admin": "admin"}
NAV = {
    "user": ["upload", "submissions", "alerts", "profile"],
    "reviewer": ["queue", "my_cases", "not_supported_lane", "alerts", "profile"],
    "admin": ["live", "queue", "cases", "escalations", "users", "quality", "audit", "settings", "alerts", "profile"],
}


@router.get("/me")
def get_me(current_user=Depends(get_current_user)):
    """Identity + profile + which portal the frontend should open (role checks stay in the backend)."""
    user_id = current_user["id"]
    portal = PORTAL.get(current_user.get("role"), "user")
    return {
        "user_id": user_id,
        "email": current_user.get("email"),
        "profile": current_user.get("profile") or {
            "id": user_id,
            "email": current_user.get("email"),
            "role": "citizen"
        },
        "portal": portal,
        "nav": NAV[portal],
    }


@router.post("/presence")
def presence(current_user=Depends(get_current_user)):
    """Heartbeat (every 60 s from the frontend) → profiles.last_seen_at; powers the admin "users online" tile."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        supabase.table("profiles").update({"last_seen_at": now}).eq("id", current_user["id"]).execute()
    except Exception as e:
        logger.warning("[auth] presence update failed: %s", e)
    return {"success": True, "data": {"last_seen_at": now}}


@router.put("/profile")
def update_profile(payload: ProfileUpdateRequest, current_user=Depends(get_current_user)):
    """Update profile details for current authenticated user."""
    user_id = current_user["id"]
    # role / badge_number are never self-editable.
    editable = {"full_name", "phone", "avatar_url"}
    update_data = {k: v for k, v in payload.model_dump().items() if v is not None and k in editable}
    if not update_data:
        raise api_error(status.HTTP_400_BAD_REQUEST, "No fields provided to update")

    try:
        res = supabase.table("profiles").update(update_data).eq("id", user_id).execute()
    except Exception as e:
        logger.error("[auth] Update profile failed: %s", e)
        raise api_error(status.HTTP_500_INTERNAL_SERVER_ERROR, "Profile update failed")
    if not res.data:
        # An UPDATE that matched no row is a 200 with [] under PostgREST; echoing the request back
        # would hide a missing profiles row (see ensure_profile / migration 009).
        raise api_error(status.HTTP_404_NOT_FOUND, "Profile not found")
    return {"success": True, "data": res.data[0]}
