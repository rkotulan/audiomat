# =============================================================================
# audiomat — multi-stage build
# =============================================================================
# Stage 1: Node 22 builder for the React frontend → frontend/dist
# Stage 2: pytorch/pytorch base (torch + torchaudio + cuDNN baked in) +
#          FastAPI app, served on :7860
#
# Why pytorch/pytorch instead of nvidia/cuda + manual pip install?
# Pushing a 6.86 GB torch+torchaudio layer to Docker Hub fails reliably on
# residential uploads (free-tier per-blob timeout). Using the official
# pytorch image means torch is part of base layers (already on Docker Hub
# under pytorch/pytorch), so docker push only uploads OUR diff (~1.5 GB).
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
# Stage 2: pytorch base + audiomat runtime
# -----------------------------------------------------------------------------
# Ships Python 3.11 (conda), torch 2.11.0, torchaudio, CUDA 12.8, cuDNN 9.
# Pinned by exact version tag (not :latest) so a base image rotation can't
# silently shift our runtime behavior.
FROM pytorch/pytorch:2.11.0-cuda12.8-cudnn9-runtime AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/data/cache/huggingface \
    AUDIOMAT_LIBRARY_ROOT=/data

# System deps the pytorch base doesn't ship: ffmpeg (audio I/O) + libsndfile
# (soundfile python backend). Kept lean — small layer = fast pushes.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# audiomat application deps. torch + torchaudio are pre-installed in the
# base image; requirements.txt deliberately omits them.
# --break-system-packages: pytorch/pytorch base ships a Debian-managed
# Python with PEP 668 protection. We're a single-purpose container, no
# other Python apps share this interpreter, so installing into the system
# site-packages is safe — and a venv layer would just bloat the image.
COPY requirements.txt /app/requirements.txt
RUN pip install --break-system-packages -r /app/requirements.txt

# audiomat code
COPY audiomat /app/audiomat
COPY pyproject.toml /app/pyproject.toml

# Frontend bundle from stage 1 → mounted by FastAPI as static
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
