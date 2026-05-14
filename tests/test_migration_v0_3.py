"""Tests for the one-shot v0.2 → v0.3 SQLite migration.

Each test stages a v0.2-shaped library on disk in the
``isolated_library`` tmp dir, runs the migration, and asserts:

* the right number of rows landed in voices / projects / chunk_manifest
* the JSON files got renamed to ``*.v0-2-backup``
* the migration is idempotent (second run = no-op, no warnings)
* corrupt / partial inputs are handled gracefully
"""
from __future__ import annotations

import json
from pathlib import Path

from audiomat.migrations.v0_3_sqlite import (
    _BACKUP_SUFFIX,
    migrate_v0_2_to_v0_3,
)


def _seed_v02_voice(library_root: Path, slug: str, name: str) -> Path:
    """Write a v0.2-shaped meta.json + stub voice.wav/voice.txt."""
    vdir = library_root / "voices" / slug
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "voice.wav").write_bytes(b"\x00" * 256)
    (vdir / "voice.txt").write_text("any transcript", encoding="utf-8")
    meta_path = vdir / "meta.json"
    meta_path.write_text(json.dumps({
        "name": name,
        "name_slug": slug,
        "created": "2026-05-01T00:00:00Z",
        "duration_s": 8.0,
        "sample_rate": 24000,
        "channels": 1,
        "transcript_chars": 14,
        "notes": "",
        "tts_model": None,
    }, ensure_ascii=False), encoding="utf-8")
    return meta_path


def _seed_v02_project(
    library_root: Path,
    slug: str,
    *,
    blocks_skipped: list[int] | None = None,
) -> Path:
    pdir = library_root / "projects" / slug
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "book.epub").write_bytes(b"")
    cfg_path = pdir / "config.json"
    cfg_path.write_text(json.dumps({
        "name": slug,
        "name_slug": slug,
        "created": "2026-05-01T00:00:00Z",
        "last_run": "",
        "book": {
            "filename": "book.epub",
            "blocks_total": 100,
            "blocks_skipped": blocks_skipped or [],
            "title": "Test Book",
            "author": "Anon",
            "language": "cs",
        },
        "voice_ref": "Voice A",
        "voice_ref_slug": "voice_a",
        "params": {
            "num_step": 48, "guidance_scale": 2.0, "speed": 1.0,
            "min_chars": 90, "max_chars": 200, "target_lufs": -16.0,
            "silence_gap_ms": 200, "section_headers": [],
        },
        "status": {
            "chapters_done": 5, "chapters_total": 100,
            "last_completed": "005_Foo", "phase": "rendering",
        },
    }, ensure_ascii=False), encoding="utf-8")
    return cfg_path


def _seed_v02_manifest(
    library_root: Path,
    project_slug: str,
    stem: str,
    entries: dict,
) -> Path:
    """Write a v0.2 chunks/<stem>/manifest.json. Caller controls schema
    via ``entries`` so legacy / mixed inputs can be exercised."""
    chap_dir = library_root / "projects" / project_slug / "chunks" / stem
    chap_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = chap_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8"
    )
    return manifest_path


def _row_count(table: str) -> int:
    from audiomat.db import get_conn
    return get_conn().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608


# ---- happy-path: voice import ---------------------------------------------


def test_voice_imports_and_renames_meta(isolated_library):
    meta_path = _seed_v02_voice(isolated_library, "alice", "Alice")
    report = migrate_v0_2_to_v0_3(isolated_library)
    assert report.voices_migrated == 1
    assert report.warnings == []
    assert _row_count("voices") == 1
    assert not meta_path.exists()
    assert meta_path.with_name("meta.json" + _BACKUP_SUFFIX).exists()


# ---- happy-path: project + blocks_skipped ---------------------------------


def test_project_imports_with_blocks_skipped(isolated_library):
    cfg_path = _seed_v02_project(isolated_library, "p1", blocks_skipped=[3, 7, 12])
    report = migrate_v0_2_to_v0_3(isolated_library)
    assert report.projects_migrated == 1
    assert _row_count("projects") == 1
    assert _row_count("project_blocks_skipped") == 3
    assert not cfg_path.exists()
    assert cfg_path.with_name("config.json" + _BACKUP_SUFFIX).exists()

    # And the row itself is loadable through the v0.3 ORM.
    from audiomat.project import Project
    proj = Project.load("p1")
    assert proj.book.blocks_skipped == [3, 7, 12]
    assert proj.status.phase == "rendering"
    assert proj.book.title == "Test Book"


# ---- happy-path: chunk manifest ------------------------------------------


def test_manifest_imports_per_chunk(isolated_library):
    _seed_v02_project(isolated_library, "p1")
    _seed_v02_manifest(isolated_library, "p1", "001_Foo", {
        "chunk_0000.wav": {"text": "Ahoj.", "sig": "abc123def456"},
        "chunk_0001.wav": {"text": "Jak se máš?", "sig": "abc123def456"},
    })
    report = migrate_v0_2_to_v0_3(isolated_library)
    assert report.manifest_files_migrated == 1
    assert report.chunk_rows_inserted == 2
    assert _row_count("chunk_manifest") == 2

    from audiomat.render import _get_chunk_entry
    assert _get_chunk_entry("p1", "001_Foo", "chunk_0001.wav") == (
        "Jak se máš?", "abc123def456",
    )


def test_manifest_skips_legacy_bare_string_entries(isolated_library):
    """v0.1 pre-fix manifests stored {wav: text}. Without a sig, they
    can't be migrated — should be reported as a warning, not crash."""
    _seed_v02_project(isolated_library, "p1")
    _seed_v02_manifest(isolated_library, "p1", "001_Foo", {
        "chunk_0000.wav": "legacy text",                    # bare string
        "chunk_0001.wav": {"text": "modern", "sig": "s"},   # new format
    })
    report = migrate_v0_2_to_v0_3(isolated_library)
    assert report.chunk_rows_inserted == 1
    assert any("legacy entry" in w for w in report.warnings)


# ---- idempotency ----------------------------------------------------------


def test_second_run_is_a_noop(isolated_library):
    _seed_v02_voice(isolated_library, "alice", "Alice")
    _seed_v02_project(isolated_library, "p1")
    _seed_v02_manifest(isolated_library, "p1", "001_Foo", {
        "chunk_0000.wav": {"text": "x", "sig": "y"},
    })
    first = migrate_v0_2_to_v0_3(isolated_library)
    assert not first.empty

    second = migrate_v0_2_to_v0_3(isolated_library)
    assert second.voices_migrated == 0
    assert second.projects_migrated == 0
    assert second.manifest_files_migrated == 0
    assert second.chunk_rows_inserted == 0
    assert second.warnings == []


# ---- dry-run --------------------------------------------------------------


def test_dry_run_walks_but_doesnt_write(isolated_library):
    meta_path = _seed_v02_voice(isolated_library, "alice", "Alice")
    cfg_path = _seed_v02_project(isolated_library, "p1")
    report = migrate_v0_2_to_v0_3(isolated_library, dry_run=True)
    assert report.voices_migrated == 1
    assert report.projects_migrated == 1
    # No DB writes, no rename.
    assert _row_count("voices") == 0
    assert _row_count("projects") == 0
    assert meta_path.exists()
    assert cfg_path.exists()


# ---- error handling -------------------------------------------------------


def test_corrupt_meta_warns_but_continues(isolated_library):
    """One corrupt voice shouldn't poison the run — other voices import."""
    bad = isolated_library / "voices" / "broken"
    bad.mkdir(parents=True)
    (bad / "meta.json").write_text("{not valid json", encoding="utf-8")
    _seed_v02_voice(isolated_library, "alice", "Alice")

    report = migrate_v0_2_to_v0_3(isolated_library)
    assert report.voices_migrated == 1
    assert any("broken" in w and "corrupt" in w for w in report.warnings)


def test_corrupt_project_config_warns_but_continues(isolated_library):
    bad = isolated_library / "projects" / "broken"
    bad.mkdir(parents=True)
    (bad / "config.json").write_text("{nope", encoding="utf-8")
    _seed_v02_project(isolated_library, "good")

    report = migrate_v0_2_to_v0_3(isolated_library)
    assert report.projects_migrated == 1
    assert any("broken" in w and "corrupt" in w for w in report.warnings)


def test_empty_library_returns_empty_report(isolated_library):
    """Fresh install (no voices/, no projects/) → no work, no warnings."""
    report = migrate_v0_2_to_v0_3(isolated_library)
    assert report.empty
    assert report.warnings == []
