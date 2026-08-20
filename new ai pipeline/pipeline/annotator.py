"""
pipeline/annotator.py
─────────────────────
Draws detection bounding boxes and a verdict overlay on evidence frames.
Used by run_pipeline.py to produce human-readable visual output for demo/review.

Helmet/no_helmet boxes are only drawn when they overlap with a person bbox
(IoU > 0) to eliminate false-positives on vehicle rooftops.
"""

from __future__ import annotations

import cv2
import numpy as np
from typing import List, Mapping, Optional

from pipeline.detector import Detection, FrameDetections
from pipeline.verification import VerificationResult

# ── Colour palette (BGR) ──────────────────────────────────────────────────────
COLOURS = {
    "person":          (0,   200, 255),   # amber
    "motorcycle":      (255, 150,  50),   # blue-orange
    "helmet":          (50,  220,  50),   # green
    "no_helmet":       (30,   30, 230),   # red
    "no helmet":       (30,   30, 230),   # red (space variant from model)
    "license_plate":   (255, 255, 255),   # white
    "Number_plate":    (255, 255, 255),   # white (variant)
    "car":             (180, 100, 255),   # purple
    "bus":             (255, 200,  50),   # cyan-ish
    "truck":           (200, 150, 100),   # brown
    "mini_lcv":        (200, 180, 100),
    "auto_rickshaw":   (80,  220, 220),
    "bicycle":         (255, 180,  80),
    "vehicle":         (180, 180, 180),
    "traffic_light":   (0,   255, 255),   # yellow
    "cell_phone":      (0,   100, 255),   # orange-red
}
DEFAULT_COLOUR = (180, 180, 180)

# Minimum confidence to draw a helmet/no_helmet box at all
HELMET_DRAW_MIN_CONF: float = 0.55
VEHICLE_CLASSES = {
    "motorcycle", "bicycle", "car", "bus", "truck", "mini_lcv",
    "auto_rickshaw", "vehicle",
}


def _iou_any(box: list, others: list) -> float:
    """Return max IoU of *box* against any box in *others*."""
    x1, y1, x2, y2 = box
    best = 0.0
    for o in others:
        ox1, oy1, ox2, oy2 = o
        ix1 = max(x1, ox1); iy1 = max(y1, oy1)
        ix2 = min(x2, ox2); iy2 = min(y2, oy2)
        inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
        if inter == 0.0:
            continue
        aA = (x2 - x1) * (y2 - y1)
        aB = (ox2 - ox1) * (oy2 - oy1)
        union = aA + aB - inter
        best = max(best, inter / union if union > 0 else 0.0)
    return best


def _person_boxes(detections) -> list:
    """Extract person bounding boxes from a detection list."""
    return [d.bbox for d in detections if d.class_name == "person"]


def _should_draw_helmet(det: Detection, person_boxes: list) -> bool:
    """
    Only draw a helmet/no_helmet box if:
      1. Confidence >= HELMET_DRAW_MIN_CONF
      2. It overlaps with at least one person bbox (IoU > 0)
         This filters out false-positives on car rooftops / windscreens.
    """
    if det.confidence < HELMET_DRAW_MIN_CONF:
        return False
    if not person_boxes:
        return False
    return _iou_any(det.bbox, person_boxes) > 0.0


def _plate_for_track(track_id: int, plates) -> Optional[str]:
    if track_id < 0 or plates is None:
        return None
    if isinstance(plates, Mapping):
        return plates.get(track_id)
    if isinstance(plates, str):
        return plates
    return None


def draw_detections(
    image: np.ndarray,
    detections: List[Detection],
    ocr_plate: Optional[str] = None,
) -> np.ndarray:
    """
    Draw all bounding boxes on a copy of *image*.
    Helmet boxes are filtered — only drawn when overlapping a person.
    Plate box also shows the OCR text if provided.
    """
    out = image.copy()
    h, w = out.shape[:2]
    person_bboxes = _person_boxes(detections)

    for det in detections:
        cls = det.class_name.lower().replace(" ", "_")

        # Helmet filter — skip noisy car-roof false positives
        if cls in ("helmet", "no_helmet", "no helmet"):
            if not _should_draw_helmet(det, person_bboxes):
                continue

        colour = COLOURS.get(det.class_name, COLOURS.get(cls, DEFAULT_COLOUR))
        x1, y1, x2, y2 = (int(v) for v in det.bbox)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w - 1, x2), min(h - 1, y2)

        cv2.rectangle(out, (x1, y1), (x2, y2), colour, 2)

        label = f"{det.class_name.replace('_', ' ')} {det.confidence:.0%}"
        if det.class_name in ("license_plate", "Number_plate") and ocr_plate:
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
    plate_by_track: Optional[Mapping[int, str]] = None,
) -> np.ndarray:
    """
    Same as draw_detections() but adds the track ID badge to each tracked
    class (person/motorcycle). Untracked classes (track_id == -1) fall back
    to the standard label. Helmet filter also applies here.
    """
    out = image.copy()
    h, w = out.shape[:2]
    person_bboxes = _person_boxes(tracked_dets)

    for det in tracked_dets:
        cls = det.class_name.lower().replace(" ", "_")

        # Helmet filter
        if cls in ("helmet", "no_helmet", "no helmet"):
            if not _should_draw_helmet(det, person_bboxes):
                continue

        colour = COLOURS.get(det.class_name, COLOURS.get(cls, DEFAULT_COLOUR))
        x1, y1, x2, y2 = (int(v) for v in det.bbox)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w - 1, x2), min(h - 1, y2)

        cv2.rectangle(out, (x1, y1), (x2, y2), colour, 2)

        track_id = getattr(det, "track_id", -1)
        track_plate = _plate_for_track(track_id, plate_by_track) or (
            ocr_plate if cls in VEHICLE_CLASSES else None
        )
        if track_id >= 0:
            label = f"ID:{track_id} {det.class_name.replace('_', ' ')} {det.confidence:.0%}"
            if track_plate and cls in VEHICLE_CLASSES:
                label += f" {track_plate}"
        elif det.class_name in ("license_plate", "Number_plate") and ocr_plate:
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
    vehicle_plate_lines: Optional[list[str]] = None,
) -> np.ndarray:
    """
    Draw a semi-transparent verdict panel in the top-left corner.
    """
    out = image.copy()
    h, w = out.shape[:2]

    # Scale text size based on image resolution (4K needs bigger text)
    scale_factor = min(w / 1280, 2.5)

    status_colours = {
        "auto_flagged":          (30,  30, 200),
        "needs_review":          (30, 160, 230),
        "insufficient_evidence": (80, 180,  80),
    }
    sc = status_colours.get(result.status, (128, 128, 128))

    violations_str = ", ".join(result.violations_detected) if result.violations_detected else "None"
    lines = [
        ("DriveTrust AI  POC",                       (255, 255, 255), 0.65 * scale_factor, 2),
        (f"Status : {result.status.replace('_', ' ').upper()}", sc, 0.55 * scale_factor, 1),
        (f"Severity  : {result.severity_score:.0%}",             (220, 220, 220), 0.50 * scale_factor, 1),
        (f"Violations: {violations_str}",                        (220, 100, 100), 0.50 * scale_factor, 1),
        (f"Riders    : {result.rider_count}",                    (220, 220, 220), 0.50 * scale_factor, 1),
        (f"Helmet    : {result.helmet_status.replace('_', ' ')}", (220, 220, 220), 0.50 * scale_factor, 1),
        (f"Plate     : {plate or 'unreadable'}",                 (220, 220, 220), 0.50 * scale_factor, 1),
        (f"Consistency: {result.frame_consistency_ratio:.0%}",   (180, 180, 180), 0.45 * scale_factor, 1),
        (f"Frame     : {frame_idx}/{total_frames}",              (150, 150, 150), 0.45 * scale_factor, 1),
    ]
    if vehicle_plate_lines:
        lines.append(("Vehicles:", (255, 255, 255), 0.48 * scale_factor, 1))
        for text in vehicle_plate_lines[:8]:
            lines.append((text, (210, 230, 230), 0.43 * scale_factor, 1))

    pad = int(10 * scale_factor)
    line_gap = int(4 * scale_factor)
    sizes = [cv2.getTextSize(t, cv2.FONT_HERSHEY_SIMPLEX, s, th) for t, _, s, th in lines]
    panel_w = max(sz[0][0] for sz in sizes) + pad * 2
    panel_h = sum(sz[0][1] + line_gap for sz in sizes) + pad * 2

    overlay = out.copy()
    cv2.rectangle(overlay, (0, 0), (panel_w, panel_h), (20, 20, 20), -1)
    cv2.addWeighted(overlay, 0.75, out, 0.25, 0, out)

    y = pad
    for (text, colour, scale, thickness), size_info in zip(lines, sizes):
        (tw, lh), baseline = size_info
        y += lh
        cv2.putText(out, text, (pad, y), cv2.FONT_HERSHEY_SIMPLEX,
                    scale, colour, thickness, cv2.LINE_AA)
        y += line_gap

    return out


def render_full_annotated_video(
    frames: list,
    frame_detections: list,
    track_results: dict,
    verification_result: VerificationResult,
    number_plate: Optional[str] = None,
    plate_by_track: Optional[Mapping[int, str]] = None,
    vehicle_plate_lines: Optional[list[str]] = None,
    output_video_path: str = "evidence_video.mp4",
    fps: float = 2.0,
) -> Optional[str]:
    """
    Render all sampled frames with bounding boxes, track IDs, and HUD overlays
    into a playable annotated MP4 video file.
    """
    if not frames:
        return None

    first_frame = frames[0].image
    h, w = first_frame.shape[:2]

    # Try MP4V codec
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(output_video_path, fourcc, fps, (w, h))

    if not out.isOpened():
        # Fallback to AVI / XVID if MP4 fails
        fourcc = cv2.VideoWriter_fourcc(*"XVID")
        out = cv2.VideoWriter(output_video_path, fourcc, fps, (w, h))

    if not out.isOpened():
        return None

    total = len(frames)
    for i, frame in enumerate(frames):
        fd = frame_detections[i]
        if frame.timestamp in track_results:
            annotated = draw_tracked_detections(
                frame.image,
                track_results[frame.timestamp],
                number_plate,
                plate_by_track=plate_by_track,
            )
        else:
            annotated = draw_detections(frame.image, fd.detections, number_plate)

        annotated = draw_verdict_overlay(
            annotated,
            verification_result,
            number_plate,
            i + 1,
            total,
            vehicle_plate_lines=vehicle_plate_lines,
        )
        out.write(annotated)

    out.release()
    return output_video_path
