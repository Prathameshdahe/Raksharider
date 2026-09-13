from typing import List, Optional

from pydantic import BaseModel


class AIReport(BaseModel):
    run_id: str
    generated_at: str

    status: str
    severity_score: float

    violations_detected: List[str]

    rider_count: int
    helmet_status: str

    number_plate: Optional[str] = None
    plate_read_confidence: float

    evidence_frame_paths: List[str]

    frame_consistency_ratio: float
    avg_yolo_confidence: float
    ocr_agreement_ratio: float

    notes: Optional[str] = None