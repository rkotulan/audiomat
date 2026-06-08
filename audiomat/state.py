"""Process-wide singletons + shared helpers used across routers.

Holds the global TTS handle, render bookkeeping (queues / threads /
cancel flags), and the resolved AudiomatPaths. Routers import from here
instead of poking module globals on api.py — keeps the FastAPI app file
focused on wiring.
"""
from __future__ import annotations

import asyncio
import os
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
# Idle-unload tunables
# ----------------------------------------------------------------------------

# Background task in api.py wakes every IDLE_CHECK_INTERVAL_S and unloads
# the TTS model from GPU if it has been idle longer than IDLE_TIMEOUT_S.
# Both are env-overridable for ops tuning. 600 s default = 10 min — long
# enough that a user toggling between Preview tabs doesn't trigger a
# costly reload, short enough that an abandoned session releases VRAM
# for whatever else might want the GPU.
IDLE_TIMEOUT_S = int(os.environ.get("AUDIOMAT_TTS_IDLE_TIMEOUT", "600"))
IDLE_CHECK_INTERVAL_S = int(os.environ.get("AUDIOMAT_TTS_IDLE_CHECK_INTERVAL", "60"))


# ----------------------------------------------------------------------------
# Resolved library paths (single instance — env-driven)
# ----------------------------------------------------------------------------

PATHS = AudiomatPaths.default()


# ----------------------------------------------------------------------------
# TTS instance registry (multi-model, LRU-evicted)
# ----------------------------------------------------------------------------

# Up to MAX_LOADED OmniVoiceTTS instances can sit in VRAM simultaneously
# — one per "target" (an HF model id like "k2-fsa/OmniVoice" or a local
# checkpoint path like "/data/models/jezkova-v1"). When a load would push
# past the cap, the least-recently-used instance is unloaded first. The
# 12 GB RTX 5070 fits ~2 OmniVoice models comfortably; bump via
# AUDIOMAT_MAX_LOADED_MODELS if you have more headroom.
MAX_LOADED_MODELS = int(os.environ.get("AUDIOMAT_MAX_LOADED_MODELS", "2"))

# Key = normalized target (resolved absolute path for local dirs; raw HF
# id otherwise). Multiple voices that point at the same target share the
# same OmniVoiceTTS handle.
_TTS_INSTANCES: dict[str, OmniVoiceTTS] = {}
_TTS_LOCK = threading.Lock()


def _normalize_target(target: str) -> str:
    """Make the dict key stable: resolved-absolute for filesystem paths,
    verbatim for HF ids. Lets ``./models/x`` and ``/abs/models/x`` share
    one instance and prevents two HF ids that differ only by case from
    overlapping."""
    try:
        p = Path(target)
        if p.exists():
            return str(p.resolve())
    except (OSError, ValueError):
        pass
    return target


def _evict_lru_locked() -> None:
    """Caller MUST hold _TTS_LOCK. Picks the instance with the largest
    seconds_since_last_used and unloads it. Never-used instances (no
    load() / generate() yet) get evicted first since they cost nothing
    to lose."""
    if not _TTS_INSTANCES:
        return

    def _age(inst: OmniVoiceTTS) -> float:
        secs = inst.seconds_since_last_used()
        # None → infinitely stale, top of the eviction queue
        return float("inf") if secs is None else secs

    lru_key = max(_TTS_INSTANCES, key=lambda k: _age(_TTS_INSTANCES[k]))
    _TTS_INSTANCES[lru_key].unload()
    del _TTS_INSTANCES[lru_key]


def get_tts(
    target: str | None = None,
    revision: str | None = None,
    backend: str = "omnivoice",
) -> "OmniVoiceTTS | HiggsTTS":
    """Return (or create) the TTS instance for ``target``.

    * ``target=None`` → stock OmniVoice (DEFAULT_MODEL_ID + DEFAULT_REVISION
      from tts.py). Backwards-compatible with existing single-singleton
      callers. ``backend`` is forced to "omnivoice" in this branch.
    * ``target=<hf_id>`` → that HF model. ``revision`` pinned if given.
    * ``target=<local_path>`` → that on-disk checkpoint. ``revision`` is
      meaningless for local snapshots (always None passed to
      from_pretrained).
    * ``backend="higgs"`` → instantiate :class:`HiggsTTS` instead of the
      default :class:`OmniVoiceTTS`. Both expose the same generate()
      interface so the renderer doesn't branch on type.

    Lazy: instantiation is cheap (no model load yet); the actual weight
    load fires on first ``generate()`` call inside the instance.
    """
    # Resolve None → stock default with its pinned revision. Stock is
    # always the OmniVoice backend (Apache-2.0); a caller can't pick
    # higgs without first registering a model in the registry.
    if target is None:
        from audiomat.tts import DEFAULT_MODEL_ID, DEFAULT_REVISION
        target = DEFAULT_MODEL_ID
        if revision is None:
            revision = DEFAULT_REVISION
        backend = "omnivoice"

    key = _normalize_target(target)
    with _TTS_LOCK:
        inst = _TTS_INSTANCES.get(key)
        if inst is not None:
            return inst
        # New target → evict LRU if at capacity, then instantiate.
        while len(_TTS_INSTANCES) >= MAX_LOADED_MODELS:
            _evict_lru_locked()
        if backend == "higgs":
            from audiomat.tts_higgs import HiggsTTS
            inst = HiggsTTS(model_id=target, model_revision=revision)
        else:
            inst = OmniVoiceTTS(model_id=target, model_revision=revision)
        _TTS_INSTANCES[key] = inst
        return inst


def peek_tts(target: str | None = None) -> OmniVoiceTTS | None:
    """Return an existing instance without instantiating one.

    * ``target=None`` → the most-recently-used instance (or the one
      currently loading, if any). Used by /system/model-status so the
      UI can show progress for whatever is happening right now.
    * ``target=<value>`` → that specific instance, or None.

    Never triggers a load.
    """
    if target is not None:
        return _TTS_INSTANCES.get(_normalize_target(target))
    if not _TTS_INSTANCES:
        return None
    # Loading > MRU > anything. UI cares most about "what's loading right now".
    loading = [i for i in _TTS_INSTANCES.values() if i.is_loading]
    if loading:
        return loading[0]
    # MRU = smallest seconds_since_last_used. None means "never used" →
    # park behind any used instance.
    return min(
        _TTS_INSTANCES.values(),
        key=lambda i: i.seconds_since_last_used() if i.seconds_since_last_used() is not None else float("inf"),
    )


def peek_all_tts() -> list[OmniVoiceTTS]:
    """Snapshot of all currently-registered TTS instances. Used by the
    idle-unload loop + admin endpoints. Caller must not mutate the list."""
    return list(_TTS_INSTANCES.values())


def get_tts_for_voice(voice) -> "OmniVoiceTTS | HiggsTTS":  # type: ignore[no-untyped-def]
    """Resolve a voice's ``tts_model`` field through the model registry
    and return the matching TTS instance. Fall back to stock OmniVoice
    if the slug is None / empty / "default" — or if the registered slug
    has been deleted since the voice was created (graceful degradation:
    user loses fine-tune-specific quality but renders still work).

    v0.4: looks up the registered ``TTSModel`` to discover the backend
    (omnivoice vs higgs), so a voice pointing at a Higgs registry entry
    instantiates :class:`HiggsTTS` instead of :class:`OmniVoiceTTS`.
    """
    from audiomat.model_registry import (
        DEFAULT_MODEL_SLUG,
        TTSModel,
    )
    tts_model_slug = getattr(voice, "tts_model", None)
    if not tts_model_slug or tts_model_slug == DEFAULT_MODEL_SLUG:
        return get_tts(target=None)

    model = TTSModel.find_by_slug(PATHS.models_root, tts_model_slug)
    if model is None:
        # Registered model went missing — log + fall back to stock so
        # the user isn't stuck staring at an error on a render they
        # didn't expect to break.
        import logging
        logging.getLogger("audiomat.state").warning(
            "voice %r references missing tts_model %r — falling back to stock",
            getattr(voice, "name_slug", "?"), tts_model_slug,
        )
        return get_tts(target=None)

    target = model.from_pretrained_target
    # ``revision`` only applies to HF-sourced models; local snapshots
    # have nothing to pin against. The TTS backends' from_pretrained
    # paths accept ``None`` and just follow the local dir.
    revision = model.hf_revision if model.source_type == "hf" else None
    return get_tts(target=target, revision=revision, backend=model.backend)


def clear_tts(target: str | None = None) -> None:
    """Unload one or all instances.

    * ``target=None`` → unload everything (lifespan shutdown).
    * ``target=<value>`` → unload that specific instance (manual evict).

    Idempotent.
    """
    with _TTS_LOCK:
        if target is None:
            for inst in _TTS_INSTANCES.values():
                inst.unload()
            _TTS_INSTANCES.clear()
            return
        key = _normalize_target(target)
        inst = _TTS_INSTANCES.pop(key, None)
        if inst is not None:
            inst.unload()


async def idle_unload_loop(
    timeout_s: int = IDLE_TIMEOUT_S,
    interval_s: int = IDLE_CHECK_INTERVAL_S,
) -> None:
    """Background task: every ``interval_s``, unload any TTS instance
    that has been idle for ``timeout_s`` seconds. Walks every entry in
    the registry — each model is evaluated independently so a fresh
    fine-tune doesn't get evicted just because the stock model went
    cold.

    Skips unload while a render is in flight (any RENDER_THREADS entry
    is alive) — wouldn't be safe and would waste a reload anyway, since
    the renderer would re-trigger a load on the very next chunk.

    Env tunables (read at module import time):
      * ``AUDIOMAT_TTS_IDLE_TIMEOUT`` (default 600 s = 10 min)
      * ``AUDIOMAT_TTS_IDLE_CHECK_INTERVAL`` (default 60 s)
    """
    while True:
        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            return
        if any(t.is_alive() for t in RENDER_THREADS.values()):
            continue
        # Snapshot the keys outside the lock — we hold the lock only for
        # the actual unload, since OmniVoiceTTS.unload() can block on
        # torch.cuda.empty_cache().
        with _TTS_LOCK:
            to_evict = [
                key for key, inst in _TTS_INSTANCES.items()
                if inst.is_loaded
                and (inst.seconds_since_last_used() or 0) >= timeout_s
            ]
            for key in to_evict:
                _TTS_INSTANCES[key].unload()
                del _TTS_INSTANCES[key]


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
    try:
        return Project.load(slug)
    except FileNotFoundError:
        raise HTTPException(404, f"project not found: {slug}")


def book_blocks(proj: Project) -> list[Block]:
    """Parse the project's book file into a Block list. EPUB goes through
    parse_epub; TXT is wrapped into a single Block. Per-chapter text
    overrides (see :mod:`audiomat.overrides`) are merged in transparently
    so downstream code (renderer / chunker / preview) sees one source of
    truth. Shared by /chapters, /preview, and /render — all of which need
    the same parse semantics.
    """
    if proj.book.filename.endswith(".epub"):
        _meta, blocks = parse_epub(proj.book_path)
    else:
        text = proj.book_path.read_text(encoding="utf-8")
        blocks = [Block(text=text, sentences=split_sentences(text))]
    # Local import to dodge a state→overrides→state cycle (overrides
    # only depends on epub, not on state).
    from audiomat.overrides import apply_overrides
    return apply_overrides(blocks, proj.dir)


def wav_duration_s(path: Path) -> float:
    """Quick WAV duration probe via the wave module (header-only, no
    decode). Used by /chapters and /preview to populate the UI."""
    import wave
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()
