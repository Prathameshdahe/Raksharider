from typing import Optional
from pydantic import BaseModel


class ProfileUpdateRequest(BaseModel):
    full_name: Optional[str] = None
    phone: Optional[str] = None
    badge_number: Optional[str] = None
    avatar_url: Optional[str] = None
