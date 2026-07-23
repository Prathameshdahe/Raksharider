# RakshaRide — Detection Pipeline

> **Status**: POC / Research prototype  
> **Purpose**: Two-wheeler traffic-violation detection from video or image files  
> **Output**: Structured JSON violation report (advisory — NOT an enforcement decision)

---

## Table of Contents

1. [Project Structure](#project-structure)
2. [Module Overview](#module-overview)
3. [Setup](#setup)
4. [Running the API](#running-the-api)
5. [Testing the API with curl](#testing-the-api-with-curl)
6. [Running Unit Tests](#running-unit-tests)
7. [Swapping in a Custom Model](#swapping-in-a-custom-model)
8. [Environment Variables](#environment-variables)
9. [Report Format](#report-format)
10. [Evaluation Notes](#evaluation-notes)

---

## Project Structure

```
detection-pipeline/
├── pipeline/
│   ├── frame_extractor.py   # OpenCV frame sampling from video / image
│   ├── detector.py          # YOLOv8 wrapper (person, motorcycle, helmet, plate)
│   ├── rules.py             # Rider counting, helmet-person association logic
│   ├── ocr.py               # EasyOCR wrapper + Indian plate format validation
│   ├── verification.py      # Multi-frame consistency + severity scoring
│   ├── report.py            # Assembles final structured JSON + evidence frames
│   └── evidence_output/     # Runtime — evidence JPEGs written here (gitignored)
├── api/
│   └── main.py              # FastAPI app: POST /analyze, GET /health
├── tests/
│   ├── sample_videos/       # Placeholder — drop test clips here
│   └── test_pipeline.py     # Synthetic unit tests (no footage needed)
├── models/                  # Placeholder — drop .pt weights here (gitignored)
├── requirements.txt
├── .gitignore
└── README.md
```

---

## Module Overview

| Module | Responsibility |
|--------|---------------|
| `frame_extractor.py` | Samples video at configurable intervals (default 0.5 s/frame); wraps images as a single-frame list so downstream code has one path. |
| `detector.py` | Loads YOLOv8n once at startup; detects `person`, `motorcycle`, `helmet`, `license_plate` per frame. Swap model in one line. |
| `rules.py` | IoU-based rider counting; head-region helmet association; triple-riding detection. |
| `ocr.py` | Crops plate bounding box; EasyOCR; cleans text; validates against `^[A-Z]{2}\d{2}[A-Z]{1,2}\d{4}$`; majority-votes across frames. |
| `verification.py` | Aggregates per-frame verdicts; computes severity score; classifies into `auto_flagged` / `needs_review` / `insufficient_evidence`. |
| `report.py` | Assembles final JSON dict; saves 1–2 evidence frames as JPEGs. |
| `api/main.py` | Stateless FastAPI wrapper; `POST /analyze` (multipart); `GET /health`. |

---

## Setup

### Prerequisites

- Python 3.10 or 3.11 (3.12 may have torch compatibility issues)
- pip

### 1. Create a virtual environment

```bash
# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1

# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install PyTorch (CPU)

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
```

> **GPU users**: replace `cpu` with `cu121` (CUDA 12.1) or the appropriate variant.
> See [pytorch.org/get-started](https://pytorch.org/get-started/locally/).

### 3. Install project dependencies

```bash
pip install -r requirements.txt
```

> **First run**: `ultralytics` will auto-download `yolov8n.pt` (~6 MB) and  
> `easyocr` will download its English language model (~100 MB) — both cached locally.

---

## Running the API

From the `detection-pipeline/` directory:

```bash
uvicorn api.main:app --reload --log-level info --port 8000
```

The server starts at `http://localhost:8000`.  
Interactive docs available at `http://localhost:8000/docs`.

---

## Testing the API with curl

### Health check

```bash
curl http://localhost:8000/health
```

Expected response:
```json
{"status": "ok", "service": "detection-pipeline"}
```

### Analyze a video file

```bash
curl -X POST http://localhost:8000/analyze \
  -F "file=@tests/sample_videos/test_clip.mp4" \
  -F "sample_interval=0.5"
```

### Analyze a single image

```bash
curl -X POST http://localhost:8000/analyze \
  -F "file=@path/to/frame.jpg"
```

### Pretty-print the JSON response (requires `jq`)

```bash
curl -s -X POST http://localhost:8000/analyze \
  -F "file=@tests/sample_videos/test_clip.mp4" | jq .
```

---

## Running Unit Tests

Tests use synthetic bounding-box data — **no footage, no model loading required**:

```bash
# From the detection-pipeline/ directory
pytest tests/test_pipeline.py -v
```

Expected output: all tests pass in < 2 seconds.

---

## Swapping in a Custom Model

Open `pipeline/detector.py` and change **one line**:

```python
# Before (pretrained YOLOv8n placeholder):
MODEL_PATH: str = os.environ.get("YOLO_MODEL_PATH", "yolov8n.pt")

# After (your fine-tuned model):
MODEL_PATH: str = os.environ.get("YOLO_MODEL_PATH", "models/raksharide_v1.pt")
```

Or set the environment variable at runtime without touching code:

```bash
# Windows PowerShell
$env:YOLO_MODEL_PATH = "models/raksharide_v1.pt"
uvicorn api.main:app --reload

# Linux / macOS
YOLO_MODEL_PATH=models/raksharide_v1.pt uvicorn api.main:app --reload
```

Your custom model must return class labels matching the `TARGET_CLASSES` dict in
`detector.py`.  If your class names differ, update that dict.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `YOLO_MODEL_PATH` | `yolov8n.pt` | Path to YOLO `.pt` weights file |
| `YOLO_CONF_THRESHOLD` | `0.35` | Min YOLO confidence to keep a detection |

---

## Report Format

The `POST /analyze` endpoint returns JSON in this shape:

```json
{
  "_disclaimer": "This report is an automated recommendation for human review. It is NOT a final enforcement or fine decision.",
  "run_id": "a1b2c3d4",
  "generated_at": "2026-07-22T08:00:00+00:00",

  "status": "auto_flagged | needs_review | insufficient_evidence",
  "severity_score": 0.0,
  "violations_detected": ["no_helmet", "triple_riding"],
  "rider_count": 2,
  "helmet_status": "helmet | no_helmet | unclear",
  "number_plate": "MH12AB1234",
  "plate_read_confidence": 0.0,

  "evidence_frame_timestamps": [0.5, 1.0],
  "evidence_frame_paths": ["/abs/path/evidence_t0.500s.jpg"],
  "frame_consistency_ratio": 0.0,
  "avg_yolo_confidence": 0.0,
  "ocr_agreement_ratio": 0.0,

  "notes": "Any caveats, e.g. 'plate not readable in any frame'."
}
```

### Severity Score Formula

```
severity = 0.5 × frame_consistency_ratio
         + 0.3 × avg_yolo_confidence
         + 0.2 × ocr_agreement_ratio
```

Weights are named constants in `verification.py` — tune them without touching arithmetic.

### Status Tiers

| Status | Severity | Meaning |
|--------|----------|---------|
| `auto_flagged` | ≥ 0.85 | High confidence — ready for human review queue |
| `needs_review` | 0.50–0.84 | Borderline — human should look closely |
| `insufficient_evidence` | < 0.50 | Do not treat as a violation |

---

## Evaluation Notes

> **⚠️ READ THIS BEFORE CITING ANY NUMBER IN A DEMO OR REPORT**

### Never report public-dataset accuracy as real-world accuracy

When you evaluate this pipeline against a public dataset (e.g., a Roboflow
motorcycle-helmet split, VisDrone, or COCO) you are measuring performance on
data that:

- Was collected in different lighting, camera angles, and traffic densities
  than our campus cameras.
- May have been used (directly or indirectly) to train the base YOLOv8 weights.
- Does not reflect the specific violation patterns we care about (e.g. campus
  speed, typical rider posture, Indian plate formats).

**A number from a public dataset tells you almost nothing about what will
happen on our footage.**

### What to do instead

Before trusting any accuracy number in a demo, a presentation, or a report:

1. **Build a held-out campus evaluation set**: Collect ≥ 100 real clips from
   the actual deployment cameras.  Never use these for training or hyperparameter
   tuning — only for final evaluation.
2. **Annotate honestly**: Label ground-truth rider count, helmet status, and
   plate strings manually.  Include hard cases (partial occlusion, night-time,
   rain, motion blur).
3. **Report metric + confidence interval**: e.g., "No-helmet precision 87% ± 4%
   on our N=120 campus clip evaluation set (July 2026)."  Include the set size,
   collection date, and camera locations.
4. **Retest after every model update**: A new fine-tuned model might improve
   one metric and regress another.  Always rerun the full evaluation suite.
5. **Distinguish detection accuracy from rule accuracy**: YOLO may detect
   persons correctly but the IoU threshold in `rules.py` might misclassify
   riders vs bystanders.  Evaluate both layers separately.

This discipline is what separates a trustworthy POC from a misleading one.
