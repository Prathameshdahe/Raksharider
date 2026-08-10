# DriveTrust AI — Detection Pipeline

> **Scope:** This folder is owned entirely by the AI/ML engineer.  
> Do not touch `admin-portal/`, `backend/`, or `frontend/` — those belong to the backend teammate.

---

## What This Is

The detection pipeline for **DriveTrust AI**, an AI-assisted driving-behaviour compliance platform for two-wheeler and four-wheeler drivers built on the RakshaRide codebase.

Given a dashcam or road-camera clip, the pipeline outputs:
- A structured **JSON report** (`report.json`) with all violations detected
- **Annotated evidence frames** (JPEG) with bounding boxes and track IDs
- A **track log** (`track_log.json`) for audit trail

---

## Quick Start

```bash
# 1. Create and activate virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy and fill in credentials
copy .env.example .env          # Windows
# cp .env.example .env          # macOS/Linux
# Then edit .env with your NVIDIA NIM API key

# 4. Run the pipeline
python run_pipeline.py tests/sample_videos/sample.mp4 --track

# With VLM tiebreaker and explicit vehicle type
python run_pipeline.py clip.mp4 --track --vlm --vehicle-type two_wheeler
```

---

## Folder Structure

```
new ai pipeline/
│
├── run_pipeline.py             ← CLI entry point (start here)
├── requirements.txt            ← Python dependencies
├── .env                        ← Credentials (gitignored — never commit)
├── .env.example                ← Template for .env
│
├── pipeline/                   ← Core detection modules
│   ├── frame_extractor.py      ← Stage 1: Sample video → Frame objects
│   ├── detector.py             ← Stage 2: Run all 4 YOLO models
│   ├── tracker.py              ← Stage 3: ByteTrack persistent IDs
│   ├── rules.py                ← Stage 4: IoU rules → FrameVerdict
│   ├── heuristics.py           ← Stage 4: Zero-training detectors
│   ├── ocr.py                  ← Stage 5: Plate crop → EasyOCR
│   ├── verification.py         ← Stage 6: Aggregate → clip-level verdict
│   ├── vlm.py                  ← Stage 7: Nemotron tiebreaker (NIM API)
│   ├── report.py               ← Stage 8: JSON report + evidence frames
│   └── annotator.py            ← Draw bounding boxes + track IDs
│
├── models/                     ← All model weights (gitignored)
│   ├── yolov8n.pt              ← COCO base model (person, vehicle, signal…)
│   ├── helmet_model.pt         ← Helmet / no-helmet (trained)
│   ├── ampr.pt                 ← Number plate detector (trained)
│   └── classifiacation.pt      ← Vehicle classification (Bus/Car/Truck…)
│
├── tests/                      ← Unit tests
│   ├── test_heuristics.py      ← 25 tests for heuristics.py
│   ├── test_tracker.py         ← 8 tests for tracker.py
│   └── sample_videos/          ← Test clips
│
├── training/                   ← Training scripts (not part of live pipeline)
│   ├── train_helmet_model.py
│   ├── prepare_dataset.py
│   └── test_ampr_model.py
│
├── docs/                       ← Internal docs (gitignored)
│   └── (architecture diagrams, dataset notes, decisions log)
│
├── api/                        ← FastAPI async wrapper
│   └── main.py                 ← POST /analyze endpoint
│
└── dataset/                    ← Dataset configs (images gitignored)
    └── helmet_dataset/
```

---

## The 8-Stage Pipeline

```
[VIDEO + vehicle_type]
         │
    ─────▼─────────────────────────────────────────────────────
    Stage 1 │ Frame Extraction         frame_extractor.py
             │ Sample video at fixed interval → Frame objects
    ─────────▼─────────────────────────────────────────────────
    Stage 2 │ Detection (4 Models)     detector.py
             │  ├── yolov8n.pt      → person, motorcycle, car, bus, truck,
             │  │                      traffic_light, cell_phone, bicycle
             │  ├── helmet_model.pt → helmet / no_helmet
             │  ├── ampr.pt         → license_plate
             │  └── classifiacation.pt → Bus, Car, Mini LCV, Truck (fine-grained)
    ─────────▼─────────────────────────────────────────────────
    Stage 3 │ ByteTrack               tracker.py
             │  Kalman + Hungarian matching → stable vehicle IDs across clip
    ─────────▼─────────────────────────────────────────────────
    Stage 4 │ Rule Engine + Heuristics rules.py + heuristics.py
             │  ├── Rider-motorcycle IoU association
             │  ├── Helmet-head IoU association
             │  ├── Triple riding (>= 3 riders on one motorcycle)
             │  ├── Wheelie detection (bbox aspect-ratio spike)
             │  ├── Phone usage (person + cell_phone IoU)
             │  ├── Erratic driving (ByteTrack centroid variance)
             │  ├── Missing plate flag
             │  └── Traffic signal color (HSV filter)
    ─────────▼─────────────────────────────────────────────────
    Stage 5 │ OCR                     ocr.py
             │  Plate crop → EasyOCR → Indian format validator
    ─────────▼─────────────────────────────────────────────────
    Stage 6 │ Aggregation             verification.py
             │  Per-frame verdicts → clip-level status + severity score
             │  Status: auto_flagged / needs_review / insufficient_evidence
    ─────────▼─────────────────────────────────────────────────
    Stage 7 │ VLM Tiebreaker          vlm.py          (--vlm flag)
             │  Fires ONLY on needs_review.
             │  Nemotron (NVIDIA NIM) → confirms / downgrades verdict.
             │  Never acts as primary detector. Fails gracefully.
    ─────────▼─────────────────────────────────────────────────
    Stage 8 │ Report                  report.py
             │  ├── evidence_output/{run_id}/report.json
             │  ├── evidence_output/{run_id}/track_log.json
             │  └── evidence_output/{run_id}/evidence_t*.jpg
```

---

## Detection Coverage

### ✅ Ready Now (Zero New Training)

| Detection | Method |
|---|---|
| Helmet / no-helmet (road cam) | `helmet_model.pt` |
| Rider count / triple-riding | IoU person ↔ motorcycle |
| Vehicle classification | `classifiacation.pt` (8 classes) + COCO |
| Number plate detection | `ampr.pt` |
| Plate OCR | EasyOCR + Indian format regex |
| Missing / obscured plate | Heuristic |
| Persistent vehicle IDs | ByteTrack |
| Wheelie / stunt riding | Bbox aspect-ratio heuristic |
| Phone usage detection | Person + cell_phone IoU |
| Erratic driving | ByteTrack centroid variance |
| Traffic signal color | HSV filter on traffic_light bbox |
| VLM tiebreaker | Nemotron via NVIDIA NIM API |

### ⚠️ Needs Training Data

| Detection | Status |
|---|---|
| Seatbelt worn / not worn | Needs labeled dataset (500–1000 frames, dashboard angle) |
| Seatbelt model integration | Stubbed as `not_applicable` in front_camera section |

### 📅 Phase 2 (Not in Current Build)

| Detection | Blocker |
|---|---|
| Signal violation (stop line crossing) | Needs map-based stop line data |
| Wrong-side driving | Needs lane direction map or calibration |
| Tailgating | Needs calibrated distance metric |
| Over-speeding | Needs GPS feed + OSM speed limit API |
| Harsh braking / acceleration | Needs IMU/accelerometer from mobile app |

---

## Output Schema

Every run creates `pipeline/evidence_output/{run_id}/report.json`:

```json
{
  "status": "auto_flagged | needs_review | insufficient_evidence",
  "severity_score": 0.92,
  "violations_detected": ["no_helmet", "triple_riding"],

  "vehicle": {
    "type_declared": "two_wheeler",
    "type_detected": "motorcycle",
    "plate": "MH12AB1234",
    "plate_confidence": 0.87,
    "plate_flag": "ok | missing | low_confidence | invalid_format"
  },

  "road_camera": {
    "rider_count": 3,
    "helmet_status": "no_helmet",
    "triple_riding": true,
    "phone_usage": false,
    "wheelie_detected": false,
    "erratic_driving": false,
    "signal_violation": false
  },

  "front_camera": {
    "seatbelt_status": "not_applicable",
    "helmet_status": "not_applicable",
    "phone_usage_detected": false
  },

  "tracking": {
    "total_unique_ids": 12,
    "frames_tracked": 71
  },

  "vlm_review": {
    "fired": true,
    "reasoning": "Violation is clearly visible in the evidence frame."
  },

  "meta": {
    "frames_analysed": 71,
    "duration_seconds": 35.0,
    "processing_time_seconds": 59.6
  }
}
```

---

## CLI Reference

```
python run_pipeline.py <video> [options]

Arguments:
  video                   Path to .mp4 / .avi / .jpg input

Options:
  --interval FLOAT        Seconds between sampled frames (default: 0.5)
  --track                 Enable ByteTrack persistent vehicle IDs (recommended)
  --vlm                   Enable Nemotron VLM tiebreaker on needs_review cases
  --vehicle-type          two_wheeler | four_wheeler (default: two_wheeler)
  --out DIR               Output directory (default: pipeline/evidence_output/<name>)
```

### Examples

```bash
# Standard two-wheeler check with tracking
python run_pipeline.py clip.mp4 --track

# Full pipeline with VLM
python run_pipeline.py clip.mp4 --track --vlm --vehicle-type two_wheeler

# Four-wheeler (seatbelt check — stubbed until model is trained)
python run_pipeline.py dashcam.mp4 --track --vlm --vehicle-type four_wheeler

# Faster sampling for long clips
python run_pipeline.py highway.mp4 --track --interval 1.0
```

---

## Running Tests

```bash
# All tests
python -m pytest tests/ -v

# Heuristics only (fast, no GPU needed)
python -m pytest tests/test_heuristics.py -v

# Tracker only
python -m pytest tests/test_tracker.py -v
```

**Current test coverage: 33/33 tests passing**

---

## Models Reference

| File | Architecture | Classes | Source |
|---|---|---|---|
| `yolov8n.pt` | YOLOv8n (COCO) | 80 classes | Ultralytics pretrained |
| `helmet_model.pt` | YOLOv8s | helmet, no_helmet | Trained on Roboflow dataset |
| `ampr.pt` | YOLOv8m | Number_plate | Custom trained |
| `classifiacation.pt` | YOLOv8m | Bus, Car, Mini LCV, Truck (3/4/5-axle), Vehicle | Custom trained |

All weights are in `models/` and listed in `.gitignore`. Share via Google Drive or Git LFS.

---

## Team Boundary

| Scope | Owner |
|---|---|
| `new ai pipeline/` (this folder) | AI/ML engineer |
| `backend/`, `admin-portal/`, `frontend/` | Backend teammate (Manamrit) |
| Supabase database | Backend teammate only — AI pipeline never touches DB directly |

**Integration contract:** AI pipeline exposes `POST /analyze` → returns verdict JSON → backend teammate writes results to Supabase.
