"""
pipeline/contract.py
--------------------
The ONE schema shared by the pipeline (producer), the worker (consumer) and the
database persistence function (`persist_run_result(jsonb)`).

contract_version 2.0 (17 Sep 2026)

* VIOLATION_POLICY   seriousness tiers / enabled flags per violation type. Mirrors the
                     `violation_policy` table.
* VehicleRecord      one tracked vehicle: identity, plate, verdicts, evidence.
* Finding            one (vehicle, violation) candidate with its auto-score tier.
* PlateObservation   one raw OCR read on one frame of one track.
* Allegation         what the uploader claimed and how the AI answered it.
* ResultPackage      everything the worker returns for one run. No decisions inside.

Invariants are asserted (never just logged) in `check()` / `validate_package()`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

CONTRACT_VERSION = "2.0"

# ── Violation policy ─────────────────────────────────────────────────────────
# tier          : top | middle | minor  (queue priority + escalation weight, see docs/PRODUCT_FLOW_DECISIONS.md §5)
# severity      : policy seriousness in [0, 1]; NOT a confidence.
# two_wheeler   : only evaluable on motorcycles / bicycles.
# four_wheeler  : reportable on cars/buses/trucks.
# review_only   : geometry alone can never "confirm"; best result is needs_review.
# enabled       : disabled types are reported as not_evaluated (uploader may still declare them).
VIOLATION_POLICY: Dict[str, Dict[str, Any]] = {
    "wheelie":          {"label": "Wheelie (stunt riding)",   "tier": "top",    "severity": 0.90, "enabled": True,  "two_wheeler": True,  "four_wheeler": False, "review_only": True},
    "phone_usage":      {"label": "Phone use while driving",  "tier": "top",    "severity": 0.85, "enabled": True,  "two_wheeler": True,  "four_wheeler": True,  "review_only": False},
    "wrong_way":        {"label": "Wrong side / wrong way",   "tier": "top",    "severity": 0.90, "enabled": False, "two_wheeler": True,  "four_wheeler": True,  "review_only": True},
    "signal_violation": {"label": "Red-light violation",      "tier": "top",    "severity": 0.90, "enabled": False, "two_wheeler": True,  "four_wheeler": True,  "review_only": True},
    "no_helmet":        {"label": "Riding without helmet",    "tier": "middle", "severity": 0.70, "enabled": True,  "two_wheeler": True,  "four_wheeler": False, "review_only": False},
    "triple_riding":    {"label": "Triple riding",            "tier": "middle", "severity": 0.75, "enabled": True,  "two_wheeler": True,  "four_wheeler": False, "review_only": False},
    "lane_cutting":     {"label": "Lane cutting",             "tier": "middle", "severity": 0.60, "enabled": False, "two_wheeler": True,  "four_wheeler": True,  "review_only": True},
    # Disabled: from a moving dashcam, lateral motion is dominated by ego-motion and
    # parallax (vehicles at different depths shift at different rates). On a real jam clip
    # the heuristic flagged 21 of 23 vehicles. Re-enable only with proper ego-motion
    # estimation and a measured false-positive rate on the evaluation set.
    "erratic_driving":  {"label": "Erratic driving",          "tier": "middle", "severity": 0.60, "enabled": False, "two_wheeler": True,  "four_wheeler": True,  "review_only": True},
    "missing_plate":    {"label": "Missing / obscured plate", "tier": "minor",  "severity": 0.50, "enabled": True,  "two_wheeler": True,  "four_wheeler": True,  "review_only": True},
    "no_seatbelt":      {"label": "No seatbelt",              "tier": "minor",  "severity": 0.50, "enabled": False, "two_wheeler": False, "four_wheeler": True,  "review_only": True},
}
KNOWN_VIOLATIONS = tuple(VIOLATION_POLICY)
TIER_PRIORITY = {"top": 3, "middle": 2, "minor": 1}

VERDICT_RESULTS   = ("confirmed", "needs_review", "observed_absent", "unobservable", "not_evaluated")
IDENTITY_STATUSES = ("resolved", "provisional", "ambiguous", "conflict")
AUTO_TIERS        = ("A", "B", "C")             # A high-confidence queue, B normal review, C report only
ALLEGATION_ANSWERS = ("supported", "not_supported", "unobservable", "ambiguous_subject", "manual_review", "not_declared")

VEHICLE_CLASSES = frozenset({
    "motorcycle", "bicycle", "car", "bus", "truck", "mini_lcv", "auto_rickshaw", "vehicle",
})

# Auto-score tier gate (docs §4.8). Tuned from reviewer rejections against the evaluation set.
TIER_A_MIN_FRAMES     = 3
TIER_A_MIN_AGREEMENT  = 0.70
TIER_A_MIN_CONFIDENCE = 0.60


def severity_for(violations: List[str]) -> float:
    return max((VIOLATION_POLICY[v]["severity"] for v in violations if v in VIOLATION_POLICY), default=0.0)


def priority_for(violation: Optional[str]) -> int:
    """Queue priority of a declared violation: 3 top, 2 middle, 1 minor, 0 unknown/none."""
    if not violation or violation not in VIOLATION_POLICY:
        return 0
    return TIER_PRIORITY[VIOLATION_POLICY[violation]["tier"]]


def auto_tier(result: str, evidence_frames: int, agreement: float, confidence: float,
              identity_status: str, vlm_contradicts: bool = False) -> str:
    """The automation gate. Identity ambiguity forces B no matter how strong the evidence."""
    if result in ("observed_absent", "not_evaluated"):
        return "C"
    if result == "unobservable":
        return "C"
    if identity_status != "resolved":       # no resolved plate -> nothing to count against; never "high confidence"
        return "B" if evidence_frames >= 1 else "C"
    if (result == "confirmed" and evidence_frames >= TIER_A_MIN_FRAMES and agreement >= TIER_A_MIN_AGREEMENT
            and confidence >= TIER_A_MIN_CONFIDENCE and not vlm_contradicts):
        return "A"
    if evidence_frames >= 1:
        return "B"
    return "C"


def finding_id(run_id: str, track_id: int, violation: str) -> str:
    """Deterministic: a retry of the same run produces the same ids (idempotent persistence)."""
    return hashlib.sha1(f"{run_id}:{track_id}:{violation}".encode()).hexdigest()[:20]


# ── Per-vehicle record ───────────────────────────────────────────────────────

@dataclass
class VehicleRecord:
    track_id:             int
    vehicle_class:        str
    class_confidence:     float
    class_stable:         bool
    first_seen:           float
    last_seen:            float
    frames_observed:      int
    plate:                Dict[str, Any]            # {text, confidence, needs_review, raw_reads, method}
    verdicts:             Dict[str, Dict[str, Any]] # violation -> {result, evidence_frames, evaluable_frames, agreement, confidence, reasoning, tier}
    confirmed_violations: List[str] = field(default_factory=list)
    review_violations:    List[str] = field(default_factory=list)
    detection_confidence: float = 0.0
    severity:             float = 0.0
    evidence:             List[str] = field(default_factory=list)
    has_violation:        bool = False
    needs_review:         bool = False
    identity_status:      str = "provisional"
    is_subject:           bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "track_id":             self.track_id,
            "vehicle_class":        self.vehicle_class,
            "class_confidence":     self.class_confidence,
            "class_stable":         self.class_stable,
            "first_seen":           self.first_seen,
            "last_seen":            self.last_seen,
            "frames_observed":      self.frames_observed,
            "plate":                self.plate,
            "verdicts":             self.verdicts,
            "confirmed_violations": list(self.confirmed_violations),
            "review_violations":    list(self.review_violations),
            "detection_confidence": self.detection_confidence,
            "severity":             self.severity,
            "evidence":             list(self.evidence),
            "has_violation":        self.has_violation,
            "needs_review":         self.needs_review,
            "identity_status":      self.identity_status,
            "is_subject":           self.is_subject,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "VehicleRecord":
        required = ("track_id", "vehicle_class", "first_seen", "last_seen", "frames_observed", "plate", "verdicts")
        missing = [k for k in required if k not in d]
        if missing:
            raise ValueError(f"VehicleRecord missing keys: {missing}")
        plate = d["plate"]
        if not isinstance(plate, dict) or "text" not in plate:
            raise ValueError("VehicleRecord.plate must be a dict with a 'text' key")
        verdicts = d["verdicts"]
        if not isinstance(verdicts, dict):
            raise ValueError("VehicleRecord.verdicts must be a dict")
        for v, vv in verdicts.items():
            if vv.get("result") not in VERDICT_RESULTS:
                raise ValueError(f"verdict {v!r} has invalid result {vv.get('result')!r}")
        identity = d.get("identity_status", "provisional")
        if identity not in IDENTITY_STATUSES:
            raise ValueError(f"invalid identity_status {identity!r}")
        rec = cls(
            track_id=int(d["track_id"]),
            vehicle_class=str(d["vehicle_class"]),
            class_confidence=float(d.get("class_confidence", 0.0)),
            class_stable=bool(d.get("class_stable", False)),
            first_seen=float(d["first_seen"]),
            last_seen=float(d["last_seen"]),
            frames_observed=int(d["frames_observed"]),
            plate={
                "text":         plate.get("text"),
                "confidence":   float(plate.get("confidence", 0.0)),
                "needs_review": bool(plate.get("needs_review", False)),
                "raw_reads":    list(plate.get("raw_reads", [])),
                "method":       plate.get("method", "none"),
            },
            verdicts=verdicts,
            confirmed_violations=list(d.get("confirmed_violations", [])),
            review_violations=list(d.get("review_violations", [])),
            detection_confidence=float(d.get("detection_confidence", 0.0)),
            severity=float(d.get("severity", 0.0)),
            evidence=list(d.get("evidence", [])),
            has_violation=bool(d.get("has_violation", False)),
            needs_review=bool(d.get("needs_review", False)),
            identity_status=identity,
            is_subject=bool(d.get("is_subject", False)),
        )
        rec.check()
        return rec

    def check(self, video_duration: Optional[float] = None) -> None:
        assert self.track_id >= 0, f"track {self.track_id}: negative track_id"
        assert 0.0 <= self.first_seen <= self.last_seen, (
            f"track {self.track_id}: first_seen={self.first_seen} last_seen={self.last_seen}")
        if video_duration is not None:
            assert self.last_seen <= video_duration + 1e-6, (
                f"track {self.track_id}: last_seen {self.last_seen} exceeds duration {video_duration}")
        for v in self.confirmed_violations:
            assert self.verdicts.get(v, {}).get("result") == "confirmed", (
                f"track {self.track_id}: {v} listed as confirmed but verdict is {self.verdicts.get(v)}")
        for v in self.review_violations:
            assert self.verdicts.get(v, {}).get("result") == "needs_review", (
                f"track {self.track_id}: {v} listed for review but verdict is {self.verdicts.get(v)}")
        assert self.has_violation == bool(self.confirmed_violations)
        assert self.needs_review == bool(self.confirmed_violations or self.review_violations)


def validate_vehicle_records(records: List[Dict[str, Any]], video_duration: Optional[float] = None) -> List[VehicleRecord]:
    parsed = [VehicleRecord.from_dict(r) for r in records]
    seen: set[int] = set()
    for rec in parsed:
        rec.check(video_duration)
        assert rec.track_id not in seen, f"duplicate track_id {rec.track_id} in vehicle records"
        seen.add(rec.track_id)
    return parsed


# ── Package parts ────────────────────────────────────────────────────────────

@dataclass
class PlateObservation:
    track_id:    int
    text:        str
    engine:      str            # easyocr | paddleocr | vlm
    confidence:  float
    frame_index: int
    timestamp:   float
    crop_path:   Optional[str] = None
    is_valid:    bool = False

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Finding:
    finding_id:       str
    track_id:         int
    violation:        str
    result:           str        # VERDICT_RESULTS
    tier:             str        # AUTO_TIERS
    evidence_frames:  int
    evaluable_frames: int
    agreement:        float
    confidence:       float
    reasoning:        str
    evidence:         List[str] = field(default_factory=list)   # relative paths
    vlm_call_id:      Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class EvidenceItem:
    finding_id:  str
    track_id:    int
    frame_index: int
    timestamp:   float
    path:        str             # relative to the run output dir; blob name = {submission}/{run}/{path}
    sha256:      str

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Allegation:
    declared_violation: Optional[str]
    claimed_plate:      Optional[str]
    vehicle_type:       str
    subject_track_id:   Optional[int]
    answer:             str          # ALLEGATION_ANSWERS
    reason:             str

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class ResultPackage:
    run_id:             str
    submission_id:      Optional[str]
    pipeline_version:   str
    model_versions:     Dict[str, str]
    started_at:         str
    finished_at:        str
    duration_seconds:   float
    allegation:         Dict[str, Any]
    vehicle_tracks:     List[Dict[str, Any]]     # VehicleRecord dicts
    plate_observations: List[Dict[str, Any]]
    findings:           List[Dict[str, Any]]
    evidence:           List[Dict[str, Any]]
    vlm_calls:          List[Dict[str, Any]]
    summary:            Dict[str, Any]
    detection_video:    Optional[str] = None     # relative path of the annotated video
    contract_version:   str = CONTRACT_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contract_version":   self.contract_version,
            "run_id":             self.run_id,
            "submission_id":      self.submission_id,
            "pipeline_version":   self.pipeline_version,
            "model_versions":     self.model_versions,
            "started_at":         self.started_at,
            "finished_at":        self.finished_at,
            "duration_seconds":   self.duration_seconds,
            "allegation":         self.allegation,
            "vehicle_tracks":     self.vehicle_tracks,
            "plate_observations": self.plate_observations,
            "findings":           self.findings,
            "evidence":           self.evidence,
            "vlm_calls":          self.vlm_calls,
            "summary":            self.summary,
            "detection_video":    self.detection_video,
        }


def validate_package(p: Dict[str, Any]) -> ResultPackage:
    """Strict parse + invariants. Raises on anything the database must not receive."""
    assert p.get("contract_version") == CONTRACT_VERSION, f"contract_version {p.get('contract_version')!r} != {CONTRACT_VERSION}"
    for k in ("run_id", "allegation", "vehicle_tracks", "findings", "evidence", "summary"):
        assert k in p, f"package missing {k}"
    tracks = validate_vehicle_records(p["vehicle_tracks"], p.get("duration_seconds"))
    track_ids = {t.track_id for t in tracks}
    alg = p["allegation"]
    assert alg.get("answer") in ALLEGATION_ANSWERS, f"bad allegation answer {alg.get('answer')!r}"
    if alg.get("subject_track_id") is not None:
        assert alg["subject_track_id"] in track_ids, "allegation.subject_track_id is not a vehicle track"
    fids: set[str] = set()
    for f in p["findings"]:
        for k in ("finding_id", "track_id", "violation", "result", "tier"):
            assert k in f, f"finding missing {k}"
        assert f["track_id"] in track_ids, f"finding {f['finding_id']} references unknown track {f['track_id']}"
        assert f["violation"] in VIOLATION_POLICY, f"unknown violation {f['violation']!r}"
        assert f["result"] in VERDICT_RESULTS and f["tier"] in AUTO_TIERS
        assert f["finding_id"] == finding_id(p["run_id"], f["track_id"], f["violation"]), "finding_id is not deterministic"
        assert f["finding_id"] not in fids, f"duplicate finding {f['finding_id']}"
        fids.add(f["finding_id"])
    for e in p["evidence"]:
        assert e["finding_id"] in fids, f"evidence {e.get('path')} references unknown finding"
        assert e["track_id"] in track_ids
        assert e.get("path") and not str(e["path"]).startswith("/"), "evidence path must be relative"
        assert len(e.get("sha256", "")) == 64, "evidence sha256 missing"
    for po in p.get("plate_observations", []):
        assert po["track_id"] in track_ids, f"plate observation references unknown track {po['track_id']}"
    return ResultPackage(
        run_id=p["run_id"], submission_id=p.get("submission_id"), pipeline_version=p.get("pipeline_version", ""),
        model_versions=p.get("model_versions", {}), started_at=p.get("started_at", ""), finished_at=p.get("finished_at", ""),
        duration_seconds=float(p.get("duration_seconds", 0.0)), allegation=alg, vehicle_tracks=p["vehicle_tracks"],
        plate_observations=p.get("plate_observations", []), findings=p["findings"], evidence=p["evidence"],
        vlm_calls=p.get("vlm_calls", []), summary=p["summary"], detection_video=p.get("detection_video"),
    )
