# RoadWatch.AI — AI Model Pipeline Worker Dockerfile
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1

# Install OpenCV / FFmpeg runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    ffmpeg \
    libgomp1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install PyTorch (CPU-compatible by default, supports GPU if nvidia container runtime is mounted)
RUN pip install --upgrade pip && \
    pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

# Install pipeline dependencies
COPY requirements.txt .
# Filter out pinned Windows CUDA torch if present and install remaining packages
RUN grep -v "torch==" requirements.txt | grep -v "torchvision==" > reqs_filtered.txt && \
    pip install -r reqs_filtered.txt && \
    pip install azure-storage-blob psycopg2-binary google-genai

# Copy application source and models
COPY models/ models/
COPY pipeline/ pipeline/
COPY api/ api/
COPY *.py .

# Run the queue worker polling Supabase
CMD ["python", "worker.py", "--poll", "10"]
