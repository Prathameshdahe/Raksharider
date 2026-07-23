"""
pipeline/verification.py
────────────────────────
Multi-frame consistency scoring and severity classification.

This is the reliability layer that converts noisy per-frame rule verdicts
into a single trustworthy verdict for the whole video clip.

Severity formula
----------------
    severity = W_CONSISTENCY * frame_consistency_ratio
             + W_YOLO_CONF   * avg_yolo_confidence
             + W_OCR         * ocr_agreement_ratio

The three weights must sum to 1.0.  They are named constants so they can
be tuned without hunting through arithmetic.

Severity tiers
--------------
    >= TIER_AUTO_FLAGGED   -> "auto_flagged"         (ready for review queue)
    >= TIER_NEEDS_REVIEW   -> "needs_review"          (human should look closer)
    <  TIER_NEEDS_REVIEW   -> "insufficient_evidence" (don't treat as violation)

Consistency confirmation
------------------------
A verdict (helmet_status OR rider_count) is only "confirmed" if the ratio
of frames agreeing with the majority reaches CONSISTENCY_THRESHOLD.  Below
that, the field is set to "unclear" / 0 so we don't over-report.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import List, Literal, Optional, Tuple

from pipeline.rules import FrameVerdict

logger = logging.getLogger(__name__)

# ── Severity weight constants (must sum to 1.0) ───────────────────────────────
W_CONSISTENCY: float = 0.5   # multi-frame agreement ratio
W_YOLO_CONF:   float = 0.3   # average YOLO detection confidence
W_OCR:         float = 0.2   # OCR plate-string agreement ratio

assert abs(W_CONSISTENCY + W_YOLO_CONF + W_OCR - 1.0) < 1e-9, \
    "Severity weights must sum to 1.0"

# ── Severity tier thresholds ──────────────────────────────────────────────────
TIER_AUTO_FLAGGED:  float = 0.85
TIER_NEEDS_REVIEW:  float = 0.50

# ── Consistency threshold ─────────────────────────────────────────────────────
# A verdict is "confirmed" only when this fraction of frames agree.
CONSISTENCY_THRESHOLD: float = 0.70

StatusTier = Literal["auto_flagged", "needs_review", "insufficient_evidence"]


@dataclass
class VerificationResult:
    """Aggregated, multi-frame verdict for the entire clip."""

    status: StatusTier
    severity_score: float
    violations_detected: List[str]
    rider_count: int
    helmet_status: str             # "helmet" | "no_helmet" | "unclear"
    frame_consistency_ratio: float
    avg_yolo_confidence: float
    ocr_agreement_ratio: float
    evidence_frame_timestamps: List[float]  # timestamps of best-confidence frames


# ── Public API ────────────────────────────────────────────────────────────────

def aggregate_verdicts(
    frame_verdicts: List[FrameVerdict],
    ocr_agreement_ratio: float = 0.0,
    top_n_evidence_frames: int = 2,
) -> VerificationResult:
    """
    Aggregate per-frame verdicts into one clip-level result.

    Parameters
    ----------
    frame_verdicts:
        Ordered list of FrameVerdict objects from rules.apply_rules_to_all().
    ocr_agreement_ratio:
        Fraction of valid OCR reads agreeing with the majority plate string.
        Pass 0.0 if no plate was detected at all.
    top_n_evidence_frames:
        How many evidence frame timestamps to include (highest YOLO conf).

    Returns
    -------
    VerificationResult
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

    total_frames = len(frame_verdicts)

    # ── Helmet status majority ────────────────────────────────────────────
    helmet_counter: Counter = Counter(fv.helmet_status for fv in frame_verdicts)
    majority_helmet, majority_helmet_count = helmet_counter.most_common(1)[0]
    helmet_consistency = majority_helmet_count / total_frames

    confirmed_helmet_status = (
        majority_helmet
        if helmet_consistency >= CONSISTENCY_THRESHOLD
        else "unclear"
    )

    logger.info(
        "Helmet status majority='%s' consistency=%.2f (threshold=%.2f) -> confirmed='%s'",
        majority_helmet, helmet_consistency, CONSISTENCY_THRESHOLD, confirmed_helmet_status,
    )

    # ── Rider count majority ──────────────────────────────────────────────
    rider_counter: Counter = Counter(fv.rider_count for fv in frame_verdicts)
    majority_rider_count, majority_rider_count_n = rider_counter.most_common(1)[0]
    rider_consistency = majority_rider_count_n / total_frames

    confirmed_rider_count = (
        majority_rider_count
        if rider_consistency >= CONSISTENCY_THRESHOLD
        else 0  # don't claim a rider count we're not confident about
    )

    # Use the worse (higher) of the two consistency ratios for overall scoring
    frame_consistency_ratio = max(helmet_consistency, rider_consistency)

    # ── Violations list ───────────────────────────────────────────────────
    violations: List[str] = []
    if confirmed_helmet_status == "no_helmet":
        violations.append("no_helmet")

    # triple_riding appeared in at least CONSISTENCY_THRESHOLD of frames?
    triple_count = sum(
        1 for fv in frame_verdicts if "triple_riding" in fv.violations
    )
    if triple_count / total_frames >= CONSISTENCY_THRESHOLD:
        violations.append("triple_riding")

    # ── Average YOLO confidence ───────────────────────────────────────────
    avg_yolo_confidence = sum(
        fv.avg_detection_confidence for fv in frame_verdicts
    ) / total_frames

    # ── Severity score ────────────────────────────────────────────────────
    severity_score = (
        W_CONSISTENCY * frame_consistency_ratio
        + W_YOLO_CONF   * avg_yolo_confidence
        + W_OCR         * ocr_agreement_ratio
    )
    severity_score = min(1.0, max(0.0, severity_score))  # clamp to [0, 1]

    # ── Status tier ───────────────────────────────────────────────────────
    if severity_score >= TIER_AUTO_FLAGGED:
        status: StatusTier = "auto_flagged"
    elif severity_score >= TIER_NEEDS_REVIEW:
        status = "needs_review"
    else:
        status = "insufficient_evidence"

    logger.info(
        "Severity score=%.3f -> status='%s' (violations=%s)",
        severity_score, status, violations,
    )

    # ── Best evidence frames ──────────────────────────────────────────────
    sorted_by_conf = sorted(
        frame_verdicts, key=lambda fv: fv.avg_detection_confidence, reverse=True
    )
    evidence_timestamps = [
        fv.timestamp for fv in sorted_by_conf[:top_n_evidence_frames]
    ]

    return VerificationResult(
        status=status,
        severity_score=round(severity_score, 4),
        violations_detected=violations,
        rider_count=confirmed_rider_count,
        helmet_status=confirmed_helmet_status,
        frame_consistency_ratio=round(frame_consistency_ratio, 4),
        avg_yolo_confidence=round(avg_yolo_confidence, 4),
        ocr_agreement_ratio=round(ocr_agreement_ratio, 4),
        evidence_frame_timestamps=sorted(evidence_timestamps),
    )
