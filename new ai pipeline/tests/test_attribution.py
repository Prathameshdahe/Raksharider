"""
tests/test_attribution.py
─────────────────────────
Golden tests for the bugs that produced confident, evidenced, wrong accusations:

  * a frame-wide violation must land on ONE vehicle track (two motorcycles, one
    helmetless rider → exactly one track flagged)
  * duplicate observations of the same frame count as ONE evidence frame
  * unobservable frames never count for or against a vehicle
  * review-only / disabled violation types can never be "confirmed"
  * person tracks never become vehicle records
  * the pipeline→worker contract round-trips without losing a field
  * timestamp invariants are asserted, not logged
"""

from __future__ import annotations

import json

import pytest

from pipeline.contract import VehicleRecord, validate_vehicle_records, VIOLATION_POLICY
from pipeline.detector import Detection, FrameDetections, fuse_specialist_vehicles
from pipeline.rules import apply_rules
from pipeline.tracker import TrackedDetection
from pipeline.vehicle_state import VehicleObservation, VehicleState, VehicleStateRegistry
from run_pipeline import match_finding_to_track, clip_status_from_states, pick_subject


def det(cls, bbox, conf=0.9):
    return Detection(class_name=cls, confidence=conf, bbox=list(bbox))


def tracked(cls, bbox, tid, conf=0.9):
    return TrackedDetection(class_name=cls, confidence=conf, bbox=list(bbox), track_id=tid)


MOTO_A, RIDER_A, HEAD_A = [0, 80, 200, 320], [10, 100, 100, 300], [15, 102, 90, 145]
MOTO_B, RIDER_B, HEAD_B = [400, 80, 600, 320], [410, 100, 500, 300], [415, 102, 490, 145]


def _two_moto_frame(ts):
    fd = FrameDetections(timestamp=ts)
    fd.detections = [
        det("motorcycle", MOTO_A), det("person", RIDER_A), det("no_helmet", HEAD_A),   # A: helmetless
        det("motorcycle", MOTO_B), det("person", RIDER_B), det("helmet", HEAD_B),      # B: helmet on
    ]
    return fd


def _two_moto_tracks():
    return [tracked("motorcycle", MOTO_A, 1), tracked("person", RIDER_A, 3),
            tracked("motorcycle", MOTO_B, 2), tracked("person", RIDER_B, 4)]


class TestVehicleSpecificAttribution:
    def test_rule_engine_emits_one_finding_per_motorcycle(self):
        fv = apply_rules(_two_moto_frame(0.0))
        assert len(fv.findings) == 2
        a, b = fv.findings
        assert a.violations == ["no_helmet"] and a.helmet_status == "no_helmet"
        assert "no_helmet" in b.observed_absent and b.helmet_status == "helmet"

    def test_exactly_one_track_carries_the_violation(self):
        """Two motorcycles, one helmetless rider, three frames → track 1 confirmed, track 2 clean."""
        registry = VehicleStateRegistry()
        registry.set_vehicle_class(1, "motorcycle", 1.0, True)
        registry.set_vehicle_class(2, "motorcycle", 1.0, True)
        registry.set_vehicle_class(3, "person")     # refused
        for i in range(3):
            ts = i * 0.5
            fv = apply_rules(_two_moto_frame(ts))
            tracks = _two_moto_tracks()
            for f in fv.findings:
                tid = match_finding_to_track(f.bbox, f.vehicle_class, tracks)
                assert tid in (1, 2)
                for v in f.violations:
                    registry.add_observation(tid, frame_index=i, timestamp=ts, source="rules", key=v, value=True, confidence=0.9)
                for v in f.observed_absent:
                    registry.add_observation(tid, frame_index=i, timestamp=ts, source="rules", key=v, value=False)
                for v in f.unobservable:
                    registry.add_observation(tid, frame_index=i, timestamp=ts, source="rules", key=v, value=None)
        registry.resolve_all()
        assert registry.get(1).confirmed_violations() == ["no_helmet"]
        assert registry.get(2).confirmed_violations() == []
        assert registry.get(2).violation_verdicts["no_helmet"].result == "observed_absent"
        assert 3 not in registry                      # person track never became a vehicle
        flagged = [r["track_id"] for r in registry.export_report() if r["needs_review"]]
        assert flagged == [1]

    def test_finding_never_matches_other_wheel_class(self):
        tracks = [tracked("car", MOTO_A, 7)]
        assert match_finding_to_track(MOTO_A, "motorcycle", tracks) == -1

    def test_phone_attributed_to_rider_of_that_motorcycle_only(self):
        fd = FrameDetections(timestamp=0.0)
        fd.detections = [
            det("motorcycle", MOTO_A), det("person", RIDER_A), det("cell_phone", [40, 140, 90, 190]),
            det("motorcycle", MOTO_B), det("person", RIDER_B),
        ]
        fv = apply_rules(fd)
        a, b = fv.findings
        assert "phone_usage" in a.violations
        assert "phone_usage" not in b.violations


class TestEvidenceCounting:
    def test_duplicate_observations_same_frame_count_once(self):
        s = VehicleState(track_id=1, vehicle_class="motorcycle")
        for _ in range(2):   # same physical frame observed twice (two code paths)
            s.add_observation(VehicleObservation(frame_index=0, timestamp=0.0, source="rules", key="phone_usage", value=True, confidence=0.9))
        s.resolve()
        v = s.violation_verdicts["phone_usage"]
        assert v.evidence_frames == 1
        assert v.result == "needs_review"          # 1 frame < MIN_EVIDENCE_FRAMES

    def test_unobservable_frames_do_not_dilute_agreement(self):
        s = VehicleState(track_id=1, vehicle_class="motorcycle")
        for i in range(3):
            s.add_observation(VehicleObservation(i, i * 0.5, "rules", "no_helmet", True, 0.9))
        for i in range(3, 10):   # occluded for 7 frames
            s.add_observation(VehicleObservation(i, i * 0.5, "rules", "no_helmet", None, 0.5))
        s.resolve()
        v = s.violation_verdicts["no_helmet"]
        assert v.evaluable_frames == 3 and v.evidence_frames == 3
        assert v.agreement == 1.0 and v.result == "confirmed"

    def test_negative_frames_lower_agreement(self):
        s = VehicleState(track_id=1, vehicle_class="motorcycle")
        for i in range(3):
            s.add_observation(VehicleObservation(i, i * 0.5, "rules", "no_helmet", True, 0.9))
        for i in range(3, 12):
            s.add_observation(VehicleObservation(i, i * 0.5, "rules", "no_helmet", False, 0.9))
        s.resolve()
        assert s.violation_verdicts["no_helmet"].result == "needs_review"

    def test_flicker_within_a_tenth_of_a_second_does_not_confirm(self):
        """Dense 30 fps: 3 positives inside 0.1 s is one detector flicker, not evidence."""
        s = VehicleState(track_id=1, vehicle_class="motorcycle")
        for i in range(3):
            s.add_observation(VehicleObservation(i, i * 0.033, "rules", "no_helmet", True, 0.9))
        s.resolve()
        assert s.violation_verdicts["no_helmet"].result == "needs_review"

    def test_absent_needs_sustained_negatives(self):
        s = VehicleState(track_id=1, vehicle_class="motorcycle")
        s.add_observation(VehicleObservation(0, 0.0, "rules", "no_helmet", False, 0.9))
        s.resolve()
        assert s.violation_verdicts["no_helmet"].result == "unobservable"
        for i in range(1, 4):
            s.add_observation(VehicleObservation(i, i * 0.5, "rules", "no_helmet", False, 0.9))
        s.resolve()
        assert s.violation_verdicts["no_helmet"].result == "observed_absent"

    def test_all_unobservable_is_unobservable_not_absent(self):
        s = VehicleState(track_id=1, vehicle_class="motorcycle")
        s.add_observation(VehicleObservation(0, 0.0, "rules", "no_helmet", None))
        s.resolve()
        assert s.violation_verdicts["no_helmet"].result == "unobservable"

    def test_never_checked_is_not_evaluated(self):
        s = VehicleState(track_id=1, vehicle_class="motorcycle")
        s.resolve()
        assert s.violation_verdicts["no_helmet"].result == "not_evaluated"

    def test_vlm_observation_overrides_rules_for_that_frame(self):
        s = VehicleState(track_id=1, vehicle_class="motorcycle")
        s.add_observation(VehicleObservation(0, 0.0, "rules", "no_helmet", True, 0.9))
        s.add_observation(VehicleObservation(0, 0.0, "vlm", "no_helmet", False, 0.9))
        s.resolve()
        assert s.violation_verdicts["no_helmet"].result == "unobservable"   # one negative frame is not "absent"
        assert s.violation_verdicts["no_helmet"].evidence_frames == 0


class TestPolicy:
    def test_wheelie_is_review_only(self):
        s = VehicleState(track_id=1, vehicle_class="motorcycle")
        for i in range(6):
            s.add_observation(VehicleObservation(i, i * 0.5, "rules", "wheelie", True, 0.95))
        s.resolve()
        assert s.violation_verdicts["wheelie"].result == "needs_review"

    def test_signal_violation_disabled(self):
        s = VehicleState(track_id=1, vehicle_class="car")
        for i in range(6):
            s.add_observation(VehicleObservation(i, i * 0.5, "rules", "signal_violation", True, 0.95))
        s.resolve()
        assert s.violation_verdicts["signal_violation"].result == "not_evaluated"
        assert not VIOLATION_POLICY["signal_violation"]["enabled"]

    def test_clip_verdict_is_derived_from_vehicles(self):
        a = VehicleState(track_id=1, vehicle_class="motorcycle")
        b = VehicleState(track_id=2, vehicle_class="car")
        for i in range(4):
            a.add_observation(VehicleObservation(i, i * 0.5, "rules", "no_helmet", True, 0.9))
        b.add_observation(VehicleObservation(0, 0.0, "rules", "phone_usage", True, 0.6))
        a.resolve(); b.resolve()
        status, violations = clip_status_from_states([a, b])
        assert status == "auto_flagged" and violations == ["no_helmet", "phone_usage"]
        assert pick_subject([a, b]).track_id == 1
        assert clip_status_from_states([VehicleState(track_id=9)])[0] == "insufficient_evidence"


class TestContract:
    def test_round_trip_pipeline_to_worker(self):
        """VehicleState → dict → JSON → VehicleRecord: every field survives."""
        s = VehicleState(track_id=5, vehicle_class="motorcycle", class_confidence=0.8, class_is_stable=True,
                         plate_text="MH02FX9484", plate_confidence=0.85, raw_plate_reads=["MH02FX9484", "MH02FX9464"])
        for i in range(4):
            s.add_observation(VehicleObservation(i, 1.0 + i * 0.5, "detector", "present", True, 0.9))
            s.add_observation(VehicleObservation(i, 1.0 + i * 0.5, "rules", "no_helmet", True, 0.9))
        s.evidence.append("tracks/5/t1.000s.jpg")
        d = s.to_dict()
        rec = VehicleRecord.from_dict(json.loads(json.dumps(d)))
        assert rec.to_dict() == d
        assert rec.plate["text"] == "MH02FX9484" and rec.first_seen == 1.0 and rec.last_seen == 2.5
        assert rec.frames_observed == 4 and rec.confirmed_violations == ["no_helmet"]
        assert rec.verdicts["no_helmet"]["confidence"] == pytest.approx(0.9)
        assert rec.evidence == ["tracks/5/t1.000s.jpg"] and rec.severity == VIOLATION_POLICY["no_helmet"]["severity"]

    def test_renamed_field_fails_loudly(self):
        d = VehicleState(track_id=1, vehicle_class="car").to_dict()
        d["plate_text"] = d.pop("plate")
        with pytest.raises(ValueError):
            VehicleRecord.from_dict(d)

    def test_timestamp_invariant_is_asserted(self):
        d = VehicleState(track_id=1, vehicle_class="car").to_dict()
        d["first_seen"], d["last_seen"] = 1587.812, 12.0     # the real bug: a pixel coordinate
        with pytest.raises(AssertionError):
            VehicleRecord.from_dict(d)
        d["first_seen"], d["last_seen"] = 1.0, 30.0
        with pytest.raises(AssertionError):
            validate_vehicle_records([d], video_duration=20.0)

    def test_duplicate_track_ids_rejected(self):
        d = VehicleState(track_id=1, vehicle_class="car").to_dict()
        with pytest.raises(AssertionError):
            validate_vehicle_records([d, dict(d)])


class TestRegistry:
    def test_person_tracks_are_refused(self):
        r = VehicleStateRegistry()
        assert r.set_vehicle_class(1, "person") is False
        assert r.set_vehicle_class(2, "motorcycle") is True
        assert 1 not in r and 2 in r

    def test_merge_is_one_operation_over_all_state(self):
        r = VehicleStateRegistry()
        r.set_vehicle_class(1, "car", 0.9, True)
        r.set_vehicle_class(2, "car", 0.9, True)
        r.add_observation(1, frame_index=0, timestamp=0.0, source="detector", key="present", value=True)
        r.add_observation(2, frame_index=5, timestamp=2.5, source="detector", key="present", value=True)
        r.add_observation(2, frame_index=5, timestamp=2.5, source="rules", key="phone_usage", value=True, confidence=0.8)
        r.set_plate(2, "MH12AB1234", 0.9, raw_reads=["MH12AB1234"])
        r.get(2).evidence.append("tracks/2/t2.500s.jpg")
        r.merge_tracks(1, 2)
        assert 2 not in r
        s = r.get(1)
        assert s.first_seen == 0.0 and s.last_seen == 2.5 and s.frames_observed == 2
        assert s.plate_text == "MH12AB1234" and s.raw_plate_reads == ["MH12AB1234"]
        assert s.evidence == ["tracks/2/t2.500s.jpg"]
        s.resolve()
        assert s.violation_verdicts["phone_usage"].result == "needs_review"


class TestDetectorFusion:
    def test_specialist_replaces_only_overlapping_box(self):
        generic = [det("car", [0, 0, 100, 100], 0.6), det("car", [300, 0, 400, 100], 0.5), det("motorcycle", [0, 200, 60, 300])]
        specialist = [det("bus", [2, 0, 100, 100], 0.5)]
        out = fuse_specialist_vehicles(generic, specialist)
        assert [d.class_name for d in out] == ["bus", "car", "motorcycle"]
        assert out[0].confidence == 0.6

    def test_unmatched_specialist_is_added(self):
        out = fuse_specialist_vehicles([det("car", [0, 0, 100, 100])], [det("truck", [500, 0, 600, 100])])
        assert sorted(d.class_name for d in out) == ["car", "truck"]
