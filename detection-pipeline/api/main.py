"""
api/main.py
───────────
FastAPI application — stateless POC API wrapper around the detection pipeline.

Endpoints
---------
POST /analyze   Upload a video or image file; returns a JSON violation report.
GET  /health    Simple liveness check.

No database, no auth, no persistence.  One request = one pipeline run.

Logging
-------
Each pipeline stage logs at INFO level so failures are traceable from the
console.  Run with:
    uvicorn api.main:app --reload --log-level info
"""

from __future__ import annotations

import logging
import tempfile
import os
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

# Configure root logger first (before any pipeline imports that also log)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Pipeline imports ──────────────────────────────────────────────────────────
from pipeline.frame_extractor import extract_frames
from pipeline.detector import detect_frames
from pipeline.rules import apply_rules_to_all
from pipeline.ocr import read_plate, majority_vote_plate
from pipeline.verification import aggregate_verdicts
from pipeline.report import build_report, report_to_json

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="RakshaRide Detection Pipeline",
    description=(
        "POC API for two-wheeler traffic-violation detection. "
        "Accepts a video or image file; returns a structured JSON report "
        "intended as a RECOMMENDATION for human review."
    ),
    version="0.1.0",
)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
def health() -> Dict[str, str]:
    """Liveness check — returns 200 OK with a simple status message."""
    return {"status": "ok", "service": "detection-pipeline"}


@app.post("/analyze", tags=["pipeline"])
async def analyze(
    file: UploadFile = File(..., description="Video or image file to analyse"),
    sample_interval: float = 0.5,
) -> JSONResponse:
    """
    Run the full detection pipeline on the uploaded file.

    - Saves the upload to a temp file (deleted after processing).
    - Returns a JSON violation report.
    - `sample_interval` controls how many seconds apart frames are sampled
      (only relevant for video input; ignored for images).
    """
    # ── 1. Save upload to temp file ───────────────────────────────────────
    suffix = Path(file.filename or "upload").suffix or ".mp4"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    logger.info("=== /analyze: received '%s' (%d bytes) ===", file.filename, len(content))

    try:
        report = _run_pipeline(tmp_path, sample_interval=sample_interval)
    except Exception as exc:
        logger.exception("Pipeline error for file '%s': %s", file.filename, exc)
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return JSONResponse(content=report)


# ── Pipeline orchestration ────────────────────────────────────────────────────

def _run_pipeline(source_path: str, sample_interval: float = 0.5) -> Dict[str, Any]:
    """
    Orchestrate all pipeline stages for a given source file.

    Stages
    ------
    1. Frame extraction
    2. YOLO detection
    3. Rule application
    4. OCR
    5. Multi-frame verification & scoring
    6. Report assembly

    Returns the final report as a dict (JSON-serialisable).
    """
    # ── Stage 1: Frame extraction ─────────────────────────────────────────
    logger.info("[1/6] Frame extraction — interval=%.2fs", sample_interval)
    frames = extract_frames(source_path, sample_interval=sample_interval)
    logger.info("      %d frame(s) extracted.", len(frames))

    # ── Stage 2: YOLO detection ───────────────────────────────────────────
    logger.info("[2/6] Running YOLO detection on %d frame(s)…", len(frames))
    frame_detections = detect_frames(frames)
    total_detections = sum(len(fd.detections) for fd in frame_detections)
    logger.info("      %d total detection(s) across all frames.", total_detections)

    # ── Stage 3: Rule layer ───────────────────────────────────────────────
    logger.info("[3/6] Applying traffic-violation rules…")
    frame_verdicts = apply_rules_to_all(frame_detections)
    violations_seen = set(v for fv in frame_verdicts for v in fv.violations)
    logger.info("      Violations seen across frames: %s", violations_seen or "none")

    # ── Stage 4: OCR ─────────────────────────────────────────────────────
    logger.info("[4/6] Running OCR on detected license-plate regions…")
    plate_reads = []
    for i, fd in enumerate(frame_detections):
        plate_detections = fd.by_class("license_plate")
        if not plate_detections:
            continue
        # Use the highest-confidence plate detection per frame
        best_plate_det = max(plate_detections, key=lambda d: d.confidence)
        read = read_plate(
            image=frames[i].image,
            plate_bbox=best_plate_det.bbox,
            timestamp=fd.timestamp,
        )
        if read is not None:
            plate_reads.append(read)

    number_plate, ocr_agreement_ratio = majority_vote_plate(plate_reads)
    logger.info(
        "      Plate: %s (agreement=%.2f, %d reads)",
        number_plate or "<unreadable>", ocr_agreement_ratio, len(plate_reads),
    )

    # ── Stage 5: Verification & scoring ──────────────────────────────────
    logger.info("[5/6] Running multi-frame verification and severity scoring…")
    verification_result = aggregate_verdicts(
        frame_verdicts,
        ocr_agreement_ratio=ocr_agreement_ratio,
    )
    logger.info(
        "      Status=%s | Severity=%.3f | Consistency=%.2f",
        verification_result.status,
        verification_result.severity_score,
        verification_result.frame_consistency_ratio,
    )

    # ── Stage 6: Report assembly ──────────────────────────────────────────
    logger.info("[6/6] Assembling final report…")
    report = build_report(
        verification_result=verification_result,
        number_plate=number_plate,
        plate_read_confidence=ocr_agreement_ratio,
        source_frames=frames,
    )

    logger.info("=== Pipeline complete. Run ID: %s ===", report.get("run_id"))
    return report
