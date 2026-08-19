"""
pipeline/verification.py
------------------------
Aggregates per-frame rule verdicts into a single clip-level result — v2.

Severity score:
    severity = W_CONSISTENCY * frame_consistency_ratio
             + W_YOLO_CONF   * avg_yolo_confidence
             + W_OCR         * ocr_agreement_ratio

New in v2:
  - All new violation types aggregated (phone_usage, wheelie, erratic_driving,
    missing_plate, signal_violation)
  - vehicle_type majority vote
  - VLM tiebreaker integration (fires only on needs_review)
  - Extended VerificationResult with new fields
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional

from pipeline.rules import FrameVerdict

logger = logging.getLogger(__name__)

W_CONSISTENCY: float = 0.5
W_YOLO_CONF:   float = 0.3
W_OCR:         float = 0.2
assert abs(W_CONSISTENCY + W_YOLO_CONF + W_OCR - 1.0) < 1e-9

TIER_AUTO_FLAGGED:    float = 0.85
TIER_NEEDS_REVIEW:    float = 0.40   # lowered so real violations with partial frames still trigger review
CONSISTENCY_THRESHOLD: float = 0.70

# Fraction of frames a heuristic violation must appear in to be "confirmed"
# Applies to noisy signals: wheelie, phone, erratic driving
HEURISTIC_CONSISTENCY_THRESHOLD: float = 0.05   # 5% of frames — catches even rare events

# Minimum absolute number of frames for safety-critical violations.
# Rationale: triple_riding + no_helmet are unambiguous — even 1 frame is sufficient
# (vs. percentage thresholds that require 12+ frames in a 41-frame clip)
MIN_SAFETY_FRAMES: int = 1

StatusTier = Literal["auto_flagged", "needs_review", "insufficient_evidence"]


@dataclass
class VerificationResult:
    """Aggregated clip-level verdict — v2."""
    status:                    StatusTier
    severity_score:            float
    violations_detected:       List[str]
    rider_count:               int
    helmet_status:             str
    frame_consistency_ratio:   float
    avg_yolo_confidence:       float
    ocr_agreement_ratio:       float
    evidence_frame_timestamps: List[float]

    # New v2 fields
    vehicle_type:              str         = "unknown"
    phone_usage:               bool        = False
    wheelie_detected:          bool        = False
    plate_flag:                str         = "missing"
    signal_violation:          bool        = False
    erratic_driving:           bool        = False
    vlm_reasoning:             str         = ""
    total_track_ids:           int         = 0
    front_cam:                 Dict        = field(default_factory=dict)


def aggregate_verdicts(
    frame_verdicts:        List[FrameVerdict],
    ocr_agreement_ratio:   float = 0.0,
    top_n_evidence_frames: int   = 3,
    total_track_ids:       int   = 0,
    # VLM integration
    vlm_enabled:           bool  = False,
    evidence_frames_bgr:   Optional[list] = None,   # list of np.ndarray
    evidence_timestamps:   Optional[List[float]] = None,
) -> VerificationResult:
    """
    Aggregate per-frame verdicts into one clip-level VerificationResult.

    ocr_agreement_ratio  : fraction of valid OCR reads that agree on plate string
    total_track_ids      : total unique track IDs from tracker (for report)
    vlm_enabled          : if True and status==needs_review, call VLM tiebreaker
    evidence_frames_bgr  : BGR frames to pass to VLM (if enabled)
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
            total_track_ids=total_track_ids,
        )

    total = len(frame_verdicts)

    # ── Helmet ────────────────────────────────────────────────────────────────
    helmet_counter = Counter(fv.helmet_status for fv in frame_verdicts)
    majority_helmet, majority_helmet_n = helmet_counter.most_common(1)[0]
    helmet_consistency  = majority_helmet_n / total
    confirmed_helmet    = majority_helmet if helmet_consistency >= CONSISTENCY_THRESHOLD else "unclear"
    logger.info("Helmet majority='%s' consistency=%.2f -> confirmed='%s'",
                majority_helmet, helmet_consistency, confirmed_helmet)

    # ── Rider count ───────────────────────────────────────────────────────────
    # Use MAX across all frames, not majority vote.
    # Rationale: triple-riding or helmet violations happen in a subset of frames;
    # requiring 70% consistency would filter out almost every real violation.
    confirmed_riders = max(fv.rider_count for fv in frame_verdicts)

    # For consistency ratio, still use the helmet majority (most reliable signal)
    rider_frames_nonzero = sum(1 for fv in frame_verdicts if fv.rider_count > 0)
    rider_consistency = rider_frames_nonzero / total if total else 0.0

    frame_consistency_ratio = max(helmet_consistency, rider_consistency)

    # ── Vehicle type — subject-first resolution ───────────────────────────────
    # Priority (highest wins):
    #   1. "motorcycle" if ANY frame saw rider_count > 0 AND vehicle_type == motorcycle
    #      → the motorcycle WAS the subject, regardless of background car/truck count
    #   2. Majority vote among violation frames (rider_count > 0 or violations fired)
    #   3. Majority vote across all frames (fallback)
    moto_rider_frames = [
        fv for fv in frame_verdicts
        if fv.rider_count > 0 and fv.vehicle_type == "motorcycle"
    ]
    if moto_rider_frames:
        dominant_vehicle = "motorcycle"
    else:
        violation_frames = [fv for fv in frame_verdicts if fv.rider_count > 0 or fv.violations]
        vote_pool = violation_frames if violation_frames else frame_verdicts
        vehicle_counter = Counter(fv.vehicle_type for fv in vote_pool)
        dominant_vehicle = vehicle_counter.most_common(1)[0][0]

    # ── Heuristic violations (lower consistency threshold) ────────────────────
    phone_frames    = sum(1 for fv in frame_verdicts if fv.phone_usage)
    wheelie_frames  = sum(1 for fv in frame_verdicts if fv.wheelie)
    erratic_frames  = sum(1 for fv in frame_verdicts if fv.erratic_track_ids)

    phone_confirmed   = phone_frames  / total >= HEURISTIC_CONSISTENCY_THRESHOLD
    wheelie_confirmed = wheelie_frames / total >= HEURISTIC_CONSISTENCY_THRESHOLD
    erratic_confirmed = erratic_frames / total >= HEURISTIC_CONSISTENCY_THRESHOLD

    # Plate flag — most severe flag wins
    plate_flags = [fv.plate_flag for fv in frame_verdicts]
    plate_priority = ["missing", "low_confidence", "invalid_format", "ok"]
    plate_flag_final = min(plate_flags, key=lambda f: plate_priority.index(f)
                          if f in plate_priority else 99)

    # Signal violation — any frame with red + vehicle present
    signal_violation = any(
        fv.signal_state == "red" and fv.rider_count > 0
        for fv in frame_verdicts
    )

    # ── Violations list ───────────────────────────────────────────────────────
    violations: List[str] = []

    # no_helmet: fires if confirmed at clip level OR seen in MIN_SAFETY_FRAMES+ frames
    no_helmet_frames = sum(1 for fv in frame_verdicts if "no_helmet" in fv.violations)
    if confirmed_helmet == "no_helmet" or no_helmet_frames >= MIN_SAFETY_FRAMES:
        violations.append("no_helmet")

    # triple_riding: fires on MIN_SAFETY_FRAMES — even 1 frame of 3 people on a bike counts
    triple_count = sum(1 for fv in frame_verdicts if "triple_riding" in fv.violations)
    if triple_count >= MIN_SAFETY_FRAMES:
        violations.append("triple_riding")
    if phone_confirmed:
        violations.append("phone_usage")
    if wheelie_confirmed:
        violations.append("wheelie")
    if erratic_confirmed:
        violations.append("erratic_driving")
    if plate_flag_final == "missing" and dominant_vehicle != "unknown":
        violations.append("missing_plate")
    if signal_violation:
        violations.append("signal_violation")

    # ── Severity ──────────────────────────────────────────────────────────────
    # Exclude zero-confidence frames (no relevant detections that frame) from avg
    conf_values = [fv.avg_detection_confidence for fv in frame_verdicts
                   if fv.avg_detection_confidence > 0.0]
    avg_yolo_confidence = sum(conf_values) / len(conf_values) if conf_values else 0.0
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

    logger.info("Severity=%.3f -> status='%s' (violations=%s)",
                severity_score, status, violations)

    # ── Evidence frames ───────────────────────────────────────────────────────
    sorted_by_conf = sorted(frame_verdicts,
                            key=lambda fv: fv.avg_detection_confidence,
                            reverse=True)
    ev_timestamps = [fv.timestamp for fv in sorted_by_conf[:top_n_evidence_frames]]

    # ── VLM tiebreaker ────────────────────────────────────────────────────────
    vlm_reasoning = ""
    if vlm_enabled and status == "needs_review" and evidence_frames_bgr and evidence_timestamps:
        from pipeline.vlm import vlm_tiebreaker

        # Pick the PEAK violation frame — highest rider count in the clip.
        # Fallback to first evidence frame if timestamps don't align.
        peak_frame_bgr = evidence_frames_bgr[0]
        if frame_verdicts and len(evidence_timestamps) > 0:
            peak_fv = max(frame_verdicts, key=lambda fv: fv.rider_count)
            # Find the evidence frame whose timestamp is closest to the peak frame
            if evidence_timestamps:
                closest_idx = min(
                    range(len(evidence_timestamps)),
                    key=lambda i: abs(evidence_timestamps[i] - peak_fv.timestamp),
                )
                if closest_idx < len(evidence_frames_bgr):
                    peak_frame_bgr = evidence_frames_bgr[closest_idx]
                    logger.info(
                        "VLM will use peak-violation frame at t=%.2fs (rider_count=%d)",
                        peak_fv.timestamp, peak_fv.rider_count,
                    )

        summary = {
            "violations_detected": violations,
            "helmet_status":       confirmed_helmet,
            "rider_count":         confirmed_riders,
            "plate":               plate_flag_final,
            "severity_score":      severity_score,
            "frame_consistency":   frame_consistency_ratio,
            "vehicle_type":        dominant_vehicle,
        }
        new_status, vlm_reasoning = vlm_tiebreaker(
            evidence_frame_bgr=peak_frame_bgr,
            structured_summary=summary,
            original_status=status,
        )
        if new_status != status:
            logger.info("VLM changed status: %s → %s", status, new_status)
            # ── Automatic Hard-Case Mining ────────────────────────────────────
            try:
                from pipeline.hard_case_miner import log_hard_case
                peak_time = peak_fv.timestamp if (frame_verdicts and 'peak_fv' in locals()) else 0.0
                log_hard_case(
                    video_source=getattr(frame_verdicts[0], "video_source", "clip") if frame_verdicts else "clip",
                    timestamp=peak_time,
                    frame_bgr=peak_frame_bgr,
                    rule_status=status,
                    rule_violations=violations,
                    vlm_verdict=new_status,
                    vlm_reasoning=vlm_reasoning,
                    trigger_reason="vlm_disagreement",
                    extra_metadata=summary,
                )
            except Exception as miner_exc:
                logger.debug("Hard-case miner logging skipped: %s", miner_exc)
            status = new_status


    return VerificationResult(
        status=status,
        severity_score=round(severity_score, 4),
        violations_detected=violations,
        rider_count=confirmed_riders,
        helmet_status=confirmed_helmet,
        frame_consistency_ratio=round(frame_consistency_ratio, 4),
        avg_yolo_confidence=round(avg_yolo_confidence, 4),
        ocr_agreement_ratio=round(ocr_agreement_ratio, 4),
        evidence_frame_timestamps=sorted(ev_timestamps),
        vehicle_type=dominant_vehicle,
        phone_usage=phone_confirmed,
        wheelie_detected=wheelie_confirmed,
        plate_flag=plate_flag_final,
        signal_violation=signal_violation,
        erratic_driving=erratic_confirmed,
        vlm_reasoning=vlm_reasoning,
        total_track_ids=total_track_ids,
    )
