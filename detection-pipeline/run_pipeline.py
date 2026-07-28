"""
run_pipeline.py
---------------
CLI entry point for the RakshaRide detection pipeline.

Usage:
    python run_pipeline.py <video_or_image> [--interval 0.5] [--out output/]

Examples:
    python run_pipeline.py tests/sample_videos/bike.mp4
    python run_pipeline.py tests/sample_videos/bike.mp4 --interval 1.0 --out results/demo
    python run_pipeline.py frame.jpg

Stages:
    1. Frame extraction       -- sample video at --interval seconds
    2. YOLO detection         -- 3 models: person/motorcycle, helmet, plate
    3. Rule engine            -- rider count, helmet association, triple-riding
    4. OCR                    -- plate crop from ampr.pt bbox -> EasyOCR
    5. Verification           -- multi-frame consistency + severity score
    6. Report                 -- annotated evidence frames + JSON report
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
from pipeline.annotator       import draw_detections, draw_verdict_overlay

STATUS_COLOURS = {
    "auto_flagged":          "\033[91m",
    "needs_review":          "\033[93m",
    "insufficient_evidence": "\033[92m",
}
RESET = "\033[0m"
BOLD  = "\033[1m"


def _banner(msg: str):
    print(f"\n{BOLD}{'-'*60}{RESET}")
    print(f"{BOLD}  {msg}{RESET}")
    print(f"{BOLD}{'-'*60}{RESET}")


def _stage(n: int, total: int, msg: str):
    print(f"\n  [{n}/{total}] {msg}")


def run(source: str, interval: float, output_dir: Path):
    t_start = time.time()

    _banner("RakshaRide -- AI Violation Detection Pipeline")
    print(f"  Source   : {source}")
    print(f"  Interval : {interval}s between sampled frames")
    print(f"  Output   : {output_dir}/")

    _stage(1, 6, "Frame Extraction")
    frames = extract_frames(source, sample_interval=interval)
    print(f"    {len(frames)} frame(s) extracted")

    _stage(2, 6, "YOLO Detection (person+motorcycle | helmet | plate)")
    frame_detections = detect_frames(frames)
    class_counts: dict = {}
    for fd in frame_detections:
        for d in fd.detections:
            class_counts[d.class_name] = class_counts.get(d.class_name, 0) + 1
    total_dets = sum(class_counts.values())
    print(f"    {total_dets} detections across {len(frames)} frames")
    for cls, cnt in sorted(class_counts.items()):
        print(f"      {cls}: {cnt}")

    _stage(3, 6, "Rule Engine (rider count, helmet association)")
    frame_verdicts = apply_rules_to_all(frame_detections)
    all_violations = set(v for fv in frame_verdicts for v in fv.violations)
    max_riders     = max((fv.rider_count for fv in frame_verdicts), default=0)
    print(f"    Max riders in any frame: {max_riders}")
    print(f"    Violations seen in frames: {all_violations or 'none'}")

    _stage(4, 6, "License Plate OCR (ampr.pt crop -> EasyOCR)")
    plate_reads = []
    for i, fd in enumerate(frame_detections):
        plates = fd.by_class("license_plate")
        if not plates:
            continue
        best = max(plates, key=lambda d: d.confidence)
        read = read_plate(frames[i].image, best.bbox, fd.timestamp)
        if read:
            plate_reads.append(read)
            status = "valid" if read.is_valid_format else "invalid format"
            print(f"      t={fd.timestamp:.2f}s -> '{read.raw_text}' [{status}]")

    number_plate, ocr_agreement = majority_vote_plate(plate_reads)
    print(f"    Final plate: {number_plate or '<unreadable>'}  "
          f"(agreement {ocr_agreement:.0%} across {len(plate_reads)} reads)")

    _stage(5, 6, "Verification & Severity Scoring")
    vr = aggregate_verdicts(frame_verdicts, ocr_agreement_ratio=ocr_agreement)
    print(f"    Frame consistency : {vr.frame_consistency_ratio:.0%}")
    print(f"    Avg YOLO conf     : {vr.avg_yolo_confidence:.0%}")
    print(f"    Severity score    : {vr.severity_score:.3f}")
    print(f"    Status            : {vr.status}")

    _stage(6, 6, "Report & Evidence Frames")
    output_dir.mkdir(parents=True, exist_ok=True)

    report = build_report(
        verification_result=vr,
        number_plate=number_plate,
        plate_read_confidence=ocr_agreement,
        source_frames=frames,
        run_id=output_dir.name,
    )

    annotated_paths = []
    for i, frame in enumerate(frames):
        if frame.timestamp not in vr.evidence_frame_timestamps:
            continue
        fd  = frame_detections[i]
        img = draw_detections(frame.image, fd.detections, number_plate)
        img = draw_verdict_overlay(img, vr, number_plate, i + 1, len(frames))
        fname = output_dir / f"evidence_t{frame.timestamp:.3f}s.jpg"
        cv2.imwrite(str(fname), img)
        annotated_paths.append(str(fname))
        print(f"    Saved: {fname}")

    report["evidence_frame_paths"] = annotated_paths

    report_path = output_dir / "report.json"
    report_path.write_text(report_to_json(report), encoding="utf-8")
    print(f"    JSON report: {report_path}")

    elapsed     = time.time() - t_start
    status_col  = STATUS_COLOURS.get(vr.status, "")
    violations  = ", ".join(vr.violations_detected) if vr.violations_detected else "None"

    _banner("ANALYSIS COMPLETE")
    print(f"""
  Source          : {Path(source).name}
  Frames analysed : {len(frames)} ({frames[-1].timestamp:.1f}s @ {interval}s interval)
  Processing time : {elapsed:.1f}s

  {BOLD}VERDICT :{status_col} {vr.status.replace('_', ' ').upper()}{RESET}
  Confidence      : {vr.severity_score:.0%}

  Violations      : {violations}
  Riders detected : {vr.rider_count}
  Helmet status   : {vr.helmet_status.replace('_', ' ')}
  Number plate    : {number_plate or 'Could not read plate'}
  Plate confidence: {ocr_agreement:.0%}

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
        description="RakshaRide -- Two-Wheeler Violation Detection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source",     help="Path to video (.mp4/.avi) or image (.jpg/.png)")
    parser.add_argument("--interval", type=float, default=0.5,
                        help="Seconds between sampled frames (default: 0.5)")
    parser.add_argument("--out",      type=str,   default=None,
                        help="Output directory (default: pipeline/evidence_output/<stem>)")

    args = parser.parse_args()

    if not os.path.isfile(args.source):
        print(f"ERROR: File not found: {args.source}", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.out) if args.out else Path("pipeline/evidence_output") / Path(args.source).stem
    run(args.source, args.interval, out_dir)


if __name__ == "__main__":
    main()
