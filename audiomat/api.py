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
import shutil
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from audiomat.audio import (
    M4BMetadata,
    build_m4b,
    convert_voice_ref,
)
from audiomat.epub import parse_epub
from audiomat.paths import AudiomatPaths
from audiomat.project import (
    BookInfo,
    Project,
    ProjectStatus,
    RenderParams,
)
from audiomat.render import ProgressEvent, ProjectRenderer
from audiomat.slug import slugify
from audiomat.tts import OmniVoiceTTS
from audiomat.voice import Voice


# ----------------------------------------------------------------------------
# App + global state
# ----------------------------------------------------------------------------


PATHS = AudiomatPaths.default()
_TTS: OmniVoiceTTS | None = None
_RENDER_QUEUES: dict[str, asyncio.Queue] = {}
_RENDER_THREADS: dict[str, threading.Thread] = {}


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
    version="0.0.0",
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
                "blocks_skipped": [],
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
    "palmknihy",
    "kupující",
    "kupujici",
    "isbn",
    "neoprávněn",
    "autorského práva",
    "trestního zákoníku",
    "kniha je určena",
    "elektronických knih",
    "kniha jako celek",
    "all rights reserved",
)


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
    from the project's book. Cached per (text, num_step, gs, speed) under
    ``<project>/previews/``. First call ~22 s on RTX 5070 (sequential
    OmniVoice inference). Subsequent calls = cache hits, instant.
    """
    import hashlib
    import time
    import soundfile as sf
    from audiomat.headers import strip_markers

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
    clean = strip_markers(sample_text)
    ref_text = voice.transcript()
    ref_audio = str(voice.wav_path)
    language = proj.book.language or "cs"

    tts = _get_tts()
    tts.load()
    sr = tts.sample_rate

    results = []
    for v in PREVIEW_MATRIX:
        key_src = f"{clean}|{v['num_step']}|{v['guidance_scale']}|{v['speed']}|{voice.name_slug}"
        key = hashlib.md5(key_src.encode("utf-8")).hexdigest()[:16]
        wav_path = previews_dir / f"{v['label']}_{key}.wav"

        if wav_path.exists() and wav_path.stat().st_size > 1024:
            results.append({
                **v,
                "audio_url": f"/api/projects/{slug}/preview-audio/{wav_path.name}",
                "cached": True,
                "gen_seconds": 0.0,
                "duration_s": _wav_duration_s(wav_path),
            })
            continue

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
        results.append({
            **v,
            "audio_url": f"/api/projects/{slug}/preview-audio/{wav_path.name}",
            "cached": False,
            "gen_seconds": round(gen_s, 2),
            "duration_s": round(audios[0].shape[-1] / sr, 2),
        })

    return {
        "sample_text": sample_text,
        "sample_chars": len(clean),
        "sample_block_index": sample_block_index,
        "sample_block_total": len(blocks),
        "variants": results,
    }


def _wav_duration_s(path: Path) -> float:
    import wave
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


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


@app.post("/api/projects/{slug}/render")
async def start_render(slug: str):
    """Kick off background render. Returns immediately. Client connects to
    /progress for SSE event stream."""
    if slug in _RENDER_THREADS and _RENDER_THREADS[slug].is_alive():
        raise HTTPException(409, "render already in progress for this project")

    proj = _load_project_or_404(slug)
    voice = Voice.find_by_name(PATHS.voices_root, proj.voice_ref)
    if voice is None:
        raise HTTPException(404, f"voice not found: {proj.voice_ref}")

    if not proj.book_path.exists():
        raise HTTPException(400, f"book file missing: {proj.book_path}")

    # Parse EPUB
    if proj.book.filename.endswith(".epub"):
        _meta, blocks = parse_epub(proj.book_path)
    else:
        # Plain TXT: one block per file (very crude — for v0.1).
        from audiomat.epub import Block, split_sentences
        text = proj.book_path.read_text(encoding="utf-8")
        blocks = [Block(text=text, sentences=split_sentences(text))]

    queue: asyncio.Queue = asyncio.Queue()
    _RENDER_QUEUES[slug] = queue
    loop = asyncio.get_running_loop()

    tts = _get_tts()
    renderer = ProjectRenderer(proj, voice, tts, blocks)

    def worker():
        try:
            for event in renderer.render_all():
                asyncio.run_coroutine_threadsafe(queue.put(event), loop).result()
        except Exception as e:
            err = ProgressEvent(kind="error", message=f"{type(e).__name__}: {e}")
            asyncio.run_coroutine_threadsafe(queue.put(err), loop).result()
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()

    t = threading.Thread(target=worker, daemon=True, name=f"render-{slug}")
    t.start()
    _RENDER_THREADS[slug] = t

    return {"status": "started", "slug": slug}


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
    final M4B with chapter markers + metadata."""
    proj = _load_project_or_404(slug)
    if not proj.chunks_dir.exists():
        raise HTTPException(400, "no chapter outputs — render first")

    voice_label = proj.voice_ref
    meta = M4BMetadata(
        title=proj.book.title or proj.name,
        artist=proj.book.author or "",
        album=proj.book.title or proj.name,
        narrator=f"{voice_label} (audiomat / OmniVoice)",
    )
    try:
        chapter_count, total_ms = build_m4b(
            chunks_root=proj.chunks_dir,
            out_path=proj.final_path,
            meta=meta,
        )
    except Exception as e:
        raise HTTPException(500, f"M4B build failed: {e}")
    proj.set_status(phase="complete")
    proj.append_log(f"M4B built: {chapter_count} chapters, {total_ms / 1000:.1f}s total")
    return {
        "out": str(proj.final_path),
        "chapters": chapter_count,
        "duration_s": total_ms / 1000,
        "size_bytes": proj.final_path.stat().st_size,
    }


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
