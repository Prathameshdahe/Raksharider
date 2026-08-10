"""
api/main.py
───────────
RakshaRide FastAPI — stateless POC API wrapper around the detection pipeline.

Endpoints
─────────
POST /analyze   Upload a video or image; returns full JSON violation report.
GET  /health    Liveness check.
GET  /           Auto-redirect to interactive docs (/docs).

Pipeline stages logged at INFO level for traceability.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse

# Configure logging before any pipeline imports
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
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

import cv2

app = FastAPI(
    title="RakshaRide Detection Pipeline",
    description=(
        "POC API for two-wheeler traffic-violation detection. "
        "Upload a video or image; receive a structured JSON violation report. "
        "**Advisory only — not an enforcement decision.**"
    ),
    version="0.2.0",
)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["Meta"])
def health():
    """Liveness check."""
    return {"status": "ok", "service": "raksharide-detection-pipeline", "version": "0.2.0"}


@app.post("/analyze", tags=["Pipeline"])
async def analyze(
    file: UploadFile = File(..., description="Video (.mp4/.avi) or image (.jpg/.png)"),
    sample_interval: float = 0.5,
) -> JSONResponse:
    """
    Run the full 6-stage detection pipeline on the uploaded file.

    - **sample_interval**: seconds between sampled frames (video only, ignored for images)
    - Returns structured JSON report with severity score and violation details.
    """
    suffix = Path(file.filename or "upload").suffix or ".mp4"
    content = await file.read()

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    logger.info("=== /analyze: '%s' (%d bytes) ===", file.filename, len(content))

    try:
        report = _run_pipeline(tmp_path, sample_interval)
    except Exception as exc:
        logger.exception("Pipeline error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return JSONResponse(content=report)


def _run_pipeline(source_path: str, sample_interval: float = 0.5) -> Dict[str, Any]:
    """Orchestrate all 6 pipeline stages."""

    # 1. Frame extraction
    logger.info("[1/6] Frame extraction — interval=%.2fs", sample_interval)
    frames = extract_frames(source_path, sample_interval=sample_interval)
    logger.info("      %d frame(s) extracted", len(frames))

    # 2. Detection (3 models)
    logger.info("[2/6] YOLO detection on %d frame(s)", len(frames))
    frame_detections = detect_frames(frames)
    total = sum(len(fd.detections) for fd in frame_detections)
    logger.info("      %d total detections", total)

    # 3. Rule engine
    logger.info("[3/6] Applying rule engine")
    frame_verdicts = apply_rules_to_all(frame_detections)
    violations_seen = {v for fv in frame_verdicts for v in fv.violations}
    logger.info("      Violations across frames: %s", violations_seen or "none")

    # 4. OCR
    logger.info("[4/6] OCR on plate crops")
    plate_reads = []
    for i, fd in enumerate(frame_detections):
        plates = fd.by_class("license_plate")
        if not plates:
            continue
        best = max(plates, key=lambda d: d.confidence)
        read = read_plate(frames[i].image, best.bbox, fd.timestamp)
        if read:
            plate_reads.append(read)
    number_plate, ocr_agreement = majority_vote_plate(plate_reads)
    logger.info("      Plate: %s  agreement=%.2f", number_plate or "<none>", ocr_agreement)

    # 5. Verification
    logger.info("[5/6] Multi-frame verification")
    vr = aggregate_verdicts(frame_verdicts, ocr_agreement_ratio=ocr_agreement)
    logger.info("      Status=%s | Severity=%.3f", vr.status, vr.severity_score)

    # 6. Report + annotated evidence frames
    logger.info("[6/6] Building report")
    annotated_paths = []
    for i, frame in enumerate(frames):
        if frame.timestamp not in vr.evidence_frame_timestamps:
            continue
        fd = frame_detections[i]
        img = draw_detections(frame.image, fd.detections, number_plate)
        img = draw_verdict_overlay(img, vr, number_plate, i + 1, len(frames))
        # save via report.py's evidence dir
        from pipeline.report import EVIDENCE_OUTPUT_DIR
        import uuid
        run_id = str(uuid.uuid4())[:8]
        out_dir = EVIDENCE_OUTPUT_DIR / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"evidence_t{frame.timestamp:.3f}s.jpg"
        cv2.imwrite(str(path), img)
        annotated_paths.append(str(path.resolve()))

    report = build_report(
        verification_result=vr,
        number_plate=number_plate,
        plate_read_confidence=ocr_agreement,
        source_frames=frames,
    )
    report["evidence_frame_paths"] = annotated_paths
    logger.info("=== Pipeline complete. run_id=%s ===", report.get("run_id"))
    return report
