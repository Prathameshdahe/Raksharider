# RakshaRide — Two-Wheeler Violation Detection Pipeline

> **Automated advisory system for helmet and triple-riding violations.**  
> Detects violations from traffic camera footage and outputs a structured JSON report for human review.  
> Not an enforcement decision — advisory only.

---

## Features

- 🪖 **Helmet Detection** — fine-tuned YOLOv8 classifies `helmet` / `no helmet` per rider
- 🏍️ **Rider Detection** — COCO YOLOv8 identifies `person` + `motorcycle`, IoU-based pairing
- 👥 **Triple-Riding Detection** — flags ≥ 3 riders on a single motorcycle
- 🔢 **License Plate OCR** — custom plate detector (`ampr.pt`) + EasyOCR with Indian format validation (`MH12AB1234`)
- 🔁 **Multi-frame Consistency** — aggregates verdicts across frames for reliability
- 📊 **Severity Scoring** — weighted formula → `auto_flagged` / `needs_review` / `insufficient_evidence`
- 🌐 **REST API** — FastAPI endpoint for video/image upload and JSON report
- ✅ **19 unit tests** — synthetic bbox inputs, no model loading, runs in ~0.2 s

---

## Architecture

```
Video / Image
      │
      ▼
┌─────────────────────┐
│  Frame Extractor    │  Sample 1 frame / 0.5 s (configurable)
└────────┬────────────┘
         │  List[Frame]
         ▼
┌─────────────────────────────────────────────────────┐
│  YOLO Detection  (3 models, lazy-loaded singletons) │
│                                                     │
│  ① yolov8n.pt       → person, motorcycle           │
│  ② helmet_model.pt  → helmet, no_helmet            │
│  ③ ampr.pt          → license_plate                │
└────────┬────────────────────────────────────────────┘
         │  List[FrameDetections]
         ▼
┌─────────────────────┐
│   Rule Engine       │  IoU-based rider↔motorcycle pairing
│                     │  Head-region helmet association (top 25 %)
│                     │  Triple-riding threshold (≥ 3 riders)
└────────┬────────────┘
         │  List[FrameVerdict]
         ▼
┌─────────────────────┐      ┌─────────────────────┐
│   OCR Engine        │      │   Verification      │
│   (EasyOCR)         │      │   & Severity Score  │
│   plate crop → text │      │   multi-frame agg.  │
└────────┬────────────┘      └────────┬────────────┘
         └──────────┬─────────────────┘
                    ▼
           ┌─────────────────┐
           │   JSON Report   │  + annotated evidence JPEGs
           └─────────────────┘
```

---

## Results

> Training was performed on the [Roboflow Motorcycle Helmet Dataset](https://roboflow.com) (YOLOv8 OBB variant).  
> **Download dataset:** [Roboflow / Google Drive link — add yours here]

| Metric        | Helmet Model (`helmet_model.pt`) |
|---------------|----------------------------------|
| Precision     | —  *(fill from `runs/` results.csv)* |
| Recall        | —  *(fill from `runs/` results.csv)* |
| mAP@50        | —  *(fill from `runs/` results.csv)* |
| mAP@50-95     | —  *(fill from `runs/` results.csv)* |

> To get your metrics: open `detection-pipeline/runs/detect/helmet_train/results.csv` locally.

---

## Project Structure

```
RakshaRide/
├── detection-pipeline/
│   ├── pipeline/
│   │   ├── frame_extractor.py   -- video/image frame sampling
│   │   ├── detector.py          -- 3-model YOLO detection
│   │   ├── rules.py             -- rider counting, helmet association, triple-riding
│   │   ├── ocr.py               -- EasyOCR + Indian plate format validation
│   │   ├── verification.py      -- multi-frame consistency + severity scoring
│   │   ├── report.py            -- JSON report + evidence frame output
│   │   └── annotator.py         -- draws bboxes and verdict overlay
│   ├── api/
│   │   └── main.py              -- FastAPI: POST /analyze, GET /health
│   ├── tests/
│   │   ├── sample_videos/       -- drop test clips here (sample.mp4 via LFS)
│   │   └── test_pipeline.py     -- 19 unit tests (no model loading needed)
│   ├── models/                  -- .pt weights via Git LFS
│   │   ├── helmet_model.pt
│   │   └── ampr.pt
│   ├── dataset/
│   │   └── helmet_dataset/
│   │       ├── data.yaml        -- class config (committed)
│   │       └── README.*.txt     -- dataset info (committed)
│   │       # train/test/valid splits → download from Roboflow (not committed)
│   ├── run_pipeline.py          -- CLI entry point
│   ├── train_helmet_model.py    -- YOLOv8 fine-tuning script
│   ├── prepare_dataset.py       -- Roboflow dataset conversion utility
│   └── requirements.txt
├── .gitattributes               -- Git LFS tracking config
├── .gitignore
└── README.md
```

---

## Setup

```bash
python -m venv .venv

# Windows
.venv\Scripts\Activate.ps1
# Linux / macOS
source .venv/bin/activate

# GPU (CUDA 12.1)
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
# CPU only
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

pip install -r detection-pipeline/requirements.txt
```

EasyOCR downloads its English model (~100 MB) on first use, cached locally.

---

## Models

Place these in `detection-pipeline/models/` before running:

| File               | Purpose                                      | Source         |
|--------------------|----------------------------------------------|----------------|
| `helmet_model.pt`  | Helmet / no-helmet detector (YOLOv8n)        | Git LFS        |
| `ampr.pt`          | License plate detector                       | Git LFS        |

`yolov8n.pt` (COCO base) is auto-downloaded by ultralytics on first run (~6 MB).

---

## CLI Usage

```bash
cd detection-pipeline

# Run on a video
python run_pipeline.py tests/sample_videos/sample.mp4

# Custom frame interval and output directory
python run_pipeline.py tests/sample_videos/sample.mp4 --interval 1.0 --out results/demo

# Run on a single image
python run_pipeline.py frame.jpg
```

Output: annotated evidence JPEGs + `report.json` in the output directory.

---

## REST API

```bash
cd detection-pipeline
uvicorn api.main:app --reload --port 8000
```

| Endpoint        | Method | Description                              |
|-----------------|--------|------------------------------------------|
| `/health`       | GET    | Liveness check                           |
| `/analyze`      | POST   | Upload video/image → returns JSON report |
| `/docs`         | GET    | Interactive Swagger UI                   |

```bash
curl -X POST http://localhost:8000/analyze \
     -F "file=@clip.mp4" \
     -F "sample_interval=0.5"
```

---

## Unit Tests

```bash
cd detection-pipeline
pytest tests/test_pipeline.py -v
```

19 synthetic tests — IoU, head-box geometry, rider counting, helmet association, edge cases.  
No footage or model loading required. Runs in **~0.2 s**.

---

## Report Format

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
  "frame_consistency_ratio": 0.78,
  "avg_yolo_confidence": 0.71,
  "ocr_agreement_ratio": 0.83,
  "notes": ""
}
```

### Severity Formula

```
severity = 0.5 × frame_consistency_ratio
         + 0.3 × avg_yolo_confidence
         + 0.2 × ocr_agreement_ratio
```

| Status                    | Severity   | Meaning                        |
|---------------------------|------------|--------------------------------|
| `auto_flagged`            | ≥ 0.85     | Ready for review queue         |
| `needs_review`            | 0.50–0.84  | Human should examine evidence  |
| `insufficient_evidence`   | < 0.50     | Do not treat as a violation    |

---

## Environment Variables

| Variable               | Default                     | Description                        |
|------------------------|-----------------------------|------------------------------------|
| `COCO_MODEL_PATH`      | `yolov8n.pt`                | COCO base model (person/motorcycle)|
| `HELMET_MODEL_PATH`    | `models/helmet_model.pt`    | Helmet/no-helmet YOLO weights      |
| `PLATE_MODEL_PATH`     | `models/ampr.pt`            | Plate detector weights             |
| `YOLO_CONF_THRESHOLD`  | `0.35`                      | Min confidence to keep a detection |

---

## Dataset

Training data: **Motorcycle Helmet Detection** dataset from [Roboflow Universe](https://universe.roboflow.com).  
**Download link:** *(add your Roboflow or Google Drive link here)*

The `detection-pipeline/dataset/helmet_dataset/` directory in this repo contains only:
- `data.yaml` — class configuration
- `README.dataset.txt` / `README.roboflow.txt` — attribution

Full train/test/valid splits are **not committed** to keep the repository lightweight.

---

## Tech Stack

| Component        | Technology                          |
|------------------|-------------------------------------|
| Detection models | YOLOv8 (Ultralytics)               |
| OCR              | EasyOCR                            |
| API              | FastAPI + Uvicorn                  |
| Testing          | pytest (19 unit tests)             |
| Large files      | Git LFS (model weights, video)     |
| Language         | Python 3.10+                       |
