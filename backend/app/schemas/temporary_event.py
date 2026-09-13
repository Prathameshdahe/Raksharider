from typing import List
from pydantic import BaseModel


class TemporaryEvent(BaseModel):
    tracking_id: str
    alert_type: str
    first_seen: float
    last_seen: float
    observation_count: int = 1
    best_confidence: float
    evidence: List[str] = []
    