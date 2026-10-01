"""
tests/test_identity.py — plate-anchored identity ledger + subject selection.

  * the uploader's claim never validates a plate
  * two reads on distinct frames resolve; two engines on one frame do not
  * near-miss claims (one character) are conflicts, never agreement
  * subject selection never reads findings
"""

from __future__ import annotations

from pipeline.identity import (ReadLike, TrackSummary, canonical, choose_subject, levenshtein, resolve_identity)
from pipeline.contract import auto_tier


def reads(*items):
    return [ReadLike(t, c, f, True) for t, c, f in items]


class TestResolveIdentity:
    def test_two_frames_resolve(self):
        r = resolve_identity(reads(("MH02FX9484", 0.8, 1), ("MH02FX9484", 0.7, 5)), None)
        assert r.status == "resolved" and r.plate == "MH02FX9484" and r.method == "ocr"

    def test_two_engines_same_frame_do_not_resolve(self):
        r = resolve_identity(reads(("MH02FX9484", 0.8, 1), ("MH02FX9484", 0.9, 1)), None)
        assert r.status == "provisional" and r.plate is None

    def test_claim_never_substitutes_for_a_second_read(self):
        r = resolve_identity(reads(("MH02FX9484", 0.8, 1)), "MH02FX9484")
        assert r.status == "provisional" and r.method == "ocr+claim" and r.plate == "MH02FX9484"
        assert auto_tier("confirmed", 5, 1.0, 0.9, r.status) == "B"      # never tier A without a resolved plate

    def test_claim_one_character_off_is_a_conflict(self):
        r = resolve_identity(reads(("MH12AB0234", 0.9, 1), ("MH12AB0234", 0.9, 4)), "MH12ABO234")
        assert r.status == "conflict" and r.claim_match == "near"

    def test_claim_exact_match_keeps_resolved(self):
        r = resolve_identity(reads(("MH12AB0234", 0.9, 1), ("MH12AB0234", 0.9, 4)), "mh 12 ab 0234")
        assert r.status == "resolved" and r.claim_match == "exact"

    def test_two_plates_on_one_track_is_a_conflict(self):
        r = resolve_identity(reads(("MH12AB0234", 0.9, 1), ("MH12AB0234", 0.9, 4), ("MH01DP1218", 0.9, 9), ("MH01DP1218", 0.9, 12)), None)
        assert r.status == "conflict"

    def test_low_confidence_and_fake_state_codes_ignored(self):
        r = resolve_identity(reads(("MH12AB0234", 0.4, 1), ("MH12AB0234", 0.4, 4), ("IO1AM7309", 0.9, 5), ("IO1AM7309", 0.9, 8)), None)
        assert r.status == "provisional" and r.plate is None

    def test_lookalike_without_plate_is_ambiguous(self):
        assert resolve_identity([], None, lookalike_nearby=True).status == "ambiguous"

    def test_canonical_and_levenshtein(self):
        assert canonical(" mh-02 fx 9484 ") == "MH02FX9484"
        assert levenshtein("MH02FX9484", "MH02FX9464") == 1


class TestSubject:
    def _tracks(self):
        return [TrackSummary(1, "motorcycle", 5.0, 4.0), TrackSummary(2, "motorcycle", 1.0, 2.0), TrackSummary(3, "car", 9.0, 8.0)]

    def test_plate_match_wins_over_dominance(self):
        ids = {2: resolve_identity(reads(("MH02FX9484", 0.9, 1), ("MH02FX9484", 0.9, 4)), "MH02FX9484")}
        c = choose_subject(self._tracks(), ids, "two_wheeler", "MH02FX9484")
        assert c.track_id == 2 and c.method == "plate"

    def test_dominant_declared_type_when_no_plate(self):
        c = choose_subject(self._tracks(), {}, "two_wheeler", None)
        assert c.track_id == 1 and c.method == "dominance" and c.answer_hint == "ok"

    def test_car_never_becomes_subject_of_a_two_wheeler_report(self):
        c = choose_subject([TrackSummary(3, "car", 9.0, 8.0)], {}, "two_wheeler", None)
        assert c.track_id is None and c.answer_hint == "declared_vehicle_not_seen"

    def test_tied_candidates_are_ambiguous(self):
        c = choose_subject([TrackSummary(1, "motorcycle", 5.0, 4.0), TrackSummary(2, "motorcycle", 4.0, 4.0)], {}, "two_wheeler", None)
        assert c.track_id is None and c.answer_hint == "ambiguous_subject" and set(c.candidates) == {1, 2}
