"""Per-chapter listing + audio + cache reset endpoints + text overrides.

The chapters list is the source of truth for the Render-tab table:
status badges, inline audio, char counts, override flag.

Text overrides let the user fix a single chapter's text (typos,
pronunciation tweaks, manual pause markers) without modifying the
source EPUB. See :mod:`audiomat.overrides` for storage details.
"""
from __future__ import annotations

import shutil

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from audiomat.chunker import DEFAULT_MAX_CHARS, DEFAULT_MIN_CHARS, make_chunks
from audiomat.headers import TIME_HEADER_RE_CS, inject_header_pause
from audiomat.overrides import (
    delete_override,
    overridden_indices,
    save_override,
)
from audiomat.slug import chapter_stem as compute_chapter_stem
from audiomat.state import book_blocks, load_project_or_404, wav_duration_s


router = APIRouter(prefix="/api/projects", tags=["chapters"])


# Stem path-traversal guard — used everywhere we accept ``{stem}`` in a
# URL. Centralized so the rule is consistent across endpoints.
def _validate_stem(stem: str) -> None:
    if "/" in stem or "\\" in stem or ".." in stem:
        raise HTTPException(400, "invalid stem")


def _parse_original_blocks(proj) -> list:
    """Re-parse the project's book WITHOUT applying overrides. Stems are
    pinned to this original-text view so editing a chapter doesn't break
    URLs / chunk-dir names that already reference the chapter."""
    from audiomat.epub import Block, parse_epub, split_sentences
    if proj.book.filename.endswith(".epub"):
        _meta, blocks = parse_epub(proj.book_path)
        return blocks
    text = proj.book_path.read_text(encoding="utf-8")
    return [Block(text=text, sentences=split_sentences(text))]


def _walk_chapters(proj):
    """Yield (block_index, renderable_index_or_None, stem_or_None,
    block, skipped) for every block in order.

    Crucially: the stem is computed from the ORIGINAL EPUB text (via
    parse_epub), not from the possibly-overridden block. That keeps
    chunk-dir names + REST URLs stable across text edits — the
    override changes the content the user hears, not how the chapter
    is addressed.

    The yielded ``block`` is the post-override version (what the
    renderer / preview / chapter list should actually display).
    """
    overridden = book_blocks(proj)              # post-override content
    originals = _parse_original_blocks(proj)    # pre-override addressing
    skip = set(proj.book.blocks_skipped or ())
    one_idx = 0
    for block_idx, block in enumerate(overridden):
        skipped = block_idx in skip or not getattr(block, "keep", True)
        if skipped:
            yield block_idx, None, None, block, True
            continue
        one_idx += 1
        original = originals[block_idx] if block_idx < len(originals) else block
        leading = original.text or (original.sentences[0] if original.sentences else "")
        stem = f"{one_idx:03d}_{compute_chapter_stem(leading)}"
        yield block_idx, one_idx, stem, block, False


def _resolve_stem(proj, stem: str) -> tuple[int, int, object]:
    """Resolve a chapter stem to (block_index, renderable_index, block).
    Raises HTTPException(404) if the stem doesn't match any current
    chapter — typically because the EPUB itself was changed.
    """
    for block_idx, one_idx, s, block, skipped in _walk_chapters(proj):
        if not skipped and s == stem:
            return block_idx, one_idx, block
    raise HTTPException(404, f"chapter stem not found: {stem}")


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

    overridden = overridden_indices(proj.dir)
    chapters = []
    rendered_count = 0
    last_one_idx = 0
    for block_idx, one_idx, stem, block, skipped in _walk_chapters(proj):
        text_full = " ".join(block.sentences).strip() if block.sentences else (block.text or "")
        char_count = len(text_full)
        preview = text_full[:140]
        is_overridden = block_idx in overridden

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
                "has_override": is_overridden,
            })
            continue

        last_one_idx = one_idx
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
            "has_override": is_overridden,
        })

    return {
        "chapters": chapters,
        "renderable_total": last_one_idx,
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
    _validate_stem(stem)
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
    _validate_stem(stem)
    target = proj.chunks_dir / stem / f"{stem}.wav"
    if not target.exists():
        raise HTTPException(404, "chapter audio not found")
    return FileResponse(target, media_type="audio/wav")


# ----------------------------------------------------------------------------
# Per-chapter text override
# ----------------------------------------------------------------------------


class ChapterTextRequest(BaseModel):
    text: str


def _detect_auto_pause(block, project) -> dict | None:
    """Probe whether inject_header_pause would modify this block's first
    sentence at render time. If so, return a small descriptor so the UI
    can show "auto-pause: 'Header X' will get [pause][break] inserted"
    above the editor. Returns None if no auto-injection would happen.
    """
    if not block.sentences:
        return None
    lang = (project.book.language or "cs").split("-")[0].split("_")[0].lower()
    if lang != "cs":
        return None
    section_headers = tuple(project.params.section_headers or ())
    original_first = block.sentences[0]
    new_sentences = inject_header_pause(
        block.sentences, lang=lang, section_headers=section_headers,
    )
    new_first = new_sentences[0]
    if new_first == original_first:
        return None
    # Find which header matched. Try character/POV headers first
    # (project-supplied), then the time-marker regex.
    for h in section_headers:
        if original_first.startswith(h + " "):
            return {"header": h, "type": "section_header"}
    m = TIME_HEADER_RE_CS.match(original_first)
    if m:
        return {"header": original_first[:m.end()], "type": "time_marker"}
    return {"header": "(detected)", "type": "unknown"}


def _chapter_text_payload(slug: str, proj, block_idx: int, one_idx: int,
                          block, original_block) -> dict:
    """Build the JSON response for GET /chapters/{stem}/text. Includes
    both the current text (override or original) and the original from
    the EPUB so the UI can show diff / Reset to original.

    The stem is pinned to the ORIGINAL block's leading text so it stays
    stable across edits — same contract as :func:`_walk_chapters`.
    """
    current_text = block.text or "\n".join(block.sentences)
    original_text = original_block.text or "\n".join(original_block.sentences)
    is_overridden = block.text != original_block.text
    chunks = make_chunks(
        block.sentences,
        min_chars=proj.params.min_chars or DEFAULT_MIN_CHARS,
        max_chars=proj.params.max_chars or DEFAULT_MAX_CHARS,
    )
    leading_original = original_block.text or (
        original_block.sentences[0] if original_block.sentences else ""
    )
    return {
        "stem": f"{one_idx:03d}_{compute_chapter_stem(leading_original)}",
        "block_index": block_idx,
        "renderable_index": one_idx,
        "text": current_text,
        "original_text": original_text,
        "has_override": is_overridden,
        "char_count": len(current_text),
        "estimated_chunks": len(chunks),
        "min_chars": proj.params.min_chars or DEFAULT_MIN_CHARS,
        "max_chars": proj.params.max_chars or DEFAULT_MAX_CHARS,
        "auto_pause": _detect_auto_pause(block, proj),
    }


def _resolve_with_original(proj, stem: str):
    """Like _resolve_stem but also returns the original (pre-override)
    block. Needed so we can show "Reset to original" diff in the editor.
    """
    block_idx, one_idx, block = _resolve_stem(proj, stem)
    originals = _parse_original_blocks(proj)
    return block_idx, one_idx, block, originals[block_idx]


@router.get("/{slug}/chapters/{stem}/text")
def get_chapter_text(slug: str, stem: str):
    """Return the current text + original text + chunk preview metadata
    for one chapter. Powers the chapter editor modal."""
    proj = load_project_or_404(slug)
    _validate_stem(stem)
    block_idx, one_idx, block, original_block = _resolve_with_original(proj, stem)
    return _chapter_text_payload(slug, proj, block_idx, one_idx, block, original_block)


@router.put("/{slug}/chapters/{stem}/text")
def put_chapter_text(slug: str, stem: str, req: ChapterTextRequest):
    """Persist a per-chapter text override. Empty text is rejected — use
    DELETE to revert. Cache invalidation is automatic via the manifest
    signature (chunk text comparison flips after re-chunking)."""
    proj = load_project_or_404(slug)
    _validate_stem(stem)
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "text cannot be empty — DELETE to revert to original")
    block_idx, _one_idx, _block = _resolve_stem(proj, stem)
    save_override(proj.dir, block_idx, text)
    proj.append_log(f"chapter text override saved: block_{block_idx:03d} ({stem})")
    # Re-resolve after save so the response reflects the new state.
    block_idx, one_idx, block, original_block = _resolve_with_original(proj, stem)
    return _chapter_text_payload(slug, proj, block_idx, one_idx, block, original_block)


@router.delete("/{slug}/chapters/{stem}/text")
def delete_chapter_text(slug: str, stem: str):
    """Revert this chapter's text to the EPUB original. Returns the
    refreshed payload (now without override). 404 if there was no
    override to remove."""
    proj = load_project_or_404(slug)
    _validate_stem(stem)
    block_idx, _one_idx, _block = _resolve_stem(proj, stem)
    if not delete_override(proj.dir, block_idx):
        raise HTTPException(404, f"no override to delete for block {block_idx}")
    proj.append_log(f"chapter text override reset: block_{block_idx:03d} ({stem})")
    block_idx, one_idx, block, original_block = _resolve_with_original(proj, stem)
    return _chapter_text_payload(slug, proj, block_idx, one_idx, block, original_block)
