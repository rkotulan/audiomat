"""Tests for the backup/restore feature.

Strategy: stage a v0.3 library on disk + DB (via the conftest
fixture's isolated tmp lib), call the export/restore helpers
directly, and verify the round-trip is faithful.

Endpoint smoke tests use FastAPI TestClient — they assert wiring
(routes registered, validation paths) without hauling the full TTS
machinery.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from audiomat.backup import (
    BackupScope,
    estimate_size,
    export_zip,
    restore_zip,
)


# ---- helpers --------------------------------------------------------------


def _seed_library(library_root: Path) -> None:
    """Lay out a v0.3-shaped library: 1 voice + 1 project (with book +
    chapter override + chunk WAV + final.m4b) and the corresponding
    DB rows. Mirrors the shape a real install would have."""
    from audiomat.db import get_conn
    conn = get_conn()
    conn.execute(
        "INSERT INTO voices (name_slug, name, duration_s, sample_rate, "
        "channels, transcript_chars, notes, created, tts_model) "
        "VALUES ('alice', 'Alice', 8.0, 24000, 1, 14, '', "
        "'2026-05-14T00:00:00Z', NULL)"
    )
    conn.execute(
        "INSERT INTO projects (name_slug, name, book_filename, "
        "book_blocks_total, voice_ref, voice_ref_slug, status_phase, "
        "status_chapters_done, status_chapters_total, created, "
        "params_json) "
        "VALUES ('p1', 'P1', 'book.epub', 100, 'Alice', 'alice', 'draft', "
        "0, 0, '2026-05-14T00:00:00Z', '{}')"
    )

    vdir = library_root / "voices" / "alice"
    vdir.mkdir(parents=True)
    (vdir / "voice.wav").write_bytes(b"\xff" * 1024)
    (vdir / "voice.txt").write_text("transcript", encoding="utf-8")

    pdir = library_root / "projects" / "p1"
    pdir.mkdir(parents=True)
    (pdir / "book.epub").write_bytes(b"epub binary")
    (pdir / "pronunciations.json").write_text('{"foo": "fú"}', encoding="utf-8")
    (pdir / "chapters").mkdir()
    (pdir / "chapters" / "001_intro.txt").write_text(
        "user override text", encoding="utf-8"
    )
    chunk_dir = pdir / "chunks" / "001_intro"
    chunk_dir.mkdir(parents=True)
    (chunk_dir / "chunk_0000.wav").write_bytes(b"\x00" * 4096)
    (chunk_dir / "001_intro.wav").write_bytes(b"\x00" * 8192)
    (pdir / "final.m4b").write_bytes(b"M4B" * 1000)

    # cache/ is a special excluded subtree — should NEVER show up in any
    # backup tier even when we add files to it.
    cache_dir = library_root / "cache"
    cache_dir.mkdir()
    (cache_dir / "huge_model.bin").write_bytes(b"X" * 100_000)


def _client(isolated_library):
    import importlib
    import audiomat.api
    importlib.reload(audiomat.api)
    return TestClient(audiomat.api.app)


# ---- size estimate --------------------------------------------------------


class TestEstimateSize:
    def test_empty_library(self, isolated_library):
        preview = estimate_size(isolated_library)
        assert preview.essentials_bytes == 0
        assert preview.renders_bytes == 0
        assert preview.finals_bytes == 0

    def test_seeded_library_buckets_correctly(self, isolated_library):
        _seed_library(isolated_library)
        preview = estimate_size(isolated_library)
        # essentials: audiomat.db + voice.wav (1024) + voice.txt (10) +
        # book.epub (11) + pronunciations.json + chapters/001_intro.txt
        assert preview.essentials_bytes > 1024
        # renders: chunk_0000.wav (4096) + 001_intro.wav (8192)
        assert preview.renders_bytes == 4096 + 8192
        # finals: final.m4b (3000)
        assert preview.finals_bytes == 3000


# ---- export round-trip ----------------------------------------------------


class TestExportZip:
    def test_essentials_excludes_renders_and_finals_and_cache(self, isolated_library):
        _seed_library(isolated_library)
        buf = export_zip(isolated_library, BackupScope())
        names = set(zipfile.ZipFile(io.BytesIO(buf.getvalue())).namelist())
        assert "audiomat.db" in names
        assert "voices/alice/voice.wav" in names
        assert "voices/alice/voice.txt" in names
        assert "projects/p1/book.epub" in names
        assert "projects/p1/pronunciations.json" in names
        assert "projects/p1/chapters/001_intro.txt" in names
        # NOT renders / finals / cache
        assert not any(n.startswith("projects/p1/chunks/") for n in names)
        assert "projects/p1/final.m4b" not in names
        assert not any(n.startswith("cache/") for n in names)

    def test_include_renders_adds_chunk_wavs(self, isolated_library):
        _seed_library(isolated_library)
        buf = export_zip(isolated_library, BackupScope(include_renders=True))
        names = set(zipfile.ZipFile(io.BytesIO(buf.getvalue())).namelist())
        assert "projects/p1/chunks/001_intro/chunk_0000.wav" in names
        assert "projects/p1/chunks/001_intro/001_intro.wav" in names
        # but still no finals or cache
        assert "projects/p1/final.m4b" not in names
        assert not any(n.startswith("cache/") for n in names)

    def test_include_finals_adds_m4b(self, isolated_library):
        _seed_library(isolated_library)
        buf = export_zip(isolated_library, BackupScope(include_finals=True))
        names = set(zipfile.ZipFile(io.BytesIO(buf.getvalue())).namelist())
        assert "projects/p1/final.m4b" in names


# ---- restore --------------------------------------------------------------


class TestRestore:
    def test_round_trip_preserves_essentials(self, isolated_library, tmp_path):
        """Export → wipe → restore → seeded files reappear with same
        contents. The DB must be loadable through the v0.3 ORM after."""
        _seed_library(isolated_library)
        zip_bytes = export_zip(
            isolated_library,
            BackupScope(include_renders=True, include_finals=True),
        ).getvalue()

        report = restore_zip(
            isolated_library, zip_bytes,
            pre_restore_dir=tmp_path / "pre_restore.zip",
        )
        assert report.files_extracted > 0
        assert (isolated_library / "voices" / "alice" / "voice.wav").read_bytes() == b"\xff" * 1024
        assert (isolated_library / "projects" / "p1" / "final.m4b").read_bytes() == b"M4B" * 1000

        # DB still loads.
        from audiomat.voice import Voice
        from audiomat.project import Project
        assert Voice.exists("alice")
        assert Project.exists("p1")

    def test_pre_restore_snapshot_is_written(self, isolated_library, tmp_path_factory):
        # Snapshot has to live OUTSIDE the library root — otherwise the
        # wipe step (which iterates library_root.iterdir()) removes it
        # before the test gets to assert. tmp_path_factory gives us a
        # separate tmp dir from the isolated_library fixture's tmp.
        snapshot_path = tmp_path_factory.mktemp("snap") / "before.zip"
        _seed_library(isolated_library)
        zip_bytes = export_zip(isolated_library, BackupScope()).getvalue()
        report = restore_zip(
            isolated_library, zip_bytes, pre_restore_dir=snapshot_path,
        )
        assert report.pre_restore_snapshot == snapshot_path
        assert snapshot_path.exists() and snapshot_path.stat().st_size > 0

    def test_cache_dir_preserved_across_restore(self, isolated_library, tmp_path):
        """The HF model cache must NOT be wiped by a restore — it's
        regenerable but huge to re-download."""
        _seed_library(isolated_library)
        zip_bytes = export_zip(isolated_library, BackupScope()).getvalue()
        restore_zip(
            isolated_library, zip_bytes,
            pre_restore_dir=tmp_path / "pre.zip",
        )
        assert (isolated_library / "cache" / "huge_model.bin").exists()

    def test_rejects_zip_without_db(self, isolated_library, tmp_path):
        """If the upload doesn't have audiomat.db at the root it's not
        an audiomat backup — bail before wiping."""
        rogue = io.BytesIO()
        with zipfile.ZipFile(rogue, "w") as zf:
            zf.writestr("not_audiomat.db", b"x")
        with pytest.raises(ValueError, match="audiomat.db"):
            restore_zip(
                isolated_library, rogue.getvalue(),
                pre_restore_dir=tmp_path / "pre.zip",
            )

    def test_rejects_path_traversal(self, isolated_library, tmp_path):
        rogue = io.BytesIO()
        with zipfile.ZipFile(rogue, "w") as zf:
            zf.writestr("audiomat.db", b"x")
            zf.writestr("../etc/evil", b"x")
        with pytest.raises(ValueError, match="path-traversal"):
            restore_zip(
                isolated_library, rogue.getvalue(),
                pre_restore_dir=tmp_path / "pre.zip",
            )

    def test_rejects_non_zip(self, isolated_library, tmp_path):
        with pytest.raises(ValueError, match="ZIP"):
            restore_zip(
                isolated_library, b"definitely not a zip",
                pre_restore_dir=tmp_path / "pre.zip",
            )


# ---- endpoint smoke -------------------------------------------------------


class TestEndpoints:
    def test_routes_registered(self, isolated_library):
        c = _client(isolated_library)
        have = {(r.path, tuple(sorted(r.methods))) for r in c.app.routes
                if hasattr(r, "methods")}
        assert ("/api/backup/preview", ("GET",)) in have
        assert ("/api/backup/export", ("GET",)) in have
        assert ("/api/backup/restore", ("POST",)) in have

    def test_preview_zero_for_empty_library(self, isolated_library):
        c = _client(isolated_library)
        r = c.get("/api/backup/preview")
        assert r.status_code == 200
        body = r.json()
        assert body["essentials_bytes"] == 0
        assert body["renders_bytes"] == 0
        assert body["finals_bytes"] == 0

    def test_export_streams_zip_with_db(self, isolated_library):
        _seed_library(isolated_library)
        c = _client(isolated_library)
        r = c.get("/api/backup/export")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        assert "audiomat-backup-" in r.headers["content-disposition"]
        names = set(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
        assert "audiomat.db" in names

    def test_restore_400_on_invalid_upload(self, isolated_library):
        c = _client(isolated_library)
        r = c.post(
            "/api/backup/restore",
            files={"archive": ("notazip.zip", b"garbage", "application/zip")},
        )
        assert r.status_code == 400
