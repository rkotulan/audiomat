"""Per-chapter text overrides.

Lets the user edit a single chapter's text in a modal without modifying
the source EPUB. Overrides live in ``<project>/overrides/block_NNN.txt``
where NNN is the 0-based block_index from :func:`audiomat.epub.parse_epub`.

Why block_index and not the chapter stem:

* The stem (``001_Zima_2019``) is derived from the leading text via
  :func:`audiomat.slug.chapter_stem` — editing the text would change
  the stem, orphaning every cached chunk under the old name.
* The renderable index (``001``) shifts whenever the user toggles
  ``blocks_skipped`` (skipping block 0 makes block 1 → renderable 1).
* The block_index is the original position in the EPUB spine parse
  and stays stable as long as the EPUB itself doesn't change.

Cache invalidation is automatic via the manifest signature fix in
:mod:`audiomat.render`: a different chunk text (which is what the
override produces after re-chunking) flips the per-chunk text
comparison and forces re-synth on the next render.
"""
from __future__ import annotations

from pathlib import Path

from audiomat.epub import Block, split_sentences


OVERRIDES_DIRNAME = "overrides"
_FILE_TEMPLATE = "block_{:03d}.txt"
_FILE_PREFIX = "block_"


def overrides_dir(project_dir: Path) -> Path:
    return project_dir / OVERRIDES_DIRNAME


def override_path(project_dir: Path, block_index: int) -> Path:
    if block_index < 0:
        raise ValueError(f"block_index must be >= 0, got {block_index}")
    return overrides_dir(project_dir) / _FILE_TEMPLATE.format(block_index)


def has_override(project_dir: Path, block_index: int) -> bool:
    return override_path(project_dir, block_index).exists()


def load_override(project_dir: Path, block_index: int) -> str | None:
    """Return the override text for ``block_index`` or None if none set."""
    p = override_path(project_dir, block_index)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def save_override(project_dir: Path, block_index: int, text: str) -> None:
    """Persist ``text`` as the override for ``block_index``. Creates the
    overrides/ dir on first use. Raises ValueError on empty text — use
    :func:`delete_override` to revert to the EPUB original instead."""
    if not text.strip():
        raise ValueError("override text cannot be empty — use delete_override to revert")
    d = overrides_dir(project_dir)
    d.mkdir(exist_ok=True)
    override_path(project_dir, block_index).write_text(text, encoding="utf-8")


def delete_override(project_dir: Path, block_index: int) -> bool:
    """Remove the override file for ``block_index``. Returns True if a
    file was removed, False if there was nothing to remove."""
    p = override_path(project_dir, block_index)
    if not p.exists():
        return False
    p.unlink()
    return True


def overridden_indices(project_dir: Path) -> set[int]:
    """Return the set of block indices that currently have an override
    file. Used by /chapters to populate the ``has_override`` flag in one
    pass instead of stat'ing every block."""
    d = overrides_dir(project_dir)
    if not d.exists():
        return set()
    out: set[int] = set()
    for p in d.glob(f"{_FILE_PREFIX}*.txt"):
        try:
            n = int(p.stem.removeprefix(_FILE_PREFIX))
            out.add(n)
        except ValueError:
            continue
    return out


def apply_overrides(blocks: list[Block], project_dir: Path) -> list[Block]:
    """Return a new block list with any per-block overrides merged in.

    Only blocks that have an override file are replaced; the rest pass
    through untouched. Override text is re-split into sentences via the
    same Czech-aware splitter parse_epub uses, so the chunker sees a
    coherent input. ``keep`` and ``source_id`` are preserved from the
    original block — overriding the text doesn't un-skip a chapter.
    """
    d = overrides_dir(project_dir)
    if not d.exists():
        return blocks
    out = list(blocks)
    for i, block in enumerate(out):
        text = load_override(project_dir, i)
        if text is None:
            continue
        out[i] = Block(
            text=text,
            sentences=split_sentences(text),
            keep=block.keep,
            source_id=block.source_id,
        )
    return out
