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

IOU_RIDER_MOTORCYCLE_THRESHOLD: float = 0.08   # IoU overlap — tightened to reduce pedestrian false positives
IOU_HELMET_HEAD_THRESHOLD:      float = 0.10   # IoU for helmet-to-head overlap
HEAD_FRACTION:                  float = 0.30   # top 30% of person bbox = head region
TRIPLE_RIDING_THRESHOLD:        int   = 3

# Minimum person box height in pixels to be counted as a rider.
# Persons smaller than this are too far away / background — not on the motorcycle.
MIN_PERSON_HEIGHT_FOR_RIDER_PX: int = 45

# Minimum person box height to reliably evaluate helmet status.
# Below this, the head region is only ~15px tall — helmet models are unreliable.
MIN_PERSON_HEIGHT_FOR_HELMET_PX: int = 55

# Proximity fallback: if person centroid is within this fraction of moto bbox height,
# count them as a rider even when IoU is low (rear-camera / dashcam angle)
RIDER_PROXIMITY_FRACTION: float = 1.5

# ── Vehicle class taxonomy ───────────────────────────────────────────────────
# Checks that are ONLY valid for two-wheelers:
#   helmet, triple-riding, wheelie
# Checks that apply to ALL vehicle classes:
#   plate/OCR, phone usage, erratic driving, signal violation

from pipeline.vehicle_class_gate import (
    is_two_wheeler,
    TWO_WHEELER_CLASSES,
)
FOUR_WHEELER_CLASSES: frozenset = frozenset({
    "car", "bus", "truck", "mini_lcv", "auto_rickshaw", "vehicle",
})


def is_four_wheeler(class_name: str) -> bool:
    """True iff class_name is a four-wheeler (car/bus/truck/etc)."""
    return class_name.lower() in FOUR_WHEELER_CLASSES


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


def _resolve_contested_rider(
    person_bbox: List[float],
    moto_a_bbox: List[float],
    moto_b_bbox: List[float],
    idx_a: int,
    idx_b: int,
) -> int:
    """
    Ported from DashCop inference/instance_funcs.py (motor2_rider_iou_tracks pattern).

    When a rider overlaps two motorcycles with similar scores, explicitly compute
    full IoU between the rider and each motorcycle, then assign the rider to the
    motorcycle with the HIGHER IoU.  This prevents the common dashcam error where
    a pedestrian walking between two parked bikes gets double-counted.

    Returns: the winning motorcycle index (idx_a or idx_b).
    """
    iou_a = _iou(person_bbox, moto_a_bbox)
    iou_b = _iou(person_bbox, moto_b_bbox)
    return idx_a if iou_a >= iou_b else idx_b


def _dominant_vehicle_class(fd: FrameDetections) -> str:
    """
    Pick the most-specific vehicle class present in this frame.
    Motorcycle beats car — violations like triple-riding are two-wheeler-specific,
    so when both appear in a frame the motorcycle is the primary subject.
    """
    priority = [
        "motorcycle", "bicycle",          # two-wheelers first
        "bus", "truck", "mini_lcv",
        "car", "auto_rickshaw", "vehicle",
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

    # ── Rider-motorcycle association (Two-wheeler guard) ──────────────────────
    # GUARD: helmet, triple-riding, and rider association are ONLY valid for
    # two-wheelers. If no motorcycle is detected in this frame, skip the entire
    # block. Returning None/empty is semantically "not applicable", not "compliant".
    moto_to_riders: dict[int, List[Detection]] = {i: [] for i in range(len(motorcycles))}
    all_assigned_riders: List[Detection] = []
    max_riders_on_single_moto = 0
    rider_count = 0

    if motorcycles:  # ← guard: only evaluate rider/helmet on frames with motorcycles
        # Track the current assignment: person_idx → moto_idx
        # Allows contested-rider re-resolution (DashCop pattern)
        person_to_moto: dict[int, int] = {}   # person list index → moto index

        for p_idx, person in enumerate(persons):
            px1, py1, px2, py2 = person.bbox
            px_c = (px1 + px2) / 2.0
            ph = py2 - py1

            # Guard 1: person too small → background pedestrian, not a rider
            if ph < MIN_PERSON_HEIGHT_FOR_RIDER_PX:
                continue

            best_moto_idx: Optional[int] = None
            best_overlap_score: float = 0.0

            for m_idx, moto in enumerate(motorcycles):
                mx1, my1, mx2, my2 = moto.bbox
                mw = mx2 - mx1
                mh = my2 - my1

                # 1. Horizontal containment: person center within motorcycle width (+10% margin)
                #    Reduced from 15% to reduce pedestrians walking beside the road.
                h_margin = mw * 0.10
                if not (mx1 - h_margin <= px_c <= mx2 + h_margin):
                    continue

                # 2. Feet placement: person's bottom (py2) must be within the motorcycle's
                #    vertical span — between my1 (top) and my2 + 0.3*mh (just below).
                #    This requires the person to be physically on/above the bike seat,
                #    not a background pedestrian behind the bike at a different depth.
                if not (my1 - mh * 0.3 <= py2 <= my2 + mh * 0.3):
                    continue

                # 3. Vertical seating: person's bottom must overlap motorcycle body
                v_overlap = max(0.0, min(py2, my2) - max(py1, my1))
                if v_overlap <= 0:
                    continue

                # 4. Person cannot be completely below or absurdly above motorcycle
                if py2 < my1 or py1 > my2:
                    continue

                # 5. Relative scale sanity: tightened from 0.35–2.2 to 0.50–2.0
                #    Prevents very small/large scale mismatches (different depths)
                if mh > 0 and not (0.50 <= ph / mh <= 2.0):
                    continue

                overlap_score = v_overlap / max(1.0, ph)
                if overlap_score > best_overlap_score:
                    best_overlap_score = overlap_score
                    best_moto_idx = m_idx

            if best_moto_idx is None:
                continue

            # ── Contested-rider resolution (DashCop motor2_rider_iou_tracks) ──
            # If this person was already assigned to a DIFFERENT motorcycle,
            # run the explicit IoU tie-breaker to decide the true owner.
            if p_idx in person_to_moto:
                prev_moto_idx = person_to_moto[p_idx]
                if prev_moto_idx != best_moto_idx:
                    winner_idx = _resolve_contested_rider(
                        person.bbox,
                        motorcycles[prev_moto_idx].bbox,
                        motorcycles[best_moto_idx].bbox,
                        prev_moto_idx,
                        best_moto_idx,
                    )
                    # Remove person from the losing motorcycle's roster
                    loser_idx = best_moto_idx if winner_idx == prev_moto_idx else prev_moto_idx
                    if person in moto_to_riders[loser_idx]:
                        moto_to_riders[loser_idx].remove(person)
                    best_moto_idx = winner_idx

            person_to_moto[p_idx] = best_moto_idx
            if person not in moto_to_riders[best_moto_idx]:
                moto_to_riders[best_moto_idx].append(person)
            if person not in all_assigned_riders:
                all_assigned_riders.append(person)

        # Max riders on ANY SINGLE motorcycle in this frame
        max_riders_on_single_moto = max(
            (len(r) for r in moto_to_riders.values()), default=0
        )
        rider_count = len(all_assigned_riders)

    logger.debug(
        "Frame %.3fs: %d moto(s), %d person(s), max_riders_single_moto=%d, total_riders=%d",
        fd.timestamp, len(motorcycles), len(persons), max_riders_on_single_moto, rider_count,
    )

    # ── Helmet association (two-wheeler only, via guard above) ────────────────
    helmet_calls: List[HelmetStatus] = []
    for rider in all_assigned_riders:  # empty list if no motorcycles → skipped entirely
        ph_px = rider.bbox[3] - rider.bbox[1]   # pixel height of this rider

        # Guard: person box too small → too far from camera to evaluate helmet reliably.
        # At < 55px tall, the head region is only ~16px — helmet model outputs at this
        # resolution are unreliable and almost always produce false "no_helmet" calls.
        # Mark as "unclear" (benefit of the doubt) rather than auto-flagging.
        if ph_px < MIN_PERSON_HEIGHT_FOR_HELMET_PX:
            helmet_calls.append("unclear")
            logger.debug(
                "Helmet check skipped for small rider (height=%.0fpx < %dpx threshold)",
                ph_px, MIN_PERSON_HEIGHT_FOR_HELMET_PX,
            )
            continue

        head = _head_box(rider.bbox)
        if any(_iou(head, h.bbox) >= IOU_HELMET_HEAD_THRESHOLD for h in helmets):
            call: HelmetStatus = "helmet"
        elif any(_iou(head, nh.bbox) >= IOU_HELMET_HEAD_THRESHOLD for nh in no_helmets):
            call = "no_helmet"
        else:
            # No helmet/no_helmet detection overlaps the head region.
            # Default to "no_helmet" only for high-confidence riders (rider is clearly
            # visible and close enough that we'd expect to see a helmet if there was one).
            # For medium-confidence riders, "unclear" is safer.
            call = "no_helmet" if rider.confidence >= 0.65 else "unclear"
        helmet_calls.append(call)

    if not helmet_calls:
        # No motorcycles in frame → helmet is not applicable ("unclear" = not evaluated)
        helmet_status: HelmetStatus = "unclear"
    else:
        counts = {s: helmet_calls.count(s) for s in ("helmet", "no_helmet", "unclear")}
        # Tiebreak update: when "unclear" and "no_helmet" tie, "unclear" wins.
        # Rationale: we should not flag a person for a violation we cannot confirm.
        helmet_status = max(counts, key=lambda k: (counts[k], k == "helmet", k == "unclear"))


    # ── Violations ────────────────────────────────────────────────────────────
    violations: List[str] = []

    # Helmet + triple-riding: ONLY fire when motorcycles present (guarded above)
    if motorcycles:
        if rider_count > 0 and helmet_status == "no_helmet":
            violations.append("no_helmet")
        if max_riders_on_single_moto >= TRIPLE_RIDING_THRESHOLD:
            violations.append("triple_riding")

    # Phone usage
    phone = phone_usage_detected(persons, phones)
    if phone:
        violations.append("phone_usage")

    # Wheelie: two-wheeler only
    wheelie = False
    if motorcycles and wheelie_detector is not None:
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
    # Use relevant detections only; filter very low-conf to avoid dragging average down
    used = [d for d in (all_assigned_riders + helmets + no_helmets + motorcycles)
            if d.confidence >= 0.35]
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
