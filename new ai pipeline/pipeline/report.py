"""
pipeline/report.py
------------------
Assembles the final JSON report, saves evidence frame images, and
writes the track_log.json audit trail — v2.

The report is advisory only — it is not a fine or enforcement decision.
Field names are intentionally worded to reflect this (e.g. "violations_detected",
not "violations_confirmed").

Output per run
--------------
pipeline/evidence_output/{run_id}/
  report.json         master report (full schema)
  track_log.json      per-track audit trail
  evidence_t*.jpg     annotated evidence frames
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
PIPELINE_VERSION = "2.0.0"


def build_report(
    verification_result:  VerificationResult,
    number_plate:         Optional[str],
    plate_read_confidence: float,
    source_frames:        List[Frame],
    processing_time_s:    float = 0.0,
    vehicle_type_declared: str  = "unknown",   # from CLI --vehicle-type
    notes:                str   = "",
    run_id:               Optional[str] = None,
    track_history:        Optional[Dict] = None,   # {track_id: track_info_dict}
) -> Dict[str, Any]:
    """
    Build the final JSON-serialisable report dict and save evidence frames.

    track_history : optional dict from tracker._active / _lost for audit log.
    """
    if run_id is None:
        run_id = str(uuid.uuid4())[:8]

    vr = verification_result
    out_dir = EVIDENCE_OUTPUT_DIR / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    evidence_paths = _save_evidence_frames(source_frames, vr.evidence_frame_timestamps, out_dir)

    # Auto-generate notes
    if not notes:
        caveats: List[str] = []
        if number_plate is None:
            caveats.append("Plate not readable in any frame.")
        if vr.helmet_status == "unclear":
            caveats.append("Helmet status ambiguous — insufficient evidence in this clip.")
        if vr.frame_consistency_ratio < 0.5:
            caveats.append("Low frame consistency; recommend a longer or clearer clip.")
        if vr.vlm_reasoning:
            caveats.append(f"VLM review: {vr.vlm_reasoning}")
        notes = " | ".join(caveats) if caveats else ""

    # Plate flag
    plate_flag = vr.plate_flag if hasattr(vr, "plate_flag") else "unknown"

    report: Dict[str, Any] = {
        "_disclaimer": (
            "Automated recommendation for human review. "
            "Not a final enforcement or fine decision."
        ),
        "pipeline_version": PIPELINE_VERSION,
        "run_id":           run_id,
        "generated_at":     datetime.now(timezone.utc).isoformat(),

        # ── Top-level verdict ─────────────────────────────────────────────────
        "status":             vr.status,
        "severity_score":     vr.severity_score,
        "violations_detected": vr.violations_detected,

        # ── Vehicle & identity ────────────────────────────────────────────────
        "vehicle": {
            "type_declared":  vehicle_type_declared,
            "type_detected":  getattr(vr, "vehicle_type", "unknown"),
            "track_ids":      [],     # populated from track_history below
            "plate":          number_plate,
            "plate_confidence": plate_read_confidence,
            "plate_flag":     plate_flag,   # ok/missing/low_confidence/invalid_format
        },

        # ── Road camera detections ────────────────────────────────────────────
        "road_camera": {
            "rider_count":      vr.rider_count,
            "helmet_status":    vr.helmet_status,
            "triple_riding":    "triple_riding" in vr.violations_detected,
            "phone_usage":      getattr(vr, "phone_usage", False),
            "wheelie_detected": getattr(vr, "wheelie_detected", False),
            "erratic_driving":  getattr(vr, "erratic_driving", False),
            "signal_violation": getattr(vr, "signal_violation", False),
        },

        # ── Front camera (stub — populated when front-cam model is ready) ─────
        "front_camera": getattr(vr, "front_cam", {
            "seatbelt_status":     "not_applicable",
            "helmet_status":       "not_applicable",
            "phone_usage_detected": False,
        }),

        # ── Tracking ──────────────────────────────────────────────────────────
        "tracking": {
            "total_unique_ids": getattr(vr, "total_track_ids", 0),
            "frames_tracked":   len(source_frames),
        },

        # ── VLM review ────────────────────────────────────────────────────────
        "vlm_review": {
            "fired":     bool(getattr(vr, "vlm_reasoning", "")),
            "reasoning": getattr(vr, "vlm_reasoning", ""),
        },

        # ── Meta ──────────────────────────────────────────────────────────────
        "meta": {
            "frames_analysed":        len(source_frames),
            "duration_seconds":       round(source_frames[-1].timestamp, 2) if source_frames else 0.0,
            "frame_consistency_ratio": vr.frame_consistency_ratio,
            "avg_yolo_confidence":    vr.avg_yolo_confidence,
            "ocr_agreement_ratio":    vr.ocr_agreement_ratio,
            "processing_time_seconds": round(processing_time_s, 2),
        },

        "evidence_frames": evidence_paths,
        "notes": notes,
    }

    # Populate track IDs from track_history
    if track_history:
        report["vehicle"]["track_ids"] = sorted(track_history.keys())
        _save_track_log(run_id, track_history, out_dir)

    # Save report.json
    report_path = out_dir / "report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)

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
    out_dir: Path,
) -> List[str]:
    """Save the frame closest to each target timestamp as a JPEG."""
    if not target_timestamps or not frames:
        return []

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


def _save_track_log(run_id: str, track_history: Dict, out_dir: Path) -> None:
    """
    Save track_log.json — per-track audit trail for admin review.

    track_history expected format:
      { track_id(int): {
          "class": str,
          "first_seen": float,
          "last_seen": float,
          "frame_count": int,
          "avg_confidence": float,
          "violations_on_track": list[str]
      }}
    """
    log = {
        "run_id":    run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tracks":    {str(k): v for k, v in sorted(track_history.items())},
    }
    path = out_dir / "track_log.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False, default=str)
    logger.info("Track log saved: %s", path)
