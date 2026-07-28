"""
pipeline/verification.py
------------------------
Aggregates per-frame rule verdicts into a single clip-level result.

Severity score:
    severity = W_CONSISTENCY * frame_consistency_ratio
             + W_YOLO_CONF   * avg_yolo_confidence
             + W_OCR         * ocr_agreement_ratio

A verdict field (helmet_status, rider_count) is only confirmed when
CONSISTENCY_THRESHOLD of frames agree; below that it becomes "unclear"/0
to avoid over-reporting on noisy single-frame detections.

Status tiers:
    >= TIER_AUTO_FLAGGED   -> "auto_flagged"
    >= TIER_NEEDS_REVIEW   -> "needs_review"
    <  TIER_NEEDS_REVIEW   -> "insufficient_evidence"
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from typing import List, Literal

from pipeline.rules import FrameVerdict

logger = logging.getLogger(__name__)

W_CONSISTENCY: float = 0.5
W_YOLO_CONF:   float = 0.3
W_OCR:         float = 0.2

assert abs(W_CONSISTENCY + W_YOLO_CONF + W_OCR - 1.0) < 1e-9, "Weights must sum to 1.0"

TIER_AUTO_FLAGGED:   float = 0.85
TIER_NEEDS_REVIEW:   float = 0.50
CONSISTENCY_THRESHOLD: float = 0.70

StatusTier = Literal["auto_flagged", "needs_review", "insufficient_evidence"]


@dataclass
class VerificationResult:
    """Aggregated clip-level verdict."""
    status: StatusTier
    severity_score: float
    violations_detected: List[str]
    rider_count: int
    helmet_status: str
    frame_consistency_ratio: float
    avg_yolo_confidence: float
    ocr_agreement_ratio: float
    evidence_frame_timestamps: List[float]


def aggregate_verdicts(
    frame_verdicts: List[FrameVerdict],
    ocr_agreement_ratio: float = 0.0,
    top_n_evidence_frames: int = 2,
) -> VerificationResult:
    """
    Aggregate per-frame verdicts into one clip-level VerificationResult.

    ocr_agreement_ratio: fraction of valid OCR reads that agree on the plate
                         string; pass 0.0 if no plate was detected.
    top_n_evidence_frames: number of best-confidence frame timestamps to include.
    """
    if not frame_verdicts:
        logger.warning("aggregate_verdicts called with empty frame list.")
        return VerificationResult(
            status="insufficient_evidence",
            severity_score=0.0,
            violations_detected=[],
            rider_count=0,
            helmet_status="unclear",
            frame_consistency_ratio=0.0,
            avg_yolo_confidence=0.0,
            ocr_agreement_ratio=0.0,
            evidence_frame_timestamps=[],
        )

    total = len(frame_verdicts)

    helmet_counter = Counter(fv.helmet_status for fv in frame_verdicts)
    majority_helmet, majority_helmet_n = helmet_counter.most_common(1)[0]
    helmet_consistency = majority_helmet_n / total
    confirmed_helmet = majority_helmet if helmet_consistency >= CONSISTENCY_THRESHOLD else "unclear"
    logger.info(
        "Helmet majority='%s' consistency=%.2f -> confirmed='%s'",
        majority_helmet, helmet_consistency, confirmed_helmet,
    )

    rider_counter = Counter(fv.rider_count for fv in frame_verdicts)
    majority_riders, majority_riders_n = rider_counter.most_common(1)[0]
    rider_consistency = majority_riders_n / total
    confirmed_riders = majority_riders if rider_consistency >= CONSISTENCY_THRESHOLD else 0

    frame_consistency_ratio = max(helmet_consistency, rider_consistency)

    violations: List[str] = []
    if confirmed_helmet == "no_helmet":
        violations.append("no_helmet")
    triple_count = sum(1 for fv in frame_verdicts if "triple_riding" in fv.violations)
    if triple_count / total >= CONSISTENCY_THRESHOLD:
        violations.append("triple_riding")

    avg_yolo_confidence = sum(fv.avg_detection_confidence for fv in frame_verdicts) / total

    severity_score = min(1.0, max(0.0,
        W_CONSISTENCY * frame_consistency_ratio
        + W_YOLO_CONF * avg_yolo_confidence
        + W_OCR       * ocr_agreement_ratio
    ))

    if severity_score >= TIER_AUTO_FLAGGED:
        status: StatusTier = "auto_flagged"
    elif severity_score >= TIER_NEEDS_REVIEW:
        status = "needs_review"
    else:
        status = "insufficient_evidence"

    logger.info("Severity=%.3f -> status='%s' (violations=%s)", severity_score, status, violations)

    sorted_by_conf = sorted(frame_verdicts, key=lambda fv: fv.avg_detection_confidence, reverse=True)
    evidence_timestamps = [fv.timestamp for fv in sorted_by_conf[:top_n_evidence_frames]]

    return VerificationResult(
        status=status,
        severity_score=round(severity_score, 4),
        violations_detected=violations,
        rider_count=confirmed_riders,
        helmet_status=confirmed_helmet,
        frame_consistency_ratio=round(frame_consistency_ratio, 4),
        avg_yolo_confidence=round(avg_yolo_confidence, 4),
        ocr_agreement_ratio=round(ocr_agreement_ratio, 4),
        evidence_frame_timestamps=sorted(evidence_timestamps),
    )
