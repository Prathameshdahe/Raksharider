"""
run_pipeline.py
---------------
CLI entry point for the DriveTrust AI detection pipeline — v2.

Usage:
    python run_pipeline.py <video_or_image> [options]

Examples:
    # Basic run (two-wheeler, with tracking)
    python run_pipeline.py tests/sample_videos/sample.mp4 --track

    # With VLM tiebreaker and explicit vehicle type
    python run_pipeline.py clip.mp4 --track --vlm --vehicle-type two_wheeler

    # Four-wheeler (front-cam seatbelt check stub)
    python run_pipeline.py dash_clip.mp4 --vehicle-type four_wheeler --track --vlm

Stages (v2):
    1. Frame extraction         sample at --interval seconds
    2. YOLO detection           4 models: COCO + helmet + plate + vehicle-class
    3. ByteTrack (--track)      persistent vehicle IDs
    4. Rule engine + heuristics rider count, helmet, phone, wheelie, erratic, signal
    5. OCR                      plate crop → EasyOCR → Indian format validator
    6. Aggregation              clip-level status + severity
    7. VLM tiebreaker (--vlm)   Gemini Vision — fires only on needs_review
    8. Report                   report.json + track_log.json + evidence JPEGs
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import cv2

# Load .env file before any pipeline imports that might read env vars
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent / ".env")
except ImportError:
    pass   # dotenv optional — key can be set via shell env instead

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

from pipeline.frame_extractor         import extract_frames
from pipeline.detector                import detect_frames
from pipeline.rules                   import apply_rules_to_all
from pipeline.ocr                     import read_plate, majority_vote_plate
from pipeline.plate_aggregator        import PlateAggregator, find_nearest_track_id
from pipeline.verification            import aggregate_verdicts, resolve_dominant_vehicle_type
from pipeline.report                  import build_report, report_to_json
from pipeline.annotator               import draw_detections, draw_verdict_overlay, draw_tracked_detections
from pipeline.tracker                 import Tracker, stitch_track_fragments
from pipeline.heuristics              import WheelieDetector, ErraticDrivingDetector
from pipeline.vehicle_state           import VehicleStateRegistry
from pipeline.vehicle_class_aggregator import VehicleClassAggregator
from pipeline.track_merger            import TrackMerger
from pipeline.indian_plate_validator  import is_real_state_code, best_plate_candidate
from pipeline.vehicle_class_gate      import is_two_wheeler

STATUS_COLOURS = {
    "auto_flagged":          "\033[91m",
    "needs_review":          "\033[93m",
    "insufficient_evidence": "\033[92m",
}
RESET = "\033[0m"
BOLD  = "\033[1m"
STAGES = 8   # total stages in v2


def _banner(msg: str):
    print(f"\n{BOLD}{'-'*60}{RESET}")
    print(f"{BOLD}  {msg}{RESET}")
    print(f"{BOLD}{'-'*60}{RESET}")


def _stage(n: int, msg: str):
    print(f"\n  [{n}/{STAGES}] {msg}")


def run(
    source:           str,
    interval:         float,
    output_dir:       Path,
    use_tracker:      bool = True,
    use_vlm:          bool = False,
    vehicle_type:     str  = "two_wheeler",
):
    t_start = time.time()

    _banner("DriveTrust AI -- Violation Detection Pipeline v2")
    print(f"  Source       : {source}")
    print(f"  Interval     : {interval}s between sampled frames")
    print(f"  Vehicle type : {vehicle_type}")
    print(f"  Tracking     : {'enabled (ByteTrack)' if use_tracker else 'disabled'}")
    print(f"  VLM          : {'enabled (Gemini Vision)' if use_vlm else 'disabled'}")
    print(f"  Output       : {output_dir}/")

    # ── Stage 1: Frame extraction ─────────────────────────────────────────────
    _stage(1, "Frame Extraction")
    frames = extract_frames(source, sample_interval=interval)
    print(f"    {len(frames)} frame(s) extracted")
    if not frames:
        print("  ERROR: No frames extracted. Check the source file.", file=sys.stderr)
        return None

    # ── Stage 2: Detection (all 4 models) ────────────────────────────────────
    _stage(2, "YOLO Detection (COCO | helmet | plate | vehicle-class)")
    frame_detections = detect_frames(frames)
    class_counts: dict = {}
    for fd in frame_detections:
        for d in fd.detections:
            class_counts[d.class_name] = class_counts.get(d.class_name, 0) + 1
    total_dets = sum(class_counts.values())
    print(f"    {total_dets} detections across {len(frames)} frames")
    for cls, cnt in sorted(class_counts.items()):
        print(f"      {cls}: {cnt}")

    # ── Stage 3: ByteTrack ────────────────────────────────────────────────────
    _stage(3, "ByteTrack Tracking" if use_tracker else "ByteTrack (skipped)")
    track_results: dict[float, list] = {}
    tracker        = Tracker()
    total_track_ids = 0
    track_history: dict = {}

    # Shared per-vehicle state registry — all stages write observations here.
    registry = VehicleStateRegistry()

    # Accumulate-then-vote vehicle class aggregator (fixes Bug 2.4).
    class_agg = VehicleClassAggregator()

    if use_tracker:
        for i_frame, fd in enumerate(frame_detections):
            tracked = tracker.update(fd)
            track_results[fd.timestamp] = tracked

            # Feed per-frame class votes into the aggregator
            for det in tracked:
                if det.track_id >= 0 and det.class_name:
                    class_agg.add_vote(
                        track_id=det.track_id,
                        vehicle_class=det.class_name,
                        confidence=det.confidence,
                        frame_index=i_frame,
                    )

        total_track_ids = tracker._next_id - 1
        print(f"    Total unique IDs assigned: {total_track_ids}")

        # Build class_name_for_id mapping for the stitcher
        class_name_for_id: dict[int, str] = {}
        for ts_tracked in track_results.values():
            for det in ts_tracked:
                if det.track_id > 0 and det.track_id not in class_name_for_id:
                    class_name_for_id[det.track_id] = det.class_name

        # Post-hoc track stitcher: merge fragment pairs of the same class
        # that end and immediately start nearby in space/time.
        id_map = stitch_track_fragments(track_results, class_name_for_id)
        n_merged = sum(1 for old, new in id_map.items() if old != new)
        if n_merged:
            print(f"    Track stitcher: merged {n_merged} fragment(s) into canonical IDs")
            # Remap track_ids in track_results in-place
            for ts_tracked in track_results.values():
                for det in ts_tracked:
                    if det.track_id in id_map:
                        det.track_id = id_map[det.track_id]

        # Build track_history for report
        for trk in list(tracker._active) + list(tracker._lost):
            track_history[trk.track_id] = {
                "class":               trk.class_name,
                "first_seen":          round(max(0.0, (trk.age - trk.hits) * interval), 3),
                "last_seen":           round(trk.age * interval, 3),
                "frame_count":         trk.hits,
                "avg_confidence":      0.0,   # filled below from track_results
                "violations_on_track": [],    # back-filled after Stage 4
            }

        # ── Bug 2.3 fix: fill avg_confidence from accumulated track detections
        # track_results holds every TrackedDetection with its .confidence score.
        # Accumulate per-track sum + count, then normalize.
        _conf_sum:   dict[int, float] = {}
        _conf_count: dict[int, int]   = {}
        for ts_tracked in track_results.values():
            for det in ts_tracked:
                if det.track_id >= 0 and det.confidence > 0.0:
                    _conf_sum[det.track_id]   = _conf_sum.get(det.track_id, 0.0) + det.confidence
                    _conf_count[det.track_id] = _conf_count.get(det.track_id, 0) + 1
        for tid, hist in track_history.items():
            if tid in _conf_count and _conf_count[tid] > 0:
                hist["avg_confidence"] = round(
                    _conf_sum[tid] / _conf_count[tid], 4
                )

        # Resolve stable vehicle classes and push into registry
        class_resolutions = class_agg.resolve_all()
        n_stable = sum(1 for r in class_resolutions.values() if r.is_stable)
        print(f"    Vehicle class votes resolved: {len(class_resolutions)} tracks, "
              f"{n_stable} stable")
        for tid, cr in class_resolutions.items():
            registry.set_vehicle_class(
                track_id=tid,
                vehicle_class=cr.resolved_class,
                confidence=cr.agreement,
                is_stable=cr.is_stable,
            )
            if cr.flip_flopped:
                print(f"      Track {tid}: class flip-flop ({cr.resolved_class} "
                      f"vs {cr.runner_up}) — treating as unstable")
    else:
        print("    Skipped (use --track to enable)")

    # ── Stage 4: Rule engine + heuristics ────────────────────────────────────
    _stage(4, "Rule Engine + Heuristics")
    wd = WheelieDetector()
    ed = ErraticDrivingDetector()

    # Collect per-frame OCR results for rule engine (used for plate flag)
    # We'll do OCR in stage 5 then back-fill; pass None for now
    frame_verdicts = apply_rules_to_all(
        frame_detections,
        frames_bgr=[f.image for f in frames],
        wheelie_detector=wd,
        erratic_detector=ed,
        track_results=track_results if use_tracker else None,
    )

    all_violations = set(v for fv in frame_verdicts for v in fv.violations)
    max_riders     = max((fv.rider_count for fv in frame_verdicts), default=0)
    print(f"    Max riders in any frame  : {max_riders}")
    print(f"    Violations seen in frames: {all_violations or 'none'}")
    phone_frames   = sum(1 for fv in frame_verdicts if fv.phone_usage)
    wheelie_frames = sum(1 for fv in frame_verdicts if fv.wheelie)
    erratic_frames = sum(1 for fv in frame_verdicts if fv.erratic_track_ids)
    if phone_frames:   print(f"      Phone usage: {phone_frames}/{len(frames)} frames")
    if wheelie_frames: print(f"      Wheelie:     {wheelie_frames}/{len(frames)} frames")
    if erratic_frames: print(f"      Erratic:     {erratic_frames}/{len(frames)} frames")

    # ── Feed per-frame violation observations into VehicleStateRegistry ──────
    # Map each FrameVerdict's violations to the tracks that were present in
    # that frame — so every observation is keyed by track_id, not frame index.
    # This is the "shared vehicle brain" step: observations accumulate here and
    # are resolved at clip end (Stage 6 output is NOT changed by this).
    _VIOLATION_KEYS = {
        "no_helmet", "triple_riding", "phone_usage",
        "wheelie", "erratic_driving", "signal_violation",
    }
    if use_tracker:
        for i_fv, fv in enumerate(frame_verdicts):
            ts = fv.timestamp
            tracks_in_frame = track_results.get(ts, [])
            # Determine which track IDs are in this frame
            frame_track_ids = {
                det.track_id for det in tracks_in_frame if det.track_id >= 0
            }
            # Update first/last seen in registry for each visible track
            for det in tracks_in_frame:
                if det.track_id >= 0:
                    registry.add_observation(
                        track_id=det.track_id,
                        frame_index=i_fv,
                        timestamp=ts,
                        source="detector",
                        key="present",
                        value=True,
                        confidence=det.confidence,
                    )

            # Push violation observations to matching visible tracks
            for violation in fv.violations:
                norm_v = violation  # e.g. 'no_helmet', 'triple_riding'
                if norm_v not in _VIOLATION_KEYS:
                    continue
                is_two_wh_viol = norm_v in {"no_helmet", "triple_riding", "wheelie"}
                for det in tracks_in_frame:
                    if det.track_id < 0:
                        continue
                    if is_two_wh_viol and not is_two_wheeler(det.class_name):
                        continue
                    registry.add_observation(
                        track_id=det.track_id,
                        frame_index=i_fv,
                        timestamp=ts,
                        source="rules",
                        key=norm_v,
                        value=True,
                        confidence=fv.avg_detection_confidence,
                    )

            # Push phone_usage and wheelie (bool fields, not in .violations list)
            for bool_key, bool_val in [
                ("phone_usage", fv.phone_usage),
                ("wheelie",     fv.wheelie),
            ]:
                if bool_val:
                    is_two_wh_viol = (bool_key == "wheelie")
                    for det in tracks_in_frame:
                        if det.track_id < 0:
                            continue
                        if is_two_wh_viol and not is_two_wheeler(det.class_name):
                            continue
                        registry.add_observation(
                            track_id=det.track_id,
                            frame_index=i_fv,
                            timestamp=ts,
                            source="rules",
                            key=bool_key,
                            value=True,
                            confidence=fv.avg_detection_confidence,
                        )

            # Push erratic_driving observations to the specific erratic tracks
            for erratic_tid in fv.erratic_track_ids:
                registry.add_observation(
                    track_id=erratic_tid,
                    frame_index=i_fv,
                    timestamp=ts,
                    source="heuristics",
                    key="erratic_driving",
                    value=True,
                    confidence=0.75,   # heuristic confidence (no model score)
                )

    # \u2500\u2500 Bug 2.3 fix: back-fill violations_on_track in track_history
    # Now that frame_verdicts exist, collect every unique violation type seen
    # in any frame where each track was visible. This makes track_log.json
    # actually useful for admin review (was always [] before this fix).
    if use_tracker and track_history:
        _track_violations: dict[int, set] = {tid: set() for tid in track_history}
        for fv in frame_verdicts:
            if not fv.violations:
                continue
            tracks_in_frame = track_results.get(fv.timestamp, [])
            for det in tracks_in_frame:
                if det.track_id < 0 or det.track_id not in _track_violations:
                    continue
                for v in fv.violations:
                    if v in {"no_helmet", "triple_riding", "wheelie"} and not is_two_wheeler(det.class_name):
                        continue
                    _track_violations[det.track_id].add(v)
            # Erratic-driving: keyed to specific erratic track IDs
            for erratic_tid in fv.erratic_track_ids:
                if erratic_tid in _track_violations:
                    _track_violations[erratic_tid].add("erratic_driving")
        for tid, viols in _track_violations.items():
            track_history[tid]["violations_on_track"] = sorted(viols)

    # Subject-vehicle identification — needed before OCR resolution so a
    # plate read on the flagged vehicle's own track outranks a cleaner read
    # on an unrelated bystander vehicle (see plate attribution below).
    dominant_vehicle_type = resolve_dominant_vehicle_type(frame_verdicts)

    vehicle_classes = {"motorcycle", "bicycle", "car", "bus", "truck", "mini_lcv", "auto_rickshaw", "vehicle"}
    vehicle_track_labels: dict[int, str] = {}
    for tracked in track_results.values():
        for det in tracked:
            if det.track_id >= 0 and det.class_name in vehicle_classes:
                prev = vehicle_track_labels.get(det.track_id)
                if prev is None or det.class_name != "vehicle":
                    vehicle_track_labels[det.track_id] = det.class_name

    # ── Stage 5: OCR (3-tier track-keyed aggregation) ────────────────────────
    _stage(5, "License Plate OCR (ampr.pt -> EasyOCR -> PaddleOCR -> Gemini VLM)")
    aggregator = PlateAggregator()

    for i, fd in enumerate(frame_detections):
        plates = fd.by_class("license_plate")
        tracked_in_frame = track_results.get(fd.timestamp, []) if use_tracker else []
        for plate in sorted(plates, key=lambda d: d.confidence, reverse=True):
            track_id = find_nearest_track_id(plate.bbox, tracked_in_frame) or -1
            raw = aggregator.add_raw_read(
                track_id=track_id,
                frame_bgr=frames[i].image,
                plate_bbox=plate.bbox,
                timestamp=fd.timestamp,
                frame_index=i,
                detector_confidence=plate.confidence,
            )
            if raw:
                status_lbl = "valid" if raw.is_valid else "invalid format"
                print(f"      t={fd.timestamp:.2f}s [track {track_id:>3d}] -> '{raw.text}' [{status_lbl}]")

        if use_tracker:
            plate_track_ids = {
                find_nearest_track_id(plate.bbox, tracked_in_frame)
                for plate in plates
            }
            for det in tracked_in_frame:
                if det.track_id < 0 or det.track_id in plate_track_ids:
                    continue
                if det.class_name not in {"motorcycle", "bicycle", "car", "bus", "truck", "mini_lcv", "auto_rickshaw", "vehicle"}:
                    continue
                aggregator.add_vehicle_roi_candidate(
                    track_id=det.track_id,
                    frame_bgr=frames[i].image,
                    vehicle_bbox=det.bbox,
                    vehicle_class=det.class_name,
                    vehicle_confidence=det.confidence,
                    timestamp=fd.timestamp,
                    frame_index=i,
                )

    # Improvement pass: strengthen weak EasyOCR results and recover missed plate boxes.
    for tid in aggregator.track_ids():
        if tid < 0:
            continue
        if aggregator.needs_escalation(tid):
            added_reads = aggregator.improve_track(tid, use_vlm=True)
            for raw in added_reads:
                status_lbl = "valid" if raw.is_valid else "invalid format"
                print(f"      [{raw.tier} {raw.source} track {tid}] -> '{raw.text}' [{status_lbl}]")

    # ── Car track fragment merger (plate-similarity + time compatibility) ──
    if use_tracker and track_history:
        car_merger = TrackMerger()
        raw_reads_map = aggregator.raw_reads_by_track()
        for tid, raw_plates in raw_reads_map.items():
            hist = track_history.get(tid)
            if hist and raw_plates:
                car_merger.register_track(
                    track_id=tid,
                    raw_plates=raw_plates,
                    first_seen=hist["first_seen"],
                    last_seen=hist["last_seen"],
                    video_duration_hint=frames[-1].timestamp if frames else 300.0,
                )
        car_merges = car_merger.find_merge_candidates()
        for mc in car_merges:
            print(f"    Car merge: track {mc.fragment_id} -> {mc.primary_id} "
                  f"(plates {mc.best_plate_pair}, sim={mc.plate_similarity:.2f})")
            aggregator.merge_tracks(mc.primary_id, mc.fragment_id)
            registry.merge_tracks(mc.primary_id, mc.fragment_id)
            if mc.fragment_id in track_history and mc.primary_id in track_history:
                p_hist = track_history[mc.primary_id]
                f_hist = track_history[mc.fragment_id]
                p_hist["first_seen"] = min(p_hist["first_seen"], f_hist["first_seen"])
                p_hist["last_seen"] = max(p_hist["last_seen"], f_hist["last_seen"])
                p_hist["frame_count"] += f_hist["frame_count"]
                p_hist["violations_on_track"] = sorted(
                    set(p_hist["violations_on_track"]) | set(f_hist["violations_on_track"])
                )
                del track_history[mc.fragment_id]
            if mc.fragment_id in vehicle_track_labels:
                del vehicle_track_labels[mc.fragment_id]

    # Final resolution: pick best plate across all tracks
    resolutions = aggregator.resolve_all()
    plate_by_track = {
        tid: res.plate_text
        for tid, res in resolutions.items()
        if tid >= 0 and res.plate_text and res.is_validated
    }
    subject_track_ids = {tid for tid, cls in vehicle_track_labels.items() if cls == dominant_vehicle_type}

    # ── State-code filter (Bug 2.5 fix) ────────────────────────────────────
    # Before picking the best plate, build a candidate list where each
    # (track_id, plate_text) pair is scored by agreement — then use
    # best_plate_candidate() to prefer real Indian state codes over
    # format-valid-but-impossible codes (e.g. HH vs MH).
    all_plate_candidates = [
        (res.plate_text, res.agreement)
        for tid, res in resolutions.items()
        if tid >= 0 and res.plate_text and res.is_validated
    ]
    state_filtered = best_plate_candidate(all_plate_candidates)

    best_resolution = aggregator.best_result(resolutions, preferred_track_ids=subject_track_ids)
    if best_resolution:
        # If state-code filtering picks a different winner, prefer it
        if (
            state_filtered
            and state_filtered[0] != best_resolution.plate_text
            and is_real_state_code(state_filtered[0])
            and not is_real_state_code(best_resolution.plate_text or "")
        ):
            print(f"    State-code filter override: "
                  f"'{best_resolution.plate_text}' → '{state_filtered[0]}'")
            number_plate  = state_filtered[0]
            ocr_agreement = state_filtered[1]
        else:
            number_plate  = best_resolution.plate_text
            ocr_agreement = best_resolution.agreement
        print(f"    Final plate: {number_plate or '<unreadable>'}  "
              f"(agreement {ocr_agreement:.0%}, tier={best_resolution.winning_tier}, "
              f"valid_reads={best_resolution.valid_reads}/{best_resolution.total_reads})")
    else:
        number_plate  = None
        ocr_agreement = 0.0
        print("    Final plate: <unreadable> (no detections)")

    # ── Push plate results into VehicleStateRegistry ─────────────────────
    if use_tracker:
        for tid, res in resolutions.items():
            if tid >= 0:
                registry.set_plate(
                    track_id=tid,
                    plate_text=res.plate_text,
                    confidence=res.confidence,
                    needs_review=bool(res.needs_review),
                )

    vehicles_detected = [
        {
            "track_id": tid,
            "class": cls,
            "plate": plate_by_track.get(tid),
            "plate_status": (
                "read"
                if tid in plate_by_track and not resolutions[tid].needs_review
                else "needs_review"
                if tid in plate_by_track
                else "unreadable"
            ),
            "plate_confidence": round(resolutions[tid].confidence, 4) if tid in resolutions else 0.0,
            "plate_agreement": round(resolutions[tid].agreement, 4) if tid in resolutions else 0.0,
            "valid_plate_reads": resolutions[tid].valid_reads if tid in resolutions else 0,
            "total_plate_reads": resolutions[tid].total_reads if tid in resolutions else 0,
            "ocr_tier": resolutions[tid].winning_tier if tid in resolutions else "none",
            "ocr_needs_review": bool(resolutions[tid].needs_review) if tid in resolutions else True,
        }
        for tid, cls in sorted(vehicle_track_labels.items())
    ]
    vehicle_plate_lines = [
        (
            f"ID:{v['track_id']} {v['class'].replace('_', ' ')} "
            f"plate: {v['plate'] or 'unreadable'} ({v['plate_status']})"
        )
        for v in vehicles_detected
    ]


    # ── Stage 6: Aggregation ──────────────────────────────────────────────────
    _stage(6, "Aggregation & Severity Scoring")
    vr = aggregate_verdicts(
        frame_verdicts,
        ocr_agreement_ratio=ocr_agreement,
        top_n_evidence_frames=3,
        total_track_ids=total_track_ids,
        vlm_enabled=False,   # VLM fires in stage 7 with actual frame
    )
    print(f"    Frame consistency : {vr.frame_consistency_ratio:.0%}")
    print(f"    Avg YOLO conf     : {vr.avg_yolo_confidence:.0%}")
    print(f"    Severity score    : {vr.severity_score:.3f}")
    print(f"    Status            : {vr.status}")
    print(f"    Violations        : {vr.violations_detected or 'none'}")

    # ── Stage 7: VLM tiebreaker ───────────────────────────────────────────────
    from pipeline.vlm import check_vlm_available
    vlm_auto = (not use_vlm) and vr.status == "needs_review" and check_vlm_available()
    vlm_active = use_vlm or vlm_auto
    if vlm_auto:
        print(f"  [{7}/{STAGES}] VLM Tiebreaker (auto-escalated — needs_review + VLM available)")
    else:
        _stage(7, "VLM Tiebreaker" + (" (Gemini Vision)" if use_vlm else " (skipped)"))

    vlm_error: str = ""
    if vlm_active and vr.status == "needs_review":
        from pipeline.vlm import vlm_tiebreaker
        if not check_vlm_available():
            msg = "No VLM key configured (GEMINI_API_KEY or NVIDIA_NIM_API_KEY missing)."
            print(f"    VLM skipped: {msg}")
            vlm_error = msg
        else:
            # Use best evidence frame as input
            best_ts   = vr.evidence_frame_timestamps[0] if vr.evidence_frame_timestamps else None
            best_frame = None
            if best_ts is not None:
                best_frame = min(frames, key=lambda f: abs(f.timestamp - best_ts)).image
            if best_frame is not None:
                summary = {
                    "violations_detected": vr.violations_detected,
                    "helmet_status":       vr.helmet_status,
                    "rider_count":         vr.rider_count,
                    "plate":               number_plate or "unreadable",
                    "severity_score":      vr.severity_score,
                    "frame_consistency":   vr.frame_consistency_ratio,
                    "vehicle_type":        vr.vehicle_type,
                }
                old_status = vr.status
                # Compute which violations were confirmed in ≥3 frames
                # so the VLM cannot downgrade those high-confidence detections.
                HIGH_CONF_MIN_FRAMES = 3
                viol_frame_counts: dict[str, int] = {}
                for fv in frame_verdicts:
                    for v in fv.violations:
                        viol_frame_counts[v] = viol_frame_counts.get(v, 0) + 1
                locked_violations = {
                    v for v, cnt in viol_frame_counts.items()
                    if cnt >= HIGH_CONF_MIN_FRAMES
                }
                if locked_violations:
                    print(f"    VLM: high-confidence violations locked (seen ≥{HIGH_CONF_MIN_FRAMES} frames): "
                          f"{sorted(locked_violations)}")

                try:
                    new_status, reasoning = vlm_tiebreaker(best_frame, summary, vr.status)
                    print(f"    VLM verdict: {old_status} -> {new_status}")
                    print(f"    Reasoning  : {reasoning}")
                    # Guard: if high-confidence violations exist, VLM can only
                    # confirm or escalate — never downgrade to insufficient_evidence.
                    if locked_violations and new_status == "insufficient_evidence":
                        logger.info(
                            "VLM downgrade blocked: locked violations %s prevent "
                            "status drop to 'insufficient_evidence'.",
                            sorted(locked_violations),
                        )
                        print(f"    VLM downgrade blocked (locked violations present) — "
                              f"retaining '{old_status}'")
                        new_status = old_status
                    vr.status        = new_status
                    vr.vlm_reasoning = reasoning
                except Exception as vlm_exc:
                    vlm_error = f"VLM call failed: {vlm_exc}"
                    logger.warning("VLM tiebreaker error — retaining '%s': %s", vr.status, vlm_exc)
                    print(f"    VLM error: {vlm_error}")

                if vr.status != old_status:
                    try:
                        from pipeline.hard_case_miner import log_hard_case
                        saved_case = log_hard_case(
                            video_source=source,
                            timestamp=best_ts or 0.0,
                            frame_bgr=best_frame,
                            rule_status=old_status,
                            rule_violations=vr.violations_detected,
                            vlm_verdict=vr.status,
                            vlm_reasoning=getattr(vr, "vlm_reasoning", ""),
                            trigger_reason="vlm_disagreement",
                            extra_metadata=summary,
                        )
                        if saved_case:
                            print(f"    Hard-case saved -> {saved_case}")
                    except Exception as exc:
                        logger.debug("Hard-case miner skipped: %s", exc)
    else:
        print("    Skipped (use --vlm to enable, fires only on needs_review)")

    # ── Stage 8: Report ───────────────────────────────────────────────────────
    _stage(8, "Report & Evidence Frames")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Resolve all accumulated per-vehicle observations before building report
    registry.resolve_all()
    vehicle_records = registry.export_report()
    n_confirmed = sum(1 for v in vehicle_records if v["has_violation"])
    print(f"    VehicleStateRegistry: {len(vehicle_records)} vehicle(s), "
          f"{n_confirmed} with confirmed violation(s)")

    report = build_report(
        verification_result=vr,
        number_plate=number_plate,
        plate_read_confidence=ocr_agreement,
        source_frames=frames,
        processing_time_s=time.time() - t_start,
        vehicle_type_declared=vehicle_type,
        run_id=output_dir.name,
        track_history=track_history or None,
        vehicles_detected=vehicles_detected,
        vehicle_records=vehicle_records,   # VehicleStateRegistry full export
    )

    # Save annotated evidence frames
    annotated_paths = []
    for i, frame in enumerate(frames):
        if frame.timestamp not in vr.evidence_frame_timestamps:
            continue
        fd = frame_detections[i]
        if use_tracker and frame.timestamp in track_results:
            img = draw_tracked_detections(
                frame.image,
                track_results[frame.timestamp],
                number_plate,
                plate_by_track=plate_by_track,
            )
        else:
            img = draw_detections(frame.image, fd.detections, number_plate)
        img = draw_verdict_overlay(
            img,
            vr,
            number_plate,
            i + 1,
            len(frames),
            vehicle_plate_lines=vehicle_plate_lines,
        )
        fname = output_dir / f"evidence_t{frame.timestamp:.3f}s.jpg"
        cv2.imwrite(str(fname), img)
        annotated_paths.append(str(fname))
        print(f"    Saved: {fname}")

    report["evidence_frames"] = annotated_paths

    # Render full annotated video (.mp4) for human review
    video_out_path = output_dir / "evidence_video.mp4"
    from pipeline.annotator import render_full_annotated_video
    rendered = render_full_annotated_video(
        frames=frames,
        frame_detections=frame_detections,
        track_results=track_results,
        verification_result=vr,
        number_plate=number_plate,
        plate_by_track=plate_by_track,
        vehicle_plate_lines=vehicle_plate_lines,
        output_video_path=str(video_out_path),
        fps=max(1.0, 1.0 / interval),
    )
    if rendered:
        print(f"    Evidence vid : {video_out_path}")
        report["evidence_video"] = str(video_out_path)

    report_path = output_dir / "report.json"
    report_path.write_text(report_to_json(report), encoding="utf-8")
    print(f"    JSON report  : {report_path}")
    if track_history:
        print(f"    Track log    : {output_dir / 'track_log.json'}")

    # ── Final summary ─────────────────────────────────────────────────────────
    elapsed    = time.time() - t_start
    status_col = STATUS_COLOURS.get(vr.status, "")
    violations = ", ".join(vr.violations_detected) if vr.violations_detected else "None"

    _banner("ANALYSIS COMPLETE")
    print(f"""
  Source          : {Path(source).name}
  Frames analysed : {len(frames)} ({frames[-1].timestamp:.1f}s @ {interval}s interval)
  Processing time : {elapsed:.1f}s

  {BOLD}VERDICT :{status_col} {vr.status.replace('_', ' ').upper()}{RESET}
  Confidence      : {vr.severity_score:.0%}

  Violations      : {violations}
  Vehicle type    : {vr.vehicle_type}
  Riders detected : {vr.rider_count}
  Helmet status   : {vr.helmet_status.replace('_', ' ')}
  Number plate    : {number_plate or 'Could not read plate'}
  Plate flag      : {vr.plate_flag}
  Phone usage     : {vr.phone_usage}
  Wheelie         : {vr.wheelie_detected}
  Erratic driving : {vr.erratic_driving}
  Track IDs used  : {total_track_ids}

  Frame consistency : {vr.frame_consistency_ratio:.0%}
  Avg YOLO conf     : {vr.avg_yolo_confidence:.0%}

  Evidence frames : {output_dir}/
  JSON report     : {report_path}
""")

    if vr.status == "auto_flagged":
        print(f"  {BOLD}\033[91m  LIKELY VIOLATION -- ready for human review queue{RESET}")
    elif vr.status == "needs_review":
        print(f"  {BOLD}\033[93m  Borderline -- human reviewer should examine evidence{RESET}")
    else:
        print(f"  {BOLD}\033[92m  Insufficient evidence -- not treated as a violation{RESET}")

    print(f"\n  Disclaimer: automated recommendation only, not an enforcement decision.\n")
    return report


def main():
    parser = argparse.ArgumentParser(
        description="DriveTrust AI -- Road Violation Detection Pipeline v2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source", help="Path to video (.mp4/.avi) or image (.jpg/.png)")
    parser.add_argument("--interval",      type=float, default=0.5,
                        help="Seconds between sampled frames (default: 0.5)")
    parser.add_argument("--out",           type=str,   default=None,
                        help="Output directory (default: pipeline/evidence_output/latest — overwritten each run)")
    parser.add_argument("--track",         action="store_true",
                        help="Enable ByteTrack persistent vehicle ID assignment (recommended)")
    parser.add_argument("--vlm",           action="store_true",
                        help="Enable VLM tiebreaker (Gemini Vision, fallback: NVIDIA NIM) on needs_review cases")
    parser.add_argument("--vehicle-type",  type=str, default="two_wheeler",
                        choices=["two_wheeler", "four_wheeler"],
                        help="Declared vehicle type (affects front-cam detection routing)")

    args = parser.parse_args()

    if not os.path.isfile(args.source):
        print(f"ERROR: File not found: {args.source}", file=sys.stderr)
        sys.exit(1)

    # Default: always write to one fixed 'latest' folder (overwrite each run).
    # Pass --out to save a named run alongside it.
    LATEST_DIR = Path("pipeline/evidence_output/latest")
    if args.out:
        out_dir = Path(args.out)
    else:
        import shutil
        if LATEST_DIR.exists():
            shutil.rmtree(LATEST_DIR)
            print(f"  (Cleared previous run — {LATEST_DIR})")
        out_dir = LATEST_DIR

    run(
        source=args.source,
        interval=args.interval,
        output_dir=out_dir,
        use_tracker=args.track,
        use_vlm=args.vlm,
        vehicle_type=args.vehicle_type,
    )


if __name__ == "__main__":
    main()
