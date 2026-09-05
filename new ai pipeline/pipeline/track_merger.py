"""
pipeline/track_merger.py
--------------------------
Finds and merges fragmented car tracks using plate-similarity + time-
window compatibility.

Why geometry-based merging (used for motorcycles in tracker.py) doesn't
work for cars
-------------
Motorcycles in traffic are typically spaced far enough apart that a
disappeared track (ID lost mid-clip) can be safely re-linked to a new
ID via centroid proximity — adjacent motorcycles are unlikely to have
identical centroids close in time.

Cars sit bumper-to-bumper. A car that temporarily loses its track ID
may reappear as a new ID at almost the same centroid as an adjacent,
different car. Geometric merging would wrongly merge two different cars.

The fix for cars: plate similarity
------------------------------------
Plate reads are the only reliable identity signal for cars. Two track
fragments belong to the same physical car if:
  1. Their plate OCR reads are very similar (Levenshtein distance <= 2).
  2. Their time windows don't heavily overlap (a car can't be in two
     places at once).

Merge happens on RAW reads, not resolved plates
-----------------------------------------------
This is important and non-obvious. Merging post-resolution can pick
the wrong plate even when the correct one exists in the combined
evidence:

  Track A resolves to 'MH02FX9484' (3 reads, 2 valid)
  Track B resolves to 'MH02FX9481' (1 read, valid)

  If we merge the resolved plates, we'd have two conflicting answers
  and might pick the wrong one via confidence.

  If instead we merge the RAW reads from A and B before resolving,
  the combined 4-read pool has 'MH02FX9484' appearing 3x vs '9481'
  appearing 1x — the correct plate wins the majority vote naturally.

Usage in run_pipeline.py (Stage 3, after motorcycle stitching)
--------------------------------------------------------------
    from pipeline.track_merger import TrackMerger

    merger = TrackMerger()

    # Register each track's raw OCR reads (before resolve_all())
    for track_id, reads in aggregator.raw_reads_by_track().items():
        merger.register_track(
            track_id=track_id,
            raw_reads=reads,
            first_seen=track_history[track_id]['first_seen'],
            last_seen=track_history[track_id]['last_seen'],
        )

    # Find merge candidates
    merges = merger.find_merge_candidates()

    # Apply: merge raw reads from fragment into primary, then delete fragment
    for primary_id, fragment_id in merges:
        aggregator.merge_tracks(primary_id, fragment_id)
        # also remap track_history, vehicle_track_labels, etc.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Maximum Levenshtein distance between plate reads to consider two tracks
# as the same physical vehicle
MAX_LEVENSHTEIN: int = 2

# Maximum time gap (seconds) between last_seen of one track and first_seen
# of the next for a merge to be considered plausible
MAX_TIME_GAP_S: float = 5.0

# Minimum plate read length to use for similarity matching (short reads
# are too ambiguous to trust as identity signals)
MIN_PLATE_LEN: int = 6


@dataclass
class TrackInfo:
    track_id: int
    raw_plates: List[str]        # all OCR text reads (including invalid-format ones)
    first_seen: float            # timestamp (seconds) — MUST be a real timestamp
    last_seen: float             # timestamp (seconds) — ditto
    is_timestamp_valid: bool     # False if first_seen looks like a pixel coordinate


@dataclass
class MergeCandidate:
    """A proposed merge of fragment_id into primary_id."""
    primary_id: int
    fragment_id: int
    plate_similarity: float      # 0.0 - 1.0 (1.0 = identical)
    best_plate_pair: Tuple[str, str]   # (primary_read, fragment_read) that matched
    time_compatible: bool


def _levenshtein(s1: str, s2: str) -> int:
    """Standard iterative Levenshtein distance."""
    if s1 == s2:
        return 0
    if not s1:
        return len(s2)
    if not s2:
        return len(s1)

    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(
                prev[j + 1] + 1,   # delete
                curr[j] + 1,       # insert
                prev[j] + (0 if c1 == c2 else 1),  # replace
            ))
        prev = curr
    return prev[-1]


def _plate_similarity(plates_a: List[str], plates_b: List[str]) -> Tuple[float, Tuple[str, str]]:
    """
    Best similarity between any pair of plates from two lists.
    Returns (similarity_score, (best_plate_a, best_plate_b)).
    similarity_score = 1 - (levenshtein / max(len_a, len_b))
    """
    best_score = 0.0
    best_pair  = ("", "")

    for pa in plates_a:
        if len(pa) < MIN_PLATE_LEN:
            continue
        for pb in plates_b:
            if len(pb) < MIN_PLATE_LEN:
                continue
            dist = _levenshtein(pa, pb)
            max_len = max(len(pa), len(pb))
            score = 1.0 - (dist / max_len)
            if score > best_score:
                best_score = score
                best_pair  = (pa, pb)

    return best_score, best_pair


def _looks_like_pixel_coordinate(value: float, video_duration_s: float = 300.0) -> bool:
    """
    Heuristic guard: if first_seen > plausible video duration, it's
    probably a pixel coordinate (Bug 2.1) rather than a real timestamp.
    Returns True if the value is suspicious.
    """
    return value > video_duration_s


class TrackMerger:
    """
    Finds car track fragments that belong to the same physical vehicle
    and should have their raw OCR reads combined before resolution.

    This deliberately does NOT do any geometric reasoning — only plate
    similarity + time-window compatibility.
    """

    def __init__(
        self,
        max_levenshtein: int = MAX_LEVENSHTEIN,
        max_time_gap_s: float = MAX_TIME_GAP_S,
    ) -> None:
        self.max_levenshtein = max_levenshtein
        self.max_time_gap_s  = max_time_gap_s
        self._tracks: Dict[int, TrackInfo] = {}

    def register_track(
        self,
        track_id: int,
        raw_plates: List[str],
        first_seen: float,
        last_seen: float,
        video_duration_hint: float = 300.0,
    ) -> None:
        """
        Register a track for merge consideration.

        Validates that first_seen / last_seen are real timestamps (not
        pixel coordinates — catches Bug 2.1 if it slips through).
        """
        ts_valid = not (
            _looks_like_pixel_coordinate(first_seen, video_duration_hint)
            or _looks_like_pixel_coordinate(last_seen, video_duration_hint)
        )
        if not ts_valid:
            logger.warning(
                "Track %d has suspicious timestamps (first_seen=%.1f, last_seen=%.1f) "
                "— likely pixel coordinates, not seconds. Bug 2.1 may not be fixed. "
                "This track will be registered but marked invalid for merging.",
                track_id, first_seen, last_seen,
            )

        self._tracks[track_id] = TrackInfo(
            track_id=track_id,
            raw_plates=raw_plates,
            first_seen=first_seen,
            last_seen=last_seen,
            is_timestamp_valid=ts_valid,
        )

    def find_merge_candidates(self) -> List[MergeCandidate]:
        """
        Find all pairs of tracks that should be merged.

        Returns a list of MergeCandidate — caller is responsible for
        actually merging raw reads and updating track maps.

        Merge criteria:
          - At least one plate in each track
          - Best plate similarity score corresponds to Levenshtein <= max_levenshtein
          - Time windows are compatible (not simultaneous, gap <= max_time_gap_s)
          - Both tracks have valid timestamps
        """
        tracks = list(self._tracks.values())
        candidates: List[MergeCandidate] = []

        for i in range(len(tracks)):
            for j in range(i + 1, len(tracks)):
                a, b = tracks[i], tracks[j]

                # Skip tracks with invalid timestamps
                if not a.is_timestamp_valid or not b.is_timestamp_valid:
                    continue

                # Skip if either has no usable plates
                if not a.raw_plates or not b.raw_plates:
                    continue

                # Plate similarity
                score, best_pair = _plate_similarity(a.raw_plates, b.raw_plates)
                if score < (1.0 - self.max_levenshtein / max(
                    len(best_pair[0]), len(best_pair[1]), 1
                )):
                    continue

                # Time compatibility: the tracks should be sequential, not simultaneous
                # (a & b don't overlap, or the gap between them is <= max_time_gap_s)
                overlap = min(a.last_seen, b.last_seen) - max(a.first_seen, b.first_seen)
                if overlap > 0:
                    # Tracks overlap in time — can't be the same physical car
                    continue

                gap = -overlap  # positive = gap between tracks
                time_compatible = gap <= self.max_time_gap_s

                # Determine primary (earlier) vs fragment (later)
                if a.first_seen <= b.first_seen:
                    primary, fragment = a, b
                else:
                    primary, fragment = b, a

                candidates.append(MergeCandidate(
                    primary_id=primary.track_id,
                    fragment_id=fragment.track_id,
                    plate_similarity=round(score, 4),
                    best_plate_pair=best_pair,
                    time_compatible=time_compatible,
                ))

                logger.info(
                    "Merge candidate: track %d → %d  plates=('%s', '%s')  "
                    "similarity=%.2f  gap=%.2fs  time_ok=%s",
                    fragment.track_id, primary.track_id,
                    best_pair[0], best_pair[1],
                    score, gap, time_compatible,
                )

        # Only return time-compatible merges
        return [c for c in candidates if c.time_compatible]
