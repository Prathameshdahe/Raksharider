from datetime import datetime
from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field

VehicleType = Literal["two_wheeler", "four_wheeler"]


class UploadInitRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str
    vehicle_type: VehicleType
    size_bytes: int = Field(gt=0)


class UploadCompleteRequest(BaseModel):
    video_id: str


class DeleteVideoRequest(BaseModel):
    reason: Optional[str] = None


class ReviewDecisionRequest(BaseModel):
    decision: Literal["confirmed", "rejected"]
    reason: str = Field(min_length=5, max_length=2000)
    corrected_plate: Optional[str] = Field(default=None, max_length=32)
    corrected_vehicle_type: Optional[VehicleType] = None
    violations: Optional[List[str]] = None
    override: bool = False


class RoleChangeRequest(BaseModel):
    role: Literal["citizen", "officer", "admin"]
    reason: str = Field(min_length=5, max_length=2000)


class PauseRequest(BaseModel):
    paused: bool


# ── v3 bodies (API.md) ──────────────────────────────────────────────────────

class UploadInitV3Request(UploadInitRequest):
    declared_violation: Optional[str] = Field(default=None, max_length=64)
    claimed_plate: Optional[str] = Field(default=None, max_length=20)
    note: str = Field(min_length=10, max_length=400)
    recording_at: Optional[datetime] = None
    location_text: Optional[str] = Field(default=None, max_length=200)
    consent: Literal[True]


class FindingDecisionRequest(BaseModel):
    decision: Literal["confirmed", "rejected", "inconclusive"]
    rejection_reason: Optional[str] = Field(default=None, max_length=64)
    note: Optional[str] = Field(default=None, max_length=2000)


class CorrectionRequest(BaseModel):
    field: Literal["plate", "vehicle_type", "violation_label", "evidence"]
    finding_id: Optional[str] = Field(default=None, max_length=64)
    # an evidence correction selects frames: a list is legitimate there, stored as JSON text in the append-only row
    ai_value: Optional[Union[str, List[str]]] = Field(default=None, max_length=500)
    corrected_value: Optional[Union[str, List[str]]] = Field(default=None, max_length=500)
    reason: Optional[str] = Field(default=None, max_length=2000)


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=5, max_length=2000)


class ReassignRequest(BaseModel):
    reviewer_id: str


class RoleChangeV3Request(RoleChangeRequest):
    verified_via: Optional[str] = Field(default=None, max_length=200)


class SettingsRequest(BaseModel):
    max_uploads_per_day: Optional[int] = Field(default=None, ge=1, le=1000)
    escalation_threshold_any: Optional[int] = Field(default=None, ge=1, le=100)
    escalation_threshold_top: Optional[int] = Field(default=None, ge=1, le=100)
    retention_days: Optional[int] = Field(default=None, ge=1, le=3650)
    worker_paused: Optional[bool] = None
    # stale-lease window for claim_next_video(); below ~10 min a slow clip is reaped mid-run
    worker_lease_minutes: Optional[int] = Field(default=None, ge=10, le=1440)


class WithdrawRequest(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=2000)


class WithdrawalAnswerRequest(BaseModel):
    accept: bool
    reason: Optional[str] = Field(default=None, max_length=2000)
