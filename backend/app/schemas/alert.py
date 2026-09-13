from pydantic import BaseModel
from typing import Optional


class Alert(BaseModel):
    tracking_id: str
    alert_type: str
    timestamp: float
    confidence: float
    evidence: Optional[str] = None