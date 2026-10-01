# RoadWatch.AI — AI pipeline queue worker
#
# Builds a CPU image (torch from the CPU wheel index; no CUDA download). Runs anywhere
# a container can run: Docker Desktop, a VPS, a Hugging Face Space (Docker SDK, free CPU
# tier has the 16 GB RAM this needs), Railway, Fly. Not the Render free web tier: torch +
# ultralytics do not fit in 512 MB.
#
# Required env (see .env.example): DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD
#                                  AZURE_STORAGE_CONNECTION_STRING AZURE_EVIDENCE_CONTAINER
# Optional: GEMINI_API_KEY / NVIDIA_NIM_API_KEY (only with --vlm), WORKER_HTTP_PORT (health).
#
#   docker build -t roadwatch-worker .
#   docker run --env-file .env -p 7860:7860 roadwatch-worker
#   curl localhost:7860/health

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # ultralytics / easyocr / torch write caches and downloaded weights here; a Space runs
    # as uid 1000 with no write access to /root, so point everything at /app.
    HOME=/app \
    YOLO_CONFIG_DIR=/app/.config/Ultralytics \
    EASYOCR_MODULE_PATH=/app/.EasyOCR \
    TORCH_HOME=/app/.cache/torch \
    OMP_NUM_THREADS=2 \
    WORKER_HTTP_PORT=7860

# OpenCV / FFmpeg runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    ffmpeg \
    libgomp1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# PyTorch CPU wheels first so nothing below pulls the 2 GB CUDA build.
RUN pip install --upgrade pip && \
    pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

# Pipeline dependencies: drop the pinned Windows CUDA torch lines, add what the worker needs.
COPY requirements.txt .
RUN grep -viE "^(torch|torchvision)==" requirements.txt > reqs_filtered.txt && \
    pip install -r reqs_filtered.txt && \
    pip install azure-storage-blob psycopg2-binary python-dotenv google-genai

# Application source and models (models/*.pt are git-lfs: run `git lfs pull` before building).
COPY models/ models/
COPY pipeline/ pipeline/
COPY api/ api/
COPY *.py ./

# Hugging Face Spaces run the container as uid 1000; make the tree writable for evidence output.
RUN useradd -m -u 1000 -d /app worker 2>/dev/null || true && \
    mkdir -p /app/pipeline/evidence_output /app/.config/Ultralytics /app/.EasyOCR /app/.cache/torch && \
    chown -R 1000:1000 /app
USER 1000

EXPOSE 7860
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -fsS http://localhost:7860/health || exit 1

# Poll the Supabase queue every 10 s (dense frame sampling by default).
CMD ["python", "worker.py", "--poll", "10"]
