"""Process-wide singletons + shared helpers used across routers.

Holds the global TTS handle, render bookkeeping (queues / threads /
cancel flags), and the resolved AudiomatPaths. Routers import from here
instead of poking module globals on api.py — keeps the FastAPI app file
focused on wiring.
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from audiomat.epub import Block, parse_epub, split_sentences
from audiomat.paths import AudiomatPaths
from audiomat.project import Project
from audiomat.tts import OmniVoiceTTS


# ----------------------------------------------------------------------------
# Resolved library paths (single instance — env-driven)
# ----------------------------------------------------------------------------

PATHS = AudiomatPaths.default()


# ----------------------------------------------------------------------------
# TTS singleton
# ----------------------------------------------------------------------------

_TTS: OmniVoiceTTS | None = None
_TTS_LOCK = threading.Lock()


def get_tts() -> OmniVoiceTTS:
    """Return the singleton TTS instance (lazy init under a lock).

    The lock prevents two concurrent first-render requests from each
    constructing their own OmniVoiceTTS — a TOCTOU race that would
    duplicate the HF download and waste GPU memory.
    """
    global _TTS
    if _TTS is None:
        with _TTS_LOCK:
            if _TTS is None:
                _TTS = OmniVoiceTTS()
    return _TTS


def peek_tts() -> OmniVoiceTTS | None:
    """Return the TTS singleton without instantiating it. Used by the
    /system/model-status endpoint, which must not trigger a model load."""
    return _TTS


def clear_tts() -> None:
    """Drop the TTS singleton (and its model). Idempotent. Used by the
    lifespan shutdown hook + the idle-unload background task."""
    global _TTS
    if _TTS is not None:
        _TTS.unload()
        _TTS = None


# ----------------------------------------------------------------------------
# Render bookkeeping
# ----------------------------------------------------------------------------

# One asyncio.Queue per active render (consumed by the SSE /progress
# endpoint). The render worker thread pushes ProgressEvent objects via
# asyncio.run_coroutine_threadsafe.
RENDER_QUEUES: dict[str, asyncio.Queue] = {}

# Render worker threads, keyed by project slug. We check is_alive() to
# refuse a duplicate /render call while one is already running.
RENDER_THREADS: dict[str, threading.Thread] = {}

# Per-project cancellation flag — POST /cancel-render sets it; the worker
# thread checks between yielded events and bails out cleanly.
RENDER_CANCEL: dict[str, threading.Event] = {}


# ----------------------------------------------------------------------------
# Shared route helpers
# ----------------------------------------------------------------------------


def dataclass_to_dict(obj: Any) -> dict:
    """asdict wrapper used by Pydantic schema constructors."""
    return asdict(obj)


def load_project_or_404(slug: str) -> Project:
    """Load a Project by slug or raise 404. Centralized so every router
    that takes ``{slug}`` returns the same error shape."""
    target = PATHS.project_dir(slug)
    if not (target / "config.json").exists():
        raise HTTPException(404, f"project not found: {slug}")
    return Project.load(target)


def book_blocks(proj: Project) -> list[Block]:
    """Parse the project's book file into a Block list. EPUB goes through
    parse_epub; TXT is wrapped into a single Block. Shared by /chapters,
    /preview, and /render — all of which need the same parse semantics.
    """
    if proj.book.filename.endswith(".epub"):
        _meta, blocks = parse_epub(proj.book_path)
        return blocks
    text = proj.book_path.read_text(encoding="utf-8")
    return [Block(text=text, sentences=split_sentences(text))]


def wav_duration_s(path: Path) -> float:
    """Quick WAV duration probe via the wave module (header-only, no
    decode). Used by /chapters and /preview to populate the UI."""
    import wave
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()
