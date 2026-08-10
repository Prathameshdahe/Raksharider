"""
pipeline/annotator.py
─────────────────────
Draws detection bounding boxes and a verdict overlay on evidence frames.
Used by run_pipeline.py to produce human-readable visual output for demo/review.
"""

from __future__ import annotations

import cv2
import numpy as np
from typing import List, Optional

from pipeline.detector import Detection, FrameDetections
from pipeline.verification import VerificationResult

# ── Colour palette (BGR) ──────────────────────────────────────────────────────
COLOURS = {
    "person":        (0,   200, 255),   # amber
    "motorcycle":    (255, 150,  50),   # blue-orange
    "helmet":        (50,  220,  50),   # green
    "no_helmet":     (30,   30, 230),   # red
    "license_plate": (255, 255, 255),   # white
}
DEFAULT_COLOUR = (180, 180, 180)


def draw_detections(
    image: np.ndarray,
    detections: List[Detection],
    ocr_plate: Optional[str] = None,
) -> np.ndarray:
    """
    Draw all bounding boxes on a copy of *image*.
    Plate box also shows the OCR text if provided.
    """
    out = image.copy()
    h, w = out.shape[:2]

    for det in detections:
        colour = COLOURS.get(det.class_name, DEFAULT_COLOUR)
        x1, y1, x2, y2 = (int(v) for v in det.bbox)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w - 1, x2), min(h - 1, y2)

        # Box
        cv2.rectangle(out, (x1, y1), (x2, y2), colour, 2)

        # Label background + text
        label = f"{det.class_name.replace('_', ' ')} {det.confidence:.0%}"
        if det.class_name == "license_plate" and ocr_plate:
            label = f"Plate: {ocr_plate} {det.confidence:.0%}"

        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        lx1, ly1 = x1, max(0, y1 - th - 6)
        cv2.rectangle(out, (lx1, ly1), (lx1 + tw + 4, ly1 + th + 6), colour, -1)
        cv2.putText(out, label, (lx1 + 2, ly1 + th + 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)

    return out


def draw_tracked_detections(
    image: np.ndarray,
    tracked_dets,                    # list[TrackedDetection] from tracker.py
    ocr_plate: Optional[str] = None,
) -> np.ndarray:
    """
    Same as draw_detections() but adds the track ID badge to each tracked
    class (person/motorcycle). Untracked classes (track_id == -1) fall back
    to the standard label.

    Accepts TrackedDetection objects (which have .track_id) or plain
    Detection objects (track_id absent → treated as -1).
    """
    out = image.copy()
    h, w = out.shape[:2]

    for det in tracked_dets:
        colour = COLOURS.get(det.class_name, DEFAULT_COLOUR)
        x1, y1, x2, y2 = (int(v) for v in det.bbox)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w - 1, x2), min(h - 1, y2)

        cv2.rectangle(out, (x1, y1), (x2, y2), colour, 2)

        track_id = getattr(det, "track_id", -1)
        if track_id >= 0:
            label = f"ID:{track_id} {det.class_name.replace('_', ' ')}"
        elif det.class_name == "license_plate" and ocr_plate:
            label = f"Plate: {ocr_plate} {det.confidence:.0%}"
        else:
            label = f"{det.class_name.replace('_', ' ')} {det.confidence:.0%}"

        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        lx1, ly1 = x1, max(0, y1 - th - 6)
        cv2.rectangle(out, (lx1, ly1), (lx1 + tw + 4, ly1 + th + 6), colour, -1)
        cv2.putText(out, label, (lx1 + 2, ly1 + th + 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)

    return out


def draw_verdict_overlay(
    image: np.ndarray,
    result: VerificationResult,
    plate: Optional[str],
    frame_idx: int,
    total_frames: int,
) -> np.ndarray:
    """
    Draw a semi-transparent verdict panel in the top-left corner.
    """
    out = image.copy()
    h, w = out.shape[:2]

    # Status colour
    status_colours = {
        "auto_flagged":          (30,  30, 200),   # red
        "needs_review":          (30, 160, 230),   # orange
        "insufficient_evidence": (80, 180,  80),   # green
    }
    sc = status_colours.get(result.status, (128, 128, 128))

    # Build text lines
    violations_str = ", ".join(result.violations_detected) if result.violations_detected else "None"
    lines = [
        ("RakshaRide  POC",                    (255, 255, 255), 0.65, 2),
        (f"Status : {result.status.replace('_', ' ').upper()}", sc,  0.55, 1),
        (f"Severity  : {result.severity_score:.0%}",             (220, 220, 220), 0.50, 1),
        (f"Violations: {violations_str}",                        (220, 100, 100), 0.50, 1),
        (f"Riders    : {result.rider_count}",                    (220, 220, 220), 0.50, 1),
        (f"Helmet    : {result.helmet_status.replace('_',' ')}",  (220, 220, 220), 0.50, 1),
        (f"Plate     : {plate or 'unreadable'}",                 (220, 220, 220), 0.50, 1),
        (f"Consistency: {result.frame_consistency_ratio:.0%}",   (180, 180, 180), 0.45, 1),
        (f"Frame     : {frame_idx}/{total_frames}",              (150, 150, 150), 0.45, 1),
    ]

    # Measure panel
    pad = 10
    line_gap = 4
    sizes = [cv2.getTextSize(t, cv2.FONT_HERSHEY_SIMPLEX, s, th) for t, _, s, th in lines]
    panel_w = max(sz[0][0] for sz in sizes) + pad * 2
    panel_h = sum(sz[0][1] + line_gap for sz in sizes) + pad * 2

    # Draw semi-transparent panel
    overlay = out.copy()
    cv2.rectangle(overlay, (0, 0), (panel_w, panel_h), (20, 20, 20), -1)
    cv2.addWeighted(overlay, 0.75, out, 0.25, 0, out)

    # Draw text lines
    y = pad
    for (text, colour, scale, thickness), size_info in zip(lines, sizes):
        (tw, lh), baseline = size_info
        y += lh
        cv2.putText(out, text, (pad, y), cv2.FONT_HERSHEY_SIMPLEX,
                    scale, colour, thickness, cv2.LINE_AA)
        y += line_gap

    return out
