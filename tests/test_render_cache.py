"""Tests for the per-chunk manifest cache (v0.3 SQLite-backed) +
:meth:`ProjectRenderer._params_signature` invariants.

Focus: voice + render-param changes must invalidate cached chunks
instead of silently returning stale audio. The renderer's full
pipeline can't run without a GPU + the OmniVoice model, so we
exercise the helpers directly.

v0.2 used per-chapter manifest.json files; v0.3 stores one row per
chunk in the chunk_manifest table. Same cache invariants — just a
different storage layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from audiomat.render import (
    ProjectRenderer,
    _delete_chunk_entry,
    _get_chunk_entry,
    _put_chunk_entry,
)


# ----------------------------------------------------------------------------
# chunk_manifest UPSERT round-trip
# ----------------------------------------------------------------------------


class TestChunkManifestRoundTrip:
    """Exercises the per-chunk SQL helpers against the isolated_library
    fixture (which sets AUDIOMAT_LIBRARY_ROOT and reloads modules so
    db.get_conn() opens against the tmp DB)."""

    def _seed_project(self):
        """We only need a row in projects to satisfy the FK on
        chunk_manifest.project_slug — no on-disk dir required for
        these unit tests."""
        from audiomat.db import get_conn
        get_conn().execute(
            "INSERT INTO projects "
            "(name_slug, name, book_filename, book_blocks_total, "
            " voice_ref, voice_ref_slug, status_phase, "
            " status_chapters_done, status_chapters_total, created, "
            " params_json) "
            "VALUES ('p1', 'P1', 'b.epub', 10, 'V', 'v', 'draft', "
            "0, 0, '2026-05-14', '{}')"
        )

    def test_empty_lookup_returns_none(self, isolated_library):
        self._seed_project()
        assert _get_chunk_entry("p1", "001_Foo", "chunk_0000.wav") is None

    def test_put_then_get(self, isolated_library):
        self._seed_project()
        _put_chunk_entry("p1", "001_Foo", "chunk_0000.wav",
                          "Ahoj.", "abc123def456", gen_seconds=1.5)
        got = _get_chunk_entry("p1", "001_Foo", "chunk_0000.wav")
        assert got == ("Ahoj.", "abc123def456")

    def test_put_upsert_replaces_existing(self, isolated_library):
        """Hot path: re-render overwrites the row in place. Sig changes
        when params change — verify the new sig sticks."""
        self._seed_project()
        _put_chunk_entry("p1", "001_Foo", "chunk_0000.wav",
                          "Ahoj.", "sig_v1", gen_seconds=1.0)
        _put_chunk_entry("p1", "001_Foo", "chunk_0000.wav",
                          "Ahoj.", "sig_v2", gen_seconds=2.0)
        got = _get_chunk_entry("p1", "001_Foo", "chunk_0000.wav")
        assert got == ("Ahoj.", "sig_v2")

    def test_delete_removes_only_target(self, isolated_library):
        self._seed_project()
        _put_chunk_entry("p1", "001_Foo", "chunk_0000.wav", "a", "s")
        _put_chunk_entry("p1", "001_Foo", "chunk_0001.wav", "b", "s")
        _delete_chunk_entry("p1", "001_Foo", "chunk_0000.wav")
        assert _get_chunk_entry("p1", "001_Foo", "chunk_0000.wav") is None
        assert _get_chunk_entry("p1", "001_Foo", "chunk_0001.wav") == ("b", "s")

    def test_project_delete_cascades_chunks(self, isolated_library):
        """ON DELETE CASCADE on project_slug FK means dropping a project
        cleans up its chunks without an explicit purge step."""
        self._seed_project()
        _put_chunk_entry("p1", "001_Foo", "chunk_0000.wav", "a", "s")
        from audiomat.db import get_conn
        get_conn().execute("DELETE FROM projects WHERE name_slug='p1'")
        assert _get_chunk_entry("p1", "001_Foo", "chunk_0000.wav") is None


# ----------------------------------------------------------------------------
# _params_signature
# ----------------------------------------------------------------------------


@dataclass
class _StubParams:
    num_step: int = 48
    guidance_scale: float = 2.0
    speed: float = 1.0
    min_chars: int = 90
    max_chars: int = 200
    target_lufs: float = -16
    silence_gap_ms: int = 200
    section_headers: tuple = ()


@dataclass
class _StubBook:
    language: str = "cs"


@dataclass
class _StubProject:
    params: _StubParams
    book: _StubBook
    chunks_dir: Path


@dataclass
class _StubVoice:
    name_slug: str
    wav_path: Path


def _renderer(tmp_path: Path, *, voice_slug="alice", lang="cs",
              num_step=48, gs=2.0, speed=1.0,
              voice_content: bytes = b"\x00\x00") -> ProjectRenderer:
    """Build a ProjectRenderer with stub project + voice. Voice WAV
    contents control the file mtime indirectly; for mtime control we
    patch via os.utime in the relevant test."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    voice_wav = tmp_path / f"{voice_slug}.wav"
    voice_wav.write_bytes(voice_content)
    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir(exist_ok=True)
    proj = _StubProject(
        params=_StubParams(num_step=num_step, guidance_scale=gs, speed=speed),
        book=_StubBook(language=lang),
        chunks_dir=chunks_dir,
    )
    voice = _StubVoice(name_slug=voice_slug, wav_path=voice_wav)
    # Bypass the real __init__ chunks_root dance — assign attrs directly.
    r = ProjectRenderer.__new__(ProjectRenderer)
    r.project = proj
    r.voice = voice
    r.tts = None
    r.blocks = []
    r.chunks_root = chunks_dir
    r.pronunciations = {}
    return r


class TestParamsSignature:
    def test_deterministic(self, tmp_path: Path):
        r1 = _renderer(tmp_path / "a")
        r2 = _renderer(tmp_path / "b")  # same content, different dir
        # Different parent dirs → different mtimes → different sigs.
        # Confirm same renderer twice yields same sig:
        assert r1._params_signature() == r1._params_signature()

    def test_voice_change_invalidates(self, tmp_path: Path):
        r_alice = _renderer(tmp_path / "1", voice_slug="alice")
        r_bob = _renderer(tmp_path / "2", voice_slug="bob")
        assert r_alice._params_signature() != r_bob._params_signature()

    def test_num_step_change_invalidates(self, tmp_path: Path):
        r_a = _renderer(tmp_path / "1", num_step=32)
        r_b = _renderer(tmp_path / "2", num_step=48)
        # Same voice content but different params and different dirs;
        # we want to assert num_step DOES affect the sig regardless of
        # any mtime difference, so isolate by giving same wav contents.
        # Both stubs already use b"\x00\x00", but mtimes differ. Test
        # the params-only delta by comparing two renderers with
        # identical voice files (same dir) but different num_step:
        same_dir = tmp_path / "shared"
        same_dir.mkdir()
        (same_dir / "alice.wav").write_bytes(b"x")
        params1 = _StubParams(num_step=32)
        params2 = _StubParams(num_step=48)

        def _build(p):
            r = ProjectRenderer.__new__(ProjectRenderer)
            r.project = _StubProject(params=p, book=_StubBook(),
                                      chunks_dir=same_dir)
            r.voice = _StubVoice(name_slug="alice",
                                  wav_path=same_dir / "alice.wav")
            r.tts = None
            r.blocks = []
            r.chunks_root = same_dir
            r.pronunciations = {}
            return r

        assert _build(params1)._params_signature() != _build(params2)._params_signature()

    def test_guidance_scale_change_invalidates(self, tmp_path: Path):
        wav = tmp_path / "v.wav"
        wav.write_bytes(b"x")

        def _build(gs):
            r = ProjectRenderer.__new__(ProjectRenderer)
            r.project = _StubProject(params=_StubParams(guidance_scale=gs),
                                      book=_StubBook(), chunks_dir=tmp_path)
            r.voice = _StubVoice(name_slug="v", wav_path=wav)
            r.tts = None
            r.blocks = []
            r.chunks_root = tmp_path
            r.pronunciations = {}
            return r

        assert _build(2.0)._params_signature() != _build(2.5)._params_signature()

    def test_language_change_invalidates(self, tmp_path: Path):
        wav = tmp_path / "v.wav"
        wav.write_bytes(b"x")

        def _build(lang):
            r = ProjectRenderer.__new__(ProjectRenderer)
            r.project = _StubProject(params=_StubParams(),
                                      book=_StubBook(language=lang),
                                      chunks_dir=tmp_path)
            r.voice = _StubVoice(name_slug="v", wav_path=wav)
            r.tts = None
            r.blocks = []
            r.chunks_root = tmp_path
            r.pronunciations = {}
            return r

        assert _build("cs")._params_signature() != _build("en")._params_signature()

    def test_voice_mtime_change_invalidates(self, tmp_path: Path):
        """Re-recording voice.wav with the same slug must invalidate cached
        chunks — otherwise users would hear the old voice on a re-render
        after they explicitly replaced it."""
        import os
        import time

        wav = tmp_path / "v.wav"
        wav.write_bytes(b"original")
        r = ProjectRenderer.__new__(ProjectRenderer)
        r.project = _StubProject(params=_StubParams(),
                                  book=_StubBook(), chunks_dir=tmp_path)
        r.voice = _StubVoice(name_slug="v", wav_path=wav)
        r.tts = None
        r.blocks = []
        r.chunks_root = tmp_path
        r.pronunciations = {}
        sig_before = r._params_signature()

        # Bump mtime forward by a full second so int() comparison flips.
        time.sleep(0.01)
        future = int(time.time()) + 5
        os.utime(wav, (future, future))
        sig_after = r._params_signature()
        assert sig_before != sig_after

    def test_returns_16_hex_chars(self, tmp_path: Path):
        r = _renderer(tmp_path)
        sig = r._params_signature()
        assert len(sig) == 16
        assert all(c in "0123456789abcdef" for c in sig)
