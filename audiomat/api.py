"""FastAPI app for audiomat.

Exposes voice library + project management + SSE-streamed render progress.
Mounts the built frontend (``frontend/dist``) at ``/`` if present, so a
single Docker image serves both API and UI on one port.

Run for local dev:

    uvicorn audiomat.api:app --reload --host 0.0.0.0 --port 8000

The Vite dev server proxies ``/api`` → ``:8000`` (see
``frontend/vite.config.ts``).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from audiomat.audio import (
    M4BMetadata,
    build_m4b,
    convert_voice_ref,
)
from audiomat.epub import parse_epub
from audiomat.num2text import normalize_lang
from audiomat.paths import AudiomatPaths
from audiomat.project import (
    BookInfo,
    Project,
    ProjectStatus,
    RenderParams,
)
from audiomat.render import ProgressEvent, ProjectRenderer
from audiomat.slug import chapter_stem as compute_chapter_stem, slugify
from audiomat.tts import OmniVoiceTTS
from audiomat.voice import Voice


# ----------------------------------------------------------------------------
# App + global state
# ----------------------------------------------------------------------------


PATHS = AudiomatPaths.default()
_TTS: OmniVoiceTTS | None = None
_RENDER_QUEUES: dict[str, asyncio.Queue] = {}
_RENDER_THREADS: dict[str, threading.Thread] = {}
# Per-project cancellation flag — POST /cancel-render sets it; the
# worker thread checks between yielded events and bails out cleanly.
_RENDER_CANCEL: dict[str, threading.Event] = {}


def _get_tts() -> OmniVoiceTTS:
    """Return the singleton TTS instance. Model is lazy-loaded inside."""
    global _TTS
    if _TTS is None:
        _TTS = OmniVoiceTTS()
    return _TTS


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Set up runtime dirs on startup; tear down resources on shutdown."""
    PATHS.ensure_dirs()
    yield
    # On shutdown, drop the model to free GPU
    if _TTS is not None:
        _TTS.unload()


app = FastAPI(
    title="audiomat",
    version="0.1.0",
    description="Convert eBooks into audiobooks with cloned voices.",
    lifespan=lifespan,
)


# Vite dev server proxy is the production path, but allow direct CORS
# from :5173 for cases where someone runs both servers without proxy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------------------------------------------------------
# Pydantic response models
# ----------------------------------------------------------------------------


class VoiceOut(BaseModel):
    name: str
    name_slug: str
    duration_s: float
    sample_rate: int
    channels: int
    transcript_chars: int
    notes: str
    created: str

    @classmethod
    def from_voice(cls, v: Voice) -> "VoiceOut":
        return cls(
            name=v.name, name_slug=v.name_slug,
            duration_s=v.duration_s, sample_rate=v.sample_rate,
            channels=v.channels, transcript_chars=v.transcript_chars,
            notes=v.notes, created=v.created,
        )


class ProjectOut(BaseModel):
    name: str
    name_slug: str
    book: dict
    voice_ref: str
    voice_ref_slug: str
    params: dict
    status: dict
    created: str
    last_run: str
    has_final_m4b: bool

    @classmethod
    def from_project(cls, p: Project) -> "ProjectOut":
        return cls(
            name=p.name, name_slug=p.name_slug,
            book=_dataclass_to_dict(p.book),
            voice_ref=p.voice_ref, voice_ref_slug=p.voice_ref_slug,
            params=_dataclass_to_dict(p.params),
            status=_dataclass_to_dict(p.status),
            created=p.created, last_run=p.last_run,
            has_final_m4b=p.final_path.exists(),
        )


def _dataclass_to_dict(obj: Any) -> dict:
    """asdict without importing dataclasses here — keeps the boundary clean."""
    from dataclasses import asdict
    return asdict(obj)


# ----------------------------------------------------------------------------
# System / model status
# ----------------------------------------------------------------------------


# Measured size of the k2-fsa/OmniVoice snapshot on disk (~3.27 GB across
# 13 files, blob layout). Used as the denominator for the "downloading"
# progress %. Slight under/overshoot is fine — UI clamps to 100.
MODEL_TARGET_BYTES = 3_300_000_000

ModelState = Literal["unloaded", "downloading", "loading", "ready"]


class ModelStatusOut(BaseModel):
    state: ModelState
    cache_bytes: int
    cache_target_bytes: int
    percent: float
    message: str | None = None


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


@app.get("/api/system/model-status", response_model=ModelStatusOut)
def model_status() -> ModelStatusOut:
    """Lightweight poll endpoint — frontend banner ticks this every ~2 s
    while waiting on first-render cold start so the user sees what's
    happening instead of staring at a hung spinner."""
    cache_bytes = _dir_size_bytes(_hf_model_cache_dir())
    is_loaded = _TTS is not None and _TTS.is_loaded
    is_loading = _TTS is not None and _TTS.is_loading

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


# ----------------------------------------------------------------------------
# Voice endpoints
# ----------------------------------------------------------------------------


# ---- specific voice routes (must come BEFORE /api/voices/{slug} catchall) ----

class TranscribeRequest(BaseModel):
    audio_path: str       # path produced by /api/voices/draft-upload
    language: str = "cs"


@app.get("/api/voices", response_model=list[VoiceOut])
def list_voices():
    return [VoiceOut.from_voice(v) for v in Voice.list_all(PATHS.voices_root)]


@app.post("/api/voices/auto-transcribe")
def auto_transcribe(req: TranscribeRequest):
    """Run faster-whisper on a previously-uploaded audio file. Returns the
    draft transcript so the UI can show it for user editing before save.
    Heavy lift — first call downloads ~1.5 GB whisper-medium."""
    from audiomat.transcribe import transcribe
    path = Path(req.audio_path)
    if not path.exists():
        raise HTTPException(404, f"audio not found: {req.audio_path}")
    try:
        text = transcribe(path, language=req.language)
    except Exception as e:
        raise HTTPException(500, f"transcribe failed: {type(e).__name__}: {e}")
    return {"transcript": text}


@app.get("/api/voices/draft-audio")
def draft_audio(path: str):
    """Serve a previously-uploaded staged voice WAV so the UI can play it
    back during the review stage. Security: only serves files inside an
    ``audiomat_voice_*`` tempdir (created by /draft-upload). Any other
    path is 404'd to prevent arbitrary filesystem reads.

    NOTE: this route MUST be registered before ``/api/voices/{slug}``,
    otherwise FastAPI matches the path-suffix as a slug — a nasty silent
    routing bug we hit during v0.0.1 testing.
    """
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, f"draft audio not found at {p}")
    if not p.parent.name.startswith("audiomat_voice_"):
        raise HTTPException(403, f"path outside staging area: parent={p.parent.name!r}")
    return FileResponse(p, media_type="audio/wav")


@app.post("/api/voices/draft-upload")
async def draft_voice_upload(audio: UploadFile = File(...)):
    """Stage 1 of voice creation: upload + ffmpeg-convert to 24 kHz mono.
    Returns the temp path + audio info so the UI can preview / auto-
    transcribe before final commit. Caller must call /api/voices to finalize.
    """
    suffix = Path(audio.filename or "voice").suffix or ".wav"
    tmpdir = Path(tempfile.mkdtemp(prefix="audiomat_voice_"))
    raw_path = tmpdir / f"raw{suffix}"
    converted_path = tmpdir / "voice.wav"

    with raw_path.open("wb") as f:
        shutil.copyfileobj(audio.file, f)

    try:
        info = convert_voice_ref(raw_path, converted_path)
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(400, f"audio conversion failed: {e}")
    finally:
        raw_path.unlink(missing_ok=True)

    if info.duration_s > 20:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(400,
            f"voice ref is {info.duration_s:.1f}s (>20s) — OmniVoice degrades "
            f"and is 2.6× slower per chunk above 20s. Trim to 5–10s.")

    return {
        "audio_path": str(converted_path),
        "duration_s": round(info.duration_s, 3),
        "sample_rate": info.sample_rate,
        "channels": info.channels,
        "warning": (
            f"voice ref is {info.duration_s:.1f}s — OmniVoice recommends 3–10s."
            if info.duration_s > 15 else ""
        ),
    }


@app.post("/api/voices", response_model=VoiceOut)
async def create_voice(
    name: str = Form(...),
    audio_path: str = Form(...),
    transcript: str = Form(...),
    notes: str = Form(""),
    overwrite: bool = Form(False),
):
    """Stage 2 of voice creation: commit a previously-uploaded audio +
    transcript into the library. ``audio_path`` comes from
    ``/api/voices/draft-upload``."""
    if not name.strip():
        raise HTTPException(400, "name is required")
    if not transcript.strip():
        raise HTTPException(400, "transcript is required")

    src = Path(audio_path)
    if not src.exists():
        raise HTTPException(404, f"audio_path not found: {audio_path}")

    from audiomat.audio import probe_wav
    info = probe_wav(src)

    try:
        voice = Voice.create(
            voices_root=PATHS.voices_root,
            name=name.strip(),
            wav_src=src,
            transcript_text=transcript,
            duration_s=info.duration_s,
            sample_rate=info.sample_rate,
            channels=info.channels,
            notes=notes,
            overwrite=overwrite,
        )
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    finally:
        # Clean up the staging tmpdir
        if src.parent.name.startswith("audiomat_voice_"):
            shutil.rmtree(src.parent, ignore_errors=True)

    return VoiceOut.from_voice(voice)


@app.get("/api/voices/{slug}", response_model=VoiceOut)
def get_voice(slug: str):
    target = PATHS.voice_dir(slug)
    if not (target / "meta.json").exists():
        raise HTTPException(404, f"voice not found: {slug}")
    return VoiceOut.from_voice(Voice.load(target))


@app.delete("/api/voices/{slug}")
def delete_voice(slug: str):
    target = PATHS.voice_dir(slug)
    if not target.exists():
        raise HTTPException(404, f"voice not found: {slug}")
    # Refuse delete if any project references this voice
    referencing = [p.name for p in Project.list_all(PATHS.projects_root)
                   if p.voice_ref_slug == slug]
    if referencing:
        raise HTTPException(409,
            f"voice is used by {len(referencing)} project(s): {', '.join(referencing)}")
    Voice.load(target).delete()
    return {"deleted": slug}


@app.get("/api/voices/{slug}/audio")
def voice_audio(slug: str):
    """Serve voice.wav inline so the UI's <audio controls> can play it.
    No filename= → no Content-Disposition: attachment → browser plays
    instead of downloading. The frontend uses <a download> on a separate
    button to force download."""
    target = PATHS.voice_dir(slug) / "voice.wav"
    if not target.exists():
        raise HTTPException(404, "voice.wav not found")
    return FileResponse(target, media_type="audio/wav")


# ----------------------------------------------------------------------------
# Project endpoints
# ----------------------------------------------------------------------------


@app.get("/api/projects", response_model=list[ProjectOut])
def list_projects():
    return [ProjectOut.from_project(p) for p in Project.list_all(PATHS.projects_root)]


@app.get("/api/projects/{slug}", response_model=ProjectOut)
def get_project(slug: str):
    target = PATHS.project_dir(slug)
    if not (target / "config.json").exists():
        raise HTTPException(404, f"project not found: {slug}")
    return ProjectOut.from_project(Project.load(target))


@app.post("/api/projects", response_model=ProjectOut)
async def create_project(
    name: str = Form(...),
    voice_ref: str = Form(...),
    book: UploadFile = File(...),
    overwrite: bool = Form(False),
):
    """Create a new project. Parses EPUB metadata to populate book info."""
    if not name.strip():
        raise HTTPException(400, "name is required")
    voice = Voice.find_by_name(PATHS.voices_root, voice_ref)
    if voice is None:
        raise HTTPException(404, f"voice not found: {voice_ref}")

    # Stage book to a temp file first so we can probe metadata
    suffix = Path(book.filename or "book.epub").suffix.lower() or ".epub"
    tmpdir = Path(tempfile.mkdtemp(prefix="audiomat_book_"))
    book_tmp = tmpdir / f"book{suffix}"
    with book_tmp.open("wb") as f:
        shutil.copyfileobj(book.file, f)

    book_meta: dict[str, Any] = {}
    if suffix == ".epub":
        try:
            meta, blocks = parse_epub(book_tmp)
            book_meta = {
                "blocks_total": len(blocks),
                "blocks_skipped": _auto_skip_indices(blocks),
                "title": meta.title,
                "author": meta.author,
                "language": meta.language,
            }
        except Exception as e:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise HTTPException(400, f"EPUB parse failed: {e}")

    try:
        proj = Project.create(
            projects_root=PATHS.projects_root,
            name=name.strip(),
            book_src=book_tmp,
            voice_name=voice.name,
            voice_slug=voice.name_slug,
            book_meta=book_meta,
            overwrite=overwrite,
        )
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return ProjectOut.from_project(proj)


class BlocksSkippedRequest(BaseModel):
    indices: list[int]


def _renderable_stems(proj: Project) -> set[str]:
    """Compute the set of valid per-chapter stems for the project given
    its current blocks_skipped. Used by the orphan-cleanup pass after
    blocks_skipped changes."""
    blocks = _book_blocks(proj)
    skip = set(proj.book.blocks_skipped or ())
    valid: set[str] = set()
    one_idx = 0
    for block_idx, block in enumerate(blocks):
        if block_idx in skip or not getattr(block, "keep", True):
            continue
        one_idx += 1
        leading = block.text or (block.sentences[0] if block.sentences else "")
        valid.add(f"{one_idx:03d}_{compute_chapter_stem(leading)}")
    return valid


def _prune_orphan_chunks(proj: Project) -> int:
    """Remove per-chapter dirs in <project>/chunks/ whose stem doesn't
    match the current renderable list. Called after blocks_skipped
    changes — when the user skips block 0, every later renderable index
    shifts (1→0, 2→1, …), and the dirs they used to live in become
    orphans. build_m4b's alphabetical glob would otherwise pick them
    up, so we shred them eagerly.

    Returns the number of dirs removed.
    """
    if not proj.chunks_dir.exists():
        return 0
    valid = _renderable_stems(proj)
    removed = 0
    for child in proj.chunks_dir.iterdir():
        if not child.is_dir():
            continue
        if child.name in valid:
            continue
        # Skip non-chapter helpers (none today, but future-proof).
        if child.name.startswith("_"):
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed += 1
    return removed


@app.patch("/api/projects/{slug}/blocks-skipped")
def update_blocks_skipped(slug: str, req: BlocksSkippedRequest):
    """Replace project.book.blocks_skipped + auto-prune any chunks dirs
    that no longer match the new renderable list.

    Removed dir count is logged into render_log.txt and returned in the
    response under ``orphans_removed`` (added field beyond ProjectOut)."""
    proj = _load_project_or_404(slug)
    proj.book.blocks_skipped = sorted(set(req.indices))
    proj.save()
    pruned = _prune_orphan_chunks(proj)
    if pruned > 0:
        proj.append_log(f"pruned {pruned} orphan chapter dir(s) after blocks_skipped change")
    out = ProjectOut.from_project(proj).model_dump()
    out["orphans_removed"] = pruned
    return out


@app.patch("/api/projects/{slug}/params")
def update_project_params(slug: str, params: dict):
    """Update render params (preview matrix selection, advanced edits).

    Voice change is **not** allowed here — invalidates whole cache;
    a separate endpoint will handle that with explicit confirmation."""
    proj = _load_project_or_404(slug)
    current = _dataclass_to_dict(proj.params)
    current.update(params)
    proj.params = RenderParams(**{k: current.get(k) for k in current
                                   if k in RenderParams.__dataclass_fields__})
    proj.save()
    return ProjectOut.from_project(proj)


@app.delete("/api/projects/{slug}")
def delete_project(slug: str):
    target = PATHS.project_dir(slug)
    if not target.exists():
        raise HTTPException(404, f"project not found: {slug}")
    Project.load(target).delete()
    if slug in _RENDER_QUEUES:
        _RENDER_QUEUES.pop(slug, None)
    return {"deleted": slug}


def _load_project_or_404(slug: str) -> Project:
    target = PATHS.project_dir(slug)
    if not (target / "config.json").exists():
        raise HTTPException(404, f"project not found: {slug}")
    return Project.load(target)


# ----------------------------------------------------------------------------
# Render endpoints — POST /render starts; GET /progress streams SSE
# ----------------------------------------------------------------------------


# ---- preview matrix --------------------------------------------------------

PREVIEW_MATRIX = [
    {"label": "Fast",     "num_step": 32, "guidance_scale": 2.0, "speed": 1.0},
    {"label": "Balanced", "num_step": 48, "guidance_scale": 2.0, "speed": 1.0},
    {"label": "Crisp",    "num_step": 48, "guidance_scale": 2.5, "speed": 1.0},
    {"label": "Stable",   "num_step": 64, "guidance_scale": 2.0, "speed": 1.0},
]


# Patterns that mark a block as DRM / copyright / metadata noise rather
# than book prose. Matched case-insensitively as substrings. Common Czech
# Palmknihy / nakladatel watermark phrases land here. Add new patterns
# only when you've seen them clobber a real preview.
_METADATA_PATTERNS = (
    # Czech CZ-ebook DRM watermarks
    "palmknihy",
    "kupující",
    "kupujici",
    "kniha je určena",
    "kniha jako celek",
    "neoprávněn",
    "elektronických knih",
    "autorského práva",
    "trestního zákoníku",
    # Generic copyright / imprint markers (CS + EN)
    "copyright",
    "©",
    "isbn",
    "all rights reserved",
    "published by",
    "published in",
    "published in agreement",
    "translation ©",
    "překlad ©",
    "vydalo nakladatelství",
)


def _auto_skip_indices(blocks: list, max_scan: int = 10) -> list[int]:
    """Scan the first ``max_scan`` blocks and return indices that look
    like front-matter / DRM watermarks. Used by /projects POST to
    pre-populate ``book.blocks_skipped`` on creation, so the user
    doesn't have to manually deselect the Palmknihy notice / publisher
    imprint before the first render.

    Conservative: only first N blocks scanned (front-matter is at the
    start), only flagged if a metadata pattern matches. Body chapters
    that legitimately reference copyright (e.g. footnotes) survive.
    """
    out: list[int] = []
    for i, b in enumerate(blocks[:max_scan]):
        text = " ".join(b.sentences).strip() if b.sentences else (b.text or "")
        if _is_metadata_block(text):
            out.append(i)
    return out


def _is_metadata_block(text: str) -> bool:
    lower = text.lower()
    return any(pat in lower for pat in _METADATA_PATTERNS)


def _pick_sample_text(
    blocks: list,
    blocks_skipped: list[int] | tuple[int, ...] = (),
    max_chars: int = 600,
) -> tuple[str, int] | None:
    """Return (sample_text, source_block_index) — a representative prose
    excerpt for the preview matrix.

    Strategy:

    1. Filter out blocks the project tagged in ``blocks_skipped`` (typically
       cover, copyright, TOC).
    2. Start search at ~33 % into the remaining blocks. This skips most
       front-matter that's not explicitly tagged (Palmknihy DRM watermark,
       publisher imprint, dedication, foreword) and lands somewhere in the
       actual narrative.
    3. Pick the first block ≥ 300 chars whose text doesn't trip the
       metadata-pattern blocklist (publisher / DRM phrases).
    4. Fall back to "any ≥ 300 char block from the same point on" if the
       blocklist is too aggressive.
    5. Final fallback: scan from the very start (ignoring all heuristics).
    """
    if not blocks:
        return None
    skip = set(blocks_skipped or ())
    available = [(i, b) for i, b in enumerate(blocks) if i not in skip]
    if not available:
        return None

    start = max(0, len(available) // 3)

    # Pass 1 — middle-onward, blocklist-clean
    for orig_idx, b in available[start:]:
        joined = " ".join(b.sentences).strip()
        if len(joined) >= 300 and not _is_metadata_block(joined):
            return joined[:max_chars], orig_idx

    # Pass 2 — middle-onward, allow metadata text (last resort within search range)
    for orig_idx, b in available[start:]:
        joined = " ".join(b.sentences).strip()
        if len(joined) >= 300:
            return joined[:max_chars], orig_idx

    # Pass 3 — anywhere in the book
    for orig_idx, b in available:
        joined = " ".join(b.sentences).strip()
        if len(joined) >= 300:
            return joined[:max_chars], orig_idx

    return None


@app.post("/api/projects/{slug}/preview-matrix")
def preview_matrix(slug: str):
    """Render the 4 default preview variants on a representative sample
    from the project's book and stream cell-by-cell progress as SSE.

    Events emitted:

    * ``started`` — header (sample text + totals + cell count)
    * ``cell_done`` — one variant finished (index + full variant payload)
    * ``complete`` — all variants done, full ``variants`` array
    * ``error`` — generation failed mid-stream

    Cached per (text, num_step, gs, speed, voice) under
    ``<project>/previews/``. Cache hits stream out instantly; misses
    take ~5 s per cell on RTX 5070.
    """
    import hashlib
    import json as _json
    import time
    import soundfile as sf
    from audiomat.headers import prepare_for_tts

    # Validate up front — these raise HTTPException synchronously, so the
    # client gets a 4xx without entering the SSE stream and seeing an
    # "error" event mid-flight.
    proj = _load_project_or_404(slug)
    voice = Voice.find_by_name(PATHS.voices_root, proj.voice_ref)
    if voice is None:
        raise HTTPException(404, f"voice not found: {proj.voice_ref}")
    if not proj.book_path.exists():
        raise HTTPException(400, f"book file missing: {proj.book_path}")

    if proj.book.filename.endswith(".epub"):
        _meta, blocks = parse_epub(proj.book_path)
    else:
        from audiomat.epub import Block, split_sentences
        text = proj.book_path.read_text(encoding="utf-8")
        blocks = [Block(text=text, sentences=split_sentences(text))]

    picked = _pick_sample_text(blocks, blocks_skipped=proj.book.blocks_skipped)
    if picked is None:
        raise HTTPException(400, "no block ≥ 300 chars found in book — preview needs prose")
    sample_text, sample_block_index = picked

    previews_dir = proj.dir / "previews"
    previews_dir.mkdir(exist_ok=True)
    # EPUB DC metadata uses BCP 47 (cs-CZ); OmniVoice + num2words want
    # ISO 639-1 (cs) — strip region suffix at the boundary.
    language = normalize_lang(proj.book.language or "cs")
    clean = prepare_for_tts(sample_text, lang=language)
    ref_text = voice.transcript()
    ref_audio = str(voice.wav_path)
    total_book_chars = _total_book_chars(blocks, proj.book.blocks_skipped)

    def event_gen():
        tts = _get_tts()
        tts.load()
        sr = tts.sample_rate

        yield {
            "event": "started",
            "data": _json.dumps({
                "total": len(PREVIEW_MATRIX),
                "sample_text": clean,
                "sample_chars": len(clean),
                "sample_block_index": sample_block_index,
                "sample_block_total": len(blocks),
                "total_book_chars": total_book_chars,
            }),
        }

        results: list[dict] = []
        for idx, v in enumerate(PREVIEW_MATRIX):
            try:
                key_src = (
                    f"{clean}|{v['num_step']}|{v['guidance_scale']}"
                    f"|{v['speed']}|{voice.name_slug}"
                )
                key = hashlib.md5(key_src.encode("utf-8")).hexdigest()[:16]
                wav_path = previews_dir / f"{v['label']}_{key}.wav"

                if wav_path.exists() and wav_path.stat().st_size > 1024:
                    cell = {
                        **v,
                        "audio_url": f"/api/projects/{slug}/preview-audio/{wav_path.name}",
                        "cached": True,
                        "gen_seconds": 0.0,
                        "duration_s": _wav_duration_s(wav_path),
                    }
                else:
                    t0 = time.time()
                    audios = tts._model.generate(
                        text=clean,
                        language=language,
                        ref_text=ref_text,
                        ref_audio=ref_audio,
                        num_step=v["num_step"],
                        guidance_scale=v["guidance_scale"],
                        speed=v["speed"],
                    )
                    gen_s = time.time() - t0
                    sf.write(str(wav_path), audios[0], sr, subtype="PCM_16")
                    cell = {
                        **v,
                        "audio_url": f"/api/projects/{slug}/preview-audio/{wav_path.name}",
                        "cached": False,
                        "gen_seconds": round(gen_s, 2),
                        "duration_s": round(audios[0].shape[-1] / sr, 2),
                    }
            except Exception as e:
                yield {
                    "event": "error",
                    "data": _json.dumps({
                        "index": idx,
                        "label": v["label"],
                        "message": str(e),
                    }),
                }
                return

            results.append(cell)
            yield {
                "event": "cell_done",
                "data": _json.dumps({"index": idx, "variant": cell}),
            }

        yield {
            "event": "complete",
            "data": _json.dumps({"variants": results}),
        }

    return EventSourceResponse(event_gen())


def _total_book_chars(blocks: list, blocks_skipped: list[int] | tuple[int, ...]) -> int:
    """Sum of joined-sentence chars across renderable blocks. Used to
    estimate full-book render wall-time given a per-variant gen rate."""
    skip = set(blocks_skipped or ())
    total = 0
    for i, b in enumerate(blocks):
        if i in skip or not getattr(b, "keep", True):
            continue
        total += len(" ".join(b.sentences).strip())
    return total


def _wav_duration_s(path: Path) -> float:
    import wave
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


class PreviewCustomRequest(BaseModel):
    num_step: int = 48
    guidance_scale: float = 2.0
    speed: float = 1.0


@app.post("/api/projects/{slug}/preview-custom")
def preview_custom(slug: str, req: PreviewCustomRequest):
    """Render ONE preview sample with the user-supplied params (NOT
    persisted to project.params). Lets the user A/B custom slider values
    before committing. Cached per (text, params, voice_slug)."""
    import hashlib
    import time
    import soundfile as sf
    from audiomat.headers import prepare_for_tts

    proj = _load_project_or_404(slug)
    voice = Voice.find_by_name(PATHS.voices_root, proj.voice_ref)
    if voice is None:
        raise HTTPException(404, f"voice not found: {proj.voice_ref}")
    if not proj.book_path.exists():
        raise HTTPException(400, f"book file missing: {proj.book_path}")

    if proj.book.filename.endswith(".epub"):
        _meta, blocks = parse_epub(proj.book_path)
    else:
        from audiomat.epub import Block, split_sentences
        text = proj.book_path.read_text(encoding="utf-8")
        blocks = [Block(text=text, sentences=split_sentences(text))]

    picked = _pick_sample_text(blocks, blocks_skipped=proj.book.blocks_skipped)
    if picked is None:
        raise HTTPException(400, "no block ≥ 300 chars found in book — preview needs prose")
    sample_text, sample_block_index = picked

    previews_dir = proj.dir / "previews"
    previews_dir.mkdir(exist_ok=True)
    language = normalize_lang(proj.book.language or "cs")
    clean = prepare_for_tts(sample_text, lang=language)
    total_book_chars = _total_book_chars(blocks, proj.book.blocks_skipped)

    key_src = f"{clean}|{req.num_step}|{req.guidance_scale}|{req.speed}|{voice.name_slug}"
    key = hashlib.md5(key_src.encode("utf-8")).hexdigest()[:16]
    wav_path = previews_dir / f"Custom_{key}.wav"

    base = {
        "num_step": req.num_step,
        "guidance_scale": req.guidance_scale,
        "speed": req.speed,
        "sample_text": clean,
        "sample_chars": len(clean),
        "sample_block_index": sample_block_index,
        "sample_block_total": len(blocks),
        "total_book_chars": total_book_chars,
        "audio_url": f"/api/projects/{slug}/preview-audio/{wav_path.name}",
    }

    if wav_path.exists() and wav_path.stat().st_size > 1024:
        return {**base, "cached": True, "gen_seconds": 0.0,
                "duration_s": _wav_duration_s(wav_path)}

    tts = _get_tts()
    tts.load()
    sr = tts.sample_rate

    t0 = time.time()
    audios = tts._model.generate(
        text=clean,
        language=language,
        ref_text=voice.transcript(),
        ref_audio=str(voice.wav_path),
        num_step=req.num_step,
        guidance_scale=req.guidance_scale,
        speed=req.speed,
    )
    gen_s = time.time() - t0
    sf.write(str(wav_path), audios[0], sr, subtype="PCM_16")
    return {**base, "cached": False, "gen_seconds": round(gen_s, 2),
            "duration_s": round(audios[0].shape[-1] / sr, 2)}


@app.get("/api/projects/{slug}/preview-audio/{filename}")
def preview_audio(slug: str, filename: str):
    """Serve a cached preview WAV. ``filename`` is the on-disk name returned
    by /preview-matrix; we don't accept arbitrary paths."""
    proj = _load_project_or_404(slug)
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "invalid filename")
    target = proj.dir / "previews" / filename
    if not target.exists():
        raise HTTPException(404, "preview audio not found")
    return FileResponse(target, media_type="audio/wav")


def _book_blocks(proj: Project) -> list:
    """Parse the project's book into a Block list. Shared by /chapters
    and /render."""
    if proj.book.filename.endswith(".epub"):
        _meta, blocks = parse_epub(proj.book_path)
        return blocks
    from audiomat.epub import Block, split_sentences
    text = proj.book_path.read_text(encoding="utf-8")
    return [Block(text=text, sentences=split_sentences(text))]


@app.get("/api/projects/{slug}/chapters")
def list_chapters(slug: str):
    """Return every block with computed stem + status for the Render-tab
    chapter table.

    Status:
      * ``skipped`` — block is in book.blocks_skipped or has keep=False
      * ``rendered`` — final per-chapter WAV exists and is non-empty
      * ``pending`` — renderable but no audio yet

    Recomputed fresh each call (file stat); no in-memory cache. The
    chapter list endpoint is the source of truth for the UI's per-row
    badges and inline audio players.
    """
    proj = _load_project_or_404(slug)
    if not proj.book_path.exists():
        raise HTTPException(400, f"book file missing: {proj.book_path}")

    blocks = _book_blocks(proj)
    skip = set(proj.book.blocks_skipped or ())
    chapters = []
    one_idx = 0
    rendered_count = 0
    for block_idx, block in enumerate(blocks):
        skipped = block_idx in skip or not getattr(block, "keep", True)
        if not skipped:
            one_idx += 1

        text_full = " ".join(block.sentences).strip() if block.sentences else (block.text or "")
        char_count = len(text_full)
        preview = text_full[:140]

        if skipped:
            chapters.append({
                "block_index": block_idx,
                "renderable_index": None,
                "stem": None,
                "char_count": char_count,
                "preview": preview,
                "status": "skipped",
                "audio_url": None,
                "duration_s": None,
            })
            continue

        leading = block.text or (block.sentences[0] if block.sentences else "")
        stem = f"{one_idx:03d}_{compute_chapter_stem(leading)}"
        final_wav = proj.chunks_dir / stem / f"{stem}.wav"
        rendered = final_wav.exists() and final_wav.stat().st_size > 1024

        if rendered:
            rendered_count += 1
            audio_url = f"/api/projects/{slug}/chapter-audio/{stem}"
            duration_s = round(_wav_duration_s(final_wav), 2)
            status = "rendered"
        else:
            audio_url = None
            duration_s = None
            status = "pending"

        chapters.append({
            "block_index": block_idx,
            "renderable_index": one_idx,
            "stem": stem,
            "char_count": char_count,
            "preview": preview,
            "status": status,
            "audio_url": audio_url,
            "duration_s": duration_s,
        })

    return {
        "chapters": chapters,
        "renderable_total": one_idx,
        "rendered_count": rendered_count,
    }


@app.delete("/api/projects/{slug}/chapters/{stem}")
def reset_chapter(slug: str, stem: str):
    """Wipe a single chapter's cache: removes ``<project>/chunks/<stem>/``
    entirely (chunks, manifest, final wav). Next render starts fresh.

    Use cases:
      * Roll the diffusion dice again on a glitchy chapter (same text,
        different sample).
      * Force re-render after a voice or params change (the manifest
        hash currently keys only on text — voice / num_step / gs / speed
        changes don't auto-invalidate, this is a known v0.0.x limitation).

    The chapters list endpoint sees status flip to ``pending`` on the
    next call. Caller is responsible for triggering a render afterwards.
    """
    proj = _load_project_or_404(slug)
    if "/" in stem or "\\" in stem or ".." in stem:
        raise HTTPException(400, "invalid stem")
    target = proj.chunks_dir / stem
    if not target.exists():
        raise HTTPException(404, f"chapter dir not found: {stem}")
    shutil.rmtree(target, ignore_errors=True)
    proj.append_log(f"reset chapter cache: {stem}")
    return {"reset": stem}


@app.get("/api/projects/{slug}/chapter-audio/{stem}")
def chapter_audio(slug: str, stem: str):
    """Serve a per-chapter loudnorm-ed WAV for inline UI playback.
    ``stem`` is the on-disk directory name (e.g. ``001_Zima_2019``);
    rejects path-traversal attempts."""
    proj = _load_project_or_404(slug)
    if "/" in stem or "\\" in stem or ".." in stem:
        raise HTTPException(400, "invalid stem")
    target = proj.chunks_dir / stem / f"{stem}.wav"
    if not target.exists():
        raise HTTPException(404, "chapter audio not found")
    return FileResponse(target, media_type="audio/wav")


class RenderRequest(BaseModel):
    """Optional body for POST /render. ``indices`` is a list of 1-based
    renderable chapter indices; if absent/empty the whole book renders."""
    indices: list[int] | None = None


@app.post("/api/projects/{slug}/render")
async def start_render(
    slug: str,
    req: RenderRequest = Body(default_factory=RenderRequest),
):
    """Kick off background render. Returns immediately. Client connects
    to /progress for SSE event stream. ``req.indices`` selects specific
    chapters (UI's "Render selected" / "Render pending"); absent = all."""
    if slug in _RENDER_THREADS and _RENDER_THREADS[slug].is_alive():
        raise HTTPException(409, "render already in progress for this project")

    proj = _load_project_or_404(slug)
    voice = Voice.find_by_name(PATHS.voices_root, proj.voice_ref)
    if voice is None:
        raise HTTPException(404, f"voice not found: {proj.voice_ref}")

    if not proj.book_path.exists():
        raise HTTPException(400, f"book file missing: {proj.book_path}")

    blocks = _book_blocks(proj)

    queue: asyncio.Queue = asyncio.Queue()
    _RENDER_QUEUES[slug] = queue
    cancel_event = threading.Event()
    _RENDER_CANCEL[slug] = cancel_event
    loop = asyncio.get_running_loop()

    tts = _get_tts()
    renderer = ProjectRenderer(proj, voice, tts, blocks)
    indices = req.indices

    def worker():
        try:
            if indices:
                events = renderer.render_indices(indices)
            else:
                events = renderer.render_all()
            for event in events:
                if cancel_event.is_set():
                    cancelled = ProgressEvent(
                        kind="error",
                        message="render cancelled by user",
                    )
                    asyncio.run_coroutine_threadsafe(queue.put(cancelled), loop).result()
                    break
                asyncio.run_coroutine_threadsafe(queue.put(event), loop).result()
        except Exception as e:
            err = ProgressEvent(kind="error", message=f"{type(e).__name__}: {e}")
            asyncio.run_coroutine_threadsafe(queue.put(err), loop).result()
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()
            _RENDER_CANCEL.pop(slug, None)

    t = threading.Thread(target=worker, daemon=True, name=f"render-{slug}")
    t.start()
    _RENDER_THREADS[slug] = t

    return {
        "status": "started",
        "slug": slug,
        "indices": indices,
        "scope": "selected" if indices else "all",
    }


@app.post("/api/projects/{slug}/cancel-render")
def cancel_render(slug: str):
    """Stop an in-progress render gracefully. Sets the per-project cancel
    flag; the worker thread checks it between yielded ProgressEvents and
    bails on the next iteration. Already-synthesized chunks stay on disk
    + in the manifest, so a subsequent /render call resumes from the
    cached point.

    Cancellation is bounded by the current chunk's synth time
    (~1.5–2 s on RTX 5070 at step 48) — model.generate is uninterruptible
    once entered, but the for-loop ends as soon as it yields the next
    event."""
    if slug not in _RENDER_THREADS or not _RENDER_THREADS[slug].is_alive():
        raise HTTPException(404, "no render in progress for this project")
    flag = _RENDER_CANCEL.get(slug)
    if flag is None:
        raise HTTPException(409, "cancel flag missing — render thread may be tearing down")
    flag.set()
    return {"status": "cancelling", "slug": slug}


@app.get("/api/projects/{slug}/progress")
async def progress_stream(slug: str):
    queue = _RENDER_QUEUES.get(slug)
    if queue is None:
        raise HTTPException(404, "no active render — call POST /render first")

    async def gen():
        try:
            while True:
                event = await queue.get()
                if event is None:
                    yield {"event": "render_complete", "data": json.dumps({"kind": "render_complete"})}
                    break
                yield {
                    "event": event.kind,
                    "data": json.dumps(event.to_json_dict(), ensure_ascii=False),
                }
        finally:
            _RENDER_QUEUES.pop(slug, None)
            _RENDER_THREADS.pop(slug, None)

    return EventSourceResponse(gen())


@app.post("/api/projects/{slug}/build-m4b")
def build_project_m4b(slug: str):
    """After render completes, concatenate per-chapter WAVs into the
    final M4B with chapter markers + metadata. Streams progress as SSE.

    Events:
      * ``started`` — chapter count + estimated total duration
      * ``progress`` — encoder percent (0–100, ~every 500 ms)
      * ``complete`` — out path, size, chapters, duration
      * ``error`` — message
    """
    import json as _json
    import queue as _queue
    import threading as _threading

    from audiomat.audio import collect_chapter_wavs

    proj = _load_project_or_404(slug)
    if not proj.chunks_dir.exists():
        raise HTTPException(400, "no chapter outputs — render first")

    items_preview = collect_chapter_wavs(proj.chunks_dir)
    if not items_preview:
        raise HTTPException(400, "no chapter WAVs — render at least one chapter first")
    pre_chapter_count = len(items_preview)
    pre_total_ms = sum(d for _, _, d in items_preview)

    voice_label = proj.voice_ref
    meta = M4BMetadata(
        title=proj.book.title or proj.name,
        artist=proj.book.author or "",
        album=proj.book.title or proj.name,
        narrator=f"{voice_label} (audiomat / OmniVoice)",
    )

    q: _queue.Queue = _queue.Queue()

    def worker():
        def cb(pct: float) -> None:
            q.put(("progress", pct))
        try:
            chapter_count, total_ms = build_m4b(
                chunks_root=proj.chunks_dir,
                out_path=proj.final_path,
                meta=meta,
                progress_cb=cb,
            )
            q.put(("complete", (chapter_count, total_ms)))
        except Exception as e:
            q.put(("error", f"{type(e).__name__}: {e}"))

    t = _threading.Thread(target=worker, daemon=True, name=f"m4b-{slug}")
    t.start()

    def event_gen():
        yield {
            "event": "started",
            "data": _json.dumps({
                "chapters": pre_chapter_count,
                "duration_s": pre_total_ms / 1000,
            }),
        }
        while True:
            kind, payload = q.get()
            if kind == "progress":
                yield {
                    "event": "progress",
                    "data": _json.dumps({"percent": payload}),
                }
            elif kind == "complete":
                chapter_count, total_ms = payload
                proj.set_status(phase="complete")
                proj.append_log(
                    f"M4B built: {chapter_count} chapters, "
                    f"{total_ms / 1000:.1f}s total"
                )
                size = (
                    proj.final_path.stat().st_size
                    if proj.final_path.exists()
                    else 0
                )
                yield {
                    "event": "complete",
                    "data": _json.dumps({
                        "chapters": chapter_count,
                        "duration_s": total_ms / 1000,
                        "size_bytes": size,
                    }),
                }
                break
            elif kind == "error":
                yield {
                    "event": "error",
                    "data": _json.dumps({"message": payload}),
                }
                break
        t.join(timeout=5)

    return EventSourceResponse(event_gen())


@app.get("/api/projects/{slug}/m4b")
def project_m4b(slug: str):
    proj = _load_project_or_404(slug)
    if not proj.final_path.exists():
        raise HTTPException(404, "M4B not built yet")
    return FileResponse(
        proj.final_path,
        media_type="audio/mp4",
        filename=f"{proj.name_slug}.m4b",
    )


# ----------------------------------------------------------------------------
# Static frontend mount
# ----------------------------------------------------------------------------

# Multi-stage Docker build copies frontend/dist → /app/static. In dev we
# don't mount static at all — Vite at :5173 serves it.
_STATIC_DIR = Path(__file__).parent.parent / "frontend" / "dist"
if not _STATIC_DIR.exists():
    _STATIC_DIR = Path(__file__).parent.parent / "static"
if _STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="frontend")


@app.exception_handler(StarletteHTTPException)
async def spa_404_fallback(request: Request, exc: StarletteHTTPException):
    """SPA deep-link fallback. Hard-refreshing a React Router path like
    ``/projects/Rezavy_les_v1`` hits the StaticFiles mount, which 404s
    because no such file exists in ``dist/``. We catch the 404 and serve
    ``index.html`` so React Router can handle the route client-side.
    ``/api/*`` paths still return JSON 404s normally."""
    if exc.status_code == 404 and not request.url.path.startswith("/api"):
        if _STATIC_DIR.exists():
            index = _STATIC_DIR / "index.html"
            if index.exists():
                return FileResponse(index)
    return JSONResponse(
        {"detail": exc.detail},
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )
