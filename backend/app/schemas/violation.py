from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class ViolationRecord(BaseModel):
    report_id: int
    violation_type: str
    confidence: float
    created_at: Optional[datetime] = None