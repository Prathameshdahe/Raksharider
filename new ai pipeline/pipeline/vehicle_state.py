"""
pipeline/vehicle_state.py
--------------------------
The single per-vehicle source of truth for the track-centric pipeline.

Every stage writes *observations* keyed by track_id; nothing is decided per
frame. At clip end each VehicleState resolves its observations into one
verdict per violation type.

Observation values are THREE-state:
    True   observed positive (violation seen)
    False  observed negative (checked, violation absent)
    None   not evaluable this frame (occluded / too small / out of frame)

Rules that matter:
  * One observation per (violation, frame). Duplicate entries for the same
    frame are collapsed before counting — evidence counts distinct frames.
  * Agreement = positive frames / evaluable frames. Unobservable frames never
    count against (or for) a vehicle.
  * Verdict results: confirmed | needs_review | observed_absent | unobservable
    | not_evaluated (see contract.VERDICT_RESULTS).
  * VLM observations override rule observations for the frame they examined.
  * Violations not applicable to the vehicle's wheel class are not_evaluated (policy flags).
  * review_only violations (wheelie, missing_plate, …) cap at needs_review.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pipeline.contract import (
    KNOWN_VIOLATIONS,
    VEHICLE_CLASSES,
    VIOLATION_POLICY,
    VehicleRecord,
    severity_for,
)
from pipeline.vehicle_class_gate import is_two_wheeler

logger = logging.getLogger(__name__)

# ── Resolution thresholds ────────────────────────────────────────────────────
MIN_EVIDENCE_FRAMES: int   = 3      # distinct positive frames needed to confirm ...
MIN_EVIDENCE_SPAN_S: float = 1.0    # ... spread over at least this much real time (dense 30 fps must not confirm on 0.1 s)
MIN_AGREEMENT_RATIO: float = 0.55   # positive / evaluable frames needed to confirm
MIN_ABSENT_FRAMES:   int   = 3      # negatives needed before "observed_absent" (else unobservable)
MIN_ABSENT_SPAN_S:   float = 1.0


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class VehicleObservation:
    """One per-frame observation about a vehicle, from any source."""
    frame_index: int
    timestamp: float
    source: str                  # 'rules' | 'heuristics' | 'vlm' | 'detector'
    key: str                     # violation type | 'present'
    value: Any                   # True | False | None
    confidence: float = 1.0


@dataclass
class ViolationVerdict:
    """Resolved verdict for one violation type on one vehicle."""
    violation:        str
    result:           str     # contract.VERDICT_RESULTS
    evidence_frames:  int     # distinct frames observed positive
    evaluable_frames: int     # distinct frames observed positive OR negative
    agreement:        float   # evidence_frames / evaluable_frames
    confidence:       float   # mean confidence of positive observations
    reasoning:        str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "result":           self.result,
            "evidence_frames":  self.evidence_frames,
            "evaluable_frames": self.evaluable_frames,
            "agreement":        self.agreement,
            "confidence":       self.confidence,
            "reasoning":        self.reasoning,
        }


def _dedup_per_frame(observations: List[VehicleObservation]) -> Dict[int, VehicleObservation]:
    """
    Collapse to ONE observation per frame_index.
    Priority: any VLM observation for that frame wins (it is the tiebreaker);
    otherwise True > False > None, ties broken by higher confidence.
    """
    def rank(o: VehicleObservation) -> tuple:
        vlm = 1 if o.source == "vlm" else 0
        val = 2 if o.value is True else (1 if o.value is False else 0)
        return (vlm, val, o.confidence)

    best: Dict[int, VehicleObservation] = {}
    for o in observations:
        cur = best.get(o.frame_index)
        if cur is None or rank(o) > rank(cur):
            best[o.frame_index] = o
    return best


@dataclass
class VehicleState:
    """Per-vehicle accumulated state and resolved verdicts."""
    track_id: int
    vehicle_class: str = "unknown"
    class_confidence: float = 0.0
    class_is_stable: bool = False

    plate_text: Optional[str] = None
    plate_confidence: float = 0.0
    plate_needs_review: bool = False
    plate_method: str = "none"                # none | ocr | ocr+claim | claim_only
    raw_plate_reads: List[str] = field(default_factory=list)
    identity_status: str = "provisional"      # contract.IDENTITY_STATUSES
    is_subject: bool = False

    observations: List[VehicleObservation] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)        # relative evidence file paths

    violation_verdicts: Dict[str, ViolationVerdict] = field(default_factory=dict)
    is_resolved: bool = False

    # ── observations ─────────────────────────────────────────────────────────
    def add_observation(self, obs: VehicleObservation) -> None:
        self.observations.append(obs)
        self.is_resolved = False

    def _presence_frames(self) -> Dict[int, float]:
        """frame_index -> timestamp for frames the vehicle was seen in."""
        present = [o for o in self.observations if o.key == "present"]
        pool = present if present else self.observations
        return {o.frame_index: o.timestamp for o in pool}

    @property
    def first_seen(self) -> float:
        fr = self._presence_frames()
        return round(min(fr.values()), 3) if fr else 0.0

    @property
    def last_seen(self) -> float:
        fr = self._presence_frames()
        return round(max(fr.values()), 3) if fr else 0.0

    @property
    def frames_observed(self) -> int:
        return len(self._presence_frames())

    # ── resolution ───────────────────────────────────────────────────────────
    def resolve(self) -> None:
        if self.is_resolved:
            return
        by_key: Dict[str, List[VehicleObservation]] = defaultdict(list)
        for obs in self.observations:
            by_key[obs.key].append(obs)
        for violation in KNOWN_VIOLATIONS:
            self.violation_verdicts[violation] = self._resolve_violation(violation, by_key.get(violation, []))
        self.is_resolved = True

    def _resolve_violation(self, violation: str, observations: List[VehicleObservation]) -> ViolationVerdict:
        policy = VIOLATION_POLICY[violation]

        def verdict(result: str, reasoning: str, n_pos: int = 0, n_eval: int = 0, agreement: float = 0.0, conf: float = 0.0):
            return ViolationVerdict(violation, result, n_pos, n_eval, round(agreement, 4), round(conf, 4), reasoning)

        if not policy["enabled"]:
            return verdict("not_evaluated", f"{violation} is disabled by policy.")
        if self.vehicle_class != "unknown":
            applicable = policy["two_wheeler"] if is_two_wheeler(self.vehicle_class) else policy["four_wheeler"]
            if not applicable:
                return verdict("not_evaluated", f"{violation} is not applicable to '{self.vehicle_class}'.")
        if not observations:
            return verdict("not_evaluated", f"{violation} was never checked for this vehicle.")

        per_frame = _dedup_per_frame(observations)
        positives = [o for o in per_frame.values() if o.value is True]
        negatives = [o for o in per_frame.values() if o.value is False]
        n_pos, n_eval = len(positives), len(positives) + len(negatives)

        if n_eval == 0:
            return verdict("unobservable",
                           f"{violation} could not be evaluated in any of {len(per_frame)} frame(s) "
                           f"(occluded / too small / out of frame).")

        agreement = n_pos / n_eval
        # VLM confirmations weigh double in the confidence mean.
        w = [(o.confidence, 2.0 if o.source == "vlm" else 1.0) for o in positives]
        mean_conf = sum(c * k for c, k in w) / sum(k for _, k in w) if w else 0.0

        if n_pos == 0:
            neg_ts = [o.timestamp for o in negatives]
            if len(negatives) >= MIN_ABSENT_FRAMES and (max(neg_ts) - min(neg_ts)) >= MIN_ABSENT_SPAN_S:
                return verdict("observed_absent",
                               f"Checked in {n_eval} frame(s) over {max(neg_ts) - min(neg_ts):.1f}s; {violation} not observed.",
                               0, n_eval, 0.0, 0.0)
            return verdict("unobservable",
                           f"{violation} checked in only {n_eval} frame(s); not enough to say it was absent.",
                           0, n_eval, 0.0, 0.0)
        pos_ts = [o.timestamp for o in positives]
        span_ok = (max(pos_ts) - min(pos_ts)) >= MIN_EVIDENCE_SPAN_S
        if n_pos >= MIN_EVIDENCE_FRAMES and span_ok and agreement >= MIN_AGREEMENT_RATIO and not policy["review_only"]:
            return verdict("confirmed",
                           f"Confirmed: {n_pos}/{n_eval} evaluable frames positive "
                           f"(agreement {agreement:.0%}, mean_conf {mean_conf:.2f}).",
                           n_pos, n_eval, agreement, mean_conf)
        why = ("review-only violation type" if policy["review_only"]
               else f"need >= {MIN_EVIDENCE_FRAMES} frames over >= {MIN_EVIDENCE_SPAN_S:.0f}s at >= {MIN_AGREEMENT_RATIO:.0%} agreement")
        return verdict("needs_review",
                       f"Needs review: {n_pos}/{n_eval} evaluable frames positive "
                       f"(agreement {agreement:.0%}, mean_conf {mean_conf:.2f}); {why}.",
                       n_pos, n_eval, agreement, mean_conf)

    # ── read-outs ────────────────────────────────────────────────────────────
    def confirmed_violations(self) -> List[str]:
        return [v for v, vv in self.violation_verdicts.items() if vv.result == "confirmed"]

    def review_violations(self) -> List[str]:
        return [v for v, vv in self.violation_verdicts.items() if vv.result == "needs_review"]

    def flagged_violations(self) -> List[str]:
        return self.confirmed_violations() + self.review_violations()

    def detection_confidence(self) -> float:
        confs = [self.violation_verdicts[v].confidence for v in self.flagged_violations()]
        return round(max(confs), 4) if confs else 0.0

    def positive_frames(self, violation: Optional[str] = None) -> List[VehicleObservation]:
        """Deduped positive observations (for evidence-frame selection), best first."""
        keys = [violation] if violation else self.flagged_violations()
        out: List[VehicleObservation] = []
        for k in keys:
            per_frame = _dedup_per_frame([o for o in self.observations if o.key == k])
            out.extend(o for o in per_frame.values() if o.value is True)
        # one per frame, highest confidence first
        best: Dict[int, VehicleObservation] = {}
        for o in out:
            if o.frame_index not in best or o.confidence > best[o.frame_index].confidence:
                best[o.frame_index] = o
        return sorted(best.values(), key=lambda o: o.confidence, reverse=True)

    def to_record(self) -> VehicleRecord:
        if not self.is_resolved:
            self.resolve()
        confirmed, review = self.confirmed_violations(), self.review_violations()
        return VehicleRecord(
            track_id=self.track_id,
            vehicle_class=self.vehicle_class,
            class_confidence=round(self.class_confidence, 4),
            class_stable=self.class_is_stable,
            first_seen=self.first_seen,
            last_seen=self.last_seen,
            frames_observed=self.frames_observed,
            plate={
                "text":         self.plate_text,
                "confidence":   round(self.plate_confidence, 4),
                "needs_review": self.plate_needs_review,
                "raw_reads":    list(self.raw_plate_reads),
                "method":       self.plate_method,
            },
            # omit not_evaluated verdicts to keep the record readable
            verdicts={v: vv.to_dict() for v, vv in self.violation_verdicts.items() if vv.result != "not_evaluated"},
            confirmed_violations=confirmed,
            review_violations=review,
            detection_confidence=self.detection_confidence(),
            severity=severity_for(confirmed + review),
            evidence=list(self.evidence),
            has_violation=bool(confirmed),
            needs_review=bool(confirmed or review),
            identity_status=self.identity_status,
            is_subject=self.is_subject,
        )

    def to_dict(self) -> Dict[str, Any]:
        return self.to_record().to_dict()


class VehicleStateRegistry:
    """All VehicleState objects for one run. Only vehicle-class tracks live here."""

    def __init__(self) -> None:
        self._states: Dict[int, VehicleState] = {}

    def get_or_create(self, track_id: int) -> VehicleState:
        if track_id not in self._states:
            self._states[track_id] = VehicleState(track_id=track_id)
        return self._states[track_id]

    def add_observation(self, track_id: int, *, frame_index: int, timestamp: float,
                        source: str, key: str, value: Any, confidence: float = 1.0) -> None:
        if track_id < 0:
            return
        self.get_or_create(track_id).add_observation(VehicleObservation(
            frame_index=frame_index, timestamp=timestamp, source=source,
            key=key, value=value, confidence=confidence,
        ))

    def set_plate(self, track_id: int, plate_text: Optional[str], confidence: float,
                  needs_review: bool = False, raw_reads: Optional[List[str]] = None,
                  method: Optional[str] = None) -> None:
        if track_id < 0:
            return
        s = self.get_or_create(track_id)
        s.plate_text, s.plate_confidence, s.plate_needs_review = plate_text, confidence, needs_review
        if raw_reads is not None:
            s.raw_plate_reads = list(raw_reads)
        s.plate_method = method if method is not None else ("ocr" if plate_text else "none")

    def set_vehicle_class(self, track_id: int, vehicle_class: str, confidence: float = 1.0,
                          is_stable: bool = False) -> bool:
        """Register a track's class. Person tracks are refused: they are not vehicles."""
        if track_id < 0 or vehicle_class not in VEHICLE_CLASSES:
            return False
        s = self.get_or_create(track_id)
        s.vehicle_class, s.class_confidence, s.class_is_stable = vehicle_class, confidence, is_stable
        s.is_resolved = False
        return True

    def merge_tracks(self, primary_id: int, fragment_id: int) -> None:
        """Merge ALL state of fragment into primary in one operation."""
        if fragment_id == primary_id or fragment_id not in self._states:
            return
        primary = self.get_or_create(primary_id)
        frag = self._states.pop(fragment_id)
        primary.observations.extend(frag.observations)
        primary.evidence.extend(frag.evidence)
        primary.raw_plate_reads.extend(frag.raw_plate_reads)
        if primary.vehicle_class == "unknown" and frag.vehicle_class != "unknown":
            primary.vehicle_class, primary.class_confidence, primary.class_is_stable = (
                frag.vehicle_class, frag.class_confidence, frag.class_is_stable)
        if frag.plate_text and (not primary.plate_text or frag.plate_confidence > primary.plate_confidence):
            primary.plate_text, primary.plate_confidence, primary.plate_needs_review, primary.plate_method = (
                frag.plate_text, frag.plate_confidence, frag.plate_needs_review, frag.plate_method)
        primary.is_subject = primary.is_subject or frag.is_subject
        primary.is_resolved = False

    def resolve_all(self) -> None:
        for s in self._states.values():
            s.resolve()

    def all_states(self) -> List[VehicleState]:
        return sorted(self._states.values(), key=lambda s: s.track_id)

    def get(self, track_id: int) -> Optional[VehicleState]:
        return self._states.get(track_id)

    def export_report(self) -> List[Dict[str, Any]]:
        """Contract-shaped vehicle records (see contract.VehicleRecord)."""
        self.resolve_all()
        return [s.to_dict() for s in self.all_states()]

    def __len__(self) -> int:
        return len(self._states)

    def __contains__(self, track_id: int) -> bool:
        return track_id in self._states
