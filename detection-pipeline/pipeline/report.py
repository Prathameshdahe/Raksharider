"""
pipeline/report.py
------------------
Assembles the final JSON report and saves evidence frame images.

The report is advisory only -- it is not a fine or enforcement decision.
Field names are intentionally worded to reflect this (e.g. "violations_detected",
not "violations_confirmed").
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2

from pipeline.frame_extractor import Frame
from pipeline.verification import VerificationResult

logger = logging.getLogger(__name__)

EVIDENCE_OUTPUT_DIR: Path = Path(__file__).parent / "evidence_output"


def build_report(
    verification_result: VerificationResult,
    number_plate: Optional[str],
    plate_read_confidence: float,
    source_frames: List[Frame],
    notes: str = "",
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build the final JSON-serialisable report dict and save evidence frames.

    source_frames is the full list of extracted frames; the subset used as
    evidence is determined by verification_result.evidence_frame_timestamps.
    run_id is auto-generated if not provided.
    """
    if run_id is None:
        run_id = str(uuid.uuid4())[:8]

    vr = verification_result
    evidence_paths = _save_evidence_frames(source_frames, vr.evidence_frame_timestamps, run_id)

    if not notes:
        caveats: List[str] = []
        if number_plate is None:
            caveats.append("Plate not readable in any frame.")
        if vr.helmet_status == "unclear":
            caveats.append("Helmet status ambiguous -- insufficient evidence in this clip.")
        if vr.frame_consistency_ratio < 0.5:
            caveats.append("Low frame consistency; recommend a longer or clearer clip.")
        notes = " | ".join(caveats) if caveats else ""

    report: Dict[str, Any] = {
        "_disclaimer": (
            "Automated recommendation for human review. "
            "Not a final enforcement or fine decision."
        ),
        "run_id":        run_id,
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "status":                  vr.status,
        "severity_score":          vr.severity_score,
        "violations_detected":     vr.violations_detected,
        "rider_count":             vr.rider_count,
        "helmet_status":           vr.helmet_status,
        "number_plate":            number_plate,
        "plate_read_confidence":   plate_read_confidence,
        "evidence_frame_timestamps": vr.evidence_frame_timestamps,
        "evidence_frame_paths":    evidence_paths,
        "frame_consistency_ratio": vr.frame_consistency_ratio,
        "avg_yolo_confidence":     vr.avg_yolo_confidence,
        "ocr_agreement_ratio":     vr.ocr_agreement_ratio,
        "notes":                   notes,
    }

    logger.info(
        "Report assembled: run_id=%s status=%s severity=%.3f violations=%s",
        run_id, vr.status, vr.severity_score, vr.violations_detected,
    )
    return report


def report_to_json(report: Dict[str, Any], indent: int = 2) -> str:
    """Serialise report dict to a pretty-printed JSON string."""
    return json.dumps(report, indent=indent, ensure_ascii=False, default=str)


def _save_evidence_frames(
    frames: List[Frame],
    target_timestamps: List[float],
    run_id: str,
) -> List[str]:
    """
    Save the frame closest to each target timestamp as a JPEG.
    Returns a list of absolute file paths.
    """
    if not target_timestamps:
        return []

    out_dir = EVIDENCE_OUTPUT_DIR / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    saved: List[str] = []
    for ts in target_timestamps:
        closest = min(frames, key=lambda f: abs(f.timestamp - ts))
        path = out_dir / f"evidence_t{ts:.3f}s.jpg"
        if cv2.imwrite(str(path), closest.image):
            saved.append(str(path.resolve()))
            logger.info("Evidence frame saved: %s", path)
        else:
            logger.warning("Failed to write evidence frame: %s", path)

    return saved
