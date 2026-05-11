"""Per-chapter listing + audio + cache reset endpoints.

The chapters list is the source of truth for the Render-tab table:
status badges, inline audio, char counts. Reset endpoints are the
escape hatch for the manifest-cache-not-keyed-on-params gotcha.
"""
from __future__ import annotations

import shutil

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from audiomat.slug import chapter_stem as compute_chapter_stem
from audiomat.state import book_blocks, load_project_or_404, wav_duration_s


router = APIRouter(prefix="/api/projects", tags=["chapters"])


@router.get("/{slug}/chapters")
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
    proj = load_project_or_404(slug)
    if not proj.book_path.exists():
        raise HTTPException(400, f"book file missing: {proj.book_path}")

    blocks = book_blocks(proj)
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
            duration_s = round(wav_duration_s(final_wav), 2)
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


@router.delete("/{slug}/chapters")
def reset_all_chapters(slug: str):
    """Wipe every per-chapter cache under ``<project>/chunks/``. Leaves
    ``previews/`` and ``final.m4b`` untouched.

    Use case: voice / params / language change that the manifest cache
    doesn't auto-detect (manifest hashes only chunk text). Without this,
    the only escape hatches were per-row Re-render or manual ``rm -rf``.
    """
    proj = load_project_or_404(slug)
    if not proj.chunks_dir.exists():
        return {"reset_count": 0}
    n = 0
    for d in proj.chunks_dir.iterdir():
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    proj.append_log(f"reset all chapter caches: wiped {n} dirs")
    return {"reset_count": n}


@router.delete("/{slug}/chapters/{stem}")
def reset_chapter(slug: str, stem: str):
    """Wipe a single chapter's cache: removes ``<project>/chunks/<stem>/``
    entirely (chunks, manifest, final wav). Next render starts fresh.

    Use cases:
      * Roll the diffusion dice again on a glitchy chapter (same text,
        params, and voice — different sample of the noise schedule).
      * Force a fresh render even when the params signature hasn't
        changed.

    Note: voice / num_step / guidance_scale / speed / language changes
    auto-invalidate via the manifest's ``sig`` field, so this endpoint is
    NOT required after a params change — just hit Render and the stale
    chunks re-synth on their own.

    The chapters list endpoint sees status flip to ``pending`` on the
    next call. Caller is responsible for triggering a render afterwards.
    """
    proj = load_project_or_404(slug)
    if "/" in stem or "\\" in stem or ".." in stem:
        raise HTTPException(400, "invalid stem")
    target = proj.chunks_dir / stem
    if not target.exists():
        raise HTTPException(404, f"chapter dir not found: {stem}")
    shutil.rmtree(target, ignore_errors=True)
    proj.append_log(f"reset chapter cache: {stem}")
    return {"reset": stem}


@router.get("/{slug}/chapter-audio/{stem}")
def chapter_audio(slug: str, stem: str):
    """Serve a per-chapter loudnorm-ed WAV for inline UI playback.
    ``stem`` is the on-disk directory name (e.g. ``001_Zima_2019``);
    rejects path-traversal attempts."""
    proj = load_project_or_404(slug)
    if "/" in stem or "\\" in stem or ".." in stem:
        raise HTTPException(400, "invalid stem")
    target = proj.chunks_dir / stem / f"{stem}.wav"
    if not target.exists():
        raise HTTPException(404, "chapter audio not found")
    return FileResponse(target, media_type="audio/wav")
