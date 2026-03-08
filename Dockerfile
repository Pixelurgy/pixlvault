# ── Stage 1: Build the Vue frontend ──────────────────────────────────────────
FROM node:22-slim AS frontend-builder

WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build


# ── Stage 2: Runtime image ────────────────────────────────────────────────────
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

# Prevent interactive prompts during apt installs
ENV DEBIAN_FRONTEND=noninteractive

# System libraries required by OpenCV, Pillow-HEIF, insightface, etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.12 \
    python3.12-venv \
    python3-pip \
    python3.12-dev \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    libheif-dev \
    libde265-dev \
    libx265-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Make python3.12 the default python3/python
RUN update-alternatives --install /usr/bin/python python /usr/bin/python3.12 1 \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1

WORKDIR /app

# ── Install Python deps in a venv ─────────────────────────────────────────────
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Upgrade pip/wheel
RUN pip install --no-cache-dir --upgrade pip wheel setuptools

# PyTorch with CUDA 12.6 — must be installed before open_clip_torch pulls CPU torch
RUN pip install --no-cache-dir \
    torch \
    torchvision \
    --index-url https://download.pytorch.org/whl/cu126

# onnxruntime-gpu replaces plain onnxruntime for CUDA inference
RUN pip install --no-cache-dir onnxruntime-gpu

# All other dependencies (onnxruntime already satisfied by onnxruntime-gpu above)
RUN pip install --no-cache-dir \
    open_clip_torch \
    fastapi \
    "uvicorn[standard]" \
    numpy \
    pillow \
    opencv-python-headless \
    scipy \
    platformdirs \
    tomli \
    colorlog \
    httpx \
    python-multipart \
    requests \
    "transformers<4.49" \
    insightface \
    rapidfuzz \
    tqdm \
    einops \
    sentence_transformers \
    spacy \
    pillow-heif \
    sqlmodel \
    alembic \
    "python-jose[cryptography]" \
    passlib \
    "bcrypt<4.0.0" \
    nvidia-ml-py \
    piexif \
    psutil \
    python-dotenv

# Remove build tools — not needed at runtime
RUN apt-get purge -y --auto-remove build-essential && rm -rf /var/lib/apt/lists/*

# Download spaCy English model
RUN python -m spacy download en_core_web_sm

# ── Copy application source ───────────────────────────────────────────────────
COPY pyproject.toml setup.py MANIFEST.in alembic.ini ./
COPY pixlvault/ pixlvault/
COPY migrations/ migrations/
COPY cpu/ cpu/
COPY cuda/ cuda/

# Install the pixlvault package itself (no deps — already installed above)
RUN pip install --no-cache-dir --no-deps -e .

# Copy the pre-built frontend into the package's expected location
COPY --from=frontend-builder /build/pixlvault/frontend/dist pixlvault/frontend/dist/

# ── Entrypoint ────────────────────────────────────────────────────────────────
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Volume for persistent data (images, vault.db, config, logs)
VOLUME ["/data"]

EXPOSE 9537

ENTRYPOINT ["docker-entrypoint.sh"]
