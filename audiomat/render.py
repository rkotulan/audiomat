"""Project rendering orchestration.

Ties the rest of the pipeline together: per-chapter chunks via
:mod:`audiomat.chunker`, header pause-injection via :mod:`audiomat.headers`,
generation via :mod:`audiomat.tts`, concat + loudness via
:mod:`audiomat.audio`. Persistence is handled here — manifest written
**per chunk** (not per chapter, fixing the CLAUDE.md gotcha #render_omnivoice
mid-chapter cache loss).

The renderer is structured as a generator: callers iterate
:meth:`ProjectRenderer.render_all` to receive :class:`ProgressEvent` objects
as work progresses. The FastAPI layer maps these events into an SSE stream.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

from audiomat.audio import concat_chunks_loudnorm
from audiomat.chunker import make_chunks
from audiomat.epub import Block
from audiomat.headers import inject_header_pause
from audiomat.project import Project
from audiomat.slug import chapter_stem
from audiomat.tts import OmniVoiceTTS
from audiomat.voice import Voice


# ----------------------------------------------------------------------------
# Progress events
# ----------------------------------------------------------------------------


@dataclass
class ProgressEvent:
    """One progress tick. ``kind`` drives interpretation:

    * ``chunk_synthed`` — fresh synthesis just finished. Has gen_seconds /
      rtf / duration_s.
    * ``chunk_cached`` — manifest hit, no work done. Counts towards progress.
    * ``chapter_concat_start`` — about to ffmpeg the chapter.
    * ``chapter_done`` — chapter WAV written.
    * ``chapter_skipped`` — final WAV already exists + all chunks valid.
    * ``render_start`` / ``render_complete`` — top-level wrapping.
    * ``error`` — non-recoverable; ``message`` carries the description.
    """
    kind: str
    chapter_idx: int = -1               # 1-based, matches stem prefix
    chapter_total: int = 0
    chapter_stem: str = ""
    chunk_idx: int = -1                 # 0-based within chapter
    chunk_total: int = 0
    text: str = ""                      # chunk text (truncated for display)
    text_chars: int = 0                 # full chunk text length (for ETA rate calc)
    gen_seconds: float = 0.0
    duration_s: float = 0.0
    rtf: float = 0.0
    message: str = ""

    def to_json_dict(self) -> dict:
        """Serialize to a plain dict for json.dumps / SSE wire format."""
        return asdict(self)


# ----------------------------------------------------------------------------
# Manifest helpers (per-chunk crash-safe persistence)
# ----------------------------------------------------------------------------


def _manifest_path(chap_dir: Path) -> Path:
    return chap_dir / "manifest.json"


def _load_manifest(chap_dir: Path) -> dict[str, str]:
    """Read manifest.json. Returns empty dict on missing / corrupt file."""
    p = _manifest_path(chap_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_manifest(chap_dir: Path, manifest: dict[str, str]) -> None:
    """Write manifest.json atomically (write to .tmp, then rename)."""
    p = _manifest_path(chap_dir)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(p)


# ----------------------------------------------------------------------------
# ProjectRenderer
# ----------------------------------------------------------------------------


class ProjectRenderer:
    """Render one Project end-to-end.

    Construct with a parsed Block list (from
    :func:`audiomat.epub.parse_epub`), a Voice from the library, and a
    pre-instantiated :class:`OmniVoiceTTS` (the renderer doesn't load
    the model — that's the API layer's responsibility, since model load
    is shared across projects).
    """

    def __init__(
        self,
        project: Project,
        voice: Voice,
        tts: OmniVoiceTTS,
        blocks: list[Block],
    ):
        self.project = project
        self.voice = voice
        self.tts = tts
        self.blocks = blocks
        self.chunks_root = project.chunks_dir
        self.chunks_root.mkdir(parents=True, exist_ok=True)

    # -- per-chapter --

    def _chapter_dir(self, chapter_idx: int, block: Block) -> tuple[str, Path]:
        """Compute ``001_<slug>`` stem + chapter dir path. Stem is derived
        from the block's leading text via :func:`chapter_stem` (stops at
        first marker), so ``"Zima 2019[break]Co to bylo?"`` → ``"Zima_2019"``."""
        stem = f"{chapter_idx:03d}_{chapter_stem(block.text or block.sentences[0])}"
        return stem, self.chunks_root / stem

    def _chunk_text_for(self, block: Block) -> list[str]:
        """Run header inject + chunk for one block."""
        sentences = inject_header_pause(
            block.sentences,
            lang=self.project.book.language or "cs",
            section_headers=tuple(self.project.params.section_headers),
        )
        return make_chunks(
            sentences,
            min_chars=self.project.params.min_chars,
            max_chars=self.project.params.max_chars,
        )

    def render_block(
        self,
        chapter_idx: int,
        block: Block,
        chapter_total: int,
    ) -> Iterator[ProgressEvent]:
        """Render one block. Yields ProgressEvent objects."""
        import soundfile as sf

        stem, chap_dir = self._chapter_dir(chapter_idx, block)
        chunks_dir = chap_dir / "chunks"
        chunks_dir.mkdir(parents=True, exist_ok=True)
        final_wav = chap_dir / f"{stem}.wav"

        chunk_texts = self._chunk_text_for(block)
        chunk_total = len(chunk_texts)
        manifest = _load_manifest(chap_dir)

        # Skip the whole chapter if the final WAV already exists AND all
        # chunks are cached. Otherwise we re-concat at the end.
        all_cached = True
        chunk_paths: list[Path] = []

        for j, text in enumerate(chunk_texts):
            wav_name = f"chunk_{j:04d}.wav"
            wav_path = chunks_dir / wav_name
            cached_text = manifest.get(wav_name)
            text_unchanged = (cached_text == text)
            wav_present = wav_path.exists() and wav_path.stat().st_size > 1024

            if wav_present and text_unchanged:
                chunk_paths.append(wav_path)
                yield ProgressEvent(
                    kind="chunk_cached",
                    chapter_idx=chapter_idx,
                    chapter_total=chapter_total,
                    chapter_stem=stem,
                    chunk_idx=j,
                    chunk_total=chunk_total,
                    text=text[:80],
                    text_chars=len(text),
                )
                continue

            # Stale or missing — invalidate and re-synth
            all_cached = False
            if wav_path.exists():
                wav_path.unlink()

            try:
                result = self.tts.generate(
                    text=text,
                    voice=self.voice,
                    params=self.project.params,
                    language=self.project.book.language or "cs",
                )
            except Exception as e:
                yield ProgressEvent(
                    kind="error",
                    chapter_idx=chapter_idx,
                    chapter_total=chapter_total,
                    chapter_stem=stem,
                    chunk_idx=j,
                    chunk_total=chunk_total,
                    text=text[:80],
                    message=f"{type(e).__name__}: {e}",
                )
                raise

            sf.write(
                str(wav_path),
                result.audio,
                result.sample_rate,
                subtype="PCM_16",
            )
            # Persist manifest AFTER each chunk — crash-safe.
            manifest[wav_name] = text
            _save_manifest(chap_dir, manifest)
            chunk_paths.append(wav_path)

            yield ProgressEvent(
                kind="chunk_synthed",
                chapter_idx=chapter_idx,
                chapter_total=chapter_total,
                chapter_stem=stem,
                chunk_idx=j,
                chunk_total=chunk_total,
                text=text[:80],
                text_chars=len(text),
                gen_seconds=result.gen_seconds,
                duration_s=result.duration_s,
                rtf=result.rtf,
            )

        # Drop orphan chunks if chunking shrunk between runs
        expected_names = {f"chunk_{j:04d}.wav" for j in range(chunk_total)}
        orphans_removed = False
        for orphan in sorted(chunks_dir.glob("chunk_*.wav")):
            if orphan.name not in expected_names:
                orphan.unlink()
                manifest.pop(orphan.name, None)
                orphans_removed = True
        if orphans_removed:
            _save_manifest(chap_dir, manifest)

        # If everything was cached AND the final WAV exists, skip the concat.
        if all_cached and final_wav.exists() and final_wav.stat().st_size > 1024 and not orphans_removed:
            yield ProgressEvent(
                kind="chapter_skipped",
                chapter_idx=chapter_idx,
                chapter_total=chapter_total,
                chapter_stem=stem,
                chunk_total=chunk_total,
                message=f"all {chunk_total} chunks cached, final WAV present",
            )
            return

        yield ProgressEvent(
            kind="chapter_concat_start",
            chapter_idx=chapter_idx,
            chapter_total=chapter_total,
            chapter_stem=stem,
            chunk_total=chunk_total,
        )

        if final_wav.exists():
            final_wav.unlink()
        concat_chunks_loudnorm(
            chunk_paths,
            final_wav,
            sample_rate=self.tts.sample_rate,
            silence_gap_ms=self.project.params.silence_gap_ms,
            target_lufs=self.project.params.target_lufs,
        )

        yield ProgressEvent(
            kind="chapter_done",
            chapter_idx=chapter_idx,
            chapter_total=chapter_total,
            chapter_stem=stem,
            chunk_total=chunk_total,
        )

    # -- top-level loops --

    def _renderable_targets(self) -> list[tuple[int, Block]]:
        """List of (one_based_index, Block) for blocks that pass the skip
        list and Block.keep filter. Shared by render_all / render_indices."""
        rendered_blocks = [
            b for i, b in enumerate(self.blocks)
            if i not in self.project.book.blocks_skipped and b.keep
        ]
        return list(enumerate(rendered_blocks, start=1))

    def render_indices(
        self,
        indices: list[int] | tuple[int, ...] | set[int],
    ) -> Iterator[ProgressEvent]:
        """Render only the requested 1-based renderable indices. Targets
        are sorted ascending so cache writes stay deterministic. Other
        chapters are left untouched on disk.

        Used by the UI's "Render selected" / "Render pending" buttons —
        the user picks specific chapters to (re-)render rather than running
        the whole book."""
        all_targets = self._renderable_targets()
        chapter_total = len(all_targets)
        wanted = set(indices)
        targets = sorted(
            [(idx, b) for idx, b in all_targets if idx in wanted],
            key=lambda t: t[0],
        )

        yield ProgressEvent(
            kind="render_start",
            chapter_total=chapter_total,
            message=f"rendering {len(targets)} of {chapter_total} chapter(s) (selected)",
        )
        self.project.set_status(phase="rendering")

        for one_idx, block in targets:
            yield from self.render_block(one_idx, block, chapter_total)

        # Don't force "complete" phase if user only rendered a subset.
        yield ProgressEvent(
            kind="render_complete",
            chapter_total=chapter_total,
            message=f"completed {len(targets)} chapter(s)",
        )

    def render_all(
        self,
        start_chapter: int = 1,
        limit: int | None = None,
    ) -> Iterator[ProgressEvent]:
        """Render every block (skipping ``project.book.blocks_skipped``).

        ``start_chapter`` is 1-based and refers to the renderable index
        AFTER skip-list filtering — useful for resume.
        """
        rendered_blocks = [
            (i, b) for i, b in enumerate(self.blocks)
            if i not in self.project.book.blocks_skipped and b.keep
        ]
        # 1-based renderable index
        targets = list(enumerate(rendered_blocks, start=1))
        if start_chapter > 1:
            targets = [t for t in targets if t[0] >= start_chapter]
        if limit is not None and limit > 0:
            targets = targets[:limit]

        chapter_total = len(rendered_blocks)

        yield ProgressEvent(
            kind="render_start",
            chapter_total=chapter_total,
            message=f"rendering {len(targets)} chapter(s) (start={start_chapter}, limit={limit or 'all'})",
        )

        for one_idx, (_orig_idx, block) in targets:
            yield from self.render_block(one_idx, block, chapter_total)
            self.project.set_status(
                chapters_done=one_idx,
                last_completed=f"{one_idx:03d}_{chapter_stem(block.text or block.sentences[0])}",
                phase="rendering",
            )

        self.project.set_status(
            chapters_done=len(targets),
            phase="complete" if (limit is None and start_chapter == 1) else self.project.status.phase,
        )
        yield ProgressEvent(
            kind="render_complete",
            chapter_total=chapter_total,
            message=f"completed {len(targets)} chapter(s)",
        )


if __name__ == "__main__":
    # Smoke test — verify the module imports and the dataclass serializes.
    e = ProgressEvent(
        kind="chunk_synthed",
        chapter_idx=1, chapter_total=163,
        chapter_stem="001_Zima",
        chunk_idx=0, chunk_total=149,
        text="Zima dva tisíce devatenáct...",
        gen_seconds=2.05, duration_s=9.5, rtf=0.22,
    )
    print(json.dumps(e.to_json_dict(), ensure_ascii=False, indent=2))
