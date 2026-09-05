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
ERRATIC_MIN_STEP_PX:      float = 12.0      # ignore tiny detector jitter (raised from 8)
ERRATIC_MIN_LATERAL_RANGE_PX: float = 220.0 # must genuinely weave across frame (raised from 150)
ERRATIC_MIN_PATH_PX:      float = 600.0     # ignore small local jitter (raised from 400)
ERRATIC_MIN_SIGN_CHANGES: int   = 4         # left-right-left-right pattern (raised from 3)

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
    Stateful per-track erratic driving detector with ego-motion (camera shake)
    compensation.

    Camera shake from speed bumps, hard braking, or rough roads causes ALL tracked
    vehicles to shift simultaneously in the same direction — making each vehicle
    appear to make a sudden lateral movement when in fact the camera moved.

    Compensation strategy:
      - Each frame, collect the lateral (x-axis) displacement for every tracked
        vehicle that was also visible in the previous frame.
      - Compute the MEDIAN of those displacements (≥3 vehicles needed for reliability).
      - Subtract this median from each vehicle's individual displacement before
        storing it in the centroid history used for erratic analysis.
      - Result: camera motion cancels out; only vehicle-specific erratic movement
        remains in the analysis window.

    Usage:
        detector = ErraticDrivingDetector()
        for frame_tracked_dets in all_tracked:
            erratic_ids = detector.update(frame_tracked_dets)
    """

    # Minimum number of simultaneously tracked vehicles required to estimate
    # camera motion reliably.  With fewer vehicles, compensation is skipped
    # (single-vehicle scenes cannot distinguish camera from vehicle motion).
    MIN_VEHICLES_FOR_COMPENSATION: int = 3

    def __init__(self) -> None:
        # track_id → deque of (compensated_cx, cy)
        self._history: dict[int, deque[tuple[float, float]]] = {}
        # track_id → last raw (cx, cy) — for computing per-frame deltas
        self._last_pos: dict[int, tuple[float, float]] = {}
        self._vehicle_classes = {
            "motorcycle", "bicycle", "car", "bus", "truck", "mini_lcv",
            "auto_rickshaw", "vehicle",
        }

    def update(self, tracked_dets: list) -> set[int]:
        """
        Update track histories and return set of track IDs showing erratic motion.
        tracked_dets: list[TrackedDetection] from tracker.py
        """
        # ── Step 1: collect current raw centroids for all tracked vehicles ────
        current_raw: dict[int, tuple[float, float]] = {}
        for det in tracked_dets:
            tid = getattr(det, "track_id", -1)
            if tid < 0:
                continue
            if getattr(det, "class_name", "") not in self._vehicle_classes:
                continue
            x1, y1, x2, y2 = det.bbox
            current_raw[tid] = ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

        # ── Step 2: compute camera motion as median lateral displacement ───────
        lateral_deltas: list[float] = []
        for tid, (cx, cy) in current_raw.items():
            if tid in self._last_pos:
                prev_cx, _ = self._last_pos[tid]
                dx = cx - prev_cx
                # Only count significant moves — ignore tracker jitter
                if abs(dx) >= ERRATIC_MIN_STEP_PX:
                    lateral_deltas.append(dx)

        camera_dx = 0.0
        if len(lateral_deltas) >= self.MIN_VEHICLES_FOR_COMPENSATION:
            lateral_deltas.sort()
            mid = len(lateral_deltas) // 2
            camera_dx = lateral_deltas[mid]
            if abs(camera_dx) > 5.0:   # only compensate for meaningful camera motion
                logger.debug(
                    "Erratic detector: camera_dx=%.1fpx compensated (from %d vehicles)",
                    camera_dx, len(lateral_deltas),
                )

        # ── Step 3: update compensated histories and analyse ──────────────────
        erratic_ids: set[int] = set()

        for tid, (cx, cy) in current_raw.items():
            # Apply compensation: subtract camera motion from the vehicle's x movement.
            if tid in self._last_pos:
                prev_cx, prev_cy = self._last_pos[tid]
                dx_raw  = cx - prev_cx
                dx_comp = dx_raw - camera_dx           # ego-motion removed
                comp_cx = prev_cx + dx_comp            # reconstructed compensated position
            else:
                comp_cx = cx                           # first sighting — no delta yet

            if tid not in self._history:
                self._history[tid] = deque(maxlen=ERRATIC_WINDOW_FRAMES)
            self._history[tid].append((comp_cx, cy))
            self._last_pos[tid] = (cx, cy)             # store RAW for next frame's delta

            # Analyse only when history window is full
            if len(self._history[tid]) < ERRATIC_WINDOW_FRAMES:
                continue

            pts = list(self._history[tid])
            vectors: list[tuple[float, float]] = []
            for a, b in zip(pts, pts[1:]):
                dx = b[0] - a[0]
                dy = b[1] - a[1]
                if math.hypot(dx, dy) >= ERRATIC_MIN_STEP_PX:
                    vectors.append((dx, dy))

            if len(vectors) < 3:
                continue

            dx_signs = [
                1 if dx > 0 else -1
                for dx, _ in vectors
                if abs(dx) >= ERRATIC_MIN_STEP_PX
            ]
            sign_changes = sum(
                1 for a, b in zip(dx_signs, dx_signs[1:])
                if a != b
            )
            xs = [p[0] for p in pts]
            lateral_range = max(xs) - min(xs)
            path_length   = sum(math.hypot(dx, dy) for dx, dy in vectors)

            if (
                sign_changes  >= ERRATIC_MIN_SIGN_CHANGES
                and lateral_range >= ERRATIC_MIN_LATERAL_RANGE_PX
                and path_length   >= ERRATIC_MIN_PATH_PX
            ):
                logger.info(
                    "Erratic driving: track_id=%d sign_changes=%d lateral_range=%.1f path=%.1f",
                    tid, sign_changes, lateral_range, path_length,
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
