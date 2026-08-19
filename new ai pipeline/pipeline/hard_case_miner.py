"""
pipeline/hard_case_miner.py
───────────────────────────
Automated Hard-Case & Active-Learning Miner.

Whenever:
  1. The Rule Engine and VLM disagree (e.g. YOLO/Rule engine flags violation
     but VLM downgrades or finds false positive)
  2. Borderline / ambiguous confidence cases occur (0.25 <= conf <= 0.45)
  3. OCR fails or is ambiguous on detected plate crops

This module automatically extracts the evidence frame, relevant object crops,
and stores a structured JSON record into `dataset/hard_cases/` so they can be
used directly for retraining and benchmarking.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

HARD_CASES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "dataset",
    "hard_cases",
)


@dataclass
class HardCaseRecord:
    timestamp_sec: float
    video_source: str
    trigger_reason: str
    rule_status: str
    rule_violations: List[str]
    vlm_verdict: str
    vlm_reasoning: str
    frame_image_path: str
    crop_paths: List[str]
    metadata: Dict[str, Any]


def log_hard_case(
    video_source: str,
    timestamp: float,
    frame_bgr: np.ndarray,
    rule_status: str,
    rule_violations: List[str],
    vlm_verdict: str,
    vlm_reasoning: str,
    trigger_reason: str = "vlm_disagreement",
    relevant_bboxes: Optional[List[List[float]]] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
    output_dir: Optional[str] = None,
) -> Optional[str]:
    """
    Save an anomalous/disagreement frame and its crops to the hard-cases dataset.

    Returns the path to the saved frame, or None if saving failed.
    """
    target_dir = output_dir or HARD_CASES_DIR
    os.makedirs(target_dir, exist_ok=True)

    clean_video_name = re.sub(r"[^a-zA-Z0-9_-]", "_", Path(video_source).stem)
    video_subfolder = os.path.join(target_dir, clean_video_name)
    os.makedirs(video_subfolder, exist_ok=True)

    time_str = f"t{timestamp:06.2f}s"
    frame_filename = f"{clean_video_name}_{time_str}_{trigger_reason}.jpg"
    frame_path = os.path.join(video_subfolder, frame_filename)

    # Save full frame
    try:
        cv2.imwrite(frame_path, frame_bgr)
    except Exception as exc:
        logger.warning("Failed to save hard-case frame: %s", exc)
        return None

    # Save relevant bounding box crops
    crop_paths: List[str] = []
    if relevant_bboxes:
        h, w = frame_bgr.shape[:2]
        for idx, bbox in enumerate(relevant_bboxes):
            x1, y1, x2, y2 = (int(v) for v in bbox)
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            if x2 > x1 and y2 > y1:
                crop = frame_bgr[y1:y2, x1:x2]
                crop_filename = f"{clean_video_name}_{time_str}_crop{idx}.jpg"
                crop_path = os.path.join(video_subfolder, crop_filename)
                cv2.imwrite(crop_path, crop)
                crop_paths.append(crop_path)

    # Record structured metadata
    record = HardCaseRecord(
        timestamp_sec=timestamp,
        video_source=video_source,
        trigger_reason=trigger_reason,
        rule_status=rule_status,
        rule_violations=rule_violations,
        vlm_verdict=vlm_verdict,
        vlm_reasoning=vlm_reasoning,
        frame_image_path=os.path.relpath(frame_path, start=target_dir),
        crop_paths=[os.path.relpath(cp, start=target_dir) for cp in crop_paths],
        metadata=extra_metadata or {},
    )

    manifest_path = os.path.join(target_dir, "hard_cases_manifest.jsonl")
    try:
        with open(manifest_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record)) + "\n")
        logger.info(
            "Logged hard case [reason=%s] at t=%.2fs -> %s",
            trigger_reason, timestamp, frame_filename,
        )
    except Exception as exc:
        logger.warning("Failed to write hard-case manifest: %s", exc)

    return frame_path
