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

from pipeline.frame_extractor import extract_frames
from pipeline.detector        import detect_frames
from pipeline.rules           import apply_rules_to_all
from pipeline.ocr             import read_plate, majority_vote_plate
from pipeline.verification    import aggregate_verdicts
from pipeline.report          import build_report, report_to_json
from pipeline.annotator       import draw_detections, draw_verdict_overlay, draw_tracked_detections
from pipeline.tracker         import Tracker
from pipeline.heuristics      import WheelieDetector, ErraticDrivingDetector

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

    if use_tracker:
        for fd in frame_detections:
            tracked = tracker.update(fd)
            track_results[fd.timestamp] = tracked

        total_track_ids = tracker._next_id - 1
        print(f"    Total unique IDs assigned: {total_track_ids}")

        # Build track_history for report
        for trk in list(tracker._active) + list(tracker._lost):
            track_history[trk.track_id] = {
                "class":               trk.class_name,
                "first_seen":          round(trk.last_bbox[0] if trk.last_bbox else 0.0, 3),
                "last_seen":           round(trk.age * interval, 3),
                "frame_count":         trk.hits,
                "avg_confidence":      0.0,   # filled below
                "violations_on_track": [],
            }
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

    # ── Stage 5: OCR ─────────────────────────────────────────────────────────
    _stage(5, "License Plate OCR (ampr.pt -> EasyOCR)")
    plate_reads = []
    for i, fd in enumerate(frame_detections):
        plates = fd.by_class("license_plate")
        if not plates:
            continue
        best = max(plates, key=lambda d: d.confidence)
        read = read_plate(frames[i].image, best.bbox, fd.timestamp)
        if read:
            plate_reads.append(read)
            status_lbl = "valid" if read.is_valid_format else "invalid format"
            print(f"      t={fd.timestamp:.2f}s -> '{read.raw_text}' [{status_lbl}]")

    number_plate, ocr_agreement = majority_vote_plate(plate_reads)
    print(f"    Final plate: {number_plate or '<unreadable>'}  "
          f"(agreement {ocr_agreement:.0%} across {len(plate_reads)} reads)")

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
    _stage(7, "VLM Tiebreaker" + (" (Gemini Vision)" if use_vlm else " (skipped)"))
    if use_vlm and vr.status == "needs_review":
        from pipeline.vlm import vlm_tiebreaker, check_vlm_available
        if not check_vlm_available():
            print("    VLM skipped: NVIDIA_NIM_API_KEY not configured.")
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
                new_status, reasoning = vlm_tiebreaker(best_frame, summary, vr.status)
                print(f"    VLM verdict: {vr.status} -> {new_status}")
                print(f"    Reasoning  : {reasoning}")
                vr.status         = new_status
                vr.vlm_reasoning  = reasoning
    else:
        print("    Skipped (use --vlm to enable, fires only on needs_review)")

    # ── Stage 8: Report ───────────────────────────────────────────────────────
    _stage(8, "Report & Evidence Frames")
    output_dir.mkdir(parents=True, exist_ok=True)

    report = build_report(
        verification_result=vr,
        number_plate=number_plate,
        plate_read_confidence=ocr_agreement,
        source_frames=frames,
        processing_time_s=time.time() - t_start,
        vehicle_type_declared=vehicle_type,
        run_id=output_dir.name,
        track_history=track_history or None,
    )

    # Save annotated evidence frames
    annotated_paths = []
    for i, frame in enumerate(frames):
        if frame.timestamp not in vr.evidence_frame_timestamps:
            continue
        fd = frame_detections[i]
        if use_tracker and frame.timestamp in track_results:
            img = draw_tracked_detections(frame.image, track_results[frame.timestamp], number_plate)
        else:
            img = draw_detections(frame.image, fd.detections, number_plate)
        img = draw_verdict_overlay(img, vr, number_plate, i + 1, len(frames))
        fname = output_dir / f"evidence_t{frame.timestamp:.3f}s.jpg"
        cv2.imwrite(str(fname), img)
        annotated_paths.append(str(fname))
        print(f"    Saved: {fname}")

    report["evidence_frames"] = annotated_paths
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
                        help="Output directory (default: pipeline/evidence_output/<stem>)")
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

    out_dir = (Path(args.out) if args.out
               else Path("pipeline/evidence_output") / Path(args.source).stem)

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
