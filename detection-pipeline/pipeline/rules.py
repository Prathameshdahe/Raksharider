"""
pipeline/rules.py
-----------------
Per-frame violation rule engine.

Operates on FrameDetections from detector.py and produces FrameVerdict objects
that verification.py aggregates across the clip.

Terminology:
  rider       -- person whose bbox overlaps a motorcycle bbox above
                 IOU_RIDER_MOTORCYCLE_THRESHOLD
  head region -- top HEAD_FRACTION of a person's bbox; helmets must overlap
                 this region to count as worn (prevents false positives from
                 helmets on the ground or on bystanders)

IOU_RIDER_MOTORCYCLE_THRESHOLD = 0.1:
    Traffic camera frames often show loose overlap between a rider and the
    motorcycle (partial occlusion, angle, distance). 0.1 captures these without
    pulling in nearby pedestrians. Tune upward if you see false associations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Literal

from pipeline.detector import Detection, FrameDetections

logger = logging.getLogger(__name__)

IOU_RIDER_MOTORCYCLE_THRESHOLD: float = 0.1
IOU_HELMET_HEAD_THRESHOLD: float = 0.15
HEAD_FRACTION: float = 0.25
TRIPLE_RIDING_THRESHOLD: int = 3

HelmetStatus = Literal["helmet", "no_helmet", "unclear"]


@dataclass
class FrameVerdict:
    """Rule-layer output for a single frame."""
    timestamp: float
    rider_count: int
    helmet_status: HelmetStatus
    violations: List[str]
    avg_detection_confidence: float


def _iou(boxA: List[float], boxB: List[float]) -> float:
    """IoU for two [x1, y1, x2, y2] boxes. Returns 0.0 for non-overlapping."""
    ix1 = max(boxA[0], boxB[0])
    iy1 = max(boxA[1], boxB[1])
    ix2 = min(boxA[2], boxB[2])
    iy2 = min(boxA[3], boxB[3])

    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter == 0.0:
        return 0.0

    areaA = max(0.0, boxA[2] - boxA[0]) * max(0.0, boxA[3] - boxA[1])
    areaB = max(0.0, boxB[2] - boxB[0]) * max(0.0, boxB[3] - boxB[1])
    union = areaA + areaB - inter
    return inter / union if union > 0 else 0.0


def _head_box(person_bbox: List[float]) -> List[float]:
    """Return the top HEAD_FRACTION of a person bbox as the expected helmet region."""
    x1, y1, x2, y2 = person_bbox
    return [x1, y1, x2, y1 + (y2 - y1) * HEAD_FRACTION]


def apply_rules(fd: FrameDetections) -> FrameVerdict:
    """
    Apply all violation rules to one frame's detections.

    Returns a FrameVerdict with rider count, helmet status, and any violations.
    """
    motorcycles = fd.by_class("motorcycle")
    persons     = fd.by_class("person")
    helmets     = fd.by_class("helmet")
    no_helmets  = fd.by_class("no_helmet")

    riders: List[Detection] = []
    for person in persons:
        for moto in motorcycles:
            if _iou(person.bbox, moto.bbox) >= IOU_RIDER_MOTORCYCLE_THRESHOLD:
                riders.append(person)
                break  # one motorcycle match per person is enough

    rider_count = len(riders)
    logger.debug(
        "Frame %.3fs: %d motorcycle(s), %d person(s), %d rider(s)",
        fd.timestamp, len(motorcycles), len(persons), rider_count,
    )

    helmet_calls: List[HelmetStatus] = []
    for rider in riders:
        head = _head_box(rider.bbox)
        if any(_iou(head, h.bbox) >= IOU_HELMET_HEAD_THRESHOLD for h in helmets):
            call: HelmetStatus = "helmet"
        elif any(_iou(head, nh.bbox) >= IOU_HELMET_HEAD_THRESHOLD for nh in no_helmets):
            call = "no_helmet"
        else:
            # No model-detected helmet/no-helmet near this rider's head.
            # High-confidence person detection with no helmet is flagged as no_helmet;
            # low-confidence detection is inconclusive.
            call = "no_helmet" if rider.confidence >= 0.5 else "unclear"
        helmet_calls.append(call)

    if not helmet_calls:
        helmet_status: HelmetStatus = "unclear"
    else:
        counts = {s: helmet_calls.count(s) for s in ("helmet", "no_helmet", "unclear")}
        helmet_status = max(counts, key=lambda k: (counts[k], k == "no_helmet"))

    violations: List[str] = []
    if rider_count > 0 and helmet_status == "no_helmet":
        violations.append("no_helmet")
    if rider_count >= TRIPLE_RIDING_THRESHOLD:
        violations.append("triple_riding")

    used = riders + helmets + motorcycles
    avg_conf = sum(d.confidence for d in used) / len(used) if used else 0.0

    return FrameVerdict(
        timestamp=fd.timestamp,
        rider_count=rider_count,
        helmet_status=helmet_status,
        violations=violations,
        avg_detection_confidence=avg_conf,
    )


def apply_rules_to_all(frame_detections: List[FrameDetections]) -> List[FrameVerdict]:
    """Run apply_rules() over every frame in the list."""
    return [apply_rules(fd) for fd in frame_detections]
