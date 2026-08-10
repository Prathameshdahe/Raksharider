"""
tests/test_heuristics.py
─────────────────────────
Unit tests for pipeline/heuristics.py

All tests are zero-dependency on YOLO / torch.
Synthetic Detection-like objects are used via a simple dataclass stub.
"""

from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import numpy as np

from pipeline.heuristics import (
    missing_plate_flag,
    obscured_plate_check,
    WheelieDetector,
    phone_usage_detected,
    ErraticDrivingDetector,
    signal_color,
    WHEELIE_ASPECT_RATIO_THRESHOLD,
    ERRATIC_VARIANCE_THRESHOLD,
)


# ── Minimal stub so we don't need to import detector.py ───────────────────────
from dataclasses import dataclass

@dataclass
class Det:
    """Minimal Detection stub."""
    class_name: str
    confidence: float
    bbox: list       # [x1, y1, x2, y2]
    track_id: int = -1


# ── 1. Plate heuristics ───────────────────────────────────────────────────────

class TestMissingPlateFlag:

    def test_no_plate_detected_returns_missing(self):
        assert missing_plate_flag([], None, 0.0) == "missing"

    def test_plate_detected_no_ocr_text_returns_low_confidence(self):
        plate = Det("license_plate", 0.8, [100, 100, 200, 150])
        assert missing_plate_flag([plate], None, 0.0) == "low_confidence"

    def test_plate_detected_low_conf_returns_low_confidence(self):
        plate = Det("license_plate", 0.8, [100, 100, 200, 150])
        assert missing_plate_flag([plate], "MH12AB1234", 0.10) == "low_confidence"

    def test_plate_detected_good_ocr_returns_ok(self):
        plate = Det("license_plate", 0.85, [100, 100, 200, 150])
        assert missing_plate_flag([plate], "MH12AB1234", 0.85) == "ok"


class TestObscuredPlateCheck:

    def test_vehicle_present_no_plate_returns_true(self):
        vehicle = Det("motorcycle", 0.9, [50, 50, 300, 400])
        assert obscured_plate_check([vehicle], []) is True

    def test_vehicle_and_plate_both_present_returns_false(self):
        vehicle = Det("motorcycle", 0.9, [50, 50, 300, 400])
        plate   = Det("license_plate", 0.7, [100, 350, 200, 390])
        assert obscured_plate_check([vehicle], [plate]) is False

    def test_no_vehicle_no_plate_returns_false(self):
        assert obscured_plate_check([], []) is False


# ── 2. Wheelie detection ──────────────────────────────────────────────────────

class TestWheelieDetector:

    def _moto(self, x1, y1, x2, y2):
        return Det("motorcycle", 0.9, [x1, y1, x2, y2])

    def test_wide_motorcycle_no_wheelie(self):
        """Normal motorcycle bbox (wider than tall) — no wheelie."""
        wd = WheelieDetector()
        moto = self._moto(100, 200, 300, 350)   # w=200, h=150 → ratio=0.75
        for _ in range(5):
            result = wd.update([moto])
        assert result is False

    def test_tall_motorcycle_triggers_wheelie(self):
        """Tall narrow bbox (rear wheel up) triggers wheelie after MIN_HITS frames."""
        wd = WheelieDetector()
        moto = self._moto(100, 50, 160, 450)    # w=60, h=400 → ratio=6.7
        result = None
        for _ in range(5):
            result = wd.update([moto])
        assert result is True

    def test_wheelie_resets_after_normal_frame(self):
        """After a normal frame, the consecutive counter resets."""
        wd = WheelieDetector()
        tall_moto = self._moto(100, 50, 160, 450)
        wide_moto = self._moto(100, 200, 300, 350)
        for _ in range(3):
            wd.update([tall_moto])
        # One normal frame resets it
        wd.update([wide_moto])
        result = wd.update([wide_moto])
        assert result is False

    def test_no_motorcycles_no_wheelie(self):
        """Empty frame list — no crash, no wheelie."""
        wd = WheelieDetector()
        for _ in range(5):
            result = wd.update([])
        assert result is False

    def test_aspect_ratio_exactly_at_threshold(self):
        """Ratio exactly at threshold should trigger (>=)."""
        wd = WheelieDetector()
        # Make ratio exactly WHEELIE_ASPECT_RATIO_THRESHOLD
        w = 100
        h = int(w * WHEELIE_ASPECT_RATIO_THRESHOLD)
        moto = self._moto(0, 0, w, h)
        for _ in range(5):
            result = wd.update([moto])
        assert result is True


# ── 3. Phone usage ────────────────────────────────────────────────────────────

class TestPhoneUsage:

    def test_overlapping_person_and_phone_detected(self):
        person = Det("person",     0.9, [100, 50, 300, 500])
        phone  = Det("cell_phone", 0.7, [150, 60, 250, 150])   # inside person bbox
        assert phone_usage_detected([person], [phone]) is True

    def test_non_overlapping_no_detection(self):
        person = Det("person",     0.9, [100, 50, 300, 500])
        phone  = Det("cell_phone", 0.7, [500, 50, 600, 150])   # far away
        assert phone_usage_detected([person], [phone]) is False

    def test_no_phone_no_detection(self):
        person = Det("person", 0.9, [100, 50, 300, 500])
        assert phone_usage_detected([person], []) is False

    def test_no_person_no_detection(self):
        phone = Det("cell_phone", 0.7, [150, 60, 250, 150])
        assert phone_usage_detected([], [phone]) is False


# ── 4. Erratic driving ────────────────────────────────────────────────────────

class TestErraticDriving:

    def _tracked(self, track_id, x1, y1, x2, y2):
        return Det("motorcycle", 0.9, [x1, y1, x2, y2], track_id=track_id)

    def test_stationary_track_not_erratic(self):
        """A vehicle that barely moves is not erratic."""
        ed = ErraticDrivingDetector()
        for i in range(10):
            ed.update([self._tracked(1, 100+i, 100, 200+i, 300)])
        erratic = ed.update([self._tracked(1, 110, 100, 210, 300)])
        assert 1 not in erratic, "Slow-moving vehicle should not be flagged erratic"

    def test_wildly_jumping_track_is_erratic(self):
        """A vehicle teleporting all over the frame is erratic."""
        ed = ErraticDrivingDetector()
        positions = [(10, 10), (600, 400), (50, 350), (700, 20),
                     (200, 500), (800, 100), (30, 200), (900, 400)]
        for i, (x, y) in enumerate(positions):
            erratic = ed.update([self._tracked(1, x, y, x+80, y+150)])
        assert 1 in erratic, "Teleporting vehicle should be flagged as erratic"

    def test_untracked_detections_ignored(self):
        """Detections with track_id=-1 are pass-through and should not affect erratic state."""
        ed = ErraticDrivingDetector()
        for _ in range(10):
            erratic = ed.update([self._tracked(-1, 100, 100, 200, 300)])
        assert len(erratic) == 0

    def test_two_tracks_independent(self):
        """One erratic track should not flag a stable track."""
        ed = ErraticDrivingDetector()
        positions = [(10, 10), (600, 400), (50, 350), (700, 20),
                     (200, 500), (800, 100), (30, 200), (900, 400)]
        for i, (x, y) in enumerate(positions):
            erratic = ed.update([
                self._tracked(1, x,   y,   x+80,  y+150),  # erratic
                self._tracked(2, 300, 300, 400,   450),     # stable
            ])
        assert 1 in erratic,     "Erratic track should be flagged"
        assert 2 not in erratic, "Stable track should NOT be flagged"

    def test_reset_clears_history(self):
        """After reset(), previously erratic tracks are no longer flagged."""
        ed = ErraticDrivingDetector()
        positions = [(10, 10), (600, 400), (50, 350), (700, 20),
                     (200, 500), (800, 100), (30, 200), (900, 400)]
        for x, y in positions:
            ed.update([self._tracked(1, x, y, x+80, y+150)])
        ed.reset()
        # After reset, no history → nothing flagged
        erratic = ed.update([self._tracked(1, 100, 100, 200, 300)])
        assert 1 not in erratic


# ── 5. Signal color detection ─────────────────────────────────────────────────

class TestSignalColor:

    def _red_frame(self, size=100):
        """BGR frame that is entirely bright red (HSV: hue~0, sat=255, val=255)."""
        frame = np.zeros((size, size, 3), dtype=np.uint8)
        frame[:, :] = (0, 0, 255)   # BGR red
        return frame

    def _green_frame(self, size=100):
        """BGR frame that is entirely bright green."""
        frame = np.zeros((size, size, 3), dtype=np.uint8)
        frame[:, :] = (0, 255, 0)   # BGR green
        return frame

    def _black_frame(self, size=100):
        return np.zeros((size, size, 3), dtype=np.uint8)

    def _signal_det(self, x1=0, y1=0, x2=100, y2=100):
        return Det("traffic_light", 0.8, [x1, y1, x2, y2])

    def test_no_signals_returns_unknown(self):
        frame = self._red_frame()
        assert signal_color(frame, []) == "unknown"

    def test_red_frame_returns_red(self):
        frame  = self._red_frame()
        signal = self._signal_det()
        result = signal_color(frame, [signal])
        assert result == "red", f"Expected 'red', got '{result}'"

    def test_green_frame_returns_green(self):
        frame  = self._green_frame()
        signal = self._signal_det()
        result = signal_color(frame, [signal])
        assert result == "green", f"Expected 'green', got '{result}'"

    def test_black_frame_returns_unknown(self):
        frame  = self._black_frame()
        signal = self._signal_det()
        result = signal_color(frame, [signal])
        assert result == "unknown"
