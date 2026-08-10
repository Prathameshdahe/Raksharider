"""
pipeline/rules.py
-----------------
Per-frame violation rule engine — v2.

Operates on FrameDetections from detector.py and produces FrameVerdict objects
that verification.py aggregates across the clip.

New in v2:
  - phone_usage detection (IoU person+cell_phone)
  - wheelie detection (via WheelieDetector from heuristics.py)
  - missing / obscured plate flag
  - erratic driving (via ErraticDrivingDetector from heuristics.py)
  - traffic signal colour (via signal_color from heuristics.py)
  - vehicle_type field on FrameVerdict

Terminology:
  rider       -- person whose bbox overlaps a motorcycle bbox above
                 IOU_RIDER_MOTORCYCLE_THRESHOLD
  head region -- top HEAD_FRACTION of a person's bbox; helmets must overlap
                 this region to count as worn
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Literal, Optional

import numpy as np

from pipeline.detector import Detection, FrameDetections
from pipeline.heuristics import (
    missing_plate_flag,
    obscured_plate_check,
    phone_usage_detected,
    signal_color,
)

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────

IOU_RIDER_MOTORCYCLE_THRESHOLD: float = 0.1
IOU_HELMET_HEAD_THRESHOLD:      float = 0.15
HEAD_FRACTION:                  float = 0.25
TRIPLE_RIDING_THRESHOLD:        int   = 3

HelmetStatus = Literal["helmet", "no_helmet", "unclear"]


# ── FrameVerdict — extended ───────────────────────────────────────────────────

@dataclass
class FrameVerdict:
    """Rule-layer output for a single frame — v2."""
    timestamp:               float
    rider_count:             int
    helmet_status:           HelmetStatus
    violations:              List[str]
    avg_detection_confidence: float

    # New fields v2
    vehicle_type:            str          = "unknown"   # car/bus/truck/motorcycle/…
    phone_usage:             bool         = False
    wheelie:                 bool         = False
    plate_flag:              str          = "missing"   # ok/missing/low_confidence/invalid_format
    signal_state:            str          = "unknown"   # red/green/amber/unknown
    erratic_track_ids:       List[int]    = field(default_factory=list)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _iou(boxA: List[float], boxB: List[float]) -> float:
    ix1 = max(boxA[0], boxB[0]); iy1 = max(boxA[1], boxB[1])
    ix2 = min(boxA[2], boxB[2]); iy2 = min(boxA[3], boxB[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter == 0.0:
        return 0.0
    aA = max(0.0, boxA[2]-boxA[0]) * max(0.0, boxA[3]-boxA[1])
    aB = max(0.0, boxB[2]-boxB[0]) * max(0.0, boxB[3]-boxB[1])
    union = aA + aB - inter
    return inter / union if union > 0 else 0.0


def _head_box(person_bbox: List[float]) -> List[float]:
    x1, y1, x2, y2 = person_bbox
    return [x1, y1, x2, y1 + (y2 - y1) * HEAD_FRACTION]


def _dominant_vehicle_class(fd: FrameDetections) -> str:
    """Pick the most-specific vehicle class present in this frame."""
    priority = [
        "bus", "truck", "mini_lcv", "car", "auto_rickshaw",
        "bicycle", "motorcycle", "vehicle",
    ]
    present = {d.class_name for d in fd.detections}
    for cls in priority:
        if cls in present:
            return cls
    return "unknown"


# ── Core rule engine ──────────────────────────────────────────────────────────

def apply_rules(
    fd: FrameDetections,
    frame_bgr: Optional[np.ndarray] = None,
    ocr_text: Optional[str] = None,
    ocr_confidence: float = 0.0,
    wheelie_detector=None,      # WheelieDetector instance (stateful)
    erratic_detector=None,      # ErraticDrivingDetector instance (stateful)
    tracked_dets: Optional[list] = None,   # list[TrackedDetection] from tracker
) -> FrameVerdict:
    """
    Apply all violation rules to one frame's detections.

    Optional args:
      frame_bgr       : raw frame needed for signal_color HSV analysis
      ocr_text        : plate OCR result for this frame (from ocr.py)
      ocr_confidence  : OCR confidence for plate flag decision
      wheelie_detector: WheelieDetector() instance from caller (stateful across frames)
      erratic_detector: ErraticDrivingDetector() instance from caller
      tracked_dets    : TrackedDetection list from tracker.update() for erratic detection
    """
    motorcycles  = fd.by_class("motorcycle")
    persons      = fd.by_class("person")
    helmets      = fd.by_class("helmet")
    no_helmets   = fd.by_class("no_helmet")
    plates       = fd.by_class("license_plate")
    phones       = fd.by_class("cell_phone")
    signals      = fd.by_class("traffic_light")
    vehicles_all = fd.by_classes("car", "bus", "truck", "mini_lcv", "auto_rickshaw",
                                 "bicycle", "motorcycle", "vehicle")

    # ── Rider-motorcycle association ──────────────────────────────────────────
    riders: List[Detection] = []
    for person in persons:
        for moto in motorcycles:
            if _iou(person.bbox, moto.bbox) >= IOU_RIDER_MOTORCYCLE_THRESHOLD:
                riders.append(person)
                break

    rider_count = len(riders)
    logger.debug(
        "Frame %.3fs: %d moto(s), %d person(s), %d rider(s)",
        fd.timestamp, len(motorcycles), len(persons), rider_count,
    )

    # ── Helmet association ────────────────────────────────────────────────────
    helmet_calls: List[HelmetStatus] = []
    for rider in riders:
        head = _head_box(rider.bbox)
        if any(_iou(head, h.bbox) >= IOU_HELMET_HEAD_THRESHOLD for h in helmets):
            call: HelmetStatus = "helmet"
        elif any(_iou(head, nh.bbox) >= IOU_HELMET_HEAD_THRESHOLD for nh in no_helmets):
            call = "no_helmet"
        else:
            call = "no_helmet" if rider.confidence >= 0.5 else "unclear"
        helmet_calls.append(call)

    if not helmet_calls:
        helmet_status: HelmetStatus = "unclear"
    else:
        counts = {s: helmet_calls.count(s) for s in ("helmet", "no_helmet", "unclear")}
        helmet_status = max(counts, key=lambda k: (counts[k], k == "no_helmet"))

    # ── Violations ────────────────────────────────────────────────────────────
    violations: List[str] = []
    if rider_count > 0 and helmet_status == "no_helmet":
        violations.append("no_helmet")
    if rider_count >= TRIPLE_RIDING_THRESHOLD:
        violations.append("triple_riding")

    # Phone usage
    phone = phone_usage_detected(persons, phones)
    if phone:
        violations.append("phone_usage")

    # Wheelie
    wheelie = False
    if wheelie_detector is not None:
        wheelie = wheelie_detector.update(motorcycles)
        if wheelie:
            violations.append("wheelie")

    # Plate flag
    p_flag = missing_plate_flag(plates, ocr_text, ocr_confidence)
    if p_flag == "missing" and vehicles_all:
        # obscured = vehicle present but no plate at all
        if obscured_plate_check(vehicles_all, plates):
            p_flag = "missing"
            violations.append("missing_plate")

    # Erratic driving
    erratic_ids: List[int] = []
    if erratic_detector is not None and tracked_dets is not None:
        erratic_ids = list(erratic_detector.update(tracked_dets))
        if erratic_ids:
            violations.append("erratic_driving")

    # Signal state
    sig_state = "unknown"
    if frame_bgr is not None and signals:
        sig_state = signal_color(frame_bgr, signals)

    # ── Confidence ────────────────────────────────────────────────────────────
    used = riders + helmets + motorcycles
    avg_conf = sum(d.confidence for d in used) / len(used) if used else 0.0

    return FrameVerdict(
        timestamp=fd.timestamp,
        rider_count=rider_count,
        helmet_status=helmet_status,
        violations=violations,
        avg_detection_confidence=avg_conf,
        vehicle_type=_dominant_vehicle_class(fd),
        phone_usage=phone,
        wheelie=wheelie,
        plate_flag=p_flag,
        signal_state=sig_state,
        erratic_track_ids=erratic_ids,
    )


def apply_rules_to_all(
    frame_detections: List[FrameDetections],
    frames_bgr: Optional[List[np.ndarray]] = None,
    ocr_texts: Optional[List[Optional[str]]] = None,
    ocr_confidences: Optional[List[float]] = None,
    wheelie_detector=None,
    erratic_detector=None,
    track_results: Optional[dict] = None,    # {timestamp: list[TrackedDetection]}
) -> List[FrameVerdict]:
    """Run apply_rules() over every frame in the list."""
    from pipeline.heuristics import WheelieDetector, ErraticDrivingDetector

    # Create detectors if caller didn't pass them (stateful across frames)
    wd = wheelie_detector or WheelieDetector()
    ed = erratic_detector or ErraticDrivingDetector()

    results = []
    for i, fd in enumerate(frame_detections):
        bgr   = frames_bgr[i]     if frames_bgr      else None
        otxt  = ocr_texts[i]      if ocr_texts        else None
        oconf = ocr_confidences[i] if ocr_confidences else 0.0
        td    = track_results.get(fd.timestamp, []) if track_results else None

        results.append(apply_rules(
            fd,
            frame_bgr=bgr,
            ocr_text=otxt,
            ocr_confidence=oconf,
            wheelie_detector=wd,
            erratic_detector=ed,
            tracked_dets=td,
        ))
    return results
