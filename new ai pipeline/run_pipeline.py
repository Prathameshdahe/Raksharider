"""
run_pipeline.py
---------------
DriveTrust / RoadWatch detection pipeline — v3 (streaming, accuracy first).

    python run_pipeline.py clip.mp4 [--declared no_helmet] [--plate MH02FX9484]
                                    [--vehicle-type two_wheeler] [--vlm]
                                    [--target-fps 15] [--imgsz 1280] [--tracker botsort|bytetrack]

Design (docs/PRODUCT_FLOW_DECISIONS.md §4):
  0  probe + stream every frame (stride to ~15 fps) — one frame in memory at a time
  1  vehicles/persons at 1280 + BoT-SORT (ReID + camera-motion compensation)
  2  helmet / plate / phone on zoomed crops of each vehicle
  3  per-vehicle findings → three-state observations on ONE track; keyframes kept per track
  4  plate reads → identity ledger (claim never validates; compared afterwards)
  5  subject chosen WITHOUT looking at findings; allegation answered from its verdict
  6  Gemini → Nemotron tiebreaker, one question per finding, max 3 calls
  7  findings + tiers, evidence per finding, detection video (faces + non-subject plates blurred)
  8  ResultPackage (contract 2.0) validated, written as package.json + report.json

Invariants are asserted, never logged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent / ".env")
except ImportError:
    pass

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

from pipeline.frame_extractor          import iter_frames, probe_video, default_stride, extract_frames
from pipeline.detector                 import (Detection, FrameDetections, DEFAULT_IMGSZ, detect_vehicles,
                                               detect_rois_owned, model_versions)
from pipeline.rules                    import apply_rules, _iou
from pipeline.plate_aggregator         import PlateAggregator, find_nearest_track_id
from pipeline.verification             import aggregate_verdicts
from pipeline.report                   import PIPELINE_VERSION as _LEGACY_VERSION
from pipeline.annotator                import draw_tracked_detections
from pipeline.tracker                  import Tracker, TrackedDetection, stitch_track_fragments
from pipeline.heuristics               import WheelieDetector, ErraticDrivingDetector
from pipeline.vehicle_state            import VehicleStateRegistry, VehicleState
from pipeline.vehicle_class_aggregator import VehicleClassAggregator
from pipeline.vehicle_class_gate       import is_two_wheeler
from pipeline.keyframes                import KeyframeStore, frame_score
from pipeline.identity                 import (ReadLike, TrackSummary, resolve_identity, choose_subject, canonical)
from pipeline.contract                 import (VEHICLE_CLASSES, VIOLATION_POLICY, ResultPackage, Finding, EvidenceItem,
                                               PlateObservation, Allegation, auto_tier, finding_id, severity_for,
                                               validate_package, CONTRACT_VERSION)

PIPELINE_VERSION = "3.0.0"
FINDING_MATCH_IOU = 0.50
EVIDENCE_PER_FINDING = 3
MAX_VLM_CALLS = 3
TRACK_MIN_HITS = 3
TRACK_MIN_SPAN_S = 0.5
LOOKALIKE_DIST_FACTOR = 0.75     # centroid distance in box widths; jam neighbours are NOT look-alikes
VIDEO_MAX_WIDTH = 1280
BOLD, RESET = "\033[1m", "\033[0m"


def _stage(n: int, msg: str):
    print(f"\n  [{n}/8] {msg}")


# ── helpers ──────────────────────────────────────────────────────────────────

def match_finding_to_track(bbox: List[float], vehicle_class: str, tracked_in_frame: list) -> int:
    """Attribute a rule-engine finding to exactly one tracked vehicle (same wheel class, best IoU)."""
    best_tid, best_iou = -1, FINDING_MATCH_IOU
    want_two = is_two_wheeler(vehicle_class)
    for det in tracked_in_frame:
        if det.track_id < 0 or det.class_name not in VEHICLE_CLASSES:
            continue
        if is_two_wheeler(det.class_name) != want_two:
            continue
        iou = _iou(bbox, det.bbox)
        if iou > best_iou:
            best_tid, best_iou = det.track_id, iou
    return best_tid


def pick_subject(states: List[VehicleState]) -> Optional[VehicleState]:
    """Legacy helper (tests): the flagged vehicle with the most serious, most confident finding."""
    flagged = [s for s in states if s.flagged_violations()]
    if not flagged:
        return None
    return max(flagged, key=lambda s: (bool(s.confirmed_violations()), severity_for(s.flagged_violations()),
                                       s.detection_confidence(), s.frames_observed))


def clip_status_from_states(states: List[VehicleState]) -> tuple[str, List[str]]:
    confirmed = sorted({v for s in states for v in s.confirmed_violations()})
    review    = sorted({v for s in states for v in s.review_violations()})
    if confirmed:
        return "auto_flagged", sorted(set(confirmed) | set(review))
    if review:
        return "needs_review", review
    return "insufficient_evidence", []


def answer_allegation(subject: Optional[VehicleState], declared: Optional[str], hint: str) -> Tuple[str, str]:
    """Answer the uploader's allegation from the SUBJECT's verdict only."""
    if hint == "not_declared" or not declared:
        return "not_declared", "No violation was declared by the uploader."
    if hint == "declared_vehicle_not_seen":
        return "not_supported", "No vehicle of the declared type was found in the clip."
    if hint == "ambiguous_subject":
        return "ambiguous_subject", "Several vehicles of the declared type are equally prominent; a reviewer must pick."
    if subject is None:
        return "unobservable", "No subject vehicle."
    pol = VIOLATION_POLICY.get(declared)
    if pol is None or not pol["enabled"]:
        return "manual_review", f"'{declared}' cannot be evaluated by the AI yet; a reviewer decides from the footage."
    vv = subject.violation_verdicts.get(declared)
    if vv is None:
        return "unobservable", "The declared violation was never checked on the subject."
    if vv.result in ("confirmed", "needs_review"):
        return "supported", vv.reasoning
    if vv.result == "observed_absent":
        return "not_supported", vv.reasoning
    return "unobservable", vv.reasoning


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _blur(img: np.ndarray, box: List[float], k: int = 31) -> None:
    h, w = img.shape[:2]
    x1, y1 = max(0, int(box[0])), max(0, int(box[1]))
    x2, y2 = min(w, int(box[2])), min(h, int(box[3]))
    if x2 - x1 < 2 or y2 - y1 < 2:
        return
    roi = img[y1:y2, x1:x2]
    kk = max(7, (min(roi.shape[:2]) // 3) | 1)
    img[y1:y2, x1:x2] = cv2.GaussianBlur(roi, (kk, kk), 0)


def _scaled(box: List[float], s: float) -> List[float]:
    return [v * s for v in box]


# ── main ─────────────────────────────────────────────────────────────────────

def run(
    source: str,
    interval: Optional[float] = None,          # legacy: seconds between frames; None = dense (target_fps)
    output_dir: Path = Path("pipeline/evidence_output/latest"),
    use_tracker: bool = True,
    use_vlm: bool = False,
    vehicle_type: str = "two_wheeler",
    declared_violation: Optional[str] = None,
    claimed_plate: Optional[str] = None,
    submission_id: Optional[str] = None,
    run_id: Optional[str] = None,
    tracker: str = "botsort",
    imgsz: int = DEFAULT_IMGSZ,
    target_fps: float = 15.0,
    roi_every: int = 1,
    render_video: bool = True,
    max_frames: Optional[int] = None,
) -> Optional[dict]:
    t0 = time.time()
    started_at = datetime.now(timezone.utc).isoformat()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = run_id or uuid.uuid4().hex[:12]
    claim = canonical(claimed_plate) or None
    timing: Dict[str, float] = {}

    print(f"\n{BOLD}{'-'*60}\n  RoadWatch pipeline v{PIPELINE_VERSION}\n{'-'*60}{RESET}")
    print(f"  Source: {source}\n  Declared: {vehicle_type} / {declared_violation or '-'} / plate {claim or '-'}\n"
          f"  Tracker: {tracker} @ imgsz {imgsz}, VLM {'on' if use_vlm else 'off'}\n  Output: {output_dir}/")

    # ── 0. probe ─────────────────────────────────────────────────────────────
    _stage(0, "Probe + stream")
    is_image = Path(source).suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    if is_image:
        fps, stride, duration = 1.0, 1, 0.0
    else:
        info = probe_video(source)
        fps = info.fps or 30.0
        stride = max(1, int(round(interval * fps))) if interval else default_stride(fps, target_fps)
        duration = info.duration
    eff_fps = fps / stride
    print(f"    fps={fps:.2f} stride={stride} → {eff_fps:.1f} processed fps, duration {duration:.1f}s")

    # ── state ────────────────────────────────────────────────────────────────
    registry = VehicleStateRegistry()
    class_agg = VehicleClassAggregator()
    kf_store = KeyframeStore()
    aggregator = PlateAggregator()
    wd, ed = WheelieDetector(), ErraticDrivingDetector()
    track_results: Dict[float, List[TrackedDetection]] = {}
    frames_meta: Dict[int, dict] = {}          # seq index -> {ts, src_index, tracked, plates, persons}
    frame_verdicts = []
    dominance: Dict[int, float] = {}
    hits: Dict[int, List[float]] = {}
    lookalike_pairs: Dict[Tuple[int, int], bool] = {}
    frame_shape = None
    n_attributed = n_unmatched = 0
    n_frames = 0

    bot = None
    legacy = None
    if tracker == "botsort" and not is_image:
        from pipeline.tracker_botsort import BotSortTracker
        bot = BotSortTracker(imgsz=imgsz, frame_rate=eff_fps)
    else:
        legacy = Tracker()

    t_loop = time.time()
    for frame in iter_frames(source, stride=stride, max_frames=max_frames):
        i, ts, img = n_frames, frame.timestamp, frame.image
        n_frames += 1
        frame_shape = img.shape
        H, W = img.shape[:2]

        # 1. vehicles + persons: detect ONCE, then track per class group
        dets = detect_vehicles(img, imgsz=imgsz)
        if bot is not None:
            tracked = bot.update(img, first=(i == 0), detections=dets)
        else:
            tracked = legacy.update(FrameDetections(timestamp=ts, detections=dets))
        vehicles = [Detection(d.class_name, d.confidence, list(d.bbox)) for d in tracked if d.class_name in VEHICLE_CLASSES]
        persons  = [Detection(d.class_name, d.confidence, list(d.bbox)) for d in tracked if d.class_name == "person"]

        # 2. small objects on zoomed crops — each tagged with the vehicle that produced it
        owned = detect_rois_owned(img, vehicles, persons) if (i % roi_every == 0) else []
        rois = [d for _, d in owned]
        veh_track = [d.track_id for d in tracked if d.class_name in VEHICLE_CLASSES]
        plates_owned = [(veh_track[o] if o is not None and o < len(veh_track) else -1, d)
                        for o, d in owned if d.class_name == "license_plate"]

        # 3. rules → per-vehicle findings
        fd = FrameDetections(timestamp=ts, detections=[Detection(d.class_name, d.confidence, list(d.bbox)) for d in tracked] + rois)
        fv = apply_rules(fd, frame_bgr=img, wheelie_detector=wd, erratic_detector=ed, tracked_dets=tracked)
        frame_verdicts.append(fv)
        track_results[ts] = tracked
        frames_meta[i] = {"ts": ts, "src_index": frame.index, "tracked": tracked,
                          "plates": [(list(d.bbox), owner) for owner, d in plates_owned],
                          # every plate box seen this frame, including ones we could not attribute
                          "all_plates": [list(d.bbox) for d in rois if d.class_name == "license_plate"],
                          "vehicles": [(list(d.bbox), d.track_id) for d in tracked if d.class_name in VEHICLE_CLASSES],
                          "persons": [list(p.bbox) for p in persons]}

        enc = kf_store.encoder(img)
        vt = [d for d in tracked if d.track_id >= 0 and d.class_name in VEHICLE_CLASSES]
        for det in vt:
            tid = det.track_id
            if tid not in registry:
                registry.get_or_create(tid)
            registry.add_observation(tid, frame_index=i, timestamp=ts, source="detector", key="present", value=True,
                                     confidence=det.confidence)
            class_agg.add_vote(track_id=tid, vehicle_class=det.class_name, confidence=det.confidence, frame_index=i)
            hits.setdefault(tid, []).append(ts)
            x1, y1, x2, y2 = det.bbox
            area_frac = max(0.0, x2 - x1) * max(0.0, y2 - y1) / float(W * H)
            cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            weight = 1.0 + 0.5 * (1.0 - abs(cx - W / 2.0) / (W / 2.0)) + 0.5 * (cy / H)
            dominance[tid] = dominance.get(tid, 0.0) + area_frac * weight
            crop = img[max(0, int(y1)):min(H, int(y2)), max(0, int(x1)):min(W, int(x2))]
            kf_store.consider(tid, i, ts, det.bbox, frame_score(det.bbox, img.shape, crop), enc)
        # look-alikes: same wheel class, close together in this frame
        for a in range(len(vt)):
            for b in range(a + 1, len(vt)):
                da, db = vt[a], vt[b]
                if is_two_wheeler(da.class_name) != is_two_wheeler(db.class_name):
                    continue
                wa = max(1.0, da.bbox[2] - da.bbox[0]); wb = max(1.0, db.bbox[2] - db.bbox[0])
                if not (0.7 <= wa / wb <= 1.4):
                    continue
                dx = (da.bbox[0] + da.bbox[2]) / 2 - (db.bbox[0] + db.bbox[2]) / 2
                dy = (da.bbox[1] + da.bbox[3]) / 2 - (db.bbox[1] + db.bbox[3]) / 2
                if (dx * dx + dy * dy) ** 0.5 < LOOKALIKE_DIST_FACTOR * max(wa, wb):
                    lookalike_pairs[(min(da.track_id, db.track_id), max(da.track_id, db.track_id))] = True

        for f in fv.findings:
            tid = match_finding_to_track(f.bbox, f.vehicle_class, tracked)
            if tid < 0:
                n_unmatched += 1
                continue
            n_attributed += 1
            for v in f.violations:
                registry.add_observation(tid, frame_index=i, timestamp=ts, source="rules", key=v, value=True, confidence=f.confidence)
                kf_store.consider(tid, i, ts, f.bbox, 1.0 + f.confidence, enc)          # positives are evidence
            for v in f.observed_absent:
                registry.add_observation(tid, frame_index=i, timestamp=ts, source="rules", key=v, value=False, confidence=f.confidence)
                if v == declared_violation:
                    kf_store.consider(tid, i, ts, f.bbox, 0.6 + f.confidence * 0.3, enc)   # counter-evidence for the allegation
            for v in f.unobservable:
                registry.add_observation(tid, frame_index=i, timestamp=ts, source="rules", key=v, value=None, confidence=f.confidence)
        for tid in fv.erratic_track_ids:
            if tid in registry:
                registry.add_observation(tid, frame_index=i, timestamp=ts, source="heuristics", key="erratic_driving", value=True, confidence=0.75)

        # 4. plate candidates — ownership comes from the crop the plate was found in.
        #    No overlap matching and no guessed plate regions: in dense traffic both
        #    attach one vehicle's plate to its neighbours.
        for owner_tid, p in sorted(plates_owned, key=lambda op: op[1].confidence, reverse=True):
            if owner_tid >= 0:
                aggregator.add_candidate(track_id=owner_tid, frame_bgr=img, plate_bbox=p.bbox, timestamp=ts,
                                         frame_index=i, detector_confidence=p.confidence)
        if i % 25 == 0:
            print(f"    frame {i} t={ts:.1f}s tracks={len(registry)} keyframes={len(kf_store)}", flush=True)
    timing["stream_detect_track_rules"] = round(time.time() - t_loop, 2)
    if n_frames == 0:
        print("  ERROR: no frames decoded", file=sys.stderr)
        return None
    if is_image or duration <= 0:
        duration = max(m["ts"] for m in frames_meta.values())
    print(f"    {n_frames} frames, findings attributed {n_attributed} (dropped {n_unmatched}), tracks {len(registry)}")

    # ── canonical merge helper ───────────────────────────────────────────────
    def merge_track_ids(primary: int, fragment: int) -> None:
        aggregator.merge_tracks(primary, fragment)
        registry.merge_tracks(primary, fragment)
        kf_store.merge(primary, fragment)
        hits.setdefault(primary, []).extend(hits.pop(fragment, []))
        dominance[primary] = dominance.get(primary, 0.0) + dominance.pop(fragment, 0.0)
        for ts_tracked in track_results.values():
            for det in ts_tracked:
                if det.track_id == fragment:
                    det.track_id = primary

    # ── 5. stitch fragments, life gate, classes ──────────────────────────────
    _stage(5, "Identity: stitch, life gate, classes, plates")
    t5 = time.time()
    class_name_for_id: Dict[int, str] = {}
    for ts_tracked in track_results.values():
        for det in ts_tracked:
            if det.track_id >= 0 and det.track_id not in class_name_for_id:
                class_name_for_id[det.track_id] = det.class_name
    id_map = stitch_track_fragments(track_results, class_name_for_id)
    for old, new in sorted(id_map.items()):
        if old != new:
            merge_track_ids(new, old)

    dropped = 0
    for tid in list(registry._states):
        tss = hits.get(tid, [])
        if len(tss) < TRACK_MIN_HITS or (max(tss) - min(tss)) < TRACK_MIN_SPAN_S:
            registry._states.pop(tid, None); kf_store.drop(tid); dominance.pop(tid, None); dropped += 1
    print(f"    stitched {sum(1 for o, n in id_map.items() if o != n)}, dropped {dropped} flicker track(s), {len(registry)} vehicles remain")

    for tid, cr in class_agg.resolve_all().items():
        if tid in registry:
            registry.set_vehicle_class(track_id=tid, vehicle_class=cr.resolved_class, confidence=cr.agreement, is_stable=cr.is_stable)

    # OCR on the best candidates per track
    for tid, raw in aggregator.scan_pending_candidates(use_vlm=use_vlm, per_track_limit=3,
                                                       only_tracks_needing_review=True,
                                                       max_total_candidates=max(12, 3 * len(registry))):
        pass
    resolutions = aggregator.resolve_all()
    plate_crop_map = aggregator.save_zoomed_crops(output_dir, per_track_limit=3)
    aggregator.write_learning_log(output_dir, resolutions, vehicle_track_labels={t: s.vehicle_class for t, s in registry._states.items()})

    lookalike = {tid: False for tid in registry._states}
    for (a, b), v in lookalike_pairs.items():
        a, b = id_map.get(a, a), id_map.get(b, b)
        if a != b and v:
            lookalike[a] = True; lookalike[b] = True
    plate_observations: List[dict] = []
    identities = {}
    crop_by_frame = {tid: {int(c["frame_index"]): c["path"] for c in crops}
                     for tid, crops in (plate_crop_map or {}).items()}
    for tid, state in registry._states.items():
        reads = aggregator._reads.get(tid, [])
        for r in reads:
            plate_observations.append(PlateObservation(tid, r.text, r.tier, round(float(r.confidence), 4), r.frame_index,
                                                       round(float(r.timestamp), 3),
                                                       crop_by_frame.get(tid, {}).get(int(r.frame_index)),
                                                       bool(r.is_valid)).to_dict())
        ident = resolve_identity([ReadLike(r.text, float(r.confidence), r.frame_index, bool(r.is_valid)) for r in reads],
                                 claimed_plate, lookalike.get(tid, False))
        identities[tid] = ident
        state.identity_status = ident.status
        registry.set_plate(tid, ident.plate, ident.confidence, needs_review=(ident.status != "resolved"),
                           raw_reads=[r.text for r in reads if r.text], method=ident.method)
    # same resolved plate on two NON-overlapping tracks (gap <= 5 s) → one vehicle
    by_plate: Dict[str, List[int]] = {}
    for tid, ident in identities.items():
        if ident.status == "resolved" and ident.plate:
            by_plate.setdefault(ident.plate, []).append(tid)
    for plate, tids in by_plate.items():
        tids = sorted(tids, key=lambda t: min(hits[t]))
        for a, b in zip(tids, tids[1:]):
            if a in registry and b in registry and min(hits[b]) > max(hits[a]) and min(hits[b]) - max(hits[a]) <= 5.0:
                print(f"    Plate merge: track {b} -> {a} ({plate})")
                merge_track_ids(a, b); identities.pop(b, None)
    n_resolved = sum(1 for i_ in identities.values() if i_.status == "resolved")
    print(f"    plates: {n_resolved} resolved / {len(registry)} vehicles; reads {len(plate_observations)}")
    timing["identity_ocr"] = round(time.time() - t5, 2)

    # ── 6. verdicts, subject, allegation ─────────────────────────────────────
    _stage(6, "Verdicts + subject + allegation")
    registry.resolve_all()
    summaries = [TrackSummary(tid, s.vehicle_class, dominance.get(tid, 0.0), (max(hits[tid]) - min(hits[tid])) if hits.get(tid) else 0.0)
                 for tid, s in registry._states.items()]
    choice = choose_subject(summaries, identities, vehicle_type if declared_violation or claimed_plate else None, claimed_plate)
    subject = registry.get(choice.track_id) if choice.track_id is not None else None
    if subject is not None:
        subject.is_subject = True
    answer, answer_reason = answer_allegation(subject, declared_violation, choice.answer_hint)
    print(f"    subject: {choice.track_id} via {choice.method} ({choice.reason})\n    allegation '{declared_violation}': {answer} — {answer_reason[:120]}")

    vr = aggregate_verdicts(frame_verdicts, ocr_agreement_ratio=0.0, top_n_evidence_frames=3, total_track_ids=len(class_name_for_id))
    evidence_strength = vr.severity_score
    vr.status, vr.violations_detected = clip_status_from_states(registry.all_states())
    vr.severity_score = severity_for(vr.violations_detected)

    # ── 7. tiebreaker: one question per finding, capped ──────────────────────
    vlm_calls: List[dict] = []
    from pipeline.vlm import check_vlm_available
    vlm_active = use_vlm and check_vlm_available()
    _stage(7, "Tiebreaker " + ("(Gemini → Nemotron)" if vlm_active else "(skipped)"))
    if vlm_active and subject is not None and subject.review_violations():
        from pipeline.vlm import vlm_tiebreaker
        for v in subject.review_violations()[:MAX_VLM_CALLS]:
            pos = subject.positive_frames(v)
            kf = kf_store.by_index(subject.track_id, pos[0].frame_index) if pos else None
            if kf is None:
                continue
            img = kf.decode()
            summary = {"track_id": subject.track_id, "violations_detected": [v], "vehicle_type": subject.vehicle_class,
                       "plate": subject.plate_text or "unreadable", "frame_consistency": vr.frame_consistency_ratio}
            call = {"id": uuid.uuid4().hex[:8], "track_id": subject.track_id, "violation": v, "frame_index": kf.frame_index,
                    "timestamp": kf.timestamp, "input": summary,
                    "model": os.environ.get("GEMINI_VLM_MODEL") or os.environ.get("NVIDIA_NIM_MODEL") or "unknown",
                    "output_status": None, "reasoning": "", "error": ""}
            try:
                new_status, reasoning = vlm_tiebreaker(img, summary, "needs_review")
                call["output_status"], call["reasoning"] = new_status, reasoning
                value = True if new_status == "auto_flagged" else (False if new_status == "insufficient_evidence" else None)
                if value is not None:
                    registry.add_observation(subject.track_id, frame_index=kf.frame_index, timestamp=kf.timestamp,
                                             source="vlm", key=v, value=value, confidence=0.9)
                print(f"    VLM {v}: {new_status} — {reasoning[:100]}")
            except Exception as exc:
                call["error"] = f"{type(exc).__name__}: {exc}"
                print(f"    VLM {v}: error, verdict unchanged ({exc})")
            vlm_calls.append(call)
        subject.resolve()
        answer, answer_reason = answer_allegation(subject, declared_violation, choice.answer_hint)
        vr.status, vr.violations_detected = clip_status_from_states(registry.all_states())
        vr.severity_score = severity_for(vr.violations_detected)

    # ── 8. findings, evidence, video, package ────────────────────────────────
    _stage(8, "Findings, evidence, detection video, package")
    t8 = time.time()
    findings: List[dict] = []
    evidence: List[dict] = []
    vlm_by_v = {c["violation"]: c["id"] for c in vlm_calls}
    for state in registry.all_states():
        tid = state.track_id
        for v, vv in state.violation_verdicts.items():
            is_declared = state.is_subject and v == declared_violation
            if vv.result == "not_evaluated" and not is_declared:
                continue
            contradicts = any(c["violation"] == v and c["output_status"] == "insufficient_evidence" for c in vlm_calls if c["track_id"] == tid)
            tier = auto_tier(vv.result, vv.evidence_frames, vv.agreement, vv.confidence, state.identity_status, contradicts)
            if is_declared and tier == "C":
                tier = "B"                                  # the allegation always reaches a human
            fid = finding_id(run_id, tid, v)
            paths: List[str] = []
            want_positive = vv.result in ("confirmed", "needs_review")
            obs = state.positive_frames(v) if want_positive else \
                  [o for o in state.observations if o.key == v and o.value is False]
            seen = set()
            for o in sorted(obs, key=lambda o: o.confidence, reverse=True):
                if o.frame_index in seen:
                    continue
                kf = kf_store.by_index(tid, o.frame_index)
                if kf is None:
                    continue
                seen.add(o.frame_index)
                img = kf.decode()
                s = kf_store.scale(frame_shape)
                meta = frames_meta[o.frame_index]
                boxes = [TrackedDetection(d.class_name, d.confidence, _scaled(d.bbox, s), d.track_id) for d in meta["tracked"]]
                img = draw_tracked_detections(img, boxes, None, plate_by_track={tid: state.plate_text} if state.plate_text else None)
                for d in boxes:
                    if d.track_id == tid:
                        x1, y1, x2, y2 = (int(q) for q in d.bbox)
                        cv2.rectangle(img, (x1, y1), (x2, y2), (38, 74, 255), 4)
                        cv2.putText(img, f"ID {tid} {v}: {vv.result}", (x1, max(18, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (38, 74, 255), 2)
                rel = f"tracks/{tid}/{v}/t{kf.timestamp:.3f}s.jpg"
                out = output_dir / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                if cv2.imwrite(str(out), img):
                    paths.append(rel)
                    evidence.append(EvidenceItem(fid, tid, kf.frame_index, kf.timestamp, rel, _sha256(out)).to_dict())
                if len(paths) >= EVIDENCE_PER_FINDING:
                    break
            state.evidence.extend(p for p in paths if p not in state.evidence)
            findings.append(Finding(fid, tid, v, vv.result, tier, vv.evidence_frames, vv.evaluable_frames, vv.agreement,
                                    vv.confidence, vv.reasoning, paths, vlm_by_v.get(v) if state.is_subject else None).to_dict())
            vv_dict = state.violation_verdicts[v]
    vehicle_tracks = registry.export_report()
    by_track: Dict[int, dict] = {r["track_id"]: r for r in vehicle_tracks}
    for f in findings:                              # tier per verdict for the reviewer UI
        rec = by_track.get(f["track_id"])
        if rec is None:
            continue
        v = f["violation"]
        if v in rec["verdicts"]:
            rec["verdicts"][v]["tier"] = f["tier"]
        else:
            # the declared violation is emitted even when the verdict was dropped from the
            # record as not_evaluated; the reviewer still needs to see it on the case.
            rec["verdicts"][v] = {"result": f["result"], "evidence_frames": f["evidence_frames"],
                                  "evaluable_frames": f["evaluable_frames"], "agreement": f["agreement"],
                                  "confidence": f["confidence"], "reasoning": f["reasoning"], "tier": f["tier"]}
    n_flagged = sum(1 for f in findings if f["tier"] in ("A", "B"))
    print(f"    findings {len(findings)} (queued {n_flagged}), evidence files {len(evidence)}")

    # detection video: faces + non-subject plates blurred; subject plate readable only if claimed AND resolved
    video_rel = None
    if render_video and not is_image and frame_shape is not None:
        t_v = time.time()
        subject_tid = subject.track_id if subject is not None else None
        show_subject_plate = bool(subject is not None and claim and identities.get(subject_tid) and
                                  identities[subject_tid].status == "resolved" and identities[subject_tid].claim_match == "exact")
        s = min(1.0, VIDEO_MAX_WIDTH / frame_shape[1])
        out_w, out_h = int(frame_shape[1] * s), int(frame_shape[0] * s)
        video_rel = "detection.mp4"
        writer = cv2.VideoWriter(str(output_dir / video_rel), cv2.VideoWriter_fourcc(*"mp4v"), max(1.0, eff_fps), (out_w, out_h))
        subj_pos = {o.frame_index for o in (subject.positive_frames() if subject is not None else [])}
        for frame in iter_frames(source, stride=stride, max_frames=max_frames):
            j = frame.index // stride
            meta = frames_meta.get(j)
            img = cv2.resize(frame.image, (out_w, out_h), interpolation=cv2.INTER_AREA) if s < 1.0 else frame.image.copy()
            if meta:
                # Faces: the top of every person box, plus the windscreen band of every
                # four-wheeler (occupants seen through glass are rarely emitted as person boxes).
                for pb in meta["persons"]:
                    x1, y1, x2, y2 = _scaled(pb, s)
                    _blur(img, [x1, y1, x2, y1 + (y2 - y1) * 0.35])
                for vb, vtid in meta.get("vehicles", []):
                    x1, y1, x2, y2 = _scaled(vb, s)
                    h = y2 - y1
                    if not is_two_wheeler(next((d.class_name for d in meta["tracked"] if d.track_id == vtid), "car")):
                        _blur(img, [x1, y1 + h * 0.08, x2, y1 + h * 0.45])    # windscreen band
                    # Plates: blur the region where a plate sits on EVERY vehicle we are not
                    # allowed to show, whether or not the detector found one. Missed plates,
                    # vehicles too small for the ROI pass and plates outside the box would
                    # otherwise leak; over-blurring a stranger's vehicle is the safe direction.
                    if not (show_subject_plate and vtid == subject_tid):
                        _blur(img, [x1, y1 + h * 0.62, x2, y2])
                for plate in meta.get("all_plates", []):
                    box = _scaled(plate, s)
                    owner = next((o for b, o in meta["plates"] if b == plate), -1)
                    if not (show_subject_plate and owner == subject_tid):
                        _blur(img, box)
                boxes = [TrackedDetection(d.class_name, d.confidence, _scaled(d.bbox, s), d.track_id) for d in meta["tracked"]
                         if d.class_name not in ("license_plate",)]
                img = draw_tracked_detections(img, boxes, None,
                                              plate_by_track={subject_tid: subject.plate_text} if show_subject_plate and subject.plate_text else None)
                if subject_tid is not None:
                    for d in boxes:
                        if d.track_id == subject_tid:
                            x1, y1, x2, y2 = (int(q) for q in d.bbox)
                            cv2.rectangle(img, (x1, y1), (x2, y2), (38, 74, 255), 3)
                            if j in subj_pos:
                                cv2.putText(img, "violation observed here", (x1, min(out_h - 10, y2 + 22)),
                                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (38, 74, 255), 2)
            hud = [f"RoadWatch AI  run {run_id}", f"Allegation: {declared_violation or '-'} -> {answer}",
                   f"Subject: {'track ' + str(subject_tid) if subject_tid is not None else 'none'}   t={meta['ts'] if meta else 0:.1f}s",
                   "Advisory only. A reviewer decides."]
            y = 24
            for line in hud:
                cv2.putText(img, line, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3)
                cv2.putText(img, line, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
                y += 22
            writer.write(img)
        writer.release()
        timing["detection_video"] = round(time.time() - t_v, 2)
        print(f"    detection video: {video_rel} ({timing['detection_video']}s)")

    # package
    finished_at = datetime.now(timezone.utc).isoformat()
    timing["findings_evidence"] = round(time.time() - t8 - timing.get("detection_video", 0.0), 2)
    timing["total"] = round(time.time() - t0, 2)
    summary = {
        "total_vehicles_tracked": len(vehicle_tracks),
        "vehicles_with_violations": sum(1 for r in vehicle_tracks if r["needs_review"]),
        "vehicles_confirmed": sum(1 for r in vehicle_tracks if r["has_violation"]),
        "vehicles_clean": sum(1 for r in vehicle_tracks if not r["needs_review"]),
        "findings_queued": n_flagged,
        "status": vr.status, "violations_detected": vr.violations_detected,
        "severity_score": vr.severity_score, "evidence_strength": round(evidence_strength, 4),
        "frames_processed": n_frames, "processed_fps": round(eff_fps, 2), "imgsz": imgsz, "tracker": tracker,
        "timing_seconds": timing,
    }
    package = ResultPackage(
        run_id=run_id, submission_id=submission_id, pipeline_version=PIPELINE_VERSION, model_versions=model_versions(),
        started_at=started_at, finished_at=finished_at, duration_seconds=round(float(duration), 3),
        allegation=Allegation(declared_violation, claim, vehicle_type, choice.track_id, answer, answer_reason).to_dict()
                   | {"subject_method": choice.method, "subject_candidates": choice.candidates},
        vehicle_tracks=vehicle_tracks, plate_observations=plate_observations, findings=findings, evidence=evidence,
        vlm_calls=vlm_calls, summary=summary, detection_video=video_rel,
    ).to_dict()
    validate_package(package)                                    # hard invariants
    (output_dir / "package.json").write_text(json.dumps(package, indent=2, default=str), encoding="utf-8")
    report = {**package, "vehicles_v2": vehicle_tracks, "status": vr.status, "violations_detected": vr.violations_detected,
              "severity_score": vr.severity_score, "evidence_strength": evidence_strength, "subject_track_id": choice.track_id,
              "meta": {"frames_analysed": n_frames, "duration_seconds": round(float(duration), 3),
                       "processing_time_seconds": timing["total"]},
              "plate_crops": plate_crop_map, "output_dir": str(output_dir)}
    (output_dir / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print(f"\n{BOLD}  DONE{RESET}  {vr.status.upper()}  vehicles {len(vehicle_tracks)}  queued findings {n_flagged}  "
          f"subject {choice.track_id}  answer {answer}  total {timing['total']}s\n  package: {output_dir / 'package.json'}\n")
    return report


def main():
    p = argparse.ArgumentParser(description="RoadWatch detection pipeline v3")
    p.add_argument("source")
    p.add_argument("--out", default=None)
    p.add_argument("--interval", type=float, default=None, help="legacy: seconds between frames (default: dense at --target-fps)")
    p.add_argument("--target-fps", type=float, default=15.0)
    p.add_argument("--imgsz", type=int, default=DEFAULT_IMGSZ)
    p.add_argument("--tracker", choices=["botsort", "bytetrack"], default="botsort")
    p.add_argument("--vlm", action="store_true", help="enable the external tiebreaker (single switch)")
    p.add_argument("--vehicle-type", choices=["two_wheeler", "four_wheeler"], default="two_wheeler")
    p.add_argument("--declared", default=None, help="violation the uploader reported")
    p.add_argument("--plate", default=None, help="plate the uploader typed (a claim, never truth)")
    p.add_argument("--roi-every", type=int, default=1)
    p.add_argument("--no-video", action="store_true")
    p.add_argument("--max-frames", type=int, default=None)
    p.add_argument("--track", action="store_true", help="(legacy flag, tracking is always on)")
    a = p.parse_args()
    if not os.path.isfile(a.source):
        sys.exit(f"ERROR: file not found: {a.source}")
    out = Path(a.out) if a.out else Path("pipeline/evidence_output/latest")
    if not a.out and out.exists():
        import shutil; shutil.rmtree(out)
    run(a.source, interval=a.interval, output_dir=out, use_vlm=a.vlm, vehicle_type=a.vehicle_type,
        declared_violation=a.declared, claimed_plate=a.plate, tracker=a.tracker, imgsz=a.imgsz,
        target_fps=a.target_fps, roi_every=a.roi_every, render_video=not a.no_video, max_frames=a.max_frames)


if __name__ == "__main__":
    main()
