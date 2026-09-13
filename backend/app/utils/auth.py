from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.database.supabase import supabase


security = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    token = credentials.credentials

    try:
        # Verify the Supabase JWT and retrieve its claims.
        response = supabase.auth.get_claims(token)

        if not response or not response.get("claims"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
            )

        claims = response["claims"]

        user_id = claims.get("sub")

        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: missing user ID",
            )

        return {
            "id": user_id,
            "email": claims.get("email"),
            "claims": claims,
        }

    except HTTPException:
        raise

    except Exception as e:
        print(f"JWT verification failed: {e}")

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )