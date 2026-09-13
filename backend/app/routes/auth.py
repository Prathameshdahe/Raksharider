import logging
from fastapi import APIRouter, Depends, HTTPException, status
from app.database.supabase import supabase
from app.schemas.auth import SignUpRequest, LoginRequest, ProfileUpdateRequest, ProfileResponse
from app.utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/auth",
    tags=["Authentication"]
)


@router.post("/signup")
def sign_up(payload: SignUpRequest):
    """Register a new user account and provision their profile."""
    try:
        # 1. Register with Supabase Auth
        meta_data = {
            "full_name": payload.full_name,
            "role": payload.role or "citizen",
            "badge_number": payload.badge_number,
            "phone": payload.phone,
        }
        user = None
        session_data = None
        
        # Try admin create_user first (auto-confirms email so user can login immediately)
        try:
            admin_res = supabase.auth.admin.create_user({
                "email": payload.email,
                "password": payload.password,
                "email_confirm": True,
                "user_metadata": meta_data
            })
            if admin_res and hasattr(admin_res, 'user') and admin_res.user:
                user = admin_res.user
        except Exception as ae:
            logger.info("[auth] Admin create_user fallback: %s", ae)

        if not user:
            # Fallback to standard sign_up
            res = supabase.auth.sign_up({
                "email": payload.email,
                "password": payload.password,
                "options": {
                    "data": meta_data
                }
            })
            user = res.user
            if res.session:
                session_data = {
                    "access_token": res.session.access_token,
                    "refresh_token": res.session.refresh_token,
                    "token_type": "bearer",
                }

        if not user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Registration failed. Check your email or password requirements."
            )

        user_id = str(user.id)

        # 2. Ensure profile exists in public.profiles (upsert)
        profile_data = {
            "id": user_id,
            "email": payload.email,
            "full_name": payload.full_name or payload.email.split("@")[0],
            "role": payload.role or "citizen",
            "badge_number": payload.badge_number,
            "phone": payload.phone,
        }
        try:
            supabase.table("profiles").upsert(profile_data).execute()
        except Exception as pe:
            logger.warning("[auth] Upsert fallback note: %s", pe)

        return {
            "message": "User registered successfully",
            "user": {
                "id": user_id,
                "email": user.email or payload.email,
            },
            "profile": profile_data,
            "session": session_data,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("[auth] Sign up error: %s", e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.post("/login")
def login(payload: LoginRequest):
    """Authenticate with email & password and retrieve session & profile."""
    try:
        res = supabase.auth.sign_in_with_password({
            "email": payload.email,
            "password": payload.password,
        })

        if not res.user or not res.session:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password."
            )

        user_id = str(res.user.id)

        # Fetch profile
        profile = None
        try:
            p_res = supabase.table("profiles").select("*").eq("id", user_id).execute()
            if p_res.data and len(p_res.data) > 0:
                profile = p_res.data[0]
        except Exception as pe:
            logger.warning("[auth] Profile fetch warning: %s", pe)

        if not profile:
            # Auto-provision if missing
            profile = {
                "id": user_id,
                "email": res.user.email,
                "full_name": (res.user.user_metadata or {}).get("full_name") or res.user.email.split("@")[0],
                "role": (res.user.user_metadata or {}).get("role", "citizen"),
                "badge_number": (res.user.user_metadata or {}).get("badge_number"),
            }
            try:
                supabase.table("profiles").upsert(profile).execute()
            except Exception:
                pass

        return {
            "access_token": res.session.access_token,
            "refresh_token": res.session.refresh_token,
            "token_type": "bearer",
            "user": {
                "id": user_id,
                "email": res.user.email,
            },
            "profile": profile,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("[auth] Login error: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e)
        )


@router.get("/me")
def get_me(current_user=Depends(get_current_user)):
    """Return authenticated user identity and profile."""
    user_id = current_user["id"]
    profile = None
    try:
        p_res = supabase.table("profiles").select("*").eq("id", user_id).execute()
        if p_res.data and len(p_res.data) > 0:
            profile = p_res.data[0]
    except Exception as e:
        logger.warning("[auth] Error fetching profile: %s", e)

    return {
        "user_id": user_id,
        "email": current_user.get("email"),
        "profile": profile or {
            "id": user_id,
            "email": current_user.get("email"),
            "role": "citizen"
        }
    }


@router.put("/profile")
def update_profile(payload: ProfileUpdateRequest, current_user=Depends(get_current_user)):
    """Update profile details for current authenticated user."""
    user_id = current_user["id"]
    update_data = {k: v for k, v in payload.dict().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    try:
        res = supabase.table("profiles").update(update_data).eq("id", user_id).execute()
        return {
            "message": "Profile updated successfully",
            "profile": res.data[0] if res.data else update_data
        }
    except Exception as e:
        logger.error("[auth] Update profile failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/logout")
def logout(current_user=Depends(get_current_user)):
    """Logout endpoint."""
    try:
        supabase.auth.sign_out()
    except Exception:
        pass
    return {"message": "Logged out successfully"}