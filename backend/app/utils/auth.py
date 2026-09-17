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


def require_role(*roles: str):
    """Dependency factory: 403 unless the caller's profile role is in `roles`."""

    def _dep(user=Depends(get_current_user)):
        if user["role"] not in roles:
            raise api_error(status.HTTP_403_FORBIDDEN, "Insufficient role")
        return user

    return _dep
