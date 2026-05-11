"""System / model-status endpoints.

A single GET that the frontend polls every ~2 s during cold-start so the
user sees download/load progress instead of a hung spinner.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter

from audiomat.schemas import ModelState, ModelStatusOut
from audiomat.state import peek_tts


router = APIRouter(prefix="/api/system", tags=["system"])


# Measured size of the k2-fsa/OmniVoice snapshot on disk (~3.27 GB across
# 13 files, blob layout). Used as the denominator for the "downloading"
# progress %. Slight under/overshoot is fine — UI clamps to 100.
MODEL_TARGET_BYTES = 3_300_000_000


def _hf_model_cache_dir() -> Path:
    """Where huggingface_hub puts the OmniVoice snapshot. Honors HF_HOME if
    set (Docker sets it to /data/cache/huggingface), falls back to the
    user's default HF cache."""
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        root = Path(hf_home)
    else:
        root = Path.home() / ".cache" / "huggingface"
    return root / "hub" / "models--k2-fsa--OmniVoice"


def _dir_size_bytes(path: Path) -> int:
    """Recursive size of regular files under ``path``, skipping symlinks.

    HF cache layout is ``blobs/<hash>`` (real file) plus
    ``snapshots/<commit>/<filename>`` (symlink → blob). Counting both
    would double every byte. ``is_file()`` follows symlinks by default,
    so we explicitly skip them.
    """
    if not path.exists():
        return 0
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_symlink():
                continue
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


@router.get("/model-status", response_model=ModelStatusOut)
def model_status() -> ModelStatusOut:
    """Lightweight poll endpoint — frontend banner ticks this every ~2 s
    while waiting on first-render cold start so the user sees what's
    happening instead of staring at a hung spinner."""
    cache_bytes = _dir_size_bytes(_hf_model_cache_dir())
    tts = peek_tts()
    is_loaded = tts is not None and tts.is_loaded
    is_loading = tts is not None and tts.is_loading

    # State machine. "loading" / "downloading" only fire when a load is
    # actually in progress — otherwise a fresh container with a warm
    # cache would falsely flash "loading" until the user clicks anything.
    if is_loaded:
        state: ModelState = "ready"
        msg = None
    elif is_loading and cache_bytes >= int(MODEL_TARGET_BYTES * 0.95):
        state = "loading"
        msg = "Načítám TTS model na GPU…"
    elif is_loading and cache_bytes > 0:
        state = "downloading"
        gb_done = cache_bytes / 1e9
        gb_total = MODEL_TARGET_BYTES / 1e9
        msg = f"Stahuji TTS model… {gb_done:.1f} / {gb_total:.1f} GB"
    elif is_loading:
        state = "downloading"
        msg = "Připojuji se k HuggingFace…"
    else:
        state = "unloaded"
        msg = None

    pct = min(100.0, (cache_bytes / MODEL_TARGET_BYTES) * 100.0) if MODEL_TARGET_BYTES else 0.0
    return ModelStatusOut(
        state=state,
        cache_bytes=cache_bytes,
        cache_target_bytes=MODEL_TARGET_BYTES,
        percent=pct,
        message=msg,
    )
