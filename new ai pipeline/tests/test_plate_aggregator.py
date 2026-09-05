"""
Unit tests for plate OCR aggregation.

These tests avoid OCR engines and model loading; they verify the consensus
logic that decides whether a track is strong enough or needs escalation.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.plate_aggregator import PlateAggregator, RawOCRRead


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

