"""
pipeline/identity.py
--------------------
Plate-anchored identity ledger and subject selection.

Rules (docs/PRODUCT_FLOW_DECISIONS.md §4.4/§4.6 after the design critique):

  * The uploader's claimed plate NEVER takes part in plate validation. A plate is
    resolved only from >= 2 agreeing OCR reads on DISTINCT frames at >= PLATE_MIN_CONF,
    Indian format, real state code.
  * The claim is compared AFTER resolution on canonicalised strings:
      exact match          -> claim_match "exact"   (identity stays resolved)
      Levenshtein 1        -> "near"  -> identity CONFLICT (0/O, 8/B, 1/I are exactly the
                              errors a human typing and an OCR share; never agreement)
      otherwise            -> "mismatch" -> CONFLICT
  * One valid read that equals the claim -> PROVISIONAL, method "ocr+claim" (tier B).
  * Two different plates each with >= 2 frames on one track -> CONFLICT (two vehicles).
  * A look-alike vehicle nearby with no plate -> AMBIGUOUS.
  * Subject selection never reads findings: plate match first, then dominance of the
    declared vehicle type (must beat the runner-up 2x), else ambiguous_subject / not seen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pipeline.indian_plate_validator import is_real_state_code
from pipeline.vehicle_class_gate import is_two_wheeler

PLATE_MIN_CONF: float = 0.60
PLATE_MIN_FRAMES: int = 2
SUBJECT_DOMINANCE_RATIO: float = 2.0


def canonical(plate: Optional[str]) -> str:
    return re.sub(r"[^A-Z0-9]", "", (plate or "").upper())


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


@dataclass
class ReadLike:
    text: str
    confidence: float
    frame_index: int
    is_valid: bool


@dataclass
class IdentityResolution:
    status: str                       # resolved | provisional | ambiguous | conflict
    plate: Optional[str]
    confidence: float
    method: str                       # none | ocr | ocr+claim
    claim_match: str                  # none | exact | near | mismatch
    reason: str
    candidates: List[Tuple[str, int, float]] = field(default_factory=list)   # (plate, distinct frames, mean conf)


def resolve_identity(reads: List[ReadLike], claimed_plate: Optional[str], lookalike_nearby: bool = False) -> IdentityResolution:
    claim = canonical(claimed_plate)
    # group valid, confident reads by canonical text; count DISTINCT frames per text
    frames: Dict[str, set] = {}
    confs: Dict[str, List[float]] = {}
    for r in reads:
        t = canonical(r.text)
        if not t or not r.is_valid or r.confidence < PLATE_MIN_CONF or not is_real_state_code(t):
            continue
        frames.setdefault(t, set()).add(r.frame_index)
        confs.setdefault(t, []).append(r.confidence)
    cands = sorted(((t, len(f), sum(confs[t]) / len(confs[t])) for t, f in frames.items()),
                   key=lambda x: (x[1], x[2]), reverse=True)
    strong = [c for c in cands if c[1] >= PLATE_MIN_FRAMES]

    if len(strong) >= 2:
        return IdentityResolution("conflict", None, 0.0, "ocr", "none",
                                  f"Two different plates read on one track: {strong[0][0]} and {strong[1][0]}.", cands)
    if strong:
        plate, n, conf = strong[0]
        match = "none"
        if claim:
            d = levenshtein(plate, claim)
            match = "exact" if d == 0 else ("near" if d == 1 else "mismatch")
            if match != "exact":
                return IdentityResolution("conflict", plate, conf, "ocr", match,
                                          f"OCR read {plate} ({n} frames) does not match the uploader's claim {claim}.", cands)
        return IdentityResolution("resolved", plate, conf, "ocr", match,
                                  f"{n} agreeing reads on distinct frames.", cands)
    if cands:
        plate, n, conf = cands[0]
        if claim and plate == claim:
            return IdentityResolution("provisional", plate, conf, "ocr+claim", "exact",
                                      "One OCR read agrees with the uploader's claim; needs a second frame to resolve.", cands)
        return IdentityResolution("ambiguous" if lookalike_nearby else "provisional", None, 0.0, "none",
                                  ("mismatch" if claim else "none"),
                                  "Only one usable OCR read; plate not resolved.", cands)
    return IdentityResolution("ambiguous" if lookalike_nearby else "provisional", None, 0.0, "none",
                              ("mismatch" if claim else "none") if False else "none",
                              "No usable plate read." + (" A similar vehicle was nearby." if lookalike_nearby else ""), cands)


@dataclass
class TrackSummary:
    track_id: int
    vehicle_class: str
    dominance: float          # sum over frames of area_fraction × lower-centre weight
    seconds_visible: float


@dataclass
class SubjectChoice:
    track_id: Optional[int]
    method: str               # plate | dominance | none
    answer_hint: str          # ok | ambiguous_subject | declared_vehicle_not_seen | not_declared
    candidates: List[int]
    reason: str


def choose_subject(tracks: List[TrackSummary], identities: Dict[int, IdentityResolution],
                   declared_type: Optional[str], claimed_plate: Optional[str]) -> SubjectChoice:
    """Independent of findings. See module docstring."""
    claim = canonical(claimed_plate)
    if claim:
        hits = [t.track_id for t in tracks if identities.get(t.track_id) and canonical(identities[t.track_id].plate) == claim]
        if len(hits) == 1:
            return SubjectChoice(hits[0], "plate", "ok", hits, "Resolved plate equals the uploader's claim.")
        if len(hits) > 1:
            return SubjectChoice(None, "plate", "ambiguous_subject", hits, "Several tracks resolved to the claimed plate.")
    if not declared_type:
        return SubjectChoice(None, "none", "not_declared", [], "No vehicle type declared.")
    want_two = declared_type == "two_wheeler"
    pool = [t for t in tracks if is_two_wheeler(t.vehicle_class) == want_two]
    if not pool:
        return SubjectChoice(None, "none", "declared_vehicle_not_seen", [], f"No {declared_type} track found in the clip.")
    pool.sort(key=lambda t: t.dominance, reverse=True)
    if len(pool) == 1 or pool[0].dominance >= SUBJECT_DOMINANCE_RATIO * max(pool[1].dominance, 1e-9):
        return SubjectChoice(pool[0].track_id, "dominance", "ok", [pool[0].track_id],
                             f"Most prominent {declared_type} in front of the camera.")
    tied = [t.track_id for t in pool if t.dominance >= pool[0].dominance / SUBJECT_DOMINANCE_RATIO]
    return SubjectChoice(None, "dominance", "ambiguous_subject", tied,
                         f"{len(tied)} {declared_type}s are equally prominent; the reviewer picks.")


def lookalikes(tracks: List[TrackSummary], overlaps: Dict[Tuple[int, int], bool]) -> Dict[int, bool]:
    """
    track_id -> another track of the same wheel class was near it at the same time.
    `overlaps[(a, b)]` is computed in the run loop (same frame, centroid distance < 1.5 × box width).
    """
    near: Dict[int, bool] = {t.track_id: False for t in tracks}
    for (a, b), v in overlaps.items():
        if v:
            near[a] = True
            near[b] = True
    return near
