"""Project rendering orchestration.

Ties the rest of the pipeline together: per-chapter chunks via
:mod:`audiomat.chunker`, header pause-injection via :mod:`audiomat.headers`,
generation via :mod:`audiomat.tts`, concat + loudness via
:mod:`audiomat.audio`. Manifest persistence is handled here — one row per
chunk in the ``chunk_manifest`` table (v0.3 migration of the v0.2
per-chapter manifest.json), UPSERTed after each chunk synth so a crash
mid-chapter only loses the in-flight chunk.

The renderer is structured as a generator: callers iterate
:meth:`ProjectRenderer.render_all` to receive :class:`ProgressEvent` objects
as work progresses. The FastAPI layer maps these events into an SSE stream.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

from audiomat.audio import concat_chunks_loudnorm
from audiomat.chunker import make_chunks
from audiomat.db import get_conn
from audiomat.epub import Block
from audiomat.headers import inject_header_pause
from audiomat.project import Project
from audiomat.pronunciations import (
    apply_pronunciations,
    load_pronunciations,
    signature as pronunciations_signature,
)
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
# Manifest helpers (per-chunk SQL — v0.3 migration of v0.2 manifest.json)
# ----------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_chunk_entry(
    project_slug: str, stem: str, chunk_name: str,
) -> tuple[str, str] | None:
    """Look up the cached (text, sig) for one chunk. Returns None when
    the row doesn't exist — render_block treats that as a cache miss
    and re-synthesizes.

    The sig is a 16-char hex digest of every render param that affects
    audio output (voice slug + voice WAV mtime + num_step + gs + speed
    + language + pronunciations dict). A change in any of those shows
    up here as a stored sig != current sig → invalidate.
    """
    row = get_conn().execute(
        "SELECT text, sig FROM chunk_manifest "
        "WHERE project_slug=? AND stem=? AND chunk_name=?",
        (project_slug, stem, chunk_name),
    ).fetchone()
    return (row["text"], row["sig"]) if row else None


def _put_chunk_entry(
    project_slug: str, stem: str, chunk_name: str,
    text: str, sig: str, gen_seconds: float | None = None,
) -> None:
    """UPSERT one chunk row. Called per chunk synth — replaces the v0.2
    "load JSON, mutate dict, write whole file" pattern with a single
    statement that's atomic at the storage layer (no .tmp + rename
    dance needed).

    ``gen_seconds`` is the wall-clock the TTS call took. Stored so the
    ETA estimator can answer "how long did this chunk take last time?"
    without having to keep a separate sidecar file.
    """
    get_conn().execute(
        "INSERT INTO chunk_manifest "
        "(project_slug, stem, chunk_name, text, sig, gen_seconds, created) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(project_slug, stem, chunk_name) DO UPDATE SET "
        "  text=excluded.text, "
        "  sig=excluded.sig, "
        "  gen_seconds=excluded.gen_seconds",
        (project_slug, stem, chunk_name, text, sig, gen_seconds, _utcnow_iso()),
    )


def _delete_chunk_entry(
    project_slug: str, stem: str, chunk_name: str,
) -> None:
    """Drop one chunk row. Used by orphan cleanup when chunking shrunk
    between renders."""
    get_conn().execute(
        "DELETE FROM chunk_manifest "
        "WHERE project_slug=? AND stem=? AND chunk_name=?",
        (project_slug, stem, chunk_name),
    )


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
        tts: "OmniVoiceTTS",       # widened in practice — v0.4 added HiggsTTS
        blocks: list[Block],
    ):
        # The type hint stays OmniVoiceTTS for back-compat with code that
        # imports ProjectRenderer with a narrow annotation. At runtime
        # either OmniVoiceTTS or HiggsTTS works because both expose the
        # same generate(text, voice, params, language) → GenerationResult
        # signature (see tests/test_render_higgs_dispatch.py).
        self.project = project
        self.voice = voice
        self.tts = tts
        self.blocks = blocks
        self.chunks_root = project.chunks_dir
        self.chunks_root.mkdir(parents=True, exist_ok=True)
        # Load the project's pronunciation dict once. Renders are short-
        # lived (one HTTP call) so re-reading mid-render isn't needed;
        # if the user edits the dict during a render, the change takes
        # effect on the next /render call (chunks already in flight
        # finish on the old map, which is the sane behavior).
        self.pronunciations = load_pronunciations(project.dir)

    # -- cache key --

    def _params_signature(self) -> str:
        """Stable hash of every render input that affects audio output but
        is NOT the chunk text itself. Stored alongside each cached chunk
        so a voice swap, num_step change, or **engine swap** invalidates
        instead of silently returning stale audio.

        Inputs:
          * voice slug (renaming a voice file should not invalidate, but
            picking a different voice should — slug captures both)
          * voice WAV mtime (re-recorded voice with same slug → invalidate)
          * num_step, guidance_scale, speed (OmniVoice generation knobs)
          * language (changes num2words expansion of digits)
          * **tts_model slug (v0.5)** — switching the project from
            OmniVoice → Higgs (or to a fine-tune) must invalidate the
            cache; otherwise the renderer would happily serve cached
            chunks generated by a different engine.

        16 hex chars (~64 bits) is plenty — collisions across the small
        config space we actually use are astronomically unlikely.
        """
        try:
            voice_mtime = int(self.voice.wav_path.stat().st_mtime)
        except OSError:
            voice_mtime = 0
        p = self.project.params
        lang = self.project.book.language or "cs"
        # Pronunciation dict hash so an edit/add/remove invalidates
        # cached chunks (otherwise stale audio with the un-substituted
        # word would replay forever).
        pron_sig = pronunciations_signature(self.pronunciations)
        # ``"default"`` and ``None`` both mean "stock OmniVoice" — fold
        # them onto a single canonical string so swapping between None
        # and the literal "default" doesn't bust the cache.
        engine_slug = self.project.tts_model or "default"
        src = (
            f"{self.voice.name_slug}|{voice_mtime}"
            f"|{p.num_step}|{p.guidance_scale}|{p.speed}"
            f"|{lang}|{pron_sig}"
            f"|engine={engine_slug}"
        )
        return hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]

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
        proj_slug = self.project.name_slug
        sig = self._params_signature()

        # Skip the whole chapter if the final WAV already exists AND all
        # chunks are cached. Otherwise we re-concat at the end.
        all_cached = True
        chunk_paths: list[Path] = []

        for j, text in enumerate(chunk_texts):
            wav_name = f"chunk_{j:04d}.wav"
            wav_path = chunks_dir / wav_name
            entry = _get_chunk_entry(proj_slug, stem, wav_name)
            if entry is not None:
                stored_text, stored_sig = entry
                text_unchanged = (stored_text == text)
                sig_unchanged = (stored_sig == sig)
            else:
                text_unchanged = sig_unchanged = False
            wav_present = wav_path.exists() and wav_path.stat().st_size > 1024

            if wav_present and text_unchanged and sig_unchanged:
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
                # Apply pronunciation overrides BEFORE handing text to
                # the TTS model. The manifest still stores the original
                # chunk text for cache equality, while the signature
                # (which includes the dict hash) handles dict-change
                # invalidation.
                text_for_tts = apply_pronunciations(text, self.pronunciations)
                result = self.tts.generate(
                    text=text_for_tts,
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
            # Persist AFTER each chunk — crash-safe. UPSERT into
            # chunk_manifest is atomic at the SQLite layer; sig captures
            # voice + params so a later run with different settings
            # invalidates this row on next read.
            _put_chunk_entry(
                proj_slug, stem, wav_name, text, sig,
                gen_seconds=result.gen_seconds,
            )
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
                _delete_chunk_entry(proj_slug, stem, orphan.name)
                orphans_removed = True

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
