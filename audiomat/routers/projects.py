"""Project CRUD + metadata patching.

Owns project create/list/get/delete plus PATCH endpoints for params,
blocks-skipped (with orphan-chunk auto-prune), and book metadata. Also
hosts the front-matter / DRM watermark auto-skip heuristic used at
project creation.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile

from audiomat.epub import parse_epub
from audiomat.project import Project, RenderParams
from audiomat.pronunciations import (
    load_pronunciations,
    save_pronunciations,
)
from audiomat.schemas import (
    BlocksSkippedRequest,
    BookMetaRequest,
    ProjectOut,
    ProjectVoiceRequest,
)
from audiomat.slug import chapter_stem as compute_chapter_stem
from audiomat.state import (
    PATHS,
    RENDER_QUEUES,
    book_blocks,
    dataclass_to_dict,
    load_project_or_404,
)
from audiomat.voice import Voice


router = APIRouter(prefix="/api/projects", tags=["projects"])


# ----------------------------------------------------------------------------
# Front-matter / DRM auto-skip heuristic
# ----------------------------------------------------------------------------


# Patterns that mark a block as DRM / copyright / metadata noise rather
# than book prose. Matched case-insensitively as substrings. Common Czech
# Palmknihy / nakladatel watermark phrases land here. Add new patterns
# only when you've seen them clobber a real preview.
METADATA_PATTERNS = (
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


def is_metadata_block(text: str) -> bool:
    lower = text.lower()
    return any(pat in lower for pat in METADATA_PATTERNS)


def auto_skip_indices(blocks: list, max_scan: int = 10) -> list[int]:
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
        if is_metadata_block(text):
            out.append(i)
    return out


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------


@router.get("", response_model=list[ProjectOut])
def list_projects():
    return [ProjectOut.from_project(p) for p in Project.list_all(PATHS.projects_root)]


@router.get("/{slug}", response_model=ProjectOut)
def get_project(slug: str):
    target = PATHS.project_dir(slug)
    if not (target / "config.json").exists():
        raise HTTPException(404, f"project not found: {slug}")
    return ProjectOut.from_project(Project.load(target))


@router.post("", response_model=ProjectOut)
async def create_project(
    name: str = Form(...),
    voice_ref: str = Form(...),
    book: UploadFile = File(...),
    overwrite: bool = Form(False),
    language: str | None = Form(None),
):
    """Create a new project. Parses EPUB metadata to populate book info.

    ``language`` is honored for ``.txt`` uploads (which carry no
    metadata of their own); for ``.epub`` it's a fallback used only when
    the file's DC metadata is missing.
    """
    if not name.strip():
        raise HTTPException(400, "name is required")
    voice = Voice.find_by_name(voice_ref)
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
                "blocks_skipped": auto_skip_indices(blocks),
                "title": meta.title,
                "author": meta.author,
                "language": meta.language or language or "cs",
            }
        except Exception as e:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise HTTPException(400, f"EPUB parse failed: {e}")
    elif suffix == ".txt":
        # Plain text has no metadata channel — language must be supplied
        # by the user (defaults to cs to keep the Czech-first audiobook
        # use case zero-config).
        book_meta = {
            "language": (language or "cs").strip() or "cs",
        }

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


def _renderable_stems(proj: Project) -> set[str]:
    """Compute the set of valid per-chapter stems for the project given
    its current blocks_skipped. Used by the orphan-cleanup pass after
    blocks_skipped changes."""
    blocks = book_blocks(proj)
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


@router.patch("/{slug}/blocks-skipped")
def update_blocks_skipped(slug: str, req: BlocksSkippedRequest):
    """Replace project.book.blocks_skipped + auto-prune any chunks dirs
    that no longer match the new renderable list.

    Removed dir count is logged into render_log.txt and returned in the
    response under ``orphans_removed`` (added field beyond ProjectOut)."""
    proj = load_project_or_404(slug)
    proj.book.blocks_skipped = sorted(set(req.indices))
    proj.save()
    pruned = _prune_orphan_chunks(proj)
    if pruned > 0:
        proj.append_log(f"pruned {pruned} orphan chapter dir(s) after blocks_skipped change")
    out = ProjectOut.from_project(proj).model_dump()
    out["orphans_removed"] = pruned
    return out


@router.patch("/{slug}/params")
def update_project_params(slug: str, params: dict):
    """Update render params (preview matrix selection, advanced edits).

    Voice change is **not** allowed here — invalidates whole cache;
    a separate endpoint will handle that with explicit confirmation."""
    proj = load_project_or_404(slug)
    current = dataclass_to_dict(proj.params)
    current.update(params)
    proj.params = RenderParams(**{k: current.get(k) for k in current
                                   if k in RenderParams.__dataclass_fields__})
    proj.save()
    return ProjectOut.from_project(proj)


# Lowercase primary subtag (2–3 letters) optionally followed by a region
# / script suffix (cs-CZ, pt-BR, zh-Hant). Mirrors the frontend regex.
_LANG_RE = re.compile(r"^[a-z]{2,3}(-[a-zA-Z]{2,4})?$")


@router.patch("/{slug}/voice", response_model=ProjectOut)
def update_project_voice(slug: str, req: ProjectVoiceRequest):
    """Swap the project's reference voice. Resolves the slug against the
    voice library and updates ``voice_ref`` + ``voice_ref_slug``.

    Cache impact: every cached chunk's manifest signature includes the
    voice slug (see ``ProjectRenderer._params_signature`` in render.py),
    so swap → automatic re-synth on next render. We don't pre-emptively
    delete chunks here — keeping them around lets the user swap *back*
    to the previous voice with no work.
    """
    proj = load_project_or_404(slug)
    try:
        voice = Voice.load(req.voice_slug)
    except FileNotFoundError:
        raise HTTPException(404, f"voice not found: {req.voice_slug}")
    proj.voice_ref = voice.name
    proj.voice_ref_slug = voice.name_slug
    proj.save()
    return ProjectOut.from_project(proj)


@router.patch("/{slug}/book", response_model=ProjectOut)
def update_project_book(slug: str, req: BookMetaRequest):
    """Patch the project's stored book metadata. Currently only
    ``language`` — used to override mis-detected EPUB DC metadata or
    correct a TXT project that was created with the wrong default.

    Note: changing the language doesn't invalidate the manifest cache
    (which hashes only chunk text), so already-rendered chapters stay
    on disk. Re-render per chapter to pick up the new language for
    number-to-text expansion (``1959`` → ``tisíc devět set padesát
    devět`` for cs).
    """
    proj = load_project_or_404(slug)
    if req.language is not None:
        norm = req.language.strip()
        if not norm:
            raise HTTPException(400, "language cannot be empty")
        if not _LANG_RE.fullmatch(norm):
            raise HTTPException(
                400,
                f"invalid language code {norm!r}: use ISO 639-1 (cs, en) "
                f"or BCP 47 (cs-CZ, pt-BR)",
            )
        proj.book.language = norm
    proj.save()
    return ProjectOut.from_project(proj)


@router.delete("/{slug}")
def delete_project(slug: str):
    target = PATHS.project_dir(slug)
    if not target.exists():
        raise HTTPException(404, f"project not found: {slug}")
    Project.load(target).delete()
    if slug in RENDER_QUEUES:
        RENDER_QUEUES.pop(slug, None)
    return {"deleted": slug}


# ----------------------------------------------------------------------------
# Pronunciation dictionary
# ----------------------------------------------------------------------------


@router.get("/{slug}/pronunciations")
def get_pronunciations(slug: str) -> dict[str, str]:
    """Return the project's pronunciation map. Empty dict if none set."""
    proj = load_project_or_404(slug)
    return load_pronunciations(proj.dir)


@router.put("/{slug}/pronunciations")
def put_pronunciations(slug: str, mapping: dict = Body(...)):
    """Replace the project's pronunciation map. Empty body deletes the
    file entirely. Cache invalidation is handled by the render
    signature — changing the dict bumps the per-chunk sig, forcing
    re-synth on the next render. Returns the saved map (with empty
    keys filtered out).

    Accepted shape: ``{string: string}``. Non-string values raise 400
    instead of being silently coerced — bad client data should fail
    loudly.
    """
    proj = load_project_or_404(slug)
    if not isinstance(mapping, dict):
        raise HTTPException(400, "body must be a JSON object {string: string}")
    for k, v in mapping.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise HTTPException(400, "pronunciations must be string→string")
    save_pronunciations(proj.dir, mapping)
    saved = load_pronunciations(proj.dir)
    proj.append_log(f"pronunciations updated: {len(saved)} entries")
    return saved
