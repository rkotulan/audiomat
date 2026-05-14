"""One-shot migration: v0.2 JSON files → v0.3 SQLite tables.

Walks ``<library_root>/voices/`` + ``<library_root>/projects/`` looking
for v0.2-shaped JSON files (``meta.json``, ``config.json``, per-chunk
``manifest.json``). For each one not already represented in the v0.3
``audiomat.db``, inserts the corresponding rows and renames the JSON
to ``<name>.v0-2-backup`` so it stays on disk as a frozen snapshot.

Idempotent. Run twice → second run finds no candidate JSONs (they got
renamed in pass 1) and exits with all-zero counts.

Auto-runs on startup from ``audiomat/api.py``'s lifespan handler.
Direct CLI:

    python -m audiomat.migrations.v0_3_sqlite             # apply
    python -m audiomat.migrations.v0_3_sqlite --dry-run   # walk + report,
                                                          # no DB writes,
                                                          # no rename
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from audiomat.db import get_conn

logger = logging.getLogger(__name__)


# Keep these in sync with the schema in audiomat/db.py — we INSERT
# into the same shape Voice.save / Project.save would.
_BACKUP_SUFFIX = ".v0-2-backup"


@dataclass
class MigrationReport:
    """Summary returned to the lifespan handler / CLI. Counts are
    cumulative across the run (not per-table totals on the DB).

    ``warnings`` collects soft errors (corrupt JSON, missing WAV,
    legacy manifest entries skipped) so the operator can scan the
    migration log without having to crawl the full file."""
    voices_migrated: int = 0
    projects_migrated: int = 0
    manifest_files_migrated: int = 0
    chunk_rows_inserted: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return (
            self.voices_migrated == 0
            and self.projects_migrated == 0
            and self.manifest_files_migrated == 0
        )

    def __str__(self) -> str:
        return (
            f"voices={self.voices_migrated}, "
            f"projects={self.projects_migrated}, "
            f"manifest_files={self.manifest_files_migrated} "
            f"({self.chunk_rows_inserted} chunk rows), "
            f"warnings={len(self.warnings)}"
        )


def migrate_v0_2_to_v0_3(
    library_root: Path,
    *,
    dry_run: bool = False,
) -> MigrationReport:
    """Sweep ``library_root`` for v0.2 JSON state and import it into the
    v0.3 audiomat.db. Returns a :class:`MigrationReport` summarizing
    what changed.

    Safe to call on an already-migrated library — JSONs that were
    imported on a prior run got renamed to ``*.v0-2-backup``, and the
    walker only picks up files with the original name.

    With ``dry_run=True`` the function still walks every file and
    reports what *would* be done, but never INSERTs and never renames.
    Useful for pre-flight checks via the CLI.
    """
    library_root = Path(library_root)
    report = MigrationReport()

    voices_root = library_root / "voices"
    projects_root = library_root / "projects"

    if voices_root.exists():
        _migrate_voices(voices_root, report, dry_run=dry_run)
    if projects_root.exists():
        _migrate_projects(projects_root, report, dry_run=dry_run)

    if not dry_run and not report.empty:
        _write_log(library_root, report)

    return report


# ---- voice migration -------------------------------------------------------


def _migrate_voices(
    voices_root: Path, report: MigrationReport, *, dry_run: bool,
) -> None:
    """Walk voices/<slug>/meta.json. For each meta still on disk under
    its v0.2 name, INSERT the row (when no DB entry yet exists) and
    rename the file to .v0-2-backup."""
    for vdir in sorted(voices_root.iterdir()):
        if not vdir.is_dir():
            continue
        meta_path = vdir / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            report.warnings.append(f"voice {vdir.name}: corrupt meta.json — {e}")
            continue

        slug = meta.get("name_slug") or vdir.name
        if _row_exists("voices", "name_slug", slug):
            # DB already has this slug — older migration run already
            # imported it; rename the lingering JSON for housekeeping.
            if not dry_run:
                _rename_to_backup(meta_path, report)
            continue

        if not dry_run:
            try:
                get_conn().execute(
                    "INSERT INTO voices "
                    "(name_slug, name, duration_s, sample_rate, "
                    " channels, transcript_chars, notes, created, "
                    " tts_model) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        slug,
                        meta.get("name", slug),
                        float(meta.get("duration_s", 0.0)),
                        int(meta.get("sample_rate", 24000)),
                        int(meta.get("channels", 1)),
                        int(meta.get("transcript_chars", 0)),
                        meta.get("notes", "") or "",
                        meta.get("created", "") or "",
                        meta.get("tts_model"),
                    ),
                )
            except Exception as e:
                report.warnings.append(f"voice {slug}: INSERT failed — {e}")
                continue
            _rename_to_backup(meta_path, report)
        report.voices_migrated += 1
        logger.info("migrated voice %r", slug)


# ---- project migration -----------------------------------------------------


def _migrate_projects(
    projects_root: Path, report: MigrationReport, *, dry_run: bool,
) -> None:
    """Walk projects/<slug>/config.json. For each, INSERT the project
    row + project_blocks_skipped rows, then descend into
    chunks/<stem>/manifest.json and import per-chunk entries.

    Project import comes BEFORE chunk import because chunk_manifest has
    a FK on projects(name_slug) ON DELETE CASCADE; without the parent
    row first, the chunk inserts would fail."""
    for pdir in sorted(projects_root.iterdir()):
        if not pdir.is_dir():
            continue
        cfg_path = pdir / "config.json"
        if cfg_path.exists():
            _migrate_one_project(pdir, cfg_path, report, dry_run=dry_run)
        # Manifest sweep runs even when config.json is already migrated
        # — the chunks may not have been touched yet, e.g. if the
        # project was created mid-render and the migration was killed.
        chunks_root = pdir / "chunks"
        if chunks_root.exists():
            _migrate_project_chunks(pdir.name, chunks_root, report, dry_run=dry_run)


def _migrate_one_project(
    pdir: Path, cfg_path: Path, report: MigrationReport, *, dry_run: bool,
) -> None:
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        report.warnings.append(f"project {pdir.name}: corrupt config.json — {e}")
        return

    slug = cfg.get("name_slug") or pdir.name
    if _row_exists("projects", "name_slug", slug):
        if not dry_run:
            _rename_to_backup(cfg_path, report)
        return

    book = cfg.get("book", {}) or {}
    params = cfg.get("params", {}) or {}
    status = cfg.get("status", {}) or {}
    blocks_skipped = book.get("blocks_skipped", []) or []

    if not dry_run:
        try:
            with get_conn() as conn:
                conn.execute(
                    "INSERT INTO projects "
                    "(name_slug, name, book_filename, book_title, "
                    " book_author, book_language, book_blocks_total, "
                    " voice_ref, voice_ref_slug, status_phase, "
                    " status_chapters_done, status_chapters_total, "
                    " status_last_completed, created, last_run, "
                    " params_json, version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (
                        slug,
                        cfg.get("name", slug),
                        book.get("filename", "book.epub"),
                        book.get("title"),
                        book.get("author"),
                        book.get("language"),
                        int(book.get("blocks_total", 0)),
                        cfg.get("voice_ref", ""),
                        cfg.get("voice_ref_slug", ""),
                        status.get("phase", "draft"),
                        int(status.get("chapters_done", 0)),
                        int(status.get("chapters_total", 0)),
                        status.get("last_completed"),
                        cfg.get("created", "") or "",
                        cfg.get("last_run", "") or "",
                        json.dumps(params, ensure_ascii=False),
                    ),
                )
                if blocks_skipped:
                    conn.executemany(
                        "INSERT INTO project_blocks_skipped "
                        "(project_slug, block_index) VALUES (?, ?)",
                        [(slug, int(i)) for i in blocks_skipped],
                    )
        except Exception as e:
            report.warnings.append(f"project {slug}: INSERT failed — {e}")
            return
        _rename_to_backup(cfg_path, report)
    report.projects_migrated += 1
    logger.info("migrated project %r (%d skipped blocks)", slug, len(blocks_skipped))


def _migrate_project_chunks(
    project_slug: str,
    chunks_root: Path,
    report: MigrationReport,
    *,
    dry_run: bool,
) -> None:
    """For each chunks/<stem>/manifest.json, import its entries into
    chunk_manifest. Entries with the legacy bare-string schema (v0.1
    pre-fix) are skipped with a warning — they can't survive without
    a sig, and the renderer would treat them as a cache miss anyway."""
    for stem_dir in sorted(chunks_root.iterdir()):
        if not stem_dir.is_dir():
            continue
        manifest_path = stem_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            report.warnings.append(
                f"manifest {project_slug}/{stem_dir.name}: corrupt — {e}"
            )
            continue

        stem = stem_dir.name
        # Skip if any manifest row for this (project, stem) already
        # exists — assume the migration ran before for this chapter.
        if _row_exists_chunk_for_stem(project_slug, stem):
            if not dry_run:
                _rename_to_backup(manifest_path, report)
            continue

        rows: list[tuple] = []
        legacy_skipped = 0
        for chunk_name, entry in manifest.items():
            if not isinstance(entry, dict):
                # Legacy bare-string schema — no sig available.
                legacy_skipped += 1
                continue
            text = entry.get("text", "")
            sig = entry.get("sig", "")
            if not text or not sig:
                legacy_skipped += 1
                continue
            rows.append((
                project_slug, stem, chunk_name, text, sig, None,
                _utcnow_iso(),
            ))
        if legacy_skipped:
            report.warnings.append(
                f"manifest {project_slug}/{stem}: skipped {legacy_skipped} "
                f"legacy entry(ies) without sig"
            )

        if rows and not dry_run:
            try:
                get_conn().executemany(
                    "INSERT OR IGNORE INTO chunk_manifest "
                    "(project_slug, stem, chunk_name, text, sig, "
                    " gen_seconds, created) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
            except Exception as e:
                report.warnings.append(
                    f"manifest {project_slug}/{stem}: INSERT failed — {e}"
                )
                continue
        if not dry_run:
            _rename_to_backup(manifest_path, report)
        report.manifest_files_migrated += 1
        report.chunk_rows_inserted += len(rows)
        logger.info(
            "migrated manifest %s/%s (%d chunks)", project_slug, stem, len(rows)
        )


# ---- shared helpers --------------------------------------------------------


def _row_exists(table: str, key_col: str, value: str) -> bool:
    """Generic existence probe. ``table`` and ``key_col`` come from the
    migration's hardcoded schema knowledge — caller never passes user
    input here, so the f-string interpolation is safe."""
    return get_conn().execute(
        f"SELECT 1 FROM {table} WHERE {key_col} = ?", (value,)  # noqa: S608
    ).fetchone() is not None


def _row_exists_chunk_for_stem(project_slug: str, stem: str) -> bool:
    return get_conn().execute(
        "SELECT 1 FROM chunk_manifest WHERE project_slug=? AND stem=? LIMIT 1",
        (project_slug, stem),
    ).fetchone() is not None


def _rename_to_backup(p: Path, report: MigrationReport) -> None:
    """Rename ``foo.json`` to ``foo.json.v0-2-backup``. If the backup
    name is already taken (e.g. running twice after an interrupted
    migration), append a timestamp so we never silently destroy a
    prior snapshot."""
    target = p.with_name(p.name + _BACKUP_SUFFIX)
    if target.exists():
        target = p.with_name(
            f"{p.name}{_BACKUP_SUFFIX}.{int(dt.datetime.now().timestamp())}"
        )
    try:
        p.rename(target)
    except OSError as e:
        report.warnings.append(f"rename {p} → {target.name}: {e}")


def _utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_log(library_root: Path, report: MigrationReport) -> None:
    """Append a timestamped block to migration.log so the operator can
    find what changed without scrolling the uvicorn output."""
    log_path = library_root / "migration.log"
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n[{_utcnow_iso()}] v0.2 → v0.3 SQLite migration\n")
            f.write(f"  voices migrated         : {report.voices_migrated}\n")
            f.write(f"  projects migrated       : {report.projects_migrated}\n")
            f.write(f"  manifest files migrated : {report.manifest_files_migrated}"
                    f" ({report.chunk_rows_inserted} chunk rows)\n")
            for w in report.warnings:
                f.write(f"  warn: {w}\n")
    except OSError as e:
        logger.warning("failed to append migration.log: %s", e)


# ---- CLI -------------------------------------------------------------------


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate v0.2 JSON state into v0.3 audiomat.db."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Walk files + report what would be done without writing.",
    )
    parser.add_argument(
        "--library",
        type=Path,
        help="Library root override (default: AUDIOMAT_LIBRARY_ROOT or ~/audiomat).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.library is not None:
        library_root = args.library
    else:
        from audiomat.state import PATHS
        library_root = PATHS.library_root

    print(f"library_root = {library_root}")
    print(f"dry_run      = {args.dry_run}")
    report = migrate_v0_2_to_v0_3(library_root, dry_run=args.dry_run)
    print(report)
    if report.warnings:
        print("warnings:")
        for w in report.warnings:
            print(f"  - {w}")


if __name__ == "__main__":
    _main()
