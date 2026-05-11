"""System / model-status endpoints.

A single GET that the frontend polls every ~2 s during cold-start so the
user sees download/load progress instead of a hung spinner. Reports the
currently-loading / loaded model by display name so the SystemBanner
can say "Načítám Ježková v1…" instead of a generic "TTS model".
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter

from audiomat.model_registry import DEFAULT_MODEL_HF_ID, TTSModel
from audiomat.schemas import ModelState, ModelStatusOut
from audiomat.state import PATHS, peek_tts


router = APIRouter(prefix="/api/system", tags=["system"])


# Measured size of the k2-fsa/OmniVoice snapshot on disk (~3.27 GB across
# 13 files, blob layout). Used as the denominator for the "downloading"
# progress % when the stock model is the active target. Slight under/
# overshoot is fine — UI clamps to 100.
STOCK_MODEL_TARGET_BYTES = 3_300_000_000

STOCK_MODEL_DISPLAY_NAME = "OmniVoice"


def _hf_model_cache_dir() -> Path:
    """Where huggingface_hub puts the stock OmniVoice snapshot. Honors
    HF_HOME (Docker sets it to /data/cache/huggingface), falls back to
    the user's default HF cache."""
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


def _resolve_active(target: str | None) -> tuple[str, int, int]:
    """Given a TTS instance's model_id, return ``(display_name,
    cache_bytes, target_bytes)``:

    * Stock OmniVoice → bytes from HF cache dir, target = pinned size.
    * Local registry path → bytes from registry meta, target = same
      (no download phase — model is already on disk before load).
    * Anything else (unknown HF id) → unknown sizes, generic name.
    """
    if target is None or target == DEFAULT_MODEL_HF_ID:
        return (
            STOCK_MODEL_DISPLAY_NAME,
            _dir_size_bytes(_hf_model_cache_dir()),
            STOCK_MODEL_TARGET_BYTES,
        )
    # Local path → look up registry entry by dir basename = slug.
    slug = Path(target).name
    model = TTSModel.find_by_slug(PATHS.models_root, slug)
    if model is not None:
        # Already on disk — cache_bytes == size_bytes always.
        return (model.name, model.size_bytes, model.size_bytes)
    # Unknown target — fall back to the raw target string.
    return (slug or target, 0, 0)


@router.get("/model-status", response_model=ModelStatusOut)
def model_status() -> ModelStatusOut:
    """Lightweight poll endpoint — frontend banner ticks this every ~2 s
    while waiting on first-render cold start so the user sees what's
    happening instead of staring at a hung spinner. Reports whichever
    model is currently loading or most-recently used."""
    tts = peek_tts()
    is_loaded = tts is not None and tts.is_loaded
    is_loading = tts is not None and tts.is_loading
    target = tts.model_id if tts is not None else None

    display_name, cache_bytes, target_bytes = _resolve_active(target)

    if is_loaded:
        state: ModelState = "ready"
        msg = None
    elif is_loading and target_bytes > 0 and cache_bytes >= int(target_bytes * 0.95):
        # Bytes already on disk (or close enough) — we're in the
        # CPU-to-GPU load phase, not network download.
        state = "loading"
        msg = f"Načítám {display_name} na GPU…"
    elif is_loading and cache_bytes > 0:
        state = "downloading"
        gb_done = cache_bytes / 1e9
        gb_total = target_bytes / 1e9 if target_bytes > 0 else 0
        if gb_total > 0:
            msg = f"Stahuji {display_name}… {gb_done:.1f} / {gb_total:.1f} GB"
        else:
            msg = f"Stahuji {display_name}…"
    elif is_loading:
        state = "downloading"
        msg = f"Připojuji se k HuggingFace pro {display_name}…"
    else:
        state = "unloaded"
        msg = None

    pct = (
        min(100.0, (cache_bytes / target_bytes) * 100.0)
        if target_bytes > 0 else 0.0
    )
    return ModelStatusOut(
        state=state,
        cache_bytes=cache_bytes,
        cache_target_bytes=target_bytes,
        percent=pct,
        message=msg,
        active_model=display_name if (is_loading or is_loaded) else None,
    )
