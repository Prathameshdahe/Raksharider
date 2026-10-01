"""
pipeline/rules.py
-----------------
Per-frame violation rule engine — v2.1 (vehicle-specific findings).

Operates on FrameDetections from detector.py and produces a FrameVerdict that
carries one VehicleFinding PER VEHICLE in the frame. A violation is attached
to the vehicle it was observed on from the moment it is created; it is never
distributed across "every vehicle visible in the frame".

Chain preserved:  rider (person box) → motorcycle box → finding → track (run_pipeline)

Three outcomes per check, per vehicle, per frame:
  violations       observed positive
  observed_absent  checked and not present
  unobservable     could not be evaluated (no rider, rider too small, …)

Helmet policy: a `no_helmet` verdict requires POSITIVE visual evidence — a
`no_helmet` detection overlapping the rider's head region. Absence of a helmet
detection is NOT evidence of a missing helmet (occlusion, distance, detector
miss); it is "unclear".

Red-light logic is intentionally NOT a violation here: without stop-line
geometry a vehicle waiting at a red light would be flagged. The signal colour
is still reported on the FrameVerdict for context.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional

import numpy as np

from pipeline.detector import Detection, FrameDetections
from pipeline.heuristics import (
    PHONE_PERSON_IOU_THRESHOLD,
    WheelieDetector,
    missing_plate_flag,
    obscured_plate_check,
    phone_usage_detected,
    signal_color,
)
from pipeline.vehicle_class_gate import is_two_wheeler, TWO_WHEELER_CLASSES

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────
IOU_RIDER_MOTORCYCLE_THRESHOLD: float = 0.08
IOU_HELMET_HEAD_THRESHOLD:      float = 0.10
HEAD_FRACTION:                  float = 0.30
TRIPLE_RIDING_THRESHOLD:        int   = 3
MIN_PERSON_HEIGHT_FOR_RIDER_PX:  int = 45
MIN_PERSON_HEIGHT_FOR_HELMET_PX: int = 55
RIDER_PROXIMITY_FRACTION: float = 1.5

FOUR_WHEELER_CLASSES: frozenset = frozenset({
    "car", "bus", "truck", "mini_lcv", "auto_rickshaw", "vehicle",
})
ALL_VEHICLE_CLASSES = tuple(sorted(FOUR_WHEELER_CLASSES | TWO_WHEELER_CLASSES))


def is_four_wheeler(class_name: str) -> bool:
    return class_name.lower() in FOUR_WHEELER_CLASSES


HelmetStatus = Literal["helmet", "no_helmet", "unclear"]


# ── Output types ──────────────────────────────────────────────────────────────

@dataclass
class VehicleFinding:
    """What the rule engine concluded about ONE vehicle in ONE frame."""
    bbox:            List[float]
    vehicle_class:   str
    confidence:      float = 0.0                    # detection confidence supporting this finding
    rider_count:     int = 0
    helmet_status:   str = "not_evaluated"          # helmet | no_helmet | unclear | not_evaluated
    violations:      List[str] = field(default_factory=list)
    observed_absent: List[str] = field(default_factory=list)
    unobservable:    List[str] = field(default_factory=list)


@dataclass
class FrameVerdict:
    """Rule-layer output for a single frame."""
    timestamp:                float
    rider_count:              int
    helmet_status:            HelmetStatus
    violations:               List[str]          # union over findings (+ erratic) — frame-level summary
    avg_detection_confidence: float
    vehicle_type:             str          = "unknown"
    phone_usage:              bool         = False
    wheelie:                  bool         = False
    plate_flag:               str          = "missing"
    signal_state:             str          = "unknown"     # context only, never a violation
    erratic_track_ids:        List[int]    = field(default_factory=list)
    findings:                 List[VehicleFinding] = field(default_factory=list)


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


def _center_inside(inner: List[float], outer: List[float]) -> bool:
    cx = (inner[0] + inner[2]) / 2.0
    cy = (inner[1] + inner[3]) / 2.0
    return outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3]


def _resolve_contested_rider(person_bbox, moto_a_bbox, moto_b_bbox, idx_a: int, idx_b: int) -> int:
    """DashCop motor2_rider_iou_tracks: the motorcycle with the higher IoU owns the rider."""
    return idx_a if _iou(person_bbox, moto_a_bbox) >= _iou(person_bbox, moto_b_bbox) else idx_b


def _dominant_vehicle_class(fd: FrameDetections) -> str:
    priority = ["motorcycle", "bicycle", "bus", "truck", "mini_lcv", "car", "auto_rickshaw", "vehicle"]
    present = {d.class_name for d in fd.detections}
    for cls in priority:
        if cls in present:
            return cls
    return "unknown"


def _assign_riders(motorcycles: List[Detection], persons: List[Detection]) -> Dict[int, List[Detection]]:
    """Return {moto_index: [rider detections]} using geometry + contested-rider tiebreak."""
    moto_to_riders: Dict[int, List[Detection]] = {i: [] for i in range(len(motorcycles))}
    person_to_moto: Dict[int, int] = {}
    for p_idx, person in enumerate(persons):
        px1, py1, px2, py2 = person.bbox
        px_c = (px1 + px2) / 2.0
        ph = py2 - py1
        if ph < MIN_PERSON_HEIGHT_FOR_RIDER_PX:
            continue
        best_idx: Optional[int] = None
        best_score = 0.0
        for m_idx, moto in enumerate(motorcycles):
            mx1, my1, mx2, my2 = moto.bbox
            mw, mh = mx2 - mx1, my2 - my1
            if not (mx1 - mw * 0.10 <= px_c <= mx2 + mw * 0.10):
                continue
            if not (my1 - mh * 0.3 <= py2 <= my2 + mh * 0.3):
                continue
            v_overlap = max(0.0, min(py2, my2) - max(py1, my1))
            if v_overlap <= 0 or py2 < my1 or py1 > my2:
                continue
            if mh > 0 and not (0.50 <= ph / mh <= 2.0):
                continue
            score = v_overlap / max(1.0, ph)
            if score > best_score:
                best_score, best_idx = score, m_idx
        if best_idx is None:
            continue
        if p_idx in person_to_moto and person_to_moto[p_idx] != best_idx:
            prev = person_to_moto[p_idx]
            winner = _resolve_contested_rider(person.bbox, motorcycles[prev].bbox, motorcycles[best_idx].bbox, prev, best_idx)
            loser = best_idx if winner == prev else prev
            if person in moto_to_riders[loser]:
                moto_to_riders[loser].remove(person)
            best_idx = winner
        person_to_moto[p_idx] = best_idx
        if person not in moto_to_riders[best_idx]:
            moto_to_riders[best_idx].append(person)
    return moto_to_riders


def _helmet_call(rider: Detection, helmets: List[Detection], no_helmets: List[Detection]) -> HelmetStatus:
    """Positive evidence only: helmet box or no_helmet box on the head region."""
    if rider.bbox[3] - rider.bbox[1] < MIN_PERSON_HEIGHT_FOR_HELMET_PX:
        return "unclear"
    head = _head_box(rider.bbox)
    if any(_iou(head, h.bbox) >= IOU_HELMET_HEAD_THRESHOLD for h in helmets):
        return "helmet"
    if any(_iou(head, nh.bbox) >= IOU_HELMET_HEAD_THRESHOLD for nh in no_helmets):
        return "no_helmet"
    return "unclear"


def _plate_check(vehicle: Detection, plates: List[Detection], finding: VehicleFinding) -> None:
    """
    missing_plate per vehicle. A plate box inside the vehicle box = plate present
    (observed_absent). No plate box is NOT evidence of a missing plate — the plate
    detector's recall is far too low (8 plates in 20 frames on the sample clip) —
    so it is 'unobservable'. A positive missing-plate call needs a detector that
    sees the empty plate area; until then this violation is never raised per vehicle.
    """
    if any(_center_inside(p.bbox, vehicle.bbox) for p in plates):
        finding.observed_absent.append("missing_plate")
    else:
        finding.unobservable.append("missing_plate")


# ── Core rule engine ──────────────────────────────────────────────────────────

def apply_rules(
    fd: FrameDetections,
    frame_bgr: Optional[np.ndarray] = None,
    ocr_text: Optional[str] = None,
    ocr_confidence: float = 0.0,
    wheelie_detector=None,
    erratic_detector=None,
    tracked_dets: Optional[list] = None,
) -> FrameVerdict:
    motorcycles  = fd.by_class("motorcycle")
    persons      = fd.by_class("person")
    helmets      = fd.by_class("helmet")
    no_helmets   = fd.by_class("no_helmet")
    plates       = fd.by_class("license_plate")
    phones       = fd.by_class("cell_phone")
    signals      = fd.by_class("traffic_light")
    four_wheelers = fd.by_classes(*FOUR_WHEELER_CLASSES)
    vehicles_all = fd.by_classes(*ALL_VEHICLE_CLASSES)

    findings: List[VehicleFinding] = []
    all_riders: List[Detection] = []
    helmet_calls: List[HelmetStatus] = []
    max_riders_single_moto = 0

    # ── Two-wheelers: rider association, helmet, triple riding, phone, wheelie ──
    moto_to_riders = _assign_riders(motorcycles, persons) if motorcycles else {}
    tall_idx = WheelieDetector.tall_indices(motorcycles) if motorcycles else set()
    for m_idx, moto in enumerate(motorcycles):
        riders = moto_to_riders.get(m_idx, [])
        f = VehicleFinding(bbox=list(moto.bbox), vehicle_class="motorcycle", rider_count=len(riders))
        confs = [moto.confidence] + [r.confidence for r in riders]
        f.confidence = sum(confs) / len(confs)
        all_riders.extend(riders)
        max_riders_single_moto = max(max_riders_single_moto, len(riders))

        if not riders:
            f.helmet_status = "not_evaluated"
            f.unobservable += ["no_helmet", "triple_riding", "phone_usage"]
        else:
            calls = [_helmet_call(r, helmets, no_helmets) for r in riders]
            helmet_calls.extend(calls)
            if "no_helmet" in calls:
                f.helmet_status = "no_helmet"; f.violations.append("no_helmet")
            elif all(c == "helmet" for c in calls):
                f.helmet_status = "helmet"; f.observed_absent.append("no_helmet")
            else:
                f.helmet_status = "unclear"; f.unobservable.append("no_helmet")

            if len(riders) >= TRIPLE_RIDING_THRESHOLD:
                f.violations.append("triple_riding")
            else:
                f.observed_absent.append("triple_riding")

            # Phone: the phone must overlap one of THIS motorcycle's riders.
            if phones and any(_iou(r.bbox, p.bbox) >= PHONE_PERSON_IOU_THRESHOLD for r in riders for p in phones):
                f.violations.append("phone_usage")
            else:
                f.unobservable.append("phone_usage")   # phones are tiny; absence is not evidence

        if m_idx in tall_idx:
            f.violations.append("wheelie")             # review-only candidate; temporal gate in VehicleState
        else:
            f.observed_absent.append("wheelie")
        _plate_check(moto, plates, f)
        findings.append(f)

    # ── Four-wheelers: phone (driver inside the vehicle box), plate ──────────
    for veh in four_wheelers:
        f = VehicleFinding(bbox=list(veh.bbox), vehicle_class=veh.class_name, confidence=veh.confidence)
        occupants = [p for p in persons if _center_inside(p.bbox, veh.bbox)]
        if occupants and phones and any(
            _iou(o.bbox, p.bbox) >= PHONE_PERSON_IOU_THRESHOLD for o in occupants for p in phones
        ):
            f.violations.append("phone_usage")
        else:
            f.unobservable.append("phone_usage")
        _plate_check(veh, plates, f)
        findings.append(f)

    # ── Frame-level summary (legacy fields, derived from the findings) ───────
    rider_count = len(all_riders)
    if not helmet_calls:
        helmet_status: HelmetStatus = "unclear"
    else:
        counts = {s: helmet_calls.count(s) for s in ("helmet", "no_helmet", "unclear")}
        helmet_status = max(counts, key=lambda k: (counts[k], k == "helmet", k == "unclear"))

    violations: List[str] = []
    for f in findings:
        for v in f.violations:
            if v not in violations:
                violations.append(v)

    # Legacy clip-level missing-plate flag (kept for aggregate_verdicts / plate_flag)
    p_flag = missing_plate_flag(plates, ocr_text, ocr_confidence)
    if p_flag == "missing" and vehicles_all and obscured_plate_check(vehicles_all, plates):
        if "missing_plate" not in violations:
            violations.append("missing_plate")

    phone = any("phone_usage" in f.violations for f in findings) or phone_usage_detected(persons, phones)
    if phone and "phone_usage" not in violations and not findings:
        violations.append("phone_usage")   # person + phone but no vehicle box (front-cam) — frame-level only

    wheelie = False
    if motorcycles and wheelie_detector is not None:
        wheelie = wheelie_detector.update(motorcycles)    # temporal legacy flag for the clip summary

    erratic_ids: List[int] = []
    if erratic_detector is not None and tracked_dets is not None:
        erratic_ids = list(erratic_detector.update(tracked_dets, timestamp=fd.timestamp))
        if erratic_ids:
            violations.append("erratic_driving")

    sig_state = "unknown"
    if frame_bgr is not None and signals:
        sig_state = signal_color(frame_bgr, signals)

    used = [d for d in (all_riders + helmets + no_helmets + motorcycles) if d.confidence >= 0.35]
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
        findings=findings,
    )


def apply_rules_to_all(
    frame_detections: List[FrameDetections],
    frames_bgr: Optional[List[np.ndarray]] = None,
    ocr_texts: Optional[List[Optional[str]]] = None,
    ocr_confidences: Optional[List[float]] = None,
    wheelie_detector=None,
    erratic_detector=None,
    track_results: Optional[dict] = None,
) -> List[FrameVerdict]:
    """Run apply_rules() over every frame in temporal order."""
    from pipeline.heuristics import ErraticDrivingDetector
    wd = wheelie_detector or WheelieDetector()
    ed = erratic_detector or ErraticDrivingDetector()
    results = []
    for i, fd in enumerate(frame_detections):
        results.append(apply_rules(
            fd,
            frame_bgr=frames_bgr[i] if frames_bgr else None,
            ocr_text=ocr_texts[i] if ocr_texts else None,
            ocr_confidence=ocr_confidences[i] if ocr_confidences else 0.0,
            wheelie_detector=wd,
            erratic_detector=ed,
            tracked_dets=track_results.get(fd.timestamp, []) if track_results else None,
        ))
    return results
