"""Preview-matrix endpoints — 4 fixed presets + custom variant.

The preview matrix renders one short prose sample at four (num_step,
guidance_scale, speed) presets so the user can A/B before committing to
a full-book render. Cached per (text, params, voice_slug) on disk.
"""
from __future__ import annotations

import hashlib
import json as _json
import time
from pathlib import Path

import soundfile as sf
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse

from audiomat.epub import Block, parse_epub, split_sentences
from audiomat.headers import prepare_for_tts
from audiomat.num2text import normalize_lang
from audiomat.project import Project
from audiomat.pronunciations import apply_pronunciations, load_pronunciations
from audiomat.routers.projects import is_metadata_block as _is_metadata_block
from audiomat.schemas import PreviewCustomRequest
from audiomat.state import (
    PATHS,
    get_tts_for_voice,
    load_project_or_404,
    wav_duration_s,
)
from audiomat.voice import Voice


router = APIRouter(prefix="/api/projects", tags=["preview"])


PREVIEW_MATRIX = [
    {"label": "Fast",     "num_step": 32, "guidance_scale": 2.0, "speed": 1.0},
    {"label": "Balanced", "num_step": 48, "guidance_scale": 2.0, "speed": 1.0},
    {"label": "Crisp",    "num_step": 48, "guidance_scale": 2.5, "speed": 1.0},
    {"label": "Stable",   "num_step": 64, "guidance_scale": 2.0, "speed": 1.0},
]


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


def _parse_blocks(proj: Project) -> list:
    """Local Block parser — same logic as state.book_blocks but inlined
    here so this router has no soft dep on the chapters router."""
    if proj.book.filename.endswith(".epub"):
        _meta, blocks = parse_epub(proj.book_path)
        return blocks
    text = proj.book_path.read_text(encoding="utf-8")
    return [Block(text=text, sentences=split_sentences(text))]


@router.post("/{slug}/preview-matrix")
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
    # Validate up front — these raise HTTPException synchronously, so the
    # client gets a 4xx without entering the SSE stream and seeing an
    # "error" event mid-flight.
    proj = load_project_or_404(slug)
    voice = Voice.find_by_name(PATHS.voices_root, proj.voice_ref)
    if voice is None:
        raise HTTPException(404, f"voice not found: {proj.voice_ref}")
    if not proj.book_path.exists():
        raise HTTPException(400, f"book file missing: {proj.book_path}")

    blocks = _parse_blocks(proj)

    picked = _pick_sample_text(blocks, blocks_skipped=proj.book.blocks_skipped)
    if picked is None:
        raise HTTPException(400, "no block ≥ 300 chars found in book — preview needs prose")
    sample_text, sample_block_index = picked

    previews_dir = proj.dir / "previews"
    previews_dir.mkdir(exist_ok=True)
    # EPUB DC metadata uses BCP 47 (cs-CZ); OmniVoice + num2words want
    # ISO 639-1 (cs) — strip region suffix at the boundary.
    language = normalize_lang(proj.book.language or "cs")
    # Apply per-project pronunciation overrides BEFORE prepare_for_tts so
    # the preview reflects what the actual render will produce.
    pronunciations = load_pronunciations(proj.dir)
    clean = prepare_for_tts(apply_pronunciations(sample_text, pronunciations), lang=language)
    ref_text = voice.transcript()
    ref_audio = str(voice.wav_path)
    total_book_chars = _total_book_chars(blocks, proj.book.blocks_skipped)

    def event_gen():
        tts = get_tts_for_voice(voice)
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
                        "duration_s": wav_duration_s(wav_path),
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


@router.post("/{slug}/preview-custom")
def preview_custom(slug: str, req: PreviewCustomRequest):
    """Render ONE preview sample with the user-supplied params (NOT
    persisted to project.params). Lets the user A/B custom slider values
    before committing. Cached per (text, params, voice_slug)."""
    proj = load_project_or_404(slug)
    voice = Voice.find_by_name(PATHS.voices_root, proj.voice_ref)
    if voice is None:
        raise HTTPException(404, f"voice not found: {proj.voice_ref}")
    if not proj.book_path.exists():
        raise HTTPException(400, f"book file missing: {proj.book_path}")

    blocks = _parse_blocks(proj)

    picked = _pick_sample_text(blocks, blocks_skipped=proj.book.blocks_skipped)
    if picked is None:
        raise HTTPException(400, "no block ≥ 300 chars found in book — preview needs prose")
    sample_text, sample_block_index = picked

    previews_dir = proj.dir / "previews"
    previews_dir.mkdir(exist_ok=True)
    language = normalize_lang(proj.book.language or "cs")
    pronunciations = load_pronunciations(proj.dir)
    clean = prepare_for_tts(apply_pronunciations(sample_text, pronunciations), lang=language)
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
                "duration_s": wav_duration_s(wav_path)}

    tts = get_tts_for_voice(voice)
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


@router.get("/{slug}/preview-audio/{filename}")
def preview_audio(slug: str, filename: str):
    """Serve a cached preview WAV. ``filename`` is the on-disk name returned
    by /preview-matrix; we don't accept arbitrary paths."""
    proj = load_project_or_404(slug)
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "invalid filename")
    target = proj.dir / "previews" / filename
    if not target.exists():
        raise HTTPException(404, "preview audio not found")
    return FileResponse(target, media_type="audio/wav")
