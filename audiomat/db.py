"""SQLite layer — single audiomat.db at the library root.

Hosts the canonical state for voices, projects, and chunk manifests
(moved off filesystem JSON in the v0.3 migration). Binaries (voice
WAVs, EPUBs, chunk WAVs, final M4Bs) and a handful of write-rare
JSONs (settings, secrets, pronunciations, preview sidecars) stay on
the filesystem — DB stores only the state that benefits from atomic
row-level writes + concurrent reads.

Design choices:

* **stdlib sqlite3, no ORM.** audiomat already ships ~5 GB of torch /
  CUDA wheels — adding SQLAlchemy + Pydantic v2 felt off-budget for a
  schema this small (4 tables, no joins more complex than a single
  FK). Dataclass adapters (``from_row`` / ``to_params``) live in the
  domain modules (audiomat/voice.py, audiomat/project.py).
* **WAL journal mode.** Lets background readers (chapter list, model
  status polling) coexist with a long-running render thread without
  blocking. Single writer at a time, but writes don't block readers.
* **Foreign keys ON.** ``ON DELETE CASCADE`` on project_blocks_skipped
  + chunk_manifest means deleting a project takes one statement
  instead of three.
* **Optimistic locking via ``version`` column.** PATCH endpoints check
  ``If-Match`` against the current version, increment on UPDATE,
  return 409 on mismatch (the two-tab lost-update scenario).
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

# Schema — applied via CREATE … IF NOT EXISTS at every connection open
# so a fresh library + an upgraded library reach the same shape without
# a separate "init" step. Schema changes between releases will live in
# audiomat/migrations/ — this file stays as the v0.3 canonical version.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS voices (
  name_slug         TEXT PRIMARY KEY,
  name              TEXT NOT NULL,
  duration_s        REAL NOT NULL,
  sample_rate       INTEGER NOT NULL,
  channels          INTEGER NOT NULL,
  transcript_chars  INTEGER NOT NULL,
  notes             TEXT NOT NULL DEFAULT '',
  created           TEXT NOT NULL,
  tts_model         TEXT
);

CREATE TABLE IF NOT EXISTS projects (
  name_slug              TEXT PRIMARY KEY,
  name                   TEXT NOT NULL,
  book_filename          TEXT NOT NULL,
  book_title             TEXT,
  book_author            TEXT,
  book_language          TEXT,
  book_blocks_total      INTEGER NOT NULL,
  voice_ref              TEXT NOT NULL,
  voice_ref_slug         TEXT NOT NULL,
  status_phase           TEXT NOT NULL,
  status_chapters_done   INTEGER NOT NULL DEFAULT 0,
  status_chapters_total  INTEGER NOT NULL DEFAULT 0,
  status_last_completed  TEXT,
  created                TEXT NOT NULL,
  last_run               TEXT NOT NULL DEFAULT '',
  params_json            TEXT NOT NULL,
  version                INTEGER NOT NULL DEFAULT 1,
  -- v0.5: TTS model slug (NULL / '' / 'default' → stock OmniVoice).
  -- Replaces the v0.4 indirection through voice.tts_model — model is
  -- now a per-project choice, not a per-voice one. Voices still carry
  -- a tts_model field but its role is "preview / clone-validation only"
  -- as far as the renderer is concerned.
  tts_model              TEXT
);

CREATE TABLE IF NOT EXISTS project_blocks_skipped (
  project_slug  TEXT NOT NULL REFERENCES projects(name_slug) ON DELETE CASCADE,
  block_index   INTEGER NOT NULL,
  PRIMARY KEY (project_slug, block_index)
);

CREATE TABLE IF NOT EXISTS chunk_manifest (
  project_slug  TEXT NOT NULL,
  stem          TEXT NOT NULL,
  chunk_name    TEXT NOT NULL,
  text          TEXT NOT NULL,
  sig           TEXT NOT NULL,
  gen_seconds   REAL,
  created       TEXT NOT NULL,
  PRIMARY KEY (project_slug, stem, chunk_name),
  FOREIGN KEY (project_slug) REFERENCES projects(name_slug) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_manifest_lookup
  ON chunk_manifest(project_slug, stem);
"""


# Per-(path, thread) connection cache. Each FastAPI threadpool worker
# gets its own sqlite3 handle so concurrent requests don't race on a
# shared Connection (the v0.3 single-handle setup with
# check_same_thread=False crashed with "bad parameter or other API
# misuse" under concurrent SSE-stream + audio-fetch load — sqlite3.Row
# cursors aren't safe to share across threads). The lock only guards
# the dict; once a connection is materialised it's used freely on its
# owning thread.
#
# Path is part of the key so the pytest ``isolated_library`` fixture
# (which monkey-patches AUDIOMAT_LIBRARY_ROOT and reloads state) still
# gets a fresh connection per test even when the same threadpool
# thread serves multiple tests.
_CONNECTIONS: dict[tuple[str, int], sqlite3.Connection] = {}
_LOCK = threading.Lock()


def get_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Return (or create) the sqlite3 connection for ``db_path`` on the
    calling thread.

    When ``db_path`` is None we resolve via :data:`audiomat.state.PATHS`
    — the normal production path. Tests can pass an explicit path to
    sidestep the global PATHS singleton entirely.

    Each connection is opened with ``check_same_thread=True`` (Python's
    default) — we never share a handle across threads. WAL keeps cross-
    thread reads non-blocking at the DB level; each thread just has its
    own handle pointed at the same file. ``row_factory=sqlite3.Row``
    gives us ``row["name"]`` dict-style access in the dataclass adapters.
    """
    if db_path is None:
        from audiomat.state import PATHS
        db_path = PATHS.library_root / "audiomat.db"
    db_path = Path(db_path).resolve()
    key = (str(db_path), threading.get_ident())
    with _LOCK:
        existing = _CONNECTIONS.get(key)
        if existing is not None:
            return existing
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(db_path),
            isolation_level=None,         # autocommit; we BEGIN explicitly when needed
        )
        conn.row_factory = sqlite3.Row
        _configure(conn)
        conn.executescript(_SCHEMA)
        _CONNECTIONS[key] = conn
        return conn


def _configure(conn: sqlite3.Connection) -> None:
    """Apply per-connection pragmas. ``WAL`` and ``foreign_keys`` are
    the load-bearing ones; the rest are conservative perf tweaks for a
    single-user local workload."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Synchronous=NORMAL is the recommended pairing with WAL for
    # single-user apps — durable across crashes (SQLite still fsyncs
    # checkpoints), faster than FULL because it skips fsync on each
    # commit. Acceptable for our use case: a power-loss mid-render
    # loses the in-flight chunk, which the renderer will redo anyway.
    conn.execute("PRAGMA synchronous=NORMAL")
    # Larger page cache helps the chunk_manifest hot path (~1000 rows
    # per book; ~2 MB of cache is plenty for the whole table).
    conn.execute("PRAGMA cache_size=-4096")  # 4 MB; negative = KB


def close_all() -> None:
    """Close every cached connection. Idempotent. Called from the
    FastAPI lifespan shutdown handler and from test teardown."""
    with _LOCK:
        for conn in _CONNECTIONS.values():
            try:
                conn.close()
            except sqlite3.Error:
                pass
        _CONNECTIONS.clear()


if __name__ == "__main__":
    # Smoke test: open a fresh DB in a temp dir, verify schema applied
    # and pragmas active. `python -m audiomat.db`
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "audiomat.db"
        conn = get_conn(db)
        print(f"opened {db}")
        print(f"journal_mode = {conn.execute('PRAGMA journal_mode').fetchone()[0]}")
        print(f"foreign_keys = {conn.execute('PRAGMA foreign_keys').fetchone()[0]}")
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )]
        print(f"tables       = {tables}")
        close_all()
