# RoadWatch.AI

> **AI-powered traffic violation detection.** Upload a dashcam video, get an automated violation report, and route flagged cases to a human officer for review.

---

## Project Structure

```
RoadWatch.AI/
├── frontend/             ← PWA (deploy to Vercel)
├── DriveTrust-Backend/   ← FastAPI REST API (deploy to Render)
├── model-pipeline/       ← AI detection worker (run locally, needs GPU)
├── evidence/             ← Sample evidence output (gitignored in production)
├── start.bat             ← One-command local launcher (Windows)
└── README.md
```

> **Note:** `DriveTrust-Backend` will be renamed to `backend` — close VS Code and rename in Windows Explorer if the folder still shows the old name.

---

## How It Works

```
User uploads video
       │
       ▼
  frontend (Vercel)
  PWA drag-drop UI
       │ POST /videos/upload
       ▼
  DriveTrust-Backend (Render)
  FastAPI — saves record,
  sets status = 'unprocessed'
       │ writes to Supabase DB
       ▼
  Supabase (PostgreSQL)
  videos table queue
       │ claim_next_video()
       ▼
  model-pipeline (your laptop)
  YOLO → ByteTrack → OCR → VLM
       │ inserts vehicle_records
       │ uploads evidence → Azure
       ▼
  frontend dashboard
  live violation cards via
  Supabase Realtime
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | Vanilla HTML/CSS/JS, Supabase JS SDK, PWA |
| Backend API | Python 3.11, FastAPI, uvicorn |
| Database | Supabase (PostgreSQL) + Row Level Security |
| Auth | Supabase Auth (JWT) |
| AI Pipeline | YOLOv8, ByteTrack, EasyOCR, Gemini Vision |
| Evidence Storage | Azure Blob Storage |
| Deployment | Vercel (frontend) + Render (backend) |

---

## Local Development

### Prerequisites
- Python 3.11+
- Node.js not required (pure static frontend)
- GPU recommended for AI pipeline (RTX 3060 or similar)

### One-Command Start
```bat
cd c:\Users\DELL\Desktop\RakshaRide
.\start.bat
```

This runs 5 stages:
1. **Preflight** — checks venvs, `.env` files, ports
2. **Backend** — starts FastAPI on `:8000`, polls `/health`
3. **Frontend** — serves PWA on `:5051`
4. **AI Worker** — starts queue polling loop
5. **Summary** — prints all URLs, opens browser

### Manual Start (if start.bat fails)

```bat
# Backend
cd DriveTrust-Backend
venv\Scripts\activate
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Frontend (new terminal)
cd frontend
python -m http.server 5051

# AI Worker (new terminal)
cd model-pipeline
.venv\Scripts\activate
python worker.py --poll 10
```

### Local URLs
| Service | URL |
|---------|-----|
| Frontend PWA | http://localhost:5051 |
| Backend API | http://localhost:8000 |
| API Docs (Swagger) | http://localhost:8000/docs |
| Admin Panel | http://localhost:5051/admin.html |

---

## Environment Variables

### Backend (`DriveTrust-Backend/.env`)
```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your_anon_key
SUPABASE_SERVICE_ROLE_KEY=your_service_role_key
```

### AI Pipeline (`model-pipeline/.env`)
```env
# Supabase DB (for queue worker)
DB_HOST=aws-0-ap-northeast-1.pooler.supabase.com
DB_PORT=5432
DB_NAME=postgres
DB_USER=drivetrust_ai_worker.your_project_ref
DB_PASSWORD=your_db_password

# Azure Blob Storage (evidence frames)
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=...
AZURE_EVIDENCE_CONTAINER=evidence
AZURE_VIDEOS_CONTAINER=videos

# Gemini Vision (VLM tiebreaker)
GEMINI_API_KEY=your_key
GEMINI_VLM_MODEL=gemini-2.0-flash
```

**Never commit `.env` files.** They are listed in `.gitignore`.

---

## Deployment

### Overview

| Component | Host | Why |
|-----------|------|-----|
| Frontend PWA | **Vercel** | Static files, free, CDN |
| FastAPI Backend | **Render** | Python runtime, free tier |
| AI Worker | **Your laptop** | Needs GPU; polls cloud DB |
| Database | **Supabase** | Already running |
| Evidence files | **Azure Blob** | Already configured |

The AI worker stays local because it loads 4 YOLO models (~130 MB) and benefits heavily from GPU. It connects to the cloud Supabase DB over the internet — works fine locally.

---

### Step 1 — Deploy Backend to Render

1. **Push `DriveTrust-Backend/` to its own GitHub repo** (separate from frontend):
   ```bash
   cd DriveTrust-Backend
   git init
   git add .
   git commit -m "Initial backend"
   git remote add origin https://github.com/your-username/roadwatch-backend.git
   git push -u origin main
   ```

2. **Create Render Web Service:**
   - Go to [render.com](https://render.com) → **New → Web Service**
   - Connect your `roadwatch-backend` repo
   - Settings:
     ```
     Runtime:       Python 3
     Build Command: pip install -r requirements.txt
     Start Command: uvicorn app.main:app --host 0.0.0.0 --port $PORT
     Instance Type: Free
     ```

3. **Add environment variables in Render → Environment tab:**
   ```
   SUPABASE_URL               = (from your .env)
   SUPABASE_ANON_KEY          = (from your .env)
   SUPABASE_SERVICE_ROLE_KEY  = (from your .env)
   ```

4. **Deploy and verify:**
   ```
   https://roadwatch-backend.onrender.com/health
   https://roadwatch-backend.onrender.com/docs
   ```
   → You should see `{"status":"healthy"}` and the Swagger UI.

5. **Copy your Render URL** — you'll need it for the next step.

---

### Step 2 — Update Frontend with Render URL

Open [`frontend/app.js`](frontend/app.js) line 10 and replace the placeholder:

```js
// Before:
const RENDER_URL = 'https://roadwatch-backend.onrender.com';

// After (your actual URL):
const RENDER_URL = 'https://your-actual-service.onrender.com';
```

---

### Step 3 — Deploy Frontend to Vercel

1. **Push `frontend/` to its own GitHub repo:**
   ```bash
   cd frontend
   git init
   git add .
   git commit -m "Initial frontend"
   git remote add origin https://github.com/your-username/roadwatch-frontend.git
   git push -u origin main
   ```

2. **Create Vercel project:**
   - Go to [vercel.com](https://vercel.com) → **Add New Project**
   - Import your `roadwatch-frontend` repo
   - Framework Preset: **Other** (it's static HTML)
   - No build command needed
   - Click **Deploy**

3. **Copy your Vercel URL** (e.g., `https://roadwatch-ai.vercel.app`)

---

### Step 4 — Add Vercel URL to Backend CORS

Open [`DriveTrust-Backend/app/main.py`](DriveTrust-Backend/app/main.py) and replace the placeholder:

```python
# Replace:
"https://roadwatch-ai.vercel.app",

# With your actual URL:
"https://your-actual-app.vercel.app",
```

Then **remove the `"*"` wildcard line** and redeploy the backend:
```bash
git add app/main.py
git commit -m "Add Vercel URL to CORS"
git push
```
Render redeploys automatically on push.

---

### Step 5 — Verify End-to-End

1. Open your Vercel URL in the browser
2. Sign in with test credentials
3. Upload a short dashcam video
4. Watch the 8-stage pipeline animation
5. Open the `AI Worker` terminal on your laptop — you should see:
   ```
   [worker] Claimed video: <video_id>
   [worker] Uploading evidence frames to Azure...
   [worker] Video <video_id> processed successfully.
   ```
6. Check the Report/Dashboard tab — violation cards should appear live

---

## Database Schema (Supabase)

### Tables

| Table | Purpose |
|-------|---------|
| `videos` | Upload queue — status: `unprocessed → processing → processed/failed` |
| `vehicle_records` | Per-vehicle results from AI pipeline |
| `violations` | Individual violations detected per vehicle |

### RLS Policies
All three tables have Row Level Security enabled with policies for:
- `anon` / `authenticated` — SELECT only
- `authenticated` — UPDATE on violations (for officer review)
- `drivetrust_ai_worker` — ALL (queue claim + insert results)

### Supabase DB Function
```sql
-- Atomically claims next unprocessed video (prevents race conditions)
SELECT * FROM claim_next_video();
```

---

## AI Pipeline (model-pipeline/)

**Runs locally — never deployed to cloud.**

### 8 Detection Stages
1. **Frame Extraction** — samples every 0.5s
2. **YOLO Detection** — 4 models (COCO, helmet, plate, vehicle class)
3. **ByteTrack** — persistent vehicle IDs across frames
4. **Rules + Heuristics** — helmet, phone, wheelie, erratic, signal
5. **OCR** — EasyOCR/PaddleOCR plate reading with consensus vote
6. **Aggregation** — clip-level verdict + severity score
7. **VLM Tiebreaker** — Gemini Vision on borderline cases
8. **Report** — JSON + evidence JPEGs → Azure Blob Storage

### Run Manually (for testing)
```bash
cd model-pipeline
.venv\Scripts\activate
python run_pipeline.py path\to\video.mp4 --track --vlm
```

### Models (in `model-pipeline/models/`)
| File | Purpose |
|------|---------|
| `yolov8n.pt` | COCO object detection |
| `helmet_model.pt` | Helmet detection |
| `ampr.pt` | License plate detection |
| `classifiacation.pt` | Vehicle class |

> Model weights are gitignored (too large). Back them up separately.

---

## Azure Evidence Storage

Evidence frames (annotated JPEGs) and the evidence video are uploaded to Azure Blob Storage after each video is processed.

**Configure in `model-pipeline/.env`:**
```env
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=roadwatch;AccountKey=YOUR_KEY;EndpointSuffix=core.windows.net
AZURE_EVIDENCE_CONTAINER=evidence
AZURE_VIDEOS_CONTAINER=videos
```

**Get your connection string:**
Azure Portal → Storage accounts → roadwatch → Access keys → key1 → Connection string

> ⚠️ Rotate the key immediately if it was ever visible in a screenshot or chat.

**Test Azure connectivity:**
```bash
cd model-pipeline
.venv\Scripts\activate
python -c "from azure_storage import _get_client; c = _get_client(); print('Connected:', c.account_name)"
```

---

## Known Issues & Gotchas

| Issue | Fix |
|-------|-----|
| Render free tier sleeps after 15min | Open backend URL 1min before demo |
| CORS error on deployed site | Add exact Vercel URL to `main.py` allow_origins, redeploy backend |
| `claim_next_video()` returns None | Queue is empty — upload a video first |
| Azure upload skipped | Fill in `AZURE_STORAGE_CONNECTION_STRING` in `model-pipeline/.env` |
| Worker can't download video | Ensure `blob_url` or `video_url` is set on the videos row |

---

## Test Credentials
```
Email:    drivetrust.test@gmail.com
Password: drivetrust123
```

---

## Contributing
This is a research/demo project. To report issues or suggest features, open a GitHub issue.

---

*RoadWatch.AI — automated assistance for traffic officers. All enforcement decisions are made by humans.*
