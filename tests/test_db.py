"""Unit tests for audiomat.db — schema bootstrap, pragmas, FK cascade.

No app code yet uses these tables; that comes in v0.3 phases 2-4
(Voice / Project / manifest migrations). These tests only assert the
DB layer behaves as expected on its own.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from audiomat import db


@pytest.fixture
def fresh_db(tmp_path: Path):
    """Open a brand-new audiomat.db in a tmp dir, yield the connection,
    close it after. Each test gets its own DB so we don't leak rows
    between cases via the module-level connection cache."""
    db_path = tmp_path / "audiomat.db"
    conn = db.get_conn(db_path)
    yield conn
    db.close_all()


def test_schema_creates_expected_tables(fresh_db):
    tables = {
        r[0] for r in fresh_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert tables == {
        "voices",
        "projects",
        "project_blocks_skipped",
        "chunk_manifest",
    }


def test_wal_mode_active(fresh_db):
    mode = fresh_db.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_foreign_keys_on(fresh_db):
    on = fresh_db.execute("PRAGMA foreign_keys").fetchone()[0]
    assert on == 1


def test_voices_pk_enforced(fresh_db):
    fresh_db.execute(
        "INSERT INTO voices (name_slug, name, duration_s, sample_rate, "
        "channels, transcript_chars, notes, created, tts_model) "
        "VALUES ('alice', 'Alice', 8.0, 24000, 1, 14, '', '2026-05-13', NULL)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.execute(
            "INSERT INTO voices (name_slug, name, duration_s, sample_rate, "
            "channels, transcript_chars, notes, created, tts_model) "
            "VALUES ('alice', 'Alice2', 5.0, 24000, 1, 10, '', '2026-05-13', NULL)"
        )


def test_project_blocks_skipped_cascade_on_project_delete(fresh_db):
    """Deleting a project must wipe its blocks_skipped rows via the
    ON DELETE CASCADE — proves the FK is wired, not just declared."""
    fresh_db.execute(
        "INSERT INTO projects (name_slug, name, book_filename, "
        "book_blocks_total, voice_ref, voice_ref_slug, status_phase, "
        "status_chapters_done, status_chapters_total, created, "
        "params_json) "
        "VALUES ('p1', 'P1', 'stub.epub', 100, 'Alice', 'alice', 'draft', "
        "0, 0, '2026-05-13', '{}')"
    )
    fresh_db.execute(
        "INSERT INTO project_blocks_skipped (project_slug, block_index) "
        "VALUES ('p1', 5), ('p1', 7)"
    )
    n_before = fresh_db.execute(
        "SELECT COUNT(*) FROM project_blocks_skipped WHERE project_slug='p1'"
    ).fetchone()[0]
    assert n_before == 2

    fresh_db.execute("DELETE FROM projects WHERE name_slug='p1'")
    n_after = fresh_db.execute(
        "SELECT COUNT(*) FROM project_blocks_skipped WHERE project_slug='p1'"
    ).fetchone()[0]
    assert n_after == 0


def test_chunk_manifest_upsert_replaces_existing_row(fresh_db):
    """The render path will UPSERT each chunk on every synth — verify
    the ON CONFLICT clause we'll use works as expected so the manifest
    migration in phase 4 doesn't get surprised."""
    fresh_db.execute(
        "INSERT INTO projects (name_slug, name, book_filename, "
        "book_blocks_total, voice_ref, voice_ref_slug, status_phase, "
        "status_chapters_done, status_chapters_total, created, "
        "params_json) "
        "VALUES ('p1', 'P1', 'stub.epub', 100, 'A', 'a', 'rendering', "
        "0, 0, '2026-05-13', '{}')"
    )
    fresh_db.execute(
        "INSERT INTO chunk_manifest (project_slug, stem, chunk_name, "
        "text, sig, gen_seconds, created) "
        "VALUES ('p1', '001_Foo', 'chunk_0001.wav', 'hello', 'sig-v1', "
        "1.5, '2026-05-13')"
    )
    fresh_db.execute(
        "INSERT INTO chunk_manifest (project_slug, stem, chunk_name, "
        "text, sig, gen_seconds, created) "
        "VALUES ('p1', '001_Foo', 'chunk_0001.wav', 'hello v2', 'sig-v2', "
        "2.1, '2026-05-13') "
        "ON CONFLICT(project_slug, stem, chunk_name) DO UPDATE SET "
        "text=excluded.text, sig=excluded.sig, gen_seconds=excluded.gen_seconds"
    )
    row = fresh_db.execute(
        "SELECT text, sig, gen_seconds FROM chunk_manifest "
        "WHERE project_slug='p1' AND stem='001_Foo' AND chunk_name='chunk_0001.wav'"
    ).fetchone()
    assert row["text"] == "hello v2"
    assert row["sig"] == "sig-v2"
    assert row["gen_seconds"] == 2.1


def test_project_version_optimistic_lock_pattern(fresh_db):
    """Smoke for the optimistic-lock UPDATE pattern PATCH endpoints
    will use: increment version, fail (rowcount=0) on stale If-Match."""
    fresh_db.execute(
        "INSERT INTO projects (name_slug, name, book_filename, "
        "book_blocks_total, voice_ref, voice_ref_slug, status_phase, "
        "status_chapters_done, status_chapters_total, created, "
        "params_json, version) "
        "VALUES ('p1', 'P1', 'b.epub', 10, 'A', 'a', 'draft', 0, 0, "
        "'2026-05-13', '{}', 1)"
    )
    # Fresh patch on version=1: succeeds, bumps to 2.
    cur = fresh_db.execute(
        "UPDATE projects SET params_json=?, version=version+1 "
        "WHERE name_slug=? AND version=?",
        ('{"speed":0.95}', 'p1', 1),
    )
    assert cur.rowcount == 1
    assert fresh_db.execute(
        "SELECT version FROM projects WHERE name_slug='p1'"
    ).fetchone()["version"] == 2

    # Stale patch on version=1: zero rows updated → 409 territory.
    cur = fresh_db.execute(
        "UPDATE projects SET params_json=?, version=version+1 "
        "WHERE name_slug=? AND version=?",
        ('{"speed":1.0}', 'p1', 1),
    )
    assert cur.rowcount == 0


def test_get_conn_returns_same_instance_per_path(tmp_path: Path):
    """Connection caching: two get_conn() calls for the same path
    return the same object (so the render thread and request handlers
    share one handle, sharing the WAL state)."""
    db_path = tmp_path / "audiomat.db"
    a = db.get_conn(db_path)
    b = db.get_conn(db_path)
    assert a is b
    db.close_all()


def test_get_conn_separate_paths_get_separate_handles(tmp_path: Path):
    a = db.get_conn(tmp_path / "lib1.db")
    b = db.get_conn(tmp_path / "lib2.db")
    assert a is not b
    db.close_all()
