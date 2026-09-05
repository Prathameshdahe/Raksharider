"""
tests/test_track_network.py
───────────────────────────
Comprehensive unit tests for the track-centric architecture components:
  - VehicleState & VehicleStateRegistry (evidence accumulation, track merging, resolution gating)
  - VehicleClassGate (guarding two-wheeler violations on four-wheelers)
  - VehicleClassAggregator (flip-flop detection and stable class resolution)
  - TrackMerger (plate-similarity & time-window compatibility for cars)
  - PlateAggregator merge_tracks & raw_reads_by_track
  - IndianPlateValidator (state-code filtering)
"""

from __future__ import annotations

import pytest

from pipeline.vehicle_class_gate import is_two_wheeler, guard_two_wheeler, GuardFailed
from pipeline.vehicle_state import (
    VehicleState,
    VehicleStateRegistry,
    VehicleObservation,
)
from pipeline.vehicle_class_aggregator import VehicleClassAggregator
from pipeline.track_merger import TrackMerger, _levenshtein, _plate_similarity
from pipeline.plate_aggregator import PlateAggregator, RawOCRRead
from pipeline.indian_plate_validator import is_real_state_code, best_plate_candidate


def test_vehicle_class_gate():
    assert is_two_wheeler("motorcycle") is True
    assert is_two_wheeler("bicycle") is True
    assert is_two_wheeler("bike") is True
    assert is_two_wheeler("scooter") is True
    assert is_two_wheeler("car") is False
    assert is_two_wheeler("bus") is False
    assert is_two_wheeler("truck") is False
    assert is_two_wheeler("mini_lcv") is False
    assert is_two_wheeler("vehicle") is False
    assert is_two_wheeler(None) is False

    with pytest.raises(GuardFailed):
        guard_two_wheeler("car", track_id=4)
    # Should not raise for motorcycle
    guard_two_wheeler("motorcycle", track_id=2)


def test_vehicle_state_two_wheeler_gating():
    """A car must NEVER confirm no_helmet or triple_riding, even if observations exist."""
    state = VehicleState(track_id=1, vehicle_class="car")
    # Simulate accidental no_helmet observations
    for i in range(5):
        state.add_observation(VehicleObservation(
            frame_index=i,
            timestamp=i * 0.5,
            source="rules",
            key="no_helmet",
            value=True,
            confidence=0.9,
        ))
    state.resolve()
    assert state.violation_verdicts["no_helmet"].result == "not_present"
    assert "no_helmet" not in state.confirmed_violations()


def test_vehicle_state_motorcycle_confirms_helmet_violation():
    """A motorcycle with sufficient positive observations MUST confirm."""
    state = VehicleState(track_id=2, vehicle_class="motorcycle")
    for i in range(3):
        state.add_observation(VehicleObservation(
            frame_index=i,
            timestamp=i * 0.5,
            source="rules",
            key="no_helmet",
            value=True,
            confidence=0.85,
        ))
    state.resolve()
    assert state.violation_verdicts["no_helmet"].result == "confirmed"
    assert "no_helmet" in state.confirmed_violations()


def test_vehicle_state_registry_merge_tracks():
    """Merging a fragment into a primary track correctly consolidates observations."""
    reg = VehicleStateRegistry()
    # Primary track (t=0.0 - 2.0)
    for i in range(3):
        reg.add_observation(
            track_id=1, frame_index=i, timestamp=i * 1.0,
            source="detector", key="present", value=True, confidence=0.8
        )
    # Fragment track (t=3.0 - 4.0)
    for i in range(3, 5):
        reg.add_observation(
            track_id=2, frame_index=i, timestamp=i * 1.0,
            source="detector", key="present", value=True, confidence=0.8
        )
    assert len(reg) == 2

    reg.merge_tracks(primary_id=1, fragment_id=2)
    assert len(reg) == 1
    assert 2 not in reg
    state1 = reg.get_or_create(1)
    assert state1.first_seen == 0.0
    assert state1.last_seen == 4.0
    assert state1.frames_observed == 5


def test_plate_aggregator_merge_tracks():
    """PlateAggregator correctly moves reads and raw candidates upon track merge."""
    agg = PlateAggregator()
    read1 = RawOCRRead(timestamp=1.0, frame_index=2, text="MH01AB1234", confidence=0.85, is_valid=True, tier="easyocr")
    read2 = RawOCRRead(timestamp=5.0, frame_index=10, text="MH01AB1234", confidence=0.90, is_valid=True, tier="easyocr")
    agg._reads[1].append(read1)
    agg._reads[2].append(read2)

    assert len(agg.raw_reads_by_track()[1]) == 1
    assert len(agg.raw_reads_by_track()[2]) == 1

    agg.merge_tracks(primary_id=1, fragment_id=2)
    assert 2 not in agg._reads
    assert len(agg._reads[1]) == 2
    res = agg.resolve_track(1)
    assert res.plate_text == "MH01AB1234"
    assert res.agreement == 1.0


def test_track_merger_logic():
    """TrackMerger detects fragments of the same car based on plate similarity and sequential time gap."""
    merger = TrackMerger(max_levenshtein=2, max_time_gap_s=4.0)
    # Track 10: MH12DE1433 seen 1.0 to 3.0s
    merger.register_track(track_id=10, raw_plates=["MH12DE1433"], first_seen=1.0, last_seen=3.0)
    # Track 11: MH12DE1438 (1 char diff) seen 4.0 to 6.0s (1.0s gap <= 4.0s)
    merger.register_track(track_id=11, raw_plates=["MH12DE1438"], first_seen=4.0, last_seen=6.0)

    candidates = merger.find_merge_candidates()
    assert len(candidates) == 1
    assert candidates[0].primary_id == 10
    assert candidates[0].fragment_id == 11
    assert candidates[0].time_compatible is True


def test_indian_plate_validator():
    """Real state codes pass; fake/impossible codes fail."""
    assert is_real_state_code("MH12DE1433") is True
    assert is_real_state_code("DL01AB1234") is True
    assert is_real_state_code("KA05MJ9999") is True
    assert is_real_state_code("HH12DE1433") is False   # HH is not a state/UT
    assert is_real_state_code("XX99YY9999") is False

    pool = [("HH12DE1433", 0.95), ("MH12DE1433", 0.80)]
    best = best_plate_candidate(pool)
    assert best is not None
    assert best[0] == "MH12DE1433"   # State code filter prefers real state code


def test_vehicle_class_aggregator():
    """Aggregator identifies flip-flops and resolves the majority class."""
    vca = VehicleClassAggregator()
    # 4 votes for motorcycle, 1 for bicycle
    for i in range(4):
        vca.add_vote(track_id=1, vehicle_class="motorcycle", confidence=0.8, frame_index=i)
    vca.add_vote(track_id=1, vehicle_class="bicycle", confidence=0.6, frame_index=4)

    resolutions = vca.resolve_all()
    res = resolutions[1]
    assert res.resolved_class == "motorcycle"
    assert res.is_stable is True
    assert res.agreement == 0.8
