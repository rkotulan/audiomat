"""Tests for the per-chunk manifest cache schema in audiomat.render.

Focuses on the cache-key correctness fix: voice + render-param changes
must invalidate cached chunks instead of silently returning stale audio.

The renderer's full pipeline can't run without a GPU + the OmniVoice
model, so we exercise the helpers directly:

  * :func:`_load_manifest` / :func:`_save_manifest` round-trip the new
    ``{wav_name: {"text", "sig"}}`` schema and tolerate the legacy
    bare-string format (treated as a cache miss).
  * :meth:`ProjectRenderer._params_signature` is deterministic for
    identical inputs and changes when any one input flips.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from audiomat.render import (
    ProjectRenderer,
    _load_manifest,
    _save_manifest,
)


# ----------------------------------------------------------------------------
# Manifest schema round-trip
# ----------------------------------------------------------------------------


class TestManifestRoundTrip:
    def test_empty_manifest(self, tmp_path: Path):
        assert _load_manifest(tmp_path) == {}

    def test_save_then_load_new_schema(self, tmp_path: Path):
        original = {
            "chunk_0000.wav": {"text": "Ahoj.", "sig": "abc123def456"},
            "chunk_0001.wav": {"text": "Jak se máš?", "sig": "abc123def456"},
        }
        _save_manifest(tmp_path, original)
        loaded = _load_manifest(tmp_path)
        assert loaded == original

    def test_load_legacy_string_schema(self, tmp_path: Path):
        # Pre-fix manifests stored bare strings instead of dicts. Loader
        # passes them through unchanged; render_block treats any non-dict
        # value as a cache miss.
        legacy = {"chunk_0000.wav": "Ahoj."}
        (tmp_path / "manifest.json").write_text('{"chunk_0000.wav": "Ahoj."}', encoding="utf-8")
        loaded = _load_manifest(tmp_path)
        assert loaded == legacy
        assert not isinstance(loaded["chunk_0000.wav"], dict), (
            "legacy schema must NOT be auto-promoted; render_block keys on isinstance(_, dict)"
        )

    def test_corrupt_manifest_returns_empty(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text("not json {{", encoding="utf-8")
        assert _load_manifest(tmp_path) == {}

    def test_atomic_write_no_partial_file(self, tmp_path: Path):
        # _save_manifest writes to .tmp then renames. The tmp file must
        # not survive a successful write.
        _save_manifest(tmp_path, {"chunk_0000.wav": {"text": "x", "sig": "y"}})
        assert not (tmp_path / "manifest.json.tmp").exists()
        assert (tmp_path / "manifest.json").exists()


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
