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
from audiomat.schemas import PreviewCustomRequest, PreviewVoicesRequest
from audiomat.state import (
    PATHS,
    get_tts_for_project,
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
    voice = Voice.find_by_name(proj.voice_ref)
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
    # Sidecar JSON next to the cached WAVs records per-file gen_seconds.
    # Without it, cached cells return gen_seconds=0 and the UI can't
    # compute the "Est. full book render" extrapolation — user has to
    # re-tune just to estimate. Persist the wall-clock from the original
    # generation so cache hits stay informative.
    gen_times_path = previews_dir / "_gen_times.json"
    gen_times: dict[str, float] = {}
    if gen_times_path.exists():
        try:
            gen_times = _json.loads(gen_times_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            gen_times = {}

    # Project's render-speed param flows into the matrix so the cells
    # reflect the user's chosen tempo (e.g. speed=0.9 for slower-paced
    # audiobook narration). Without this, cells were stuck at the
    # hardcoded PREVIEW_MATRIX value of 1.0 even after the user dropped
    # the project to 0.9 via Fine tune → Use this — and the displayed
    # "speed 1.00" no longer matched the project state.
    project_speed = proj.params.speed

    # Per-cell tuning overrides persisted by /preview-custom (when called
    # with a ``label`` field). Lets a Fine tune dialog session survive a
    # page refresh: the matrix re-render finds the tuned params here and
    # applies them on top of the preset.
    tuned_cells_path = previews_dir / "_tuned_cells.json"
    tuned_cells: dict[str, dict] = {}
    if tuned_cells_path.exists():
        try:
            tuned_cells = _json.loads(tuned_cells_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            tuned_cells = {}

    def event_gen():
        # v0.5: engine choice is project-level, not voice-level. Matrix
        # cells are still per-voice in name but render through whatever
        # engine the project owns.
        tts = get_tts_for_project(proj)
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
        gen_times_dirty = False
        for idx, v in enumerate(PREVIEW_MATRIX):
            try:
                # Speed comes from the project, not the preset — matrix
                # is a num_step/gs A/B at the user's chosen tempo. A
                # per-cell tuning override (from a prior Fine tune call)
                # wins over both the preset and the project speed.
                variant = {**v, "speed": project_speed}
                override = tuned_cells.get(v["label"])
                if isinstance(override, dict):
                    if "num_step" in override:
                        variant["num_step"] = int(override["num_step"])
                    if "guidance_scale" in override:
                        variant["guidance_scale"] = float(override["guidance_scale"])
                    if "speed" in override:
                        variant["speed"] = float(override["speed"])
                    variant["tuned"] = True
                key_src = (
                    f"{clean}|{variant['num_step']}|{variant['guidance_scale']}"
                    f"|{variant['speed']}|{voice.name_slug}"
                )
                key = hashlib.md5(key_src.encode("utf-8")).hexdigest()[:16]
                wav_path = previews_dir / f"{variant['label']}_{key}.wav"

                if wav_path.exists() and wav_path.stat().st_size > 1024:
                    cell = {
                        **variant,
                        "audio_url": f"/api/projects/{slug}/preview-audio/{wav_path.name}",
                        "cached": True,
                        "gen_seconds": float(gen_times.get(wav_path.name, 0.0)),
                        "duration_s": wav_duration_s(wav_path),
                    }
                else:
                    # Use the high-level backend-agnostic generate() so
                    # the same code works on OmniVoice and Higgs. Build
                    # a per-cell RenderParams (matrix is a num_step/gs
                    # A/B at the project's chosen speed) — params Higgs
                    # ignores anyway, but OmniVoice needs them.
                    from audiomat.project import RenderParams as _RP
                    cell_params = _RP(
                        num_step=int(variant["num_step"]),
                        guidance_scale=float(variant["guidance_scale"]),
                        speed=float(variant["speed"]),
                    )
                    t0 = time.time()
                    result = tts.generate(clean, voice, cell_params, language=language)
                    gen_s = time.time() - t0
                    sf.write(str(wav_path), result.audio, result.sample_rate, subtype="PCM_16")
                    gen_times[wav_path.name] = round(gen_s, 2)
                    gen_times_dirty = True
                    cell = {
                        **variant,
                        "audio_url": f"/api/projects/{slug}/preview-audio/{wav_path.name}",
                        "cached": False,
                        "gen_seconds": round(gen_s, 2),
                        "duration_s": round(result.duration_s, 2),
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

        if gen_times_dirty:
            try:
                gen_times_path.write_text(
                    _json.dumps(gen_times, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                # Non-fatal — estimates will just have to re-time next round.
                pass

        yield {
            "event": "complete",
            "data": _json.dumps({"variants": results}),
        }

    return EventSourceResponse(event_gen())


@router.post("/{slug}/preview-voices")
def preview_voices(slug: str, req: PreviewVoicesRequest):
    """Render the project's sample text with each requested voice and
    stream cell-by-cell progress as SSE.

    Mirrors :func:`preview_matrix`'s shape but iterates over voices
    instead of param presets — params are taken from ``project.params``
    so cells differ only by voice. Cache layout is shared with the
    quality matrix (same ``previews/`` directory + ``_gen_times.json``
    sidecar); cell filenames are ``voice_<slug>_<hash>.wav`` so they
    don't collide with quality-matrix cells (``<label>_<hash>.wav``).

    No upper cap on count — the UI's smart default seeds a small set,
    but users can opt to compare all voices in the library if they
    want. Voices that resolve to different fine-tunes (per the model
    registry) reuse the per-target TTS singleton, so repeated voices
    on the same fine-tune don't trigger extra model loads.
    """
    if len(req.voice_slugs) < 1:
        raise HTTPException(400, "voice_slugs must contain at least one slug")
    if len(set(req.voice_slugs)) != len(req.voice_slugs):
        raise HTTPException(400, "voice_slugs must be unique")

    proj = load_project_or_404(slug)
    if not proj.book_path.exists():
        raise HTTPException(400, f"book file missing: {proj.book_path}")

    voices: list[Voice] = []
    for vs in req.voice_slugs:
        try:
            voices.append(Voice.load(vs))
        except FileNotFoundError:
            raise HTTPException(404, f"voice not found: {vs}")

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

    p = proj.params
    gen_times_path = previews_dir / "_gen_times.json"
    gen_times: dict[str, float] = {}
    if gen_times_path.exists():
        try:
            gen_times = _json.loads(gen_times_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            gen_times = {}

    def event_gen():
        yield {
            "event": "started",
            "data": _json.dumps({
                "total": len(voices),
                "sample_text": clean,
                "sample_chars": len(clean),
                "sample_block_index": sample_block_index,
                "sample_block_total": len(blocks),
                "total_book_chars": total_book_chars,
                # Echo the params so the UI can label "rendered at 48/2.0/1.0"
                # in case the user later changes them and wonders why the
                # cached samples sound different from a fresh quality preview.
                "num_step": p.num_step,
                "guidance_scale": p.guidance_scale,
                "speed": p.speed,
            }),
        }

        results: list[dict] = []
        gen_times_dirty = False
        for idx, voice in enumerate(voices):
            try:
                key_src = (
                    f"{clean}|{p.num_step}|{p.guidance_scale}"
                    f"|{p.speed}|{voice.name_slug}"
                )
                key = hashlib.md5(key_src.encode("utf-8")).hexdigest()[:16]
                wav_path = previews_dir / f"voice_{voice.name_slug}_{key}.wav"

                if wav_path.exists() and wav_path.stat().st_size > 1024:
                    cell = {
                        "voice_slug": voice.name_slug,
                        "voice_name": voice.name,
                        "audio_url": f"/api/projects/{slug}/preview-audio/{wav_path.name}",
                        "cached": True,
                        "gen_seconds": float(gen_times.get(wav_path.name, 0.0)),
                        "duration_s": wav_duration_s(wav_path),
                    }
                else:
                    # Backend-agnostic generate() — engine comes from the
                    # project (v0.5), not the voice. Each cell in this
                    # multi-voice matrix uses the same engine.
                    tts = get_tts_for_project(proj)
                    t0 = time.time()
                    result = tts.generate(clean, voice, p, language=language)
                    gen_s = time.time() - t0
                    sf.write(str(wav_path), result.audio, result.sample_rate, subtype="PCM_16")
                    gen_times[wav_path.name] = round(gen_s, 2)
                    gen_times_dirty = True
                    cell = {
                        "voice_slug": voice.name_slug,
                        "voice_name": voice.name,
                        "audio_url": f"/api/projects/{slug}/preview-audio/{wav_path.name}",
                        "cached": False,
                        "gen_seconds": round(gen_s, 2),
                        "duration_s": round(result.duration_s, 2),
                    }
            except Exception as e:
                yield {
                    "event": "error",
                    "data": _json.dumps({
                        "index": idx,
                        "voice_slug": voice.name_slug,
                        "message": str(e),
                    }),
                }
                return

            results.append(cell)
            yield {
                "event": "cell_done",
                "data": _json.dumps({"index": idx, "voice": cell}),
            }

        if gen_times_dirty:
            try:
                gen_times_path.write_text(
                    _json.dumps(gen_times, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                pass

        yield {
            "event": "complete",
            "data": _json.dumps({"voices": results}),
        }

    return EventSourceResponse(event_gen())


@router.post("/{slug}/preview-custom")
def preview_custom(slug: str, req: PreviewCustomRequest):
    """Render ONE preview sample with the user-supplied params (NOT
    persisted to project.params). Lets the user A/B custom slider values
    before committing. Cached per (text, params, voice_slug)."""
    proj = load_project_or_404(slug)
    voice = Voice.find_by_name(proj.voice_ref)
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

    # If the caller tagged this with a matrix cell label, persist the
    # tuning as a per-cell override so /preview-matrix shows the same
    # params after a refresh (instead of reverting to the preset).
    tuned_cells_path = previews_dir / "_tuned_cells.json"
    if req.label:
        try:
            tuned = (
                _json.loads(tuned_cells_path.read_text(encoding="utf-8"))
                if tuned_cells_path.exists() else {}
            )
        except (OSError, ValueError):
            tuned = {}
        tuned[req.label] = {
            "num_step": req.num_step,
            "guidance_scale": req.guidance_scale,
            "speed": req.speed,
        }
        try:
            tuned_cells_path.write_text(
                _json.dumps(tuned, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            # Non-fatal — preview-custom still succeeds, just no persistence.
            pass

    key_src = f"{clean}|{req.num_step}|{req.guidance_scale}|{req.speed}|{voice.name_slug}"
    key = hashlib.md5(key_src.encode("utf-8")).hexdigest()[:16]
    # When tagged with a label, save under the matrix-friendly filename
    # (``<label>_<hash>.wav``) so /preview-matrix sees a cache hit on
    # next render. Without the label, fall back to ``Custom_<hash>.wav``
    # — ephemeral pre-`label` behavior, kept for backwards compat.
    file_prefix = req.label if req.label else "Custom"
    wav_path = previews_dir / f"{file_prefix}_{key}.wav"

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

    # Same sidecar that /preview-matrix uses so cache hits can recover
    # gen_seconds on either endpoint. We update it for all preview-custom
    # generations (label or not) — Custom_*.wav entries are harmless if
    # they never get looked up by the matrix.
    gen_times_path = previews_dir / "_gen_times.json"
    gen_times: dict[str, float] = {}
    if gen_times_path.exists():
        try:
            gen_times = _json.loads(gen_times_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            gen_times = {}

    if wav_path.exists() and wav_path.stat().st_size > 1024:
        return {**base, "cached": True,
                "gen_seconds": float(gen_times.get(wav_path.name, 0.0)),
                "duration_s": wav_duration_s(wav_path)}

    # v0.5: engine resolved from the project; the voice ref drives the
    # speaker clone but not the engine choice.
    tts = get_tts_for_project(proj)

    # High-level generate() so the call works for both backends. Higgs
    # ignores num_step/guidance_scale/speed; OmniVoice consumes them.
    from audiomat.project import RenderParams as _RP
    custom_params = _RP(
        num_step=int(req.num_step),
        guidance_scale=float(req.guidance_scale),
        speed=float(req.speed),
    )
    t0 = time.time()
    result = tts.generate(clean, voice, custom_params, language=language)
    gen_s = time.time() - t0
    sf.write(str(wav_path), result.audio, result.sample_rate, subtype="PCM_16")
    gen_times[wav_path.name] = round(gen_s, 2)
    try:
        gen_times_path.write_text(
            _json.dumps(gen_times, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass
    return {**base, "cached": False, "gen_seconds": round(gen_s, 2),
            "duration_s": round(result.duration_s, 2)}


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
