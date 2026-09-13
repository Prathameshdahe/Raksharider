from typing import List

from pydantic import BaseModel, Field


class FinalizedEvent(BaseModel):
    tracking_id: str
    alert_type: str
    first_seen: float
    last_seen: float
    observation_count: int
    best_confidence: float
    evidence: List[str] = Field(default_factory=list)