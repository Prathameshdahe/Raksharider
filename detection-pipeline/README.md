# RakshaRide — Detection Pipeline

POC for two-wheeler traffic-violation detection from a single video or image.
Outputs a structured JSON advisory report.

> Not an enforcement decision. Advisory only.

---

## Project structure

```
detection-pipeline/
├── pipeline/
│   ├── frame_extractor.py   -- video/image frame sampling
│   ├── detector.py          -- 3-model YOLO detection (person/motorcycle, helmet, plate)
│   ├── rules.py             -- rider counting, helmet association, triple-riding
│   ├── ocr.py               -- EasyOCR + Indian plate format validation
│   ├── verification.py      -- multi-frame consistency + severity scoring
│   ├── report.py            -- JSON report assembly + evidence frame output
│   └── annotator.py         -- draws bboxes and verdict overlay on evidence frames
├── api/
│   └── main.py              -- FastAPI: POST /analyze, GET /health
├── tests/
│   ├── sample_videos/       -- drop test clips here
│   └── test_pipeline.py     -- unit tests (no footage or model loading needed)
├── models/                  -- drop .pt weights here (gitignored)
├── run_pipeline.py          -- CLI entry point
├── train_helmet_model.py    -- YOLOv8 fine-tuning script
├── prepare_dataset.py       -- Roboflow dataset conversion utility
├── requirements.txt
└── README.md
```

---

## Models required

Place these in `models/` before running:

| File | Purpose |
|------|---------|
| `helmet_model.pt` | Fine-tuned YOLOv8n for `helmet` / `no helmet` |
| `ampr.pt` | License plate detector (`Number_plate` class) |

`yolov8n.pt` is auto-downloaded by ultralytics on first run (~6 MB).

---

## Setup

```bash
python -m venv .venv
# Windows:  .venv\Scripts\Activate.ps1
# Linux:    source .venv/bin/activate

# GPU (CUDA 12.1):
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
# CPU only:
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

pip install -r requirements.txt
```

EasyOCR downloads its English model (~100 MB) on first use, cached locally.

---

## CLI usage

```bash
python run_pipeline.py <video_or_image> [--interval 0.5] [--out output/]

# examples
python run_pipeline.py tests/sample_videos/clip.mp4
python run_pipeline.py tests/sample_videos/clip.mp4 --interval 1.0 --out results/demo
python run_pipeline.py frame.jpg
```

Output: annotated evidence JPEGs + `report.json` in the output directory.

---

## API

```bash
uvicorn api.main:app --reload --port 8000
```

- `GET /health` -- liveness check
- `POST /analyze` -- multipart upload (video or image), returns JSON report
- `GET /docs` -- interactive Swagger UI

```bash
curl -X POST http://localhost:8000/analyze -F "file=@clip.mp4" -F "sample_interval=0.5"
```

---

## Unit tests

```bash
pytest tests/test_pipeline.py -v
```

19 synthetic tests covering IoU, head-box geometry, rider counting, helmet association,
and edge cases. No footage or model loading required. Runs in ~0.2 s.

---

## Report format

```json
{
  "_disclaimer": "Automated recommendation for human review. Not a final enforcement or fine decision.",
  "run_id": "a1b2c3d4",
  "generated_at": "2026-07-22T08:00:00+00:00",
  "status": "auto_flagged | needs_review | insufficient_evidence",
  "severity_score": 0.87,
  "violations_detected": ["no_helmet", "triple_riding"],
  "rider_count": 3,
  "helmet_status": "no_helmet | helmet | unclear",
  "number_plate": "MH12AB1234",
  "plate_read_confidence": 0.83,
  "evidence_frame_timestamps": [7.5, 23.9],
  "evidence_frame_paths": ["/abs/path/evidence_t7.508s.jpg"],
  "frame_consistency_ratio": 0.78,
  "avg_yolo_confidence": 0.71,
  "ocr_agreement_ratio": 0.83,
  "notes": ""
}
```

### Severity formula

```
severity = 0.5 * frame_consistency_ratio
         + 0.3 * avg_yolo_confidence
         + 0.2 * ocr_agreement_ratio
```

| Status | Severity | Meaning |
|--------|----------|---------|
| `auto_flagged` | >= 0.85 | Ready for review queue |
| `needs_review` | 0.50-0.84 | Human should examine evidence |
| `insufficient_evidence` | < 0.50 | Do not treat as a violation |

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `YOLO_MODEL_PATH` | `models/helmet_model.pt` | Helmet/no-helmet YOLO weights |
| `PLATE_MODEL_PATH` | `models/ampr.pt` | Plate detector weights |
| `YOLO_CONF_THRESHOLD` | `0.35` | Min confidence to keep a detection |
