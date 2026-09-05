"""
pipeline/vehicle_class_aggregator.py
--------------------------------------
Same accumulate-then-vote pattern as plate_aggregator.py, applied to
vehicle classification.

Why this exists
---------------
The YOLO vehicle-class model outputs one class per frame per detection.
In practice it flip-flops: a motorcycle may be classified as 'bicycle'
in some frames, 'motorcycle' in others, and 'vehicle' (generic) in yet
others. Trusting a single frame's class label causes:
  - Helmet/triple-riding checks running against a mis-classified car
    (Bug 2.4 — fixed by vehicle_class_gate.py)
  - A motorcycle misclassified as 'car' never getting rider-count checked

This aggregator collects all per-frame class votes for a track and
resolves to the winning class with an agreement ratio and a stability
flag. The stability flag (is_stable) tells downstream logic whether to
fully trust the resolved class or treat it as uncertain.

Integration
-----------
    from pipeline.vehicle_class_aggregator import VehicleClassAggregator

    agg = VehicleClassAggregator()

    # Per frame, per tracked detection:
    agg.add_vote(track_id=det.track_id, vehicle_class=det.class_name,
                 confidence=det.confidence, frame_index=i)

    # At clip end:
    results = agg.resolve_all()
    for track_id, result in results.items():
        print(track_id, result.resolved_class, result.agreement, result.is_stable)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Generic catch-all classes — low information content, deprioritised in voting
_GENERIC_CLASSES: frozenset[str] = frozenset({"vehicle", "object", "unknown", ""})

# Minimum agreement ratio to call the class "stable"
_STABILITY_THRESHOLD: float = 0.60

# Minimum number of votes to trust the result at all
_MIN_VOTES: int = 2


@dataclass
class ClassVote:
    """Single per-frame vote for a track's vehicle class."""
    frame_index: int
    vehicle_class: str
    confidence: float


@dataclass
class ClassResolution:
    """Resolved vehicle class for one tracked vehicle."""
    track_id: int
    resolved_class: str          # winning class (most frequent specific class)
    agreement: float             # fraction of votes that agreed with winner
    total_votes: int
    specific_votes: int          # votes for non-generic classes
    is_stable: bool              # True if agreement >= _STABILITY_THRESHOLD
    runner_up: Optional[str]     # second most frequent class (for diagnostics)
    flip_flopped: bool           # True if two specific classes tied closely


class VehicleClassAggregator:
    """
    Collects per-frame vehicle-class votes for each tracked vehicle and
    resolves to a final class at clip end.

    Voting rules (in priority order):
      1. Specific classes (motorcycle, car, truck, …) outrank generic ones
         ('vehicle', 'unknown') — a single confirmed 'motorcycle' vote
         beats ten 'vehicle' votes.
      2. Among specific classes, the most frequent wins.
      3. Tie between two specific classes → resolved by total confidence,
         and flip_flopped is set to True on the result.
      4. If only generic votes exist, the generic winner is used but
         is_stable is always False.
    """

    def __init__(self) -> None:
        # track_id -> list of ClassVote
        self._votes: Dict[int, List[ClassVote]] = defaultdict(list)

    def add_vote(
        self,
        track_id: int,
        vehicle_class: str,
        confidence: float = 1.0,
        frame_index: int = 0,
    ) -> None:
        """Record one frame's class prediction for a track."""
        if track_id < 0:
            return
        self._votes[track_id].append(ClassVote(
            frame_index=frame_index,
            vehicle_class=vehicle_class.strip().lower(),
            confidence=max(0.0, min(1.0, confidence)),
        ))

    def resolve_track(self, track_id: int) -> ClassResolution:
        """Resolve one track's accumulated votes into a final class."""
        votes = self._votes.get(track_id, [])
        total = len(votes)

        if total == 0:
            return ClassResolution(
                track_id=track_id,
                resolved_class="unknown",
                agreement=0.0,
                total_votes=0,
                specific_votes=0,
                is_stable=False,
                runner_up=None,
                flip_flopped=False,
            )

        # Separate specific from generic votes
        specific = [v for v in votes if v.vehicle_class not in _GENERIC_CLASSES]
        generic  = [v for v in votes if v.vehicle_class in _GENERIC_CLASSES]

        working_votes = specific if specific else generic
        n_specific    = len(specific)

        # Count votes per class
        class_counts: Dict[str, int]   = defaultdict(int)
        class_conf:   Dict[str, float] = defaultdict(float)
        for v in working_votes:
            class_counts[v.vehicle_class] += 1
            class_conf[v.vehicle_class]   += v.confidence

        # Sort by (count, total_confidence) descending
        ranked = sorted(
            class_counts.items(),
            key=lambda kv: (kv[1], class_conf[kv[0]]),
            reverse=True,
        )

        winner, winner_count = ranked[0]
        runner_up = ranked[1][0] if len(ranked) > 1 else None
        runner_up_count = ranked[1][1] if len(ranked) > 1 else 0

        n_working = len(working_votes)
        agreement = winner_count / n_working if n_working > 0 else 0.0

        # Detect flip-flop: top-2 classes within 1 vote of each other
        flip_flopped = (
            runner_up is not None
            and abs(winner_count - runner_up_count) <= 1
            and len(specific) >= _MIN_VOTES
        )

        is_stable = (
            agreement >= _STABILITY_THRESHOLD
            and total >= _MIN_VOTES
            and bool(specific)   # generic-only never stable
            and not flip_flopped
        )

        return ClassResolution(
            track_id=track_id,
            resolved_class=winner,
            agreement=round(agreement, 4),
            total_votes=total,
            specific_votes=n_specific,
            is_stable=is_stable,
            runner_up=runner_up,
            flip_flopped=flip_flopped,
        )

    def resolve_all(self) -> Dict[int, ClassResolution]:
        """Resolve all tracked vehicles. Returns {track_id: ClassResolution}."""
        return {tid: self.resolve_track(tid) for tid in self._votes}

    def track_ids(self) -> list[int]:
        """All track IDs that have at least one vote."""
        return list(self._votes.keys())

    def resolved_class(self, track_id: int) -> str:
        """Convenience: resolve one track and return just the class string."""
        return self.resolve_track(track_id).resolved_class
