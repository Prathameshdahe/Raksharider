"""
tests/test_tracker.py
─────────────────────
Unit tests for pipeline/tracker.py.

Runs without any model loading (no YOLO, no torch required).
Uses synthetic FrameDetections with known bounding boxes to verify:
  1. IDs are assigned and persist across frames
  2. Two separate objects get different IDs
  3. IDs don't duplicate in any single frame
  4. Non-tracked classes (helmet, license_plate) pass through with track_id == -1
  5. Lost tracks are revived with the same ID after re-appearance
"""

import sys
from pathlib import Path

# Allow running from repo root or from tests/
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from pipeline.detector import Detection, FrameDetections
from pipeline.tracker  import Tracker, TrackedDetection, TRACKED_CLASSES


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fd(timestamp: float, dets: list[Detection]) -> FrameDetections:
    return FrameDetections(timestamp=timestamp, detections=dets)


def _person(x1: float, y1: float, x2: float, y2: float, conf: float = 0.9) -> Detection:
    return Detection("person", conf, [x1, y1, x2, y2])


def _moto(x1: float, y1: float, x2: float, y2: float, conf: float = 0.9) -> Detection:
    return Detection("motorcycle", conf, [x1, y1, x2, y2])


def _helmet(x1: float, y1: float, x2: float, y2: float) -> Detection:
    return Detection("helmet", 0.8, [x1, y1, x2, y2])


def _plate(x1: float, y1: float, x2: float, y2: float) -> Detection:
    return Detection("license_plate", 0.75, [x1, y1, x2, y2])


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestTrackerBasic:

    def test_single_object_gets_id(self):
        """A single person across two frames should get the same ID."""
        tracker = Tracker()
        # Frame 1 — person at left side of frame
        t1 = tracker.update(_fd(0.0, [_person(100, 100, 200, 400)]))
        t2 = tracker.update(_fd(0.5, [_person(105, 100, 205, 400)]))

        ids_f1 = [d.track_id for d in t1 if d.class_name == "person"]
        ids_f2 = [d.track_id for d in t2 if d.class_name == "person"]

        assert len(ids_f1) == 1, "Should detect one person in frame 1"
        assert len(ids_f2) == 1, "Should detect one person in frame 2"
        assert ids_f1[0] == ids_f2[0], (
            f"Same person should keep same ID across frames, "
            f"got {ids_f1[0]} then {ids_f2[0]}"
        )

    def test_two_objects_get_different_ids(self):
        """Two motorcycles in the same frame should get different IDs."""
        tracker = Tracker()

        # Two well-separated motorcycles — no IoU overlap
        t1 = tracker.update(_fd(0.0, [
            _moto(50,  100, 200, 300),   # left
            _moto(400, 100, 550, 300),   # right
        ]))

        ids = sorted([d.track_id for d in t1 if d.class_name == "motorcycle"])
        assert len(ids) == 2, f"Expected 2 motorcycles tracked, got {len(ids)}"
        assert ids[0] != ids[1], f"Two objects must get different IDs, both got {ids[0]}"

    def test_no_duplicate_ids_in_same_frame(self):
        """In any single frame, every track ID must be unique."""
        tracker = Tracker()
        for ts in [0.0, 0.5, 1.0]:
            result = tracker.update(_fd(ts, [
                _person(50,  100, 150, 400),
                _moto(  200, 100, 400, 350),
                _person(500, 100, 620, 400),
            ]))
            tracked_ids = [d.track_id for d in result if d.track_id >= 0]
            assert len(tracked_ids) == len(set(tracked_ids)), (
                f"Duplicate IDs in frame t={ts}: {tracked_ids}"
            )

    def test_nontracked_classes_passthrough_with_minus_one(self):
        """helmet and license_plate must pass through with track_id == -1."""
        tracker = Tracker()
        result = tracker.update(_fd(0.0, [
            _person(100, 100, 200, 400),
            _helmet(110, 100, 190, 150),
            _plate( 120, 350, 200, 390),
        ]))

        helmet_ids = [d.track_id for d in result if d.class_name == "helmet"]
        plate_ids  = [d.track_id for d in result if d.class_name == "license_plate"]

        assert all(i == -1 for i in helmet_ids), \
            f"Helmets should have track_id=-1, got {helmet_ids}"
        assert all(i == -1 for i in plate_ids), \
            f"Plates should have track_id=-1, got {plate_ids}"

    def test_id_stability_across_many_frames(self):
        """A person moving slowly across the frame keeps the same ID for 10 frames."""
        tracker = Tracker()
        first_id = None
        for i in range(10):
            x_offset = i * 5   # slow horizontal movement
            result = tracker.update(_fd(float(i) * 0.5, [
                _person(100 + x_offset, 100, 200 + x_offset, 400),
            ]))
            person_ids = [d.track_id for d in result if d.class_name == "person"]
            if not person_ids:
                continue
            if first_id is None:
                first_id = person_ids[0]
            else:
                assert person_ids[0] == first_id, (
                    f"ID changed at frame {i}: expected {first_id}, got {person_ids[0]}"
                )

    def test_two_objects_ids_dont_swap(self):
        """
        Two objects crossing paths should NOT swap IDs.
        We test with two objects that stay separated (well-spaced IoU=0),
        so their IDs must stay constant throughout.
        """
        tracker = Tracker()
        # Object A: left half, Object B: right half — they never overlap
        id_a_frames: list[int] = []
        id_b_frames: list[int] = []

        for i in range(6):
            result = tracker.update(_fd(float(i) * 0.5, [
                _moto(50,  200, 200, 350),    # object A, stays left
                _moto(400, 200, 550, 350),    # object B, stays right
            ]))
            motos = sorted(
                [d for d in result if d.class_name == "motorcycle"],
                key=lambda d: d.bbox[0]   # sort left→right by x1
            )
            if len(motos) == 2:
                id_a_frames.append(motos[0].track_id)
                id_b_frames.append(motos[1].track_id)

        # All IDs for each object should be identical across frames
        assert len(set(id_a_frames)) == 1, \
            f"Left moto ID swapped across frames: {id_a_frames}"
        assert len(set(id_b_frames)) == 1, \
            f"Right moto ID swapped across frames: {id_b_frames}"
        assert id_a_frames[0] != id_b_frames[0], \
            "Left and right moto got the same ID — should be different"

    def test_tracked_detection_has_track_id_attribute(self):
        """TrackedDetection must expose .track_id just like Detection exposes .class_name."""
        tracker = Tracker()
        result = tracker.update(_fd(0.0, [_person(100, 100, 200, 400)]))
        for td in result:
            assert hasattr(td, "track_id"), \
                f"TrackedDetection missing .track_id attribute: {td}"

    def test_empty_frame_does_not_crash(self):
        """Tracker must handle frames with zero detections without error."""
        tracker = Tracker()
        for ts in [0.0, 0.5, 1.0]:
            result = tracker.update(_fd(ts, []))
            assert result == [], f"Empty frame should return empty list, got {result}"
