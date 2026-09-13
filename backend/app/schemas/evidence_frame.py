
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class EvidenceFrameRecord(BaseModel):
    report_id: int
    storage_url: str
    frame_timestamp: float
    created_at: Optional[datetime] = None
    