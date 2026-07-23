"""
tests/test_pipeline.py
──────────────────────
Unit tests for the rule layer (rider counting, helmet association).

Design philosophy
─────────────────
Tests use SYNTHETIC bounding-box inputs — no real footage, no model
loading.  This makes the suite fast (< 1 s total), fully deterministic,
and runnable in CI without GPU or network access.

Run:
    pytest tests/test_pipeline.py -v
"""

from __future__ import annotations

import pytest
from pipeline.rules import (
    HEAD_FRACTION,
    IOU_HELMET_HEAD_THRESHOLD,
    IOU_RIDER_MOTORCYCLE_THRESHOLD,
    _head_box,
    _iou,
    apply_rules,
)
from pipeline.detector import Detection, FrameDetections


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def make_detection(cls: str, bbox: list, conf: float = 0.9) -> Detection:
    return Detection(class_name=cls, confidence=conf, bbox=bbox)


def make_fd(timestamp: float, detections: list) -> FrameDetections:
    fd = FrameDetections(timestamp=timestamp)
    fd.detections = detections
    return fd


# ────────────────────────────────────────────────────────────────────────────
# IoU helper tests
# ────────────────────────────────────────────────────────────────────────────

class TestIou:
    def test_identical_boxes(self):
        assert _iou([0, 0, 10, 10], [0, 0, 10, 10]) == pytest.approx(1.0)

    def test_no_overlap(self):
        assert _iou([0, 0, 5, 5], [10, 10, 20, 20]) == pytest.approx(0.0)

    def test_partial_overlap(self):
        # Two 10×10 boxes overlapping by 5×5 (25) → IoU = 25/(100+100-25)
        iou = _iou([0, 0, 10, 10], [5, 5, 15, 15])
        assert iou == pytest.approx(25 / 175, abs=1e-6)

    def test_one_inside_other(self):
        # 4×4 box fully inside 10×10 → IoU = 16/(100+16-16) = 16/100
        iou = _iou([0, 0, 10, 10], [3, 3, 7, 7])
        assert iou == pytest.approx(16 / 100, abs=1e-6)

    def test_zero_area_box(self):
        assert _iou([5, 5, 5, 5], [0, 0, 10, 10]) == pytest.approx(0.0)


# ────────────────────────────────────────────────────────────────────────────
# Head-box helper tests
# ────────────────────────────────────────────────────────────────────────────

class TestHeadBox:
    def test_head_height_fraction(self):
        """Head region should be exactly HEAD_FRACTION of the person height."""
        person_bbox = [10.0, 20.0, 50.0, 120.0]  # height = 100
        hbox = _head_box(person_bbox)
        expected_bottom = 20.0 + 100.0 * HEAD_FRACTION
        assert hbox == pytest.approx([10.0, 20.0, 50.0, expected_bottom])

    def test_head_box_preserves_x(self):
        """X coordinates should be unchanged."""
        bbox = [30.0, 50.0, 80.0, 200.0]
        hbox = _head_box(bbox)
        assert hbox[0] == bbox[0]
        assert hbox[2] == bbox[2]


# ────────────────────────────────────────────────────────────────────────────
# Rider counting tests
# ────────────────────────────────────────────────────────────────────────────

class TestRiderCounting:
    def test_no_motorcycle_no_riders(self):
        """Persons without a motorcycle should not be counted as riders."""
        fd = make_fd(0.0, [
            make_detection("person", [0, 0, 50, 150]),
            make_detection("person", [60, 0, 110, 150]),
        ])
        verdict = apply_rules(fd)
        assert verdict.rider_count == 0

    def test_single_rider_clear_overlap(self):
        """Person overlapping moto above threshold → 1 rider."""
        fd = make_fd(0.0, [
            make_detection("motorcycle", [0, 50, 200, 200]),
            make_detection("person",     [20, 30, 100, 190]),  # clear overlap
        ])
        verdict = apply_rules(fd)
        assert verdict.rider_count == 1

    def test_two_riders_on_one_motorcycle(self):
        """Two persons overlapping the same motorcycle → 2 riders."""
        fd = make_fd(0.0, [
            make_detection("motorcycle", [0, 50, 300, 250]),
            make_detection("person",     [10, 40, 100, 240]),   # front rider
            make_detection("person",     [150, 40, 280, 240]),  # pillion rider
        ])
        verdict = apply_rules(fd)
        assert verdict.rider_count == 2

    def test_triple_riding_violation_flagged(self):
        """Three riders on one motorcycle should flag triple_riding."""
        fd = make_fd(0.0, [
            make_detection("motorcycle", [0, 50, 400, 250]),
            make_detection("person",     [10, 40, 110, 240]),
            make_detection("person",     [120, 40, 230, 240]),
            make_detection("person",     [240, 40, 370, 240]),
        ])
        verdict = apply_rules(fd)
        assert verdict.rider_count == 3
        assert "triple_riding" in verdict.violations

    def test_bystander_not_counted_as_rider(self):
        """A pedestrian far from the motorcycle should NOT be counted."""
        fd = make_fd(0.0, [
            make_detection("motorcycle", [0, 50, 100, 200]),
            make_detection("person",     [10, 40, 90, 190]),    # rider — overlaps
            make_detection("person",     [500, 0, 560, 200]),   # bystander — no overlap
        ])
        verdict = apply_rules(fd)
        assert verdict.rider_count == 1


# ────────────────────────────────────────────────────────────────────────────
# Helmet association tests
# ────────────────────────────────────────────────────────────────────────────

class TestHelmetAssociation:
    def test_helmet_on_head_detected(self):
        """Helmet overlapping the top 25% of the rider's bbox → 'helmet'."""
        # Person: y from 100 to 300 → head region: y 100 to 150
        fd = make_fd(0.0, [
            make_detection("motorcycle", [0, 80, 200, 320]),
            make_detection("person",     [10, 100, 100, 300]),
            make_detection("helmet",     [15, 102, 90, 145]),   # in head region
        ])
        verdict = apply_rules(fd)
        assert verdict.helmet_status == "helmet"
        assert "no_helmet" not in verdict.violations

    def test_no_helmet_high_conf_person(self):
        """High-confidence person with no helmet in head region → 'no_helmet'."""
        fd = make_fd(0.0, [
            make_detection("motorcycle", [0, 80, 200, 320]),
            make_detection("person",     [10, 100, 100, 300], conf=0.92),
            # No helmet detection
        ])
        verdict = apply_rules(fd)
        assert verdict.helmet_status == "no_helmet"
        assert "no_helmet" in verdict.violations

    def test_unclear_low_conf_person_no_helmet(self):
        """Low-confidence person, no helmet → 'unclear' (ambiguous evidence)."""
        fd = make_fd(0.0, [
            make_detection("motorcycle", [0, 80, 200, 320]),
            make_detection("person",     [10, 100, 100, 300], conf=0.35),
        ])
        verdict = apply_rules(fd)
        assert verdict.helmet_status == "unclear"

    def test_helmet_below_head_region_ignored(self):
        """Helmet box entirely below the head region should NOT count."""
        # Person: y 100 to 300 → head: y 100 to 150
        # Helmet: y 200 to 250 — this is on the torso, not the head
        fd = make_fd(0.0, [
            make_detection("motorcycle", [0, 80, 200, 320]),
            make_detection("person",     [10, 100, 100, 300], conf=0.85),
            make_detection("helmet",     [15, 200, 90, 250]),  # torso level
        ])
        verdict = apply_rules(fd)
        # Helmet on torso should not count; high-conf person has no head-helmet
        assert verdict.helmet_status == "no_helmet"

    def test_no_motorcycle_no_helmet_check(self):
        """Without a motorcycle, there are no riders, so no helmet check."""
        fd = make_fd(0.0, [
            make_detection("person",  [10, 100, 100, 300]),
            make_detection("helmet",  [15, 102, 90, 145]),
        ])
        verdict = apply_rules(fd)
        assert verdict.rider_count == 0
        assert verdict.helmet_status == "unclear"  # no riders → unclear
        assert verdict.violations == []


# ────────────────────────────────────────────────────────────────────────────
# Edge-case tests
# ────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_empty_frame(self):
        """Empty detection list should produce safe defaults."""
        fd = make_fd(1.5, [])
        verdict = apply_rules(fd)
        assert verdict.rider_count == 0
        assert verdict.helmet_status == "unclear"
        assert verdict.violations == []
        assert verdict.avg_detection_confidence == 0.0

    def test_motorcycle_only_no_persons(self):
        """Motorcycle detected but no persons → 0 riders."""
        fd = make_fd(0.0, [
            make_detection("motorcycle", [50, 50, 250, 200]),
        ])
        verdict = apply_rules(fd)
        assert verdict.rider_count == 0
        assert verdict.violations == []
