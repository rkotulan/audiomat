# =============================================================================
# audiomat — multi-stage build
# =============================================================================
# Stage 1: Node 20 builder for the React frontend → frontend/dist
# Stage 2: CUDA 12.8 + Python 3.12 runtime serving FastAPI + static frontend
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: frontend builder
# -----------------------------------------------------------------------------
FROM node:22-alpine AS frontend-builder
WORKDIR /build

# package files first → docker layer cache friendly when only src/ changes
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

# components.json + tsconfig + vite config + index.html
COPY frontend/components.json frontend/tsconfig.json frontend/tsconfig.app.json \
     frontend/tsconfig.node.json frontend/vite.config.ts frontend/eslint.config.js \
     frontend/index.html ./

COPY frontend/public ./public
COPY frontend/src ./src

# Produce dist/
RUN npm run build


# -----------------------------------------------------------------------------
# Stage 2: Python + CUDA runtime
# -----------------------------------------------------------------------------
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/data/cache/huggingface \
    AUDIOMAT_LIBRARY_ROOT=/data

# System deps — Python 3.12 + ffmpeg (for the bundled imageio-ffmpeg fallback
# we *could* skip apt ffmpeg, but having it system-wide is cheaper than
# imageio-ffmpeg downloading the binary on every container build).
RUN apt-get update && apt-get install -y --no-install-recommends \
        software-properties-common \
        curl ca-certificates \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-venv python3.12-dev \
        ffmpeg \
        libsndfile1 \
        git \
    && rm -rf /var/lib/apt/lists/* \
    && curl -sS https://bootstrap.pypa.io/get-pip.py | python3.12 - \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python \
    && ln -sf /usr/local/bin/pip /usr/local/bin/pip3

WORKDIR /app

# 1) Install PyTorch with CUDA 12.8 wheels first (separate layer — these are
#    multi-GB and rarely change vs application deps).
RUN pip install --extra-index-url https://download.pytorch.org/whl/cu128 \
        torch==2.8.0+cu128 \
        torchaudio==2.8.0+cu128

# 2) audiomat application deps (without torch — already installed above).
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# 3) audiomat code.
COPY audiomat /app/audiomat
COPY pyproject.toml /app/pyproject.toml

# 4) Frontend bundle from stage 1 → mounted by FastAPI as static.
COPY --from=frontend-builder /build/dist /app/static

# Library volume (voices/, projects/, cache/). Will be a Docker volume
# mounted at runtime. We pre-create the dir so AudiomatPaths.ensure_dirs()
# has somewhere to write on first request.
RUN mkdir -p /data && chmod 0777 /data

# Entrypoint
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 7860

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "audiomat.api:app", "--host", "0.0.0.0", "--port", "7860"]
