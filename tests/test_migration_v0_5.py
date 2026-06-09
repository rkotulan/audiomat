"""Tests for the v0.5 schema bump — projects.tts_model column + backfill.

The migration runs against the real :func:`audiomat.db.get_conn` so the
tests share the production schema/WAL config. ``isolated_library``
fixture redirects ``AUDIOMAT_LIBRARY_ROOT`` at a tmp tree.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from audiomat.db import close_all, get_conn
from audiomat.migrations.v0_5_project_tts_model import (
    V05MigrationReport,
    migrate_v0_4_to_v0_5,
)
from audiomat.project import BookInfo, Project, ProjectStatus, RenderParams
from audiomat.voice import Voice


def _project_row_tts(slug: str) -> str | None:
    """Inline tts_model probe — bypasses Project.from_row so we know
    the test actually reads the column we expect."""
    conn = get_conn()
    row = conn.execute(
        "SELECT tts_model FROM projects WHERE name_slug=?", (slug,),
    ).fetchone()
    return None if row is None else row["tts_model"]


def _seed_voice(
    slug: str, name: str = "V", *, tts_model: str | None = None,
) -> Voice:
    """INSERT a minimal voice row so backfill has something to join on.
    Mirrors what Voice.save would do without depending on WAV files on
    disk (the migration touches only DB rows)."""
    v = Voice(
        name=name, name_slug=slug,
        duration_s=8.0, sample_rate=24000, channels=1,
        transcript_chars=50, notes="", created="2026-05-01T00:00:00Z",
        tts_model=tts_model,
    )
    v.save()
    return v


def _seed_project(slug: str, voice_slug: str, *, tts_model: str | None = None) -> None:
    """INSERT a project row directly. We don't go through Project.create
    because that wants a real book file on disk — the migration cares
    only about DB state."""
    proj = Project(
        name=slug,
        name_slug=slug,
        book=BookInfo(filename="book.epub", blocks_total=10),
        voice_ref=voice_slug,
        voice_ref_slug=voice_slug,
        params=RenderParams(),
        status=ProjectStatus(chapters_total=10),
        created="2026-05-01T00:00:00Z",
        last_run="",
        version=1,
        tts_model=tts_model,
    )
    proj.save()


# ---- Schema additions ------------------------------------------------


class TestSchemaShape:
    def test_fresh_install_already_has_tts_model_column(
        self, isolated_library: Path,
    ):
        """db.get_conn applies CREATE TABLE IF NOT EXISTS on every
        connection — for a fresh install the v0.5 schema already
        includes the column, so the migration is a no-op."""
        get_conn()
        report = migrate_v0_4_to_v0_5(isolated_library)
        # column_added is False because the column was already there.
        assert report.column_added is False
        assert report.empty is True

    def test_migration_is_idempotent(self, isolated_library: Path):
        get_conn()
        first = migrate_v0_4_to_v0_5(isolated_library)
        second = migrate_v0_4_to_v0_5(isolated_library)
        assert first.empty == second.empty


# ---- Backfill ---------------------------------------------------------


class TestBackfillBehavior:
    def test_backfills_from_voice_tts_model(self, isolated_library: Path):
        get_conn()
        _seed_voice("anna", tts_model="higgs_demo")
        _seed_project("book1", voice_slug="anna")
        assert _project_row_tts("book1") is None  # pre-backfill

        report = migrate_v0_4_to_v0_5(isolated_library)
        assert report.projects_seen == 1
        assert report.projects_backfilled == 1
        assert _project_row_tts("book1") == "higgs_demo"

    def test_preserves_existing_project_tts_model(
        self, isolated_library: Path,
    ):
        """A project that was created fresh in v0.5 already has its
        tts_model set — the migration must NOT overwrite it with the
        voice's value (project takes precedence)."""
        get_conn()
        _seed_voice("anna", tts_model="higgs_demo")
        _seed_project("book1", voice_slug="anna", tts_model="ov_finetune")

        report = migrate_v0_4_to_v0_5(isolated_library)
        # Seen but not backfilled — already set.
        assert report.projects_seen == 1
        assert report.projects_backfilled == 0
        assert _project_row_tts("book1") == "ov_finetune"

    def test_leaves_null_when_voice_has_no_tts_model(
        self, isolated_library: Path,
    ):
        get_conn()
        _seed_voice("anna", tts_model=None)
        _seed_project("book1", voice_slug="anna")

        report = migrate_v0_4_to_v0_5(isolated_library)
        assert report.projects_seen == 1
        assert report.projects_backfilled == 0
        assert _project_row_tts("book1") is None

    def test_leaves_null_when_bound_voice_missing(
        self, isolated_library: Path,
    ):
        """User deleted the voice before v0.5 upgrade. LEFT JOIN gives
        NULL voice_tts_model → project tts_model stays NULL → renderer
        falls back to stock OmniVoice. No exception."""
        get_conn()
        _seed_project("book1", voice_slug="ghost_voice")

        report = migrate_v0_4_to_v0_5(isolated_library)
        assert report.projects_seen == 1
        assert report.projects_backfilled == 0
        assert _project_row_tts("book1") is None

    def test_dry_run_does_not_mutate(self, isolated_library: Path):
        get_conn()
        _seed_voice("anna", tts_model="higgs_demo")
        _seed_project("book1", voice_slug="anna")

        report = migrate_v0_4_to_v0_5(isolated_library, dry_run=True)
        # Counts still increment but no UPDATE issued.
        assert report.projects_backfilled == 1
        assert _project_row_tts("book1") is None


# ---- ALTER TABLE on a legacy schema ---------------------------------


class TestAlterTableLegacySchema:
    """Simulates an upgrade from a v0.4 DB that predates the v0.5
    schema. We build a projects table by hand with the v0.4 column set,
    then check that the migration adds the new column without losing
    rows."""

    def _build_v0_4_db(self, library_root: Path) -> Path:
        """Hand-roll a v0.4-shaped audiomat.db in ``library_root``. No
        tts_model column on projects. Returns the db path."""
        db = library_root / "audiomat.db"
        close_all()
        raw = sqlite3.connect(str(db))
        raw.executescript("""
            CREATE TABLE voices (
              name_slug TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              duration_s REAL NOT NULL,
              sample_rate INTEGER NOT NULL,
              channels INTEGER NOT NULL,
              transcript_chars INTEGER NOT NULL,
              notes TEXT NOT NULL DEFAULT '',
              created TEXT NOT NULL,
              tts_model TEXT
            );
            CREATE TABLE projects (
              name_slug TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              book_filename TEXT NOT NULL,
              book_title TEXT,
              book_author TEXT,
              book_language TEXT,
              book_blocks_total INTEGER NOT NULL,
              voice_ref TEXT NOT NULL,
              voice_ref_slug TEXT NOT NULL,
              status_phase TEXT NOT NULL,
              status_chapters_done INTEGER NOT NULL DEFAULT 0,
              status_chapters_total INTEGER NOT NULL DEFAULT 0,
              status_last_completed TEXT,
              created TEXT NOT NULL,
              last_run TEXT NOT NULL DEFAULT '',
              params_json TEXT NOT NULL,
              version INTEGER NOT NULL DEFAULT 1
            );
        """)
        raw.execute(
            "INSERT INTO voices (name_slug, name, duration_s, sample_rate, "
            "channels, transcript_chars, notes, created, tts_model) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("anna", "Anna", 8.0, 24000, 1, 50, "", "2026-05-01T00:00:00Z",
             "higgs_demo"),
        )
        raw.execute(
            "INSERT INTO projects (name_slug, name, book_filename, "
            "book_blocks_total, voice_ref, voice_ref_slug, status_phase, "
            "created, params_json) VALUES (?,?,?,?,?,?,?,?,?)",
            ("book1", "Book 1", "book.epub", 10, "Anna", "anna", "draft",
             "2026-05-01T00:00:00Z", "{}"),
        )
        raw.commit()
        raw.close()
        return db

    def test_alter_adds_column_and_backfills(self, isolated_library: Path):
        self._build_v0_4_db(isolated_library)
        report = migrate_v0_4_to_v0_5(isolated_library)
        assert report.column_added is True
        assert report.projects_backfilled == 1

        # Verify with a fresh connection that doesn't reapply schema —
        # we want to confirm ALTER TABLE survived the migration alone.
        conn = get_conn()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(projects)")]
        assert "tts_model" in cols
        row = conn.execute(
            "SELECT tts_model FROM projects WHERE name_slug='book1'"
        ).fetchone()
        assert row["tts_model"] == "higgs_demo"


# ---- Project dataclass round-trip with the new field ----------------


class TestProjectRoundTrip:
    def test_save_load_round_trips_tts_model(self, isolated_library: Path):
        get_conn()
        _seed_voice("anna")
        proj = Project(
            name="Book A", name_slug="book_a",
            book=BookInfo(filename="book.epub", blocks_total=5),
            voice_ref="Anna", voice_ref_slug="anna",
            params=RenderParams(),
            status=ProjectStatus(chapters_total=5),
            created="2026-05-01T00:00:00Z",
            version=1,
            tts_model="higgs_demo",
        )
        proj.save()
        loaded = Project.load("book_a")
        assert loaded.tts_model == "higgs_demo"

    def test_save_with_version_persists_tts_model_change(
        self, isolated_library: Path,
    ):
        get_conn()
        _seed_voice("anna")
        _seed_project("book_a", voice_slug="anna")
        loaded = Project.load("book_a")
        loaded.tts_model = "ov_finetune"
        new_version = loaded.save_with_version(loaded.version)
        again = Project.load("book_a")
        assert again.version == new_version
        assert again.tts_model == "ov_finetune"
