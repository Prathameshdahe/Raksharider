"""
pipeline/report.py
──────────────────
Assembles the final structured JSON report and saves evidence frames.

⚠️  IMPORTANT — naming / intent note
This report is a RECOMMENDATION for a human reviewer.  It is NOT a final
fine or enforcement decision.  Field names have been chosen to make that
explicit (e.g. `violations_detected`, not `violations_confirmed`; `status`
is advisory).

Evidence frames
───────────────
The 1–2 best-confidence frames (as identified by verification.py) are
written to pipeline/evidence_output/<run_id>/ so a reviewer has visual
context.  Their paths are embedded in the JSON report.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2
import numpy as np

from pipeline.frame_extractor import Frame
from pipeline.verification import VerificationResult

logger = logging.getLogger(__name__)

# Directory where evidence images are written (relative to this file)
EVIDENCE_OUTPUT_DIR: Path = Path(__file__).parent / "evidence_output"


# ── Public API ────────────────────────────────────────────────────────────────

def build_report(
    verification_result: VerificationResult,
    number_plate: Optional[str],
    plate_read_confidence: float,
    source_frames: List[Frame],
    notes: str = "",
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build the final JSON-serialisable report dict and write evidence frames.

    Parameters
    ----------
    verification_result:
        Output from verification.aggregate_verdicts().
    number_plate:
        Majority-vote plate string (None if unreadable).
    plate_read_confidence:
        OCR agreement ratio (0.0 if no plate).
    source_frames:
        All frames extracted from the source video/image — used to pull the
        best-confidence frames as evidence images.
    notes:
        Human-readable caveats to include in the report.
    run_id:
        Optional unique run identifier.  Auto-generated (UUID4) if not given.

    Returns
    -------
    dict that is json.dumps()-able.
    """
    if run_id is None:
        run_id = str(uuid.uuid4())[:8]

    vr = verification_result

    # ── Save evidence frames ──────────────────────────────────────────────
    evidence_paths = _save_evidence_frames(
        source_frames, vr.evidence_frame_timestamps, run_id
    )

    # ── Auto-generate notes ───────────────────────────────────────────────
    auto_notes: List[str] = []
    if not notes:
        if number_plate is None:
            auto_notes.append("Plate not readable in any frame.")
        if vr.helmet_status == "unclear":
            auto_notes.append(
                "Helmet status ambiguous — insufficient evidence in this clip."
            )
        if vr.frame_consistency_ratio < 0.5:
            auto_notes.append(
                "Low frame consistency: results may be unreliable; recommend longer clip."
            )
        notes = " | ".join(auto_notes) if auto_notes else "No additional caveats."

    # ── Assemble report ───────────────────────────────────────────────────
    report: Dict[str, Any] = {
        # ── Advisory header ───────────────────────────────────────────
        "_disclaimer": (
            "This report is an automated recommendation for human review. "
            "It is NOT a final enforcement or fine decision."
        ),
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),

        # ── Core verdict (matches spec) ───────────────────────────────
        "status": vr.status,
        "severity_score": vr.severity_score,
        "violations_detected": vr.violations_detected,
        "rider_count": vr.rider_count,
        "helmet_status": vr.helmet_status,
        "number_plate": number_plate,
        "plate_read_confidence": plate_read_confidence,

        # ── Evidence and reliability signals ──────────────────────────
        "evidence_frame_timestamps": vr.evidence_frame_timestamps,
        "evidence_frame_paths": evidence_paths,
        "frame_consistency_ratio": vr.frame_consistency_ratio,
        "avg_yolo_confidence": vr.avg_yolo_confidence,
        "ocr_agreement_ratio": vr.ocr_agreement_ratio,

        # ── Caveats ───────────────────────────────────────────────────
        "notes": notes,
    }

    logger.info(
        "Report assembled: run_id=%s status=%s severity=%.3f violations=%s",
        run_id, vr.status, vr.severity_score, vr.violations_detected,
    )
    return report


def report_to_json(report: Dict[str, Any], indent: int = 2) -> str:
    """Serialise report dict to a pretty-printed JSON string."""
    return json.dumps(report, indent=indent, ensure_ascii=False, default=str)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _save_evidence_frames(
    frames: List[Frame],
    target_timestamps: List[float],
    run_id: str,
) -> List[str]:
    """
    For each timestamp in *target_timestamps*, find the closest frame in
    *frames* and save it as a JPEG to the evidence output directory.

    Returns a list of absolute file paths (as strings).
    """
    if not target_timestamps:
        return []

    output_dir = EVIDENCE_OUTPUT_DIR / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: List[str] = []

    for ts in target_timestamps:
        # Find the frame whose timestamp is closest to ts
        closest = min(frames, key=lambda f: abs(f.timestamp - ts))
        filename = f"evidence_t{ts:.3f}s.jpg"
        filepath = output_dir / filename

        success = cv2.imwrite(str(filepath), closest.image)
        if success:
            saved_paths.append(str(filepath.resolve()))
            logger.info("Evidence frame saved: %s", filepath)
        else:
            logger.warning("Failed to write evidence frame: %s", filepath)

    return saved_paths
