from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.database.supabase import supabase


security = HTTPBearer()

ROLES = ("citizen", "officer", "admin")


def api_error(status_code: int, message: str) -> HTTPException:
    """Error envelope shared by every route: {"detail": {"success": false, "error": msg}}."""
    return HTTPException(status_code=status_code, detail={"success": False, "error": message})


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    token = credentials.credentials

    try:
        # Verify the Supabase JWT and retrieve its claims.
        response = supabase.auth.get_claims(token)
        claims = (response or {}).get("claims") if response else None
        user_id = claims.get("sub") if claims else None
    except Exception as e:
        print(f"JWT verification failed: {e}")
        raise api_error(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")

    if not user_id:
        raise api_error(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")

    # Role comes from public.profiles ONLY (never from JWT user_metadata).
    try:
        res = supabase.table("profiles").select("*").eq("id", user_id).limit(1).execute()
        profile = res.data[0] if res.data else None
    except Exception as e:
        print(f"Profile lookup failed: {e}")
        profile = None
    if profile is None:
        profile = ensure_profile(user_id, claims)

    role = (profile or {}).get("role") or "citizen"
    if role not in ROLES:
        role = "citizen"

    return {
        "id": user_id,
        "email": claims.get("email") or (profile or {}).get("email"),
        "role": role,
        "profile": profile,
        "claims": claims,
    }


def ensure_profile(user_id: str, claims: dict) -> Optional[dict]:
    """
    Create the public.profiles row for an authenticated user who has none.

    Normally the on_auth_user_created trigger writes it, but accounts created before the
    trigger existed, or whose trigger run failed, sign in with no row: the browser gets a
    406 on its own profile and an admin can never assign them a role. Always 'citizen';
    ON CONFLICT DO NOTHING so a row written concurrently by the trigger is never overwritten.
    Returns the row (or None when the insert is refused), never raises.
    """
    claims = claims or {}
    meta = claims.get("user_metadata") or {}
    email = (claims.get("email") or "").strip().lower() or None
    row = {
        "id": user_id,
        "email": email,
        "full_name": meta.get("full_name") or meta.get("name") or (email.split("@")[0] if email else None),
        "avatar_url": meta.get("avatar_url") or meta.get("picture"),
        "role": "citizen",
    }
    try:
        res = supabase.table("profiles").upsert(row, on_conflict="id", ignore_duplicates=True).execute()
        if res.data:
            print(f"[auth] created missing profile for {user_id}")
            return res.data[0]
        # the trigger got there first: read what it wrote
        again = supabase.table("profiles").select("*").eq("id", user_id).limit(1).execute()
        return again.data[0] if again.data else row
    except Exception as e:
        print(f"[auth] could not create missing profile for {user_id}: {e}")
        return None


def require_role(*roles: str):
    """Dependency factory: 403 unless the caller's profile role is in `roles`."""

    def _dep(user=Depends(get_current_user)):
        if user["role"] not in roles:
            raise api_error(status.HTTP_403_FORBIDDEN, "Insufficient role")
        return user

    return _dep
