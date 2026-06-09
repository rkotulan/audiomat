"""v0.5 schema bump: promote ``tts_model`` from voice → project.

In v0.4 the renderer derived the TTS engine from the bound voice's
``tts_model`` field via ``state.get_tts_for_voice``. v0.5 makes the
engine a project-level choice — a single voice ref can now drive
either OmniVoice or Higgs depending on which project consumes it,
without cloning the voice or mutating its row.

This module:

1. **ALTERs** the projects table to add a nullable ``tts_model TEXT``
   column. Idempotent — PRAGMA table_info is checked first so re-runs
   no-op cleanly.
2. **Backfills** each project's ``tts_model`` from its bound voice's
   ``tts_model``, looked up via ``voice_ref_slug``. Projects whose
   bound voice is gone or had a NULL tts_model leave the project
   column NULL — same fallback to stock OmniVoice the renderer
   already honors.

Auto-runs on FastAPI startup (audiomat/api.py lifespan) after the v0.3
JSON-to-SQLite migration so both migrations stay independent of each
other's success path.

Direct CLI::

    python -m audiomat.migrations.v0_5_project_tts_model
    python -m audiomat.migrations.v0_5_project_tts_model --dry-run
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from pathlib import Path

from audiomat.db import get_conn

logger = logging.getLogger(__name__)


@dataclass
class V05MigrationReport:
    """Summary of the v0.5 schema bump. Counts are cumulative.

    ``column_added`` flips True on the first run that creates the
    column. Subsequent runs report False (idempotent no-op) — useful for
    operators reading the migration log to confirm "this was applied
    cleanly once and is stable".

    ``warnings`` collects soft errors (bound voice missing, voice has
    no tts_model field) so an audit can recover them later.
    """

    column_added: bool = False
    projects_seen: int = 0
    projects_backfilled: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return (
            not self.column_added
            and self.projects_seen == 0
            and self.projects_backfilled == 0
        )

    def __str__(self) -> str:
        return (
            f"column_added={self.column_added}, "
            f"projects_seen={self.projects_seen}, "
            f"backfilled={self.projects_backfilled}, "
            f"warnings={len(self.warnings)}"
        )


def migrate_v0_4_to_v0_5(
    library_root: Path,
    *,
    dry_run: bool = False,
) -> V05MigrationReport:
    """Apply the v0.5 ``projects.tts_model`` schema + backfill pass.

    ``library_root`` is the audiomat data root (typically
    ``PATHS.library_root``). The function opens an isolated connection
    via :func:`audiomat.db.get_conn` — same WAL config as the rest of
    the app, so concurrent reads during the brief migration window
    don't block.

    With ``dry_run=True`` the function still walks every project and
    reports what would change, but issues no DDL or UPDATE.
    """
    library_root = Path(library_root)
    report = V05MigrationReport()

    db_path = library_root / "audiomat.db"
    if not db_path.exists():
        # Nothing to migrate — fresh install. db.get_conn will create
        # the schema (already includes tts_model) on first connection.
        return report

    conn = get_conn(db_path)

    if not _column_exists(conn, "projects", "tts_model"):
        if not dry_run:
            conn.execute("ALTER TABLE projects ADD COLUMN tts_model TEXT")
        report.column_added = True

    # Backfill — pair each project with its bound voice's tts_model.
    # We use LEFT JOIN so projects whose voice was deleted still get a
    # row in the result (with NULL voice_tts_model → no-op, falls back
    # to stock OmniVoice at render time).
    rows = list(conn.execute(
        "SELECT p.name_slug, p.voice_ref_slug, p.tts_model AS proj_tts, "
        "       v.tts_model AS voice_tts, v.name_slug AS voice_slug "
        "FROM projects p "
        "LEFT JOIN voices v ON v.name_slug = p.voice_ref_slug"
    ))
    for row in rows:
        report.projects_seen += 1
        # Already set by a prior run, or by a fresh-install
        # Project.create that picked up the v0.5 kwarg → skip.
        if row["proj_tts"] not in (None, ""):
            continue
        voice_tts = row["voice_tts"]
        if voice_tts in (None, ""):
            # Voice missing or had no tts_model — leave NULL, the
            # renderer's fallback to stock OmniVoice handles it.
            continue
        if not dry_run:
            conn.execute(
                "UPDATE projects SET tts_model=? WHERE name_slug=?",
                (voice_tts, row["name_slug"]),
            )
        report.projects_backfilled += 1

    return report


# ---- helpers ---------------------------------------------------------


def _column_exists(conn, table: str, column: str) -> bool:
    """Probe PRAGMA table_info — cheap one-shot check used to make the
    ALTER TABLE idempotent. SQLite has no IF NOT EXISTS variant for
    ADD COLUMN; this is the canonical workaround."""
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(r[1] == column for r in cur.fetchall())


# ---- CLI -------------------------------------------------------------


def _cli() -> int:
    parser = argparse.ArgumentParser(
        description="v0.5 schema migration: promote tts_model from voice → project.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Walk projects + report what would change. No DDL / UPDATE issued.",
    )
    parser.add_argument(
        "--library-root", type=Path,
        help="Override the AUDIOMAT_LIBRARY_ROOT-derived path. Useful for "
             "running the migration against a snapshot copy.",
    )
    args = parser.parse_args()

    if args.library_root is not None:
        library_root = args.library_root
    else:
        from audiomat.state import PATHS
        library_root = PATHS.library_root

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    report = migrate_v0_4_to_v0_5(library_root, dry_run=args.dry_run)
    print(f"v0.4 → v0.5: {report}" + (" (dry-run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
