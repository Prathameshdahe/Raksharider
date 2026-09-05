"""
tests/test_plate_isolation.py
─────────────────────────────
Regression test: headline plate must NEVER bleed from a bystander vehicle
(e.g. a passing car) into the violation report for a motorcycle.

Scenario
--------
- Motorcycle track (ID=1) has only invalid OCR reads → no validated plate.
- Car track (ID=99) has a clean validated plate "MH01AB1234".
- preferred_track_ids = {1} (only the motorcycle).

Expected: best_result() returns None (unreadable), NOT the car's plate.
"""
from __future__ import annotations

import pytest
from pipeline.plate_aggregator import PlateAggregator, PlateResolution


def _make_resolution(plate: str | None, is_validated: bool, agreement: float, confidence: float, track_id: int = 0) -> PlateResolution:
    return PlateResolution(
        track_id=track_id,
        plate_text=plate,
        is_validated=is_validated,
        agreement=agreement,
        confidence=confidence,
        valid_reads=1 if is_validated else 0,
        total_reads=1,
        needs_review=not is_validated,
        winning_tier="easyocr",
    )


class TestCrossTrackPlateIsolation:
    """Headline plate must not bleed from bystander vehicles into the violator's report."""

    def test_motorcycle_unreadable_car_valid_returns_none(self):
        """
        When the subject motorcycle (preferred_track) has no valid read,
        best_result() MUST return None rather than the passing car's plate.
        """
        aggregator = PlateAggregator()
        resolutions = {
            1:  _make_resolution(None,          False, 0.0, 0.0),    # motorcycle — no read
            99: _make_resolution("MH01AB1234",  True,  1.0, 0.92),   # passing car
        }
        result = aggregator.best_result(resolutions, preferred_track_ids={1})
        assert result is None, (
            f"Expected None (motorcycle plate unreadable), "
            f"but got '{result.plate_text if result else result}' — cross-track plate bleed!"
        )

    def test_motorcycle_valid_beats_car_valid(self):
        """
        When the motorcycle has its own validated plate, it wins over the car's,
        even if the car's agreement is higher.
        """
        aggregator = PlateAggregator()
        resolutions = {
            1:  _make_resolution("MH12XY0001", True, 0.75, 0.80),   # motorcycle — valid
            99: _make_resolution("MH01AB1234", True, 1.00, 0.95),   # car — higher agreement
        }
        result = aggregator.best_result(resolutions, preferred_track_ids={1})
        assert result is not None
        assert result.plate_text == "MH12XY0001", (
            f"Expected motorcycle plate 'MH12XY0001', got '{result.plate_text}'"
        )

    def test_no_preferred_falls_back_to_best_available(self):
        """
        When no preferred_track_ids are given (legacy path),
        best_result() returns the highest-confidence validated plate from any track.
        """
        aggregator = PlateAggregator()
        resolutions = {
            1:  _make_resolution("MH12XY0001", True, 0.75, 0.80),
            99: _make_resolution("MH01AB1234", True, 1.00, 0.95),
        }
        result = aggregator.best_result(resolutions, preferred_track_ids=None)
        assert result is not None
        # Should return the car's plate (highest agreement * confidence product)
        assert result.plate_text == "MH01AB1234"

    def test_multiple_preferred_tracks_stitched(self):
        """
        When preferred_track_ids contains multiple IDs (stitched fragments),
        valid reads on any of them satisfy the constraint.
        """
        aggregator = PlateAggregator()
        resolutions = {
            1:  _make_resolution(None,          False, 0.0,  0.0),   # frag 1 — no read
            2:  _make_resolution("MH12XY0001", True,  0.80, 0.85),   # frag 2 — read found
            99: _make_resolution("MH01AB1234", True,  1.00, 0.95),   # car
        }
        # Both frags 1 and 2 are preferred (stitched motorcycle)
        result = aggregator.best_result(resolutions, preferred_track_ids={1, 2})
        assert result is not None
        assert result.plate_text == "MH12XY0001"
