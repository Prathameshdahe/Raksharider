"""
pipeline/vehicle_state.py
--------------------------
The central shared-state object for the track-centric pipeline.

Each tracked vehicle gets one VehicleState. Per-frame observations
(from rules.py, heuristics.py, plate_aggregator.py, vehicle_class_aggregator.py)
accumulate here keyed by track_id. At clip end, each VehicleState
resolves its observations into a per-violation verdict using:
  - Evidence count thresholds (minimum N frames to confirm)
  - Agreement ratio (minimum fraction of frames in agreement)
  - Confidence-weighted scoring

The registry (VehicleStateRegistry) holds all VehicleState objects and
is the single source of truth for building the final report.

Architecture note
-----------------
This is the "shared state" layer described in Section 5 of the master
build spec. It is intentionally separate from:
  - plate_aggregator.py  (OCR accumulation — feeds observations here)
  - vehicle_class_aggregator.py (class votes — feeds observations here)
  - rules.py / heuristics.py  (detection engines — feed observations here)
  - report.py  (consumes VehicleStateRegistry.export_report())

Only VehicleStateRegistry is imported by run_pipeline.py. Everything
else is an internal detail of this module.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pipeline.vehicle_class_gate import is_two_wheeler

logger = logging.getLogger(__name__)

# ── Resolution thresholds ────────────────────────────────────────────────────

# Minimum number of evidence frames to call something "confirmed"
MIN_EVIDENCE_FRAMES: int = 2

# Minimum fraction of evidence frames that agree to call something "confirmed"
MIN_AGREEMENT_RATIO: float = 0.55

# ── Violation types the pipeline knows about ─────────────────────────────────
KNOWN_VIOLATIONS = frozenset({
    "no_helmet",
    "triple_riding",
    "phone_usage",
    "wheelie",
    "erratic_driving",
    "signal_violation",
    "no_seatbelt",
    "wrong_way",
})


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class VehicleObservation:
    """
    One per-frame observation about a vehicle, from any source.

    source: which module produced this ('rules', 'heuristics', 'ocr',
            'vehicle_class', 'vlm')
    key:    what was observed (violation type OR 'vehicle_class' OR 'plate')
    value:  the observed value (True/False for violations, str for class/plate)
    confidence: detection confidence 0.0-1.0
    """
    frame_index: int
    timestamp: float
    source: str
    key: str
    value: Any                   # bool (violation) | str (class/plate)
    confidence: float = 1.0


@dataclass
class ViolationVerdict:
    """Resolved verdict for one violation type on one vehicle."""
    violation: str
    result: str                  # 'confirmed' | 'insufficient_evidence' | 'not_present'
    evidence_frames: int         # number of frames that observed this
    agreement: float             # fraction that agreed (positive)
    confidence: float            # mean confidence of positive observations
    reasoning: str               # human-readable explanation


@dataclass
class VehicleState:
    """
    Per-vehicle accumulated state and resolved verdicts.

    Created once per track_id. Observations are added via add_observation().
    Call resolve() at clip end to fill violation_verdicts.
    """
    track_id: int
    first_seen: float = 0.0
    last_seen: float = 0.0
    frames_observed: int = 0

    # Resolved from vehicle_class_aggregator
    vehicle_class: str = "unknown"
    class_confidence: float = 0.0
    class_is_stable: bool = False

    # Resolved from plate_aggregator
    plate_text: Optional[str] = None
    plate_confidence: float = 0.0
    plate_needs_review: bool = False

    # Raw observations accumulated per frame
    observations: List[VehicleObservation] = field(default_factory=list)

    # Resolved at end of clip
    violation_verdicts: Dict[str, ViolationVerdict] = field(default_factory=dict)
    is_resolved: bool = False

    def add_observation(self, obs: VehicleObservation) -> None:
        """Add one per-frame observation. Thread-safe enough for single-process use."""
        self.observations.append(obs)

    def resolve(self) -> None:
        """
        Resolve all accumulated observations into per-violation verdicts.
        Call once at clip end (after all frames have been processed).
        """
        if self.is_resolved:
            return

        # Group observations by key
        by_key: Dict[str, List[VehicleObservation]] = defaultdict(list)
        for obs in self.observations:
            by_key[obs.key].append(obs)

        for violation in KNOWN_VIOLATIONS:
            obs_list = by_key.get(violation, [])
            self.violation_verdicts[violation] = self._resolve_violation(
                violation, obs_list
            )

        self.is_resolved = True

    def _resolve_violation(
        self,
        violation: str,
        observations: List[VehicleObservation],
    ) -> ViolationVerdict:
        """
        Resolve one violation from its observation list.

        Evidence-count + agreement-ratio thresholds decide the verdict.
        VLM observations are weighted 2x (they're more reliable than
        per-frame YOLO checks).
        """
        if not observations:
            return ViolationVerdict(
                violation=violation,
                result="not_present",
                evidence_frames=0,
                agreement=0.0,
                confidence=0.0,
                reasoning=f"No observations for {violation}.",
            )

        # Guard: two-wheeler only violations must never be confirmed on non-two-wheelers
        if violation in {"no_helmet", "triple_riding", "wheelie"}:
            if self.vehicle_class != "unknown" and not is_two_wheeler(self.vehicle_class):
                return ViolationVerdict(
                    violation=violation,
                    result="not_present",
                    evidence_frames=0,
                    agreement=0.0,
                    confidence=0.0,
                    reasoning=f"Gated: {violation} is not applicable to non-two-wheeler '{self.vehicle_class}'.",
                )

        # Count positive vs total observations
        positive = [o for o in observations if bool(o.value) is True]
        total    = len(observations)
        n_pos    = len(positive)
        agreement = n_pos / total if total > 0 else 0.0

        # Weighted confidence (VLM observations count double)
        conf_sum = sum(
            (o.confidence * 2.0 if o.source == "vlm" else o.confidence)
            for o in positive
        )
        weight_sum = sum(
            (2.0 if o.source == "vlm" else 1.0)
            for o in positive
        )
        mean_conf = conf_sum / weight_sum if weight_sum > 0 else 0.0

        # Verdict logic
        if n_pos >= MIN_EVIDENCE_FRAMES and agreement >= MIN_AGREEMENT_RATIO:
            result = "confirmed"
            reasoning = (
                f"Confirmed: {n_pos}/{total} frames positive "
                f"(agreement {agreement:.0%}, mean_conf {mean_conf:.2f})."
            )
        elif n_pos > 0:
            result = "insufficient_evidence"
            reasoning = (
                f"Insufficient evidence: {n_pos}/{total} frames positive "
                f"(need >= {MIN_EVIDENCE_FRAMES} frames at >= {MIN_AGREEMENT_RATIO:.0%} agreement). "
                f"Agreement was {agreement:.0%}."
            )
        else:
            result = "not_present"
            reasoning = f"Not observed in {total} frame(s)."

        return ViolationVerdict(
            violation=violation,
            result=result,
            evidence_frames=n_pos,
            agreement=round(agreement, 4),
            confidence=round(mean_conf, 4),
            reasoning=reasoning,
        )

    def confirmed_violations(self) -> List[str]:
        """Shorthand: list of violation names with result='confirmed'."""
        return [
            v for v, vv in self.violation_verdicts.items()
            if vv.result == "confirmed"
        ]

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-safe dict for report output."""
        return {
            "track_id":         self.track_id,
            "first_seen":       self.first_seen,
            "last_seen":        self.last_seen,
            "frames_observed":  self.frames_observed,
            "vehicle_class":    self.vehicle_class,
            "class_confidence": self.class_confidence,
            "class_stable":     self.class_is_stable,
            "plate": {
                "text":         self.plate_text,
                "confidence":   self.plate_confidence,
                "needs_review": self.plate_needs_review,
            },
            "violations": {
                v: {
                    "result":          vv.result,
                    "evidence_frames": vv.evidence_frames,
                    "agreement":       vv.agreement,
                    "confidence":      vv.confidence,
                    "reasoning":       vv.reasoning,
                }
                for v, vv in self.violation_verdicts.items()
                if vv.result != "not_present"  # omit empty verdicts from output
            },
        }


class VehicleStateRegistry:
    """
    Holds all VehicleState objects for one video run.

    This is the single object passed between pipeline stages. It is
    created once in run_pipeline.py and referenced by every stage that
    needs to emit or read per-vehicle state.
    """

    def __init__(self) -> None:
        self._states: Dict[int, VehicleState] = {}

    def get_or_create(self, track_id: int) -> VehicleState:
        """Return existing VehicleState for track_id, or create a new one."""
        if track_id not in self._states:
            self._states[track_id] = VehicleState(track_id=track_id)
        return self._states[track_id]

    def add_observation(
        self,
        track_id: int,
        *,
        frame_index: int,
        timestamp: float,
        source: str,
        key: str,
        value: Any,
        confidence: float = 1.0,
    ) -> None:
        """Add one observation to the given track (creates state if needed)."""
        if track_id < 0:
            return
        state = self.get_or_create(track_id)
        state.add_observation(VehicleObservation(
            frame_index=frame_index,
            timestamp=timestamp,
            source=source,
            key=key,
            value=value,
            confidence=confidence,
        ))
        # Track first/last seen timestamps
        if state.frames_observed == 0:
            state.first_seen = timestamp
            state.last_seen  = timestamp
        else:
            if timestamp < state.first_seen:
                state.first_seen = timestamp
            if timestamp > state.last_seen:
                state.last_seen = timestamp
        state.frames_observed += 1

    def set_plate(
        self,
        track_id: int,
        plate_text: Optional[str],
        confidence: float,
        needs_review: bool = False,
    ) -> None:
        """Set resolved plate for a track (called from plate_aggregator result)."""
        if track_id < 0:
            return
        state = self.get_or_create(track_id)
        state.plate_text         = plate_text
        state.plate_confidence   = confidence
        state.plate_needs_review = needs_review

    def set_vehicle_class(
        self,
        track_id: int,
        vehicle_class: str,
        confidence: float = 1.0,
        is_stable: bool = False,
    ) -> None:
        """Set resolved vehicle class for a track (from vehicle_class_aggregator)."""
        if track_id < 0:
            return
        state = self.get_or_create(track_id)
        state.vehicle_class     = vehicle_class
        state.class_confidence  = confidence
        state.class_is_stable   = is_stable

    def merge_tracks(self, primary_id: int, fragment_id: int) -> None:
        """Merge state from fragment_id into primary_id."""
        if fragment_id == primary_id or fragment_id not in self._states:
            return
        primary = self.get_or_create(primary_id)
        frag = self._states.pop(fragment_id)
        primary.observations.extend(frag.observations)
        if primary.frames_observed == 0:
            primary.first_seen = frag.first_seen
            primary.last_seen = frag.last_seen
        elif frag.frames_observed > 0:
            primary.first_seen = min(primary.first_seen, frag.first_seen)
            primary.last_seen = max(primary.last_seen, frag.last_seen)
        primary.frames_observed += frag.frames_observed
        if not primary.plate_text and frag.plate_text:
            primary.plate_text = frag.plate_text
            primary.plate_confidence = frag.plate_confidence
            primary.plate_needs_review = frag.plate_needs_review

    def resolve_all(self) -> None:
        """Resolve all VehicleState objects. Call once at end of clip processing."""
        for state in self._states.values():
            state.resolve()

    def all_states(self) -> List[VehicleState]:
        """All VehicleState objects, sorted by track_id."""
        return sorted(self._states.values(), key=lambda s: s.track_id)

    def export_report(self) -> List[Dict[str, Any]]:
        """
        Export vehicle-centric report data for report.py.

        Returns a list of vehicle dicts, one per track, with confirmed
        violations only (insufficient_evidence and not_present are
        omitted from the vehicles array to keep the report readable —
        they are still in each VehicleState.violation_verdicts if
        needed for admin deep-dives).
        """
        if not any(s.is_resolved for s in self._states.values()):
            self.resolve_all()

        vehicles = []
        for state in self.all_states():
            confirmed = state.confirmed_violations()
            vehicles.append({
                **state.to_dict(),
                "confirmed_violations": confirmed,
                "has_violation": bool(confirmed),
            })
        return vehicles

    def __len__(self) -> int:
        return len(self._states)

    def __contains__(self, track_id: int) -> bool:
        return track_id in self._states
