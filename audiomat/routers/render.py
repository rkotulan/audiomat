"""Render lifecycle endpoints + M4B build.

POST /render kicks off a background worker thread that pushes
ProgressEvent objects through an asyncio.Queue. GET /progress is the
SSE consumer. POST /cancel-render sets a per-project Event flag the
worker checks between yields.
"""
from __future__ import annotations

import asyncio
import json
import json as _json
import queue as _queue
import threading
import threading as _threading

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse

from audiomat.audio import M4BMetadata, build_m4b, collect_chapter_wavs
from audiomat.render import ProgressEvent, ProjectRenderer
from audiomat.schemas import RenderRequest
from audiomat.state import (
    PATHS,
    RENDER_CANCEL,
    RENDER_QUEUES,
    RENDER_THREADS,
    book_blocks,
    get_tts,
    load_project_or_404,
)
from audiomat.voice import Voice


router = APIRouter(prefix="/api/projects", tags=["render"])


@router.post("/{slug}/render")
async def start_render(
    slug: str,
    req: RenderRequest = Body(default_factory=RenderRequest),
):
    """Kick off background render. Returns immediately. Client connects
    to /progress for SSE event stream. ``req.indices`` selects specific
    chapters (UI's "Render selected" / "Render pending"); absent = all."""
    if slug in RENDER_THREADS and RENDER_THREADS[slug].is_alive():
        raise HTTPException(409, "render already in progress for this project")

    proj = load_project_or_404(slug)
    voice = Voice.find_by_name(PATHS.voices_root, proj.voice_ref)
    if voice is None:
        raise HTTPException(404, f"voice not found: {proj.voice_ref}")

    if not proj.book_path.exists():
        raise HTTPException(400, f"book file missing: {proj.book_path}")

    blocks = book_blocks(proj)

    queue: asyncio.Queue = asyncio.Queue()
    RENDER_QUEUES[slug] = queue
    cancel_event = threading.Event()
    RENDER_CANCEL[slug] = cancel_event
    loop = asyncio.get_running_loop()

    tts = get_tts()
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
            RENDER_CANCEL.pop(slug, None)

    t = threading.Thread(target=worker, daemon=True, name=f"render-{slug}")
    t.start()
    RENDER_THREADS[slug] = t

    return {
        "status": "started",
        "slug": slug,
        "indices": indices,
        "scope": "selected" if indices else "all",
    }


@router.post("/{slug}/cancel-render")
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
    if slug not in RENDER_THREADS or not RENDER_THREADS[slug].is_alive():
        raise HTTPException(404, "no render in progress for this project")
    flag = RENDER_CANCEL.get(slug)
    if flag is None:
        raise HTTPException(409, "cancel flag missing — render thread may be tearing down")
    flag.set()
    return {"status": "cancelling", "slug": slug}


@router.get("/{slug}/progress")
async def progress_stream(slug: str):
    queue = RENDER_QUEUES.get(slug)
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
            RENDER_QUEUES.pop(slug, None)
            RENDER_THREADS.pop(slug, None)

    return EventSourceResponse(gen())


@router.post("/{slug}/build-m4b")
def build_project_m4b(slug: str):
    """After render completes, concatenate per-chapter WAVs into the
    final M4B with chapter markers + metadata. Streams progress as SSE.

    Events:
      * ``started`` — chapter count + estimated total duration
      * ``progress`` — encoder percent (0–100, ~every 500 ms)
      * ``complete`` — out path, size, chapters, duration
      * ``error`` — message
    """
    proj = load_project_or_404(slug)
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


@router.get("/{slug}/m4b")
def project_m4b(slug: str):
    proj = load_project_or_404(slug)
    if not proj.final_path.exists():
        raise HTTPException(404, "M4B not built yet")
    return FileResponse(
        proj.final_path,
        media_type="audio/mp4",
        filename=f"{proj.name_slug}.m4b",
    )
