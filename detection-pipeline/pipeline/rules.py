"""
pipeline/rules.py
─────────────────
Traffic-violation rule layer.

Operates on FrameDetections objects produced by detector.py and produces
per-frame verdicts that verification.py then aggregates across the whole
video.

Terminology
-----------
rider       : a `person` whose bounding box overlaps a `motorcycle` box
              above IOU_RIDER_MOTORCYCLE_THRESHOLD.
head region : the top HEAD_FRACTION of a person's bounding box.  Helmets
              are associated with a person only if the helmet box overlaps
              that region — this avoids false positives from helmets sitting
              on the seat or visible on bystanders.

Why IOU_RIDER_MOTORCYCLE_THRESHOLD = 0.1?
    In a typical traffic camera frame, a rider may be positioned loosely
    relative to the motorcycle — e.g., the person box might overlap only
    a corner of the bike or vice-versa.  A low threshold (0.1) lets us
    capture these looser overlaps without requiring pixel-perfect
    co-location.  Tune upward if you're getting too many false associations
    (e.g., pedestrians near parked bikes flagged as riders).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Literal, Optional, Tuple

from pipeline.detector import Detection, FrameDetections

logger = logging.getLogger(__name__)

# ── Tuneable constants ────────────────────────────────────────────────────────

# Minimum IoU between a person box and a motorcycle box to count that
# person as a rider.  See docstring above for rationale.
IOU_RIDER_MOTORCYCLE_THRESHOLD: float = 0.1

# Minimum IoU between a helmet box and a person's head region (top HEAD_FRACTION
# of that person's bbox) to call it a "helmet worn" association.
IOU_HELMET_HEAD_THRESHOLD: float = 0.15

# Fraction of a person's bounding box height that defines the "head region".
HEAD_FRACTION: float = 0.25

# Rider count above which we flag "triple_riding" (i.e., more than two people
# on a single motorcycle).
TRIPLE_RIDING_THRESHOLD: int = 3


# ── Result types ──────────────────────────────────────────────────────────────

HelmetStatus = Literal["helmet", "no_helmet", "unclear"]


@dataclass
class FrameVerdict:
    """Rule-layer output for a single frame."""

    timestamp: float
    rider_count: int
    helmet_status: HelmetStatus         # majority over all riders in frame
    violations: List[str]               # e.g. ["no_helmet", "triple_riding"]
    avg_detection_confidence: float     # mean YOLO conf for all detections used


# ── IoU helper ────────────────────────────────────────────────────────────────

def _iou(boxA: List[float], boxB: List[float]) -> float:
    """
    Compute Intersection-over-Union for two axis-aligned bounding boxes.

    Boxes are [x1, y1, x2, y2] in any consistent unit (pixels or normalised).
    Returns 0.0 if the boxes do not overlap at all.
    """
    inter_x1 = max(boxA[0], boxB[0])
    inter_y1 = max(boxA[1], boxB[1])
    inter_x2 = min(boxA[2], boxB[2])
    inter_y2 = min(boxA[3], boxB[3])

    inter_w = max(0.0, inter_x2 - inter_x1)
    inter_h = max(0.0, inter_y2 - inter_y1)
    inter_area = inter_w * inter_h

    if inter_area == 0.0:
        return 0.0

    areaA = max(0.0, boxA[2] - boxA[0]) * max(0.0, boxA[3] - boxA[1])
    areaB = max(0.0, boxB[2] - boxB[0]) * max(0.0, boxB[3] - boxB[1])
    union_area = areaA + areaB - inter_area

    return inter_area / union_area if union_area > 0 else 0.0


def _head_box(person_bbox: List[float]) -> List[float]:
    """
    Return the bounding box for the top HEAD_FRACTION of a person's bbox.

    This is the region we search for helmets — a helmet on someone's head
    must overlap here; a helmet on the ground does not.
    """
    x1, y1, x2, y2 = person_bbox
    head_height = (y2 - y1) * HEAD_FRACTION
    return [x1, y1, x2, y1 + head_height]


# ── Public API ────────────────────────────────────────────────────────────────

def apply_rules(fd: FrameDetections) -> FrameVerdict:
    """
    Apply all traffic-violation rules to a single frame's detections.

    Parameters
    ----------
    fd : FrameDetections
        Raw per-frame detections from detector.detect_frame().

    Returns
    -------
    FrameVerdict
        Structured verdict for this frame.
    """
    motorcycles = fd.by_class("motorcycle")
    persons = fd.by_class("person")
    helmets = fd.by_class("helmet")

    # ── Step 1: identify riders ───────────────────────────────────────────
    riders: List[Detection] = []
    for person in persons:
        for moto in motorcycles:
            if _iou(person.bbox, moto.bbox) >= IOU_RIDER_MOTORCYCLE_THRESHOLD:
                riders.append(person)
                break  # avoid double-counting person against multiple motos

    rider_count = len(riders)
    logger.debug(
        "Frame %.3fs: %d motorcycle(s), %d person(s), %d rider(s) identified",
        fd.timestamp, len(motorcycles), len(persons), rider_count,
    )

    # ── Step 2: helmet association per rider ─────────────────────────────
    helmet_calls: List[HelmetStatus] = []

    no_helmets = fd.by_class("no_helmet")

    for rider in riders:
        head = _head_box(rider.bbox)
        overlapping_helmets = [
            h for h in helmets
            if _iou(head, h.bbox) >= IOU_HELMET_HEAD_THRESHOLD
        ]
        overlapping_no_helmets = [
            nh for nh in no_helmets
            if _iou(head, nh.bbox) >= IOU_HELMET_HEAD_THRESHOLD
        ]

        if overlapping_helmets:
            call: HelmetStatus = "helmet"
        elif overlapping_no_helmets:
            call = "no_helmet"
        else:
            # Fallback for general models (e.g. COCO yolov8n): if no helmet detected on head
            if rider.confidence >= 0.5:
                call = "no_helmet"
            else:
                call = "unclear"

        helmet_calls.append(call)
        logger.debug(
            "  Rider bbox=%s head_region=%s -> %s",
            [f"{v:.0f}" for v in rider.bbox],
            [f"{v:.0f}" for v in head],
            call,
        )

    # ── Step 3: aggregate helmet status for this frame ───────────────────
    if not helmet_calls:
        # No riders detected in this frame
        helmet_status: HelmetStatus = "unclear"
    else:
        # Majority vote; ties go to "no_helmet" (conservative / safer flag)
        counts = {s: helmet_calls.count(s) for s in ("helmet", "no_helmet", "unclear")}
        helmet_status = max(counts, key=lambda k: (counts[k], k == "no_helmet"))

    # ── Step 4: determine violations ─────────────────────────────────────
    violations: List[str] = []

    if rider_count > 0 and helmet_status == "no_helmet":
        violations.append("no_helmet")

    if rider_count >= TRIPLE_RIDING_THRESHOLD:
        violations.append("triple_riding")

    # ── Step 5: mean confidence over relevant detections ─────────────────
    used_detections = riders + helmets + motorcycles
    avg_conf = (
        sum(d.confidence for d in used_detections) / len(used_detections)
        if used_detections
        else 0.0
    )

    return FrameVerdict(
        timestamp=fd.timestamp,
        rider_count=rider_count,
        helmet_status=helmet_status,
        violations=violations,
        avg_detection_confidence=avg_conf,
    )


def apply_rules_to_all(frame_detections: List[FrameDetections]) -> List[FrameVerdict]:
    """Apply apply_rules() over a list of FrameDetections."""
    return [apply_rules(fd) for fd in frame_detections]
