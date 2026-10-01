# Deploying the AI worker

The worker (`worker.py`) polls the Supabase queue as the scoped Postgres role
`drivetrust_ai_worker`, runs `run_pipeline.run()` on each clip, uploads evidence to Azure
Blob, and persists the result package in one transaction. It needs **torch + ultralytics +
easyocr** (~2 GB of wheels, ~3–6 GB RAM at run time on CPU), so it does **not** fit the
Render free web tier (512 MB). Three ways to run it, cheapest first.

| Option | Cost | Always on? | GPU | Notes |
|---|---|---|---|---|
| **A. Laptop** (`start.bat`) | free | only while the laptop is on | yes (RTX 3060, CUDA) | fastest; what the team uses today |
| **B. Hugging Face Space** (Docker SDK, CPU basic) | free | sleeps after 48 h without an HTTP request; the keep-alive workflow pings it every 10 min | no | 2 vCPU / 16 GB RAM; a 20 s 4K clip takes several minutes |
| **C. Any Docker host** (VPS, Railway, Fly, Azure Container Apps) | from ~$5/mo | yes | optional | same image as B |

All three read the same `.env` keys (see `.env.example`). The worker never holds a Supabase
service key; only the scoped DB role + the Azure connection string.

## A. Laptop (today)

```bat
cd "new ai pipeline"
start.bat
```

`start.bat` activates `.venv`, installs missing packages, and runs `python worker.py --poll 10`.
Set `WORKER_HTTP_PORT=7860` in `.env` if you want the local health endpoint
(`http://localhost:7860/health`).

## B. Hugging Face Space (free, always reachable)

1. Create a Space: https://huggingface.co/new-space → SDK **Docker**, hardware **CPU basic
   (free)**, visibility private.
2. Push this folder to the Space. The Space repo needs `git lfs` for the models:
   ```bash
   cd "new ai pipeline"
   git lfs pull                      # make sure models/*.pt are real files, not pointers
   git init space && cd space
   cp -r ../Dockerfile ../requirements.txt ../worker.py ../run_pipeline.py ../azure_storage.py \
         ../pipeline ../api ../models .
   git lfs install && git lfs track "*.pt"
   git add . && git commit -m "RoadWatch worker"
   git remote add origin https://huggingface.co/spaces/<user>/roadwatch-worker
   git push -u origin main
   ```
3. Space → **Settings → Variables and secrets**. Add as *secrets*:
   `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`,
   `AZURE_STORAGE_CONNECTION_STRING`, `AZURE_EVIDENCE_CONTAINER` (=`evidence`),
   and optionally `GEMINI_API_KEY`, `NVIDIA_NIM_API_KEY`.
   `WORKER_HTTP_PORT` is already `7860` in the Dockerfile (the port Spaces expose).
4. Wait for the build (10–15 min the first time). The Space URL answers
   `GET /health` with the worker state, and the admin **Live board** shows the heartbeat
   within a minute.
5. In the GitHub repo add the secret `WORKER_HEALTH_URL = https://<user>-roadwatch-worker.hf.space/health`
   so `.github/workflows/keepalive.yml` keeps the Space from sleeping.

Only one worker should run at a time per queue unless you want parallel processing; the
queue is leased (`claim_next_video()`), so two workers are safe but will share the clips.
Stop the laptop worker when the Space is live, or run both for throughput.

## C. Docker anywhere

```bash
cd "new ai pipeline"
docker build -t roadwatch-worker .
docker run -d --name roadwatch-worker --restart unless-stopped --env-file .env -p 7860:7860 roadwatch-worker
curl localhost:7860/health
```

`docker-compose.yml` does the same with the evidence output mounted. For a GPU host add
`--gpus all` and build with the CUDA torch index instead of the CPU one.

## Health endpoint

`GET /health` (port `WORKER_HTTP_PORT`) returns JSON:

```json
{"service":"roadwatch-ai-worker","status":"idle|processing|degraded","started_at":"…",
 "last_heartbeat_at":"…","last_claim_at":"…","jobs_done":3,"jobs_failed":0,
 "current":null,"last_job":{"video_id":"…","ok":true,"seconds":212,"finished_at":"…"}}
```

The DB heartbeat (`system_settings.worker_last_seen`) is what the dashboard and
`check_idle_alerts()` use; the HTTP endpoint is for the host and the keep-alive pinger.

## Verifying a deployment

1. Admin → **Live board**: *Worker heartbeat* turns **OK** within a minute.
2. Upload a short clip as a citizen; it should move Queued → Analysing → Awaiting review.
3. `GET https://raksharider.onrender.com/status` shows `worker.online: true`.
