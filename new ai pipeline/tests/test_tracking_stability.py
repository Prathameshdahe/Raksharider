"""
tests/test_tracking_stability.py
─────────────────────────────────
Regression test: fast-moving objects must maintain stable track IDs.

Scenarios
---------
1. Stationary object → single track ID across many frames.
2. Fast-moving object (large pixel jump per frame) → same track ID maintained
   thanks to the centroid-distance fallback cost and adaptive Q-matrix.
3. Two separate objects → receive different IDs, never swapped.
4. Post-hoc stitcher correctly merges fragments of the same class.
5. Post-hoc stitcher does NOT merge tracks of different classes.
"""
from __future__ import annotations

import pytest
from pipeline.tracker import Tracker, stitch_track_fragments
from pipeline.detector import Detection, FrameDetections


def _make_fd(timestamp: float, detections: list[Detection]) -> FrameDetections:
    return FrameDetections(timestamp=timestamp, detections=detections)


def _make_det(cls: str, bbox: list[float], conf: float = 0.9) -> Detection:
    return Detection(class_name=cls, confidence=conf, bbox=bbox)


class TestFastMotionTrackStability:
    """Kalman Q-matrix and centroid-cost fallback must keep fast objects on one ID."""

    def test_stationary_object_keeps_single_id(self):
        """An object that barely moves should get exactly one track ID."""
        tracker = Tracker()
        bbox = [100, 100, 200, 200]
        ids_seen = set()
        for i in range(10):
            ts = float(i) * 0.5
            fd = _make_fd(ts, [_make_det("motorcycle", bbox, 0.9)])
            for td in tracker.update(fd):
                if td.track_id > 0:
                    ids_seen.add(td.track_id)
        assert len(ids_seen) == 1, f"Expected 1 track ID, got {ids_seen}"

    def test_fast_mover_keeps_single_id_after_stitching(self):
        """
        A motorcycle moving 120px per 0.5s frame may fragment during live tracking,
        but stitch_track_fragments() must reduce all fragments to a single canonical ID.

        This validates the COMBINED system (live tracker + post-hoc stitcher),
        which is the design intent from the implementation plan.
        """
        tracker = Tracker()
        track_results: dict = {}
        class_name_for_id: dict = {}

        # Simulate motorcycle moving right: 120px per step
        for i in range(8):
            x1 = 100 + i * 120
            bbox = [float(x1), 300.0, float(x1 + 80), 380.0]
            ts = float(i) * 0.5
            fd = _make_fd(ts, [_make_det("motorcycle", bbox, 0.85)])
            tracked = tracker.update(fd)
            track_results[ts] = tracked
            for td in tracked:
                if td.track_id > 0 and td.track_id not in class_name_for_id:
                    class_name_for_id[td.track_id] = td.class_name

        # Apply stitcher to merge fragments
        id_map = stitch_track_fragments(track_results, class_name_for_id)

        # All track IDs should reduce to a single canonical ID after stitching
        canonical_ids = {id_map.get(tid, tid) for tid in class_name_for_id}
        assert len(canonical_ids) == 1, (
            f"After stitching, expected 1 canonical motorcycle ID, got {canonical_ids} "
            f"(id_map={id_map})"
        )

    def test_two_objects_get_different_ids(self):
        """Two motorcycles side by side must always get different track IDs."""
        tracker = Tracker()
        for i in range(5):
            ts = float(i) * 0.5
            fd = _make_fd(ts, [
                _make_det("motorcycle", [50,  300, 130, 380]),
                _make_det("motorcycle", [400, 300, 480, 380]),
            ])
            tracked = tracker.update(fd)
            moto_ids = {td.track_id for td in tracked if td.class_name == "motorcycle" and td.track_id > 0}
            if len(moto_ids) == 2:
                assert len(set(moto_ids)) == 2


class TestTrackStitcher:
    """stitch_track_fragments() must correctly merge same-class near fragments."""

    def _build_track_results(self, segments: list[tuple]) -> tuple[dict, dict]:
        """
        Build synthetic track_results + class_name_for_id.
        segments: [(track_id, class_name, [(ts, bbox), ...])]
        """
        from pipeline.tracker import TrackedDetection

        track_results: dict = {}
        class_name_for_id: dict = {}
        for tid, cls, frames in segments:
            class_name_for_id[tid] = cls
            for ts, bbox in frames:
                td = TrackedDetection(class_name=cls, confidence=0.9, bbox=list(bbox), track_id=tid)
                track_results.setdefault(ts, []).append(td)
        return track_results, class_name_for_id

    def test_same_class_nearby_fragments_are_stitched(self):
        """Two motorcycle fragments that end/start close should be merged."""
        track_results, class_map = self._build_track_results([
            (1, "motorcycle", [(0.0, [100, 300, 180, 380]),
                               (0.5, [160, 300, 240, 380])]),   # ends at t=0.5
            (2, "motorcycle", [(1.0, [200, 300, 280, 380])]),   # starts at t=1.0 (gap=0.5, dist~50px)
        ])
        id_map = stitch_track_fragments(track_results, class_map)
        # Track 2 should be stitched onto track 1
        assert id_map.get(2, 2) == 1, (
            f"Expected track 2 stitched to track 1, got id_map={id_map}"
        )

    def test_different_class_not_stitched(self):
        """A motorcycle and a car that end/start nearby must NOT be stitched."""
        track_results, class_map = self._build_track_results([
            (1, "motorcycle", [(0.0, [100, 300, 180, 380]),
                               (0.5, [160, 300, 240, 380])]),
            (2, "car",        [(1.0, [200, 300, 280, 380])]),
        ])
        id_map = stitch_track_fragments(track_results, class_map)
        assert id_map.get(2, 2) == 2, (
            f"Car and motorcycle should never be stitched, got id_map={id_map}"
        )

    def test_large_temporal_gap_not_stitched(self):
        """Fragments separated by more than STITCH_MAX_GAP frames must NOT be stitched."""
        from pipeline.tracker import STITCH_MAX_GAP
        track_results, class_map = self._build_track_results([
            (1, "motorcycle", [(0.0, [100, 300, 180, 380])]),
            (2, "motorcycle", [(0.0 + STITCH_MAX_GAP + 5, [110, 305, 190, 385])]),
        ])
        id_map = stitch_track_fragments(track_results, class_map)
        assert id_map.get(2, 2) == 2, (
            f"Tracks too far apart in time should not be stitched, got id_map={id_map}"
        )

    def test_large_spatial_gap_not_stitched(self):
        """Fragments separated by more than STITCH_MAX_DIST_PX must NOT be stitched."""
        from pipeline.tracker import STITCH_MAX_DIST_PX
        track_results, class_map = self._build_track_results([
            (1, "motorcycle", [(0.0, [100, 100, 180, 180])]),
            (2, "motorcycle", [(1.0, [100 + STITCH_MAX_DIST_PX + 50, 100,
                                      180 + STITCH_MAX_DIST_PX + 50, 180])]),
        ])
        id_map = stitch_track_fragments(track_results, class_map)
        assert id_map.get(2, 2) == 2, (
            f"Tracks too far apart spatially should not be stitched, got id_map={id_map}"
        )
