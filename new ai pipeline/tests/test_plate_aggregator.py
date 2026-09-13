"""
Unit tests for plate OCR aggregation.

These tests avoid OCR engines and model loading; they verify the consensus
logic that decides whether a track is strong enough or needs escalation.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.plate_aggregator import PlateAggregator, RawOCRRead


def _repo_tmp_dir(name: str) -> Path:
    path = Path(__file__).parent / "_tmp" / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def _read(text: str, conf: float = 0.8, valid: bool = True, tier: str = "easyocr") -> RawOCRRead:
    return RawOCRRead(
        timestamp=0.0,
        frame_index=0,
        text=text,
        confidence=conf,
        is_valid=valid,
        tier=tier,
    )


def test_single_valid_read_still_needs_escalation():
    aggregator = PlateAggregator()
    aggregator._reads[7].append(_read("MH01DP4248", conf=0.92))

    resolution = aggregator.resolve_track(7)

    assert resolution.plate_text == "MH01DP4248"
    assert resolution.is_validated is True
    assert resolution.needs_review is True
    assert aggregator.needs_escalation(7) is True


def test_repeated_valid_consensus_is_strong():
    aggregator = PlateAggregator()
    aggregator._reads[7].extend([
        _read("MH01DP4248", conf=0.88),
        _read("MH01DP4248", conf=0.86),
        _read("MH01DP4248", conf=0.91),
    ])

    resolution = aggregator.resolve_track(7)

    assert resolution.plate_text == "MH01DP4248"
    assert resolution.agreement == 1.0
    assert resolution.valid_reads == 3
    assert resolution.needs_review is False


def test_conflicting_valid_reads_need_escalation():
    aggregator = PlateAggregator()
    aggregator._reads[7].extend([
        _read("MH01DP4248", conf=0.88),
        _read("MH01DP4248", conf=0.86),
        _read("MH01BD1383", conf=0.91),
        _read("MH01BD1383", conf=0.90),
    ])

    resolution = aggregator.resolve_track(7)

    assert resolution.is_validated is True
    assert resolution.agreement == 0.5
    assert resolution.needs_review is True


def test_positional_consensus_recovers_plate_no_single_frame_got_right():
    # Each read has exactly one wrong character in a different position —
    # the "4 misread as 1" / "M misread as H" bug: no two reads are
    # identical, so exact-string majority voting finds nothing useful, but
    # every position individually has a clear 2-out-of-3 correct majority.
    aggregator = PlateAggregator()
    aggregator._reads[9].extend([
        _read("HH01DP4248", conf=0.80),   # M misread as H (pos 0)
        _read("MH01DP1248", conf=0.82),   # 4 misread as 1 (pos 6)
        _read("MH01DP4241", conf=0.79),   # 8 misread as 1 (pos 9)
    ])

    resolution = aggregator.resolve_track(9)

    assert resolution.plate_text == "MH01DP4248"
    assert resolution.is_validated is True
    assert resolution.agreement > 1 / 3   # beats what exact-string voting would give


def test_positional_consensus_declines_on_tied_positions():
    # Two genuinely different plates, evenly split — every disputed position
    # is an exact 2-2 tie. Positional voting must decline (not invent a
    # composite) and fall back to exact-string majority, same as the
    # conflicting-reads test above.
    aggregator = PlateAggregator()
    aggregator._reads[11].extend([
        _read("MH01DP4248", conf=0.88),
        _read("MH01DP4248", conf=0.86),
        _read("MH01BD1383", conf=0.91),
        _read("MH01BD1383", conf=0.90),
    ])

    resolution = aggregator.resolve_track(11)

    assert resolution.plate_text in ("MH01DP4248", "MH01BD1383")
    assert resolution.agreement == 0.5


def test_zoomed_crops_are_saved_for_plate_candidates():
    out_dir = _repo_tmp_dir("zoomed_crops")
    aggregator = PlateAggregator()
    frame = np.full((120, 220, 3), 255, dtype=np.uint8)
    aggregator.add_candidate(
        track_id=4,
        frame_bgr=frame,
        plate_bbox=[50, 40, 150, 65],
        timestamp=1.5,
        frame_index=3,
        detector_confidence=0.91,
    )

    crop_map = aggregator.save_zoomed_crops(out_dir)

    assert 4 in crop_map
    crop_path = Path(crop_map[4][0]["path"])
    assert crop_path.exists()
    saved = cv2.imread(str(crop_path))
    assert saved is not None
    assert saved.shape[0] >= 120


def test_learning_log_records_weak_conflicting_reads():
    out_dir = _repo_tmp_dir("learning_log")
    aggregator = PlateAggregator()
    frame = np.full((120, 220, 3), 255, dtype=np.uint8)
    aggregator.add_candidate(
        track_id=7,
        frame_bgr=frame,
        plate_bbox=[50, 40, 150, 65],
        timestamp=0.0,
        frame_index=0,
        detector_confidence=0.80,
    )
    aggregator._reads[7].extend([
        _read("MH01DP4248", conf=0.88),
        _read("MH01BD1383", conf=0.90, tier="paddleocr"),
    ])
    resolutions = aggregator.resolve_all()
    aggregator.save_zoomed_crops(out_dir)

    log_path = aggregator.write_learning_log(
        out_dir,
        resolutions,
        vehicle_track_labels={7: "car"},
    )

    assert log_path is not None
    lines = Path(log_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    sample = json.loads(lines[0])
    assert sample["track_id"] == 7
    assert sample["vehicle_class"] == "car"
    assert len(sample["ocr_reads"]) == 2
    assert sample["crops"][0]["path"]


def test_scan_pending_candidates_prioritizes_and_caps(monkeypatch):
    aggregator = PlateAggregator()
    frame = np.full((120, 220, 3), 255, dtype=np.uint8)

    # Add candidates across tracks
    aggregator.add_candidate(
        track_id=1,
        frame_bgr=frame,
        plate_bbox=[10, 10, 60, 40],
        timestamp=1.0,
        frame_index=1,
        detector_confidence=0.50,
        source="vehicle_roi",
    )
    aggregator.add_candidate(
        track_id=2,
        frame_bgr=frame,
        plate_bbox=[10, 10, 60, 40],
        timestamp=1.0,
        frame_index=1,
        detector_confidence=0.85,
        source="plate_detector",
    )
    aggregator.add_candidate(
        track_id=3,
        frame_bgr=frame,
        plate_bbox=[10, 10, 60, 40],
        timestamp=1.0,
        frame_index=1,
        detector_confidence=0.90,
        source="vehicle_roi",
    )

    scanned_tracks = []

    def fake_scan(candidate, use_vlm=True):
        scanned_tracks.append(candidate.track_id)
        return [_read("MH12AB1234", conf=0.9)]

    monkeypatch.setattr(aggregator, "_scan_candidate", fake_scan)

    recovered = aggregator.scan_pending_candidates(
        use_vlm=False,
        per_track_limit=1,
        max_total_candidates=2,
    )

    # Should prioritize plate_detector (track 2), then highest confidence (track 3), capped at 2
    assert len(recovered) == 2
    assert scanned_tracks == [2, 3]


