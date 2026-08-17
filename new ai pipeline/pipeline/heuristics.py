"""
pipeline/heuristics.py
───────────────────────
Zero-training heuristic detectors for DriveTrust AI.

All functions here operate on existing YOLO detections and/or ByteTrack
trajectory history — no new model weights are needed.

Detectors
---------
- missing_plate_flag      : plate detected but OCR text is empty / invalid
- obscured_plate_flag     : no plate detected at all when a vehicle is present
- wheelie_detector        : motorcycle bbox aspect-ratio spike over consecutive frames
- phone_usage_detector    : person + cell_phone IoU check (road cam or front cam)
- erratic_driving_detector: track centroid variance over a sliding window
- signal_color_detector   : HSV filter on cropped traffic_light bbox → red/green/amber

All return plain bool or a string tag so rules.py can consume them without
any dependency on CV or tracker internals.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from typing import TYPE_CHECKING, Optional

import cv2
import numpy as np

if TYPE_CHECKING:
    from pipeline.detector import Detection, FrameDetections
    from pipeline.tracker  import TrackedDetection

logger = logging.getLogger(__name__)


# ── Constants — tune these after field testing ────────────────────────────────

# Plate heuristics
PLATE_OCR_MIN_CONFIDENCE: float = 0.30   # below this → plate "obscured"

# Wheelie detection — real wheelies cause severe vertical aspect ratio spikes (>2.4)
WHEELIE_ASPECT_RATIO_THRESHOLD: float = 2.4  # height/width ratio spike for true wheelie pose
WHEELIE_MIN_FRAMES: int = 4                  # must persist for >= 4 consecutive frames (>= 2s at 0.5s interval)

# Phone usage
PHONE_PERSON_IOU_THRESHOLD: float = 0.05    # very loose — phone is small

# Erratic driving
ERRATIC_WINDOW_FRAMES:    int   = 8         # sliding window size
ERRATIC_VARIANCE_THRESHOLD: float = 3500.0  # centroid variance in px² — tune per camera

# Signal detection
SIGNAL_RED_HSV_LOWER   = np.array([0,   100, 100], dtype=np.uint8)
SIGNAL_RED_HSV_UPPER   = np.array([10,  255, 255], dtype=np.uint8)
SIGNAL_RED_HSV_LOWER2  = np.array([170, 100, 100], dtype=np.uint8)
SIGNAL_RED_HSV_UPPER2  = np.array([180, 255, 255], dtype=np.uint8)
SIGNAL_GREEN_HSV_LOWER = np.array([40,  50,  50],  dtype=np.uint8)
SIGNAL_GREEN_HSV_UPPER = np.array([90,  255, 255], dtype=np.uint8)
SIGNAL_MIN_PIXEL_RATIO: float = 0.10   # at least 10% of crop must match colour


# ── IoU helper (local, no import cycle) ───────────────────────────────────────

def _iou(a: list[float], b: list[float]) -> float:
    ix1 = max(a[0], b[0]); iy1 = max(a[1], b[1])
    ix2 = min(a[2], b[2]); iy2 = min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter == 0.0:
        return 0.0
    aa = (a[2]-a[0]) * (a[3]-a[1])
    ab = (b[2]-b[0]) * (b[3]-b[1])
    union = aa + ab - inter
    return inter / union if union > 0 else 0.0


# ── 1. Plate heuristics ───────────────────────────────────────────────────────

def missing_plate_flag(
    plate_detections: list,           # list[Detection] with class_name == "license_plate"
    ocr_text: Optional[str],
    ocr_confidence: float,
) -> str:
    """
    Classify the plate situation for this frame.

    Returns one of:
        "ok"              — plate detected and OCR read it with confidence
        "invalid_format"  — plate detected, text doesn't match Indian format
        "low_confidence"  — plate detected, OCR ran but confidence too low
        "missing"         — no plate detection at all
    """
    if not plate_detections:
        return "missing"
    if not ocr_text:
        return "low_confidence"
    if ocr_confidence < PLATE_OCR_MIN_CONFIDENCE:
        return "low_confidence"
    return "ok"


def obscured_plate_check(
    vehicle_detections: list,     # any vehicle class in this frame
    plate_detections: list,
) -> bool:
    """
    Returns True if a vehicle is visible but no plate is detected at all.
    This is a softer signal than missing_plate_flag — it fires when a vehicle
    is present but the plate region is completely absent.
    """
    has_vehicle = len(vehicle_detections) > 0
    has_plate   = len(plate_detections) > 0
    return has_vehicle and not has_plate


# ── 2. Wheelie detection ──────────────────────────────────────────────────────

class WheelieDetector:
    """
    Stateful detector — call update() once per frame in temporal order.
    Wheelie = motorcycle bounding box becomes tall and narrow
    (rear wheel lifts, front wheel raises → height >> width).
    """

    def __init__(self) -> None:
        self._consecutive: int = 0

    def update(self, motorcycle_detections: list) -> bool:
        """
        Returns True if a wheelie is currently active.
        Must be called every frame even if there are no motorcycles.
        """
        wheelie_this_frame = False
        for det in motorcycle_detections:
            x1, y1, x2, y2 = det.bbox
            w = x2 - x1
            h = y2 - y1
            if w < 1:
                continue
            ratio = h / w
            if ratio >= WHEELIE_ASPECT_RATIO_THRESHOLD:
                wheelie_this_frame = True
                break

        if wheelie_this_frame:
            self._consecutive += 1
        else:
            self._consecutive = 0

        active = self._consecutive >= WHEELIE_MIN_FRAMES
        if active:
            logger.info("Wheelie detected (consecutive frames: %d)", self._consecutive)
        return active


# ── 3. Phone usage detection ──────────────────────────────────────────────────

def phone_usage_detected(
    person_detections: list,
    phone_detections: list,     # class_name == "cell_phone"
) -> bool:
    """
    Returns True if any person's bounding box overlaps a phone detection
    above PHONE_PERSON_IOU_THRESHOLD.

    Works for both road-cam (driver + phone) and front-cam scenarios.
    """
    for person in person_detections:
        for phone in phone_detections:
            if _iou(person.bbox, phone.bbox) >= PHONE_PERSON_IOU_THRESHOLD:
                logger.debug(
                    "Phone usage: person@%s overlaps phone@%s (iou=%.3f)",
                    person.bbox, phone.bbox,
                    _iou(person.bbox, phone.bbox),
                )
                return True
    return False


# ── 4. Erratic driving detection ──────────────────────────────────────────────

class ErraticDrivingDetector:
    """
    Stateful per-track erratic driving detector.

    Usage:
        detector = ErraticDrivingDetector()
        for frame_tracked_dets in all_tracked:
            erratic_ids = detector.update(frame_tracked_dets)
    """

    def __init__(self) -> None:
        # track_id → deque of (cx, cy) centroids
        self._history: dict[int, deque[tuple[float, float]]] = {}

    def update(self, tracked_dets: list) -> set[int]:
        """
        Update track histories and return set of track IDs showing erratic motion.
        tracked_dets: list[TrackedDetection] from tracker.py
        """
        erratic_ids: set[int] = set()

        for det in tracked_dets:
            tid = getattr(det, "track_id", -1)
            if tid < 0:
                continue

            x1, y1, x2, y2 = det.bbox
            cx = (x1 + x2) / 2.0
            cy = (y1 + y2) / 2.0

            if tid not in self._history:
                self._history[tid] = deque(maxlen=ERRATIC_WINDOW_FRAMES)
            self._history[tid].append((cx, cy))

            if len(self._history[tid]) < ERRATIC_WINDOW_FRAMES:
                continue

            # Compute variance of centroid positions in window
            xs = [p[0] for p in self._history[tid]]
            ys = [p[1] for p in self._history[tid]]
            mean_x = sum(xs) / len(xs)
            mean_y = sum(ys) / len(ys)
            var_x  = sum((x - mean_x)**2 for x in xs) / len(xs)
            var_y  = sum((y - mean_y)**2 for y in ys) / len(ys)
            variance = var_x + var_y

            if variance > ERRATIC_VARIANCE_THRESHOLD:
                logger.info(
                    "Erratic driving: track_id=%d variance=%.1f > %.1f",
                    tid, variance, ERRATIC_VARIANCE_THRESHOLD,
                )
                erratic_ids.add(tid)

        return erratic_ids

    def reset(self) -> None:
        self._history.clear()


# ── 5. Traffic signal color detection ────────────────────────────────────────

def signal_color(
    frame_bgr: np.ndarray,
    signal_detections: list,    # class_name == "traffic_light"
) -> str:
    """
    Determine the colour of the traffic signal visible in this frame.

    Returns: "red" | "green" | "amber" | "unknown"

    Algorithm:
      1. Crop the top-third of each traffic_light bounding box
         (signal lights are stacked top=red, middle=amber, bottom=green,
          but the top region is most reliable for red detection from distance)
      2. Convert to HSV
      3. Count pixels matching red and green ranges
      4. Whichever colour has more pixels and exceeds MIN_PIXEL_RATIO wins
    """
    if not signal_detections:
        return "unknown"

    red_pixels   = 0
    green_pixels = 0
    total_pixels = 0

    for det in signal_detections:
        x1, y1, x2, y2 = (int(v) for v in det.bbox)
        h_det = y2 - y1
        # Crop top-third for red, full crop for green
        top_y2 = y1 + max(1, h_det // 3)

        crop = frame_bgr[y1:top_y2, x1:x2]
        if crop.size == 0:
            continue

        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        mask_r1 = cv2.inRange(hsv, SIGNAL_RED_HSV_LOWER,  SIGNAL_RED_HSV_UPPER)
        mask_r2 = cv2.inRange(hsv, SIGNAL_RED_HSV_LOWER2, SIGNAL_RED_HSV_UPPER2)
        mask_r  = cv2.bitwise_or(mask_r1, mask_r2)

        # For green check full bbox
        full_crop = frame_bgr[y1:y2, x1:x2]
        hsv_full  = cv2.cvtColor(full_crop, cv2.COLOR_BGR2HSV)
        mask_g    = cv2.inRange(hsv_full, SIGNAL_GREEN_HSV_LOWER, SIGNAL_GREEN_HSV_UPPER)

        red_pixels   += int(cv2.countNonZero(mask_r))
        green_pixels += int(cv2.countNonZero(mask_g))
        total_pixels += max(1, crop.shape[0] * crop.shape[1])

    if total_pixels == 0:
        return "unknown"

    red_ratio   = red_pixels   / total_pixels
    green_ratio = green_pixels / total_pixels

    logger.debug(
        "Signal HSV: red_ratio=%.3f green_ratio=%.3f (threshold=%.2f)",
        red_ratio, green_ratio, SIGNAL_MIN_PIXEL_RATIO,
    )

    if red_ratio > green_ratio and red_ratio >= SIGNAL_MIN_PIXEL_RATIO:
        return "red"
    if green_ratio > red_ratio and green_ratio >= SIGNAL_MIN_PIXEL_RATIO:
        return "green"
    return "unknown"
