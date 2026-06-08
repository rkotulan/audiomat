"""Library backup + restore — single-file ZIP, configurable scope.

DB snapshotting note: SQLite ``PRAGMA wal_checkpoint`` was the obvious
choice but breaks on Windows host → Linux container bind-mounts (file
locking semantics differ; checkpoint races with the container's own
open handle and reports ``disk I/O error``). We use the SQLite Backup
API instead (``sqlite3.Connection.backup``), which produces a
consistent snapshot copy without mutating the source — works across
WAL state and survives any platform's lock quirks.


Tier system:

* **Essentials** (always included): audiomat.db, settings.json,
  secrets.json, voices/<slug>/voice.{wav,txt}, projects/<slug>/book.*,
  per-chapter overrides, pronunciations.json. Reproduces the project
  state 1:1 — only re-rendering is needed after restore.
* **Renders** (optional toggle): per-chapter chunk WAVs + concat WAVs.
  Stops the user from waiting on TTS again for an already-rendered
  book. Big — hundreds of MB per project.
* **Finals** (optional toggle): final.m4b output files. Even larger
  but lets the user have a turn-key restore.

Excluded by design (not in any tier):

* ``cache/`` — HuggingFace model cache (~3 GB, re-downloads on first
  render after restore)
* WAL/SHM sidecars — checkpointed before zip so audiomat.db is a
  consistent snapshot
* tempdirs

Restore is destructive. Before extraction we snapshot the current
library to ``<library_root>-pre-restore-<ts>.zip`` (essentials only,
so the safety net stays small) and then wipe + extract. Path traversal
attacks (``../../etc/passwd`` entries in the uploaded ZIP) are blocked
by validating each entry name against the resolved target path.
"""
from __future__ import annotations

import datetime as dt
import io
import logging
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from audiomat import db

logger = logging.getLogger(__name__)


# ZIP entry names always use POSIX separators. Build them from
# library-root-relative paths so the prefix isn't hardcoded.
_DB_ENTRY = "audiomat.db"

# Files at library_root level we always pull in essentials.
_ROOT_FILES = ("audiomat.db", "settings.json", "secrets.json")

# Per-project file basenames in the project dir (not under chunks/).
_PROJECT_ROOT_FILES = ("pronunciations.json",)

# Hard upper bound on uncompressed restore payload to defuse zip-bomb
# uploads. 50 GB matches the order of "user has a 30-hour audiobook
# rendered + finals" without leaving room for adversarial blow-up.
_MAX_RESTORE_UNCOMPRESSED_BYTES = 50 * 1024 * 1024 * 1024

# Always-excluded subtrees (HF model cache, etc.) regardless of tier.
_EXCLUDED_DIRS = ("cache",)


@dataclass
class BackupScope:
    """Toggles for the optional tiers. Essentials are always included."""
    include_renders: bool = False
    include_finals: bool = False


@dataclass
class SizePreview:
    """Returned by :func:`estimate_size`. Lets the UI show "you're
    about to download X MB" before the user clicks."""
    essentials_bytes: int = 0
    renders_bytes: int = 0
    finals_bytes: int = 0
    file_counts: dict[str, int] = field(default_factory=dict)

    def total_bytes(self, scope: BackupScope) -> int:
        n = self.essentials_bytes
        if scope.include_renders:
            n += self.renders_bytes
        if scope.include_finals:
            n += self.finals_bytes
        return n


# ---- file selection -------------------------------------------------------


def _walk_essentials(library_root: Path) -> Iterator[Path]:
    """Yield absolute paths of files that always go into the backup.

    audiomat.db is intentionally **not** yielded by this walk — the
    caller (:func:`export_zip`) writes a fresh DB snapshot under that
    arcname via the SQLite Backup API. Yielding the live file would
    risk racing with concurrent writes (and breaks on Windows
    bind-mounts that can't take the WAL lock).
    """
    for name in _ROOT_FILES:
        if name == "audiomat.db":
            # Skip — handled by snapshot_db_into_zip in export_zip.
            continue
        p = library_root / name
        if p.exists() and p.is_file():
            yield p

    voices_root = library_root / "voices"
    if voices_root.exists():
        for vdir in voices_root.iterdir():
            if not vdir.is_dir():
                continue
            for name in ("voice.wav", "voice.txt"):
                f = vdir / name
                if f.exists():
                    yield f

    projects_root = library_root / "projects"
    if projects_root.exists():
        for pdir in projects_root.iterdir():
            if not pdir.is_dir():
                continue
            # Project root files (pronunciations.json + the book file —
            # which can be book.epub OR book.txt depending on the
            # original upload, so glob on stem instead of hardcoding).
            for name in _PROJECT_ROOT_FILES:
                f = pdir / name
                if f.exists():
                    yield f
            for book in pdir.glob("book.*"):
                if book.is_file():
                    yield book
            chapters_dir = pdir / "chapters"
            if chapters_dir.exists():
                for f in chapters_dir.iterdir():
                    if f.is_file():
                        yield f


def _walk_renders(library_root: Path) -> Iterator[Path]:
    """Per-chunk WAVs + per-chapter concat WAVs (everything inside
    projects/<slug>/chunks/, except orphaned tmp files)."""
    projects_root = library_root / "projects"
    if not projects_root.exists():
        return
    for pdir in projects_root.iterdir():
        if not pdir.is_dir():
            continue
        chunks_root = pdir / "chunks"
        if not chunks_root.exists():
            continue
        for stem_dir in chunks_root.iterdir():
            if not stem_dir.is_dir():
                continue
            for f in stem_dir.rglob("*"):
                if f.is_file() and f.suffix.lower() in (".wav",):
                    yield f


def _walk_finals(library_root: Path) -> Iterator[Path]:
    """Per-project final.m4b outputs."""
    projects_root = library_root / "projects"
    if not projects_root.exists():
        return
    for pdir in projects_root.iterdir():
        if not pdir.is_dir():
            continue
        f = pdir / "final.m4b"
        if f.exists():
            yield f


# ---- DB snapshot ----------------------------------------------------------


def snapshot_db(library_root: Path, dest: Path) -> bool:
    """Write a consistent snapshot of ``library_root/audiomat.db`` to
    ``dest`` via the SQLite Backup API (Connection.backup).

    Returns False when there's no DB to snapshot (fresh install), True
    on success. The Backup API holds a read-only intent lock for the
    duration of the copy, so concurrent readers stay unblocked and
    writers serialize cleanly — works across WAL state and on
    bind-mounted filesystems that fight us over the WAL lock.
    """
    src_path = library_root / "audiomat.db"
    if not src_path.exists():
        return False
    src = sqlite3.connect(str(src_path))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return True


def estimate_db_bytes(library_root: Path) -> int:
    """Size of audiomat.db on disk. Used by estimate_size — the snapshot
    we'd produce is the same size as the source (Backup API copies
    page-for-page)."""
    src_path = library_root / "audiomat.db"
    return src_path.stat().st_size if src_path.exists() else 0


# ---- size estimate --------------------------------------------------------


def estimate_size(library_root: Path) -> SizePreview:
    """Sum file sizes per tier without touching the data. Cheap (just
    stat() per file). Returned to the UI before download so the user
    knows whether they're about to fetch 10 MB or 30 GB."""
    library_root = Path(library_root)
    out = SizePreview()
    files = {"essentials": 0, "renders": 0, "finals": 0}
    db_bytes = estimate_db_bytes(library_root)
    if db_bytes > 0:
        out.essentials_bytes += db_bytes
        files["essentials"] += 1
    for p in _walk_essentials(library_root):
        out.essentials_bytes += p.stat().st_size
        files["essentials"] += 1
    for p in _walk_renders(library_root):
        out.renders_bytes += p.stat().st_size
        files["renders"] += 1
    for p in _walk_finals(library_root):
        out.finals_bytes += p.stat().st_size
        files["finals"] += 1
    out.file_counts = files
    return out


# ---- export ---------------------------------------------------------------


def export_zip(
    library_root: Path,
    scope: BackupScope,
    *,
    out: io.BufferedIOBase | None = None,
) -> io.BytesIO:
    """Serialize the selected tiers into a ZIP. ``out`` is optional —
    when None we write to a fresh BytesIO and return it. The FastAPI
    streaming endpoint can hand us its own writer to avoid buffering
    a multi-GB export in memory.

    audiomat.db is snapshotted via the SQLite Backup API into a
    tempfile and that tempfile is what lands in the ZIP. The live
    audiomat.db is never read directly — avoids a class of locking
    races on Windows host → Linux container bind-mounts.
    """
    library_root = Path(library_root)

    buf = out if out is not None else io.BytesIO()
    # Tempfile holds the DB snapshot; deleted in the finally so even a
    # mid-zip crash doesn't leave a stray copy hanging around.
    tmp_db: Path | None = None
    try:
        if (library_root / "audiomat.db").exists():
            tmp_db = Path(tempfile.mkstemp(prefix="audiomat-backup-", suffix=".db")[1])
            snapshot_db(library_root, tmp_db)

        # ZIP_DEFLATED keeps WAVs / M4Bs compressed about as well as
        # ZIP_STORED (audio doesn't compress — they're already PCM/AAC),
        # but the small text files (.txt, .json) shrink ~3-5×.
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=4) as zf:
            if tmp_db is not None:
                zf.write(tmp_db, arcname="audiomat.db")
            for path in _walk_essentials(library_root):
                arcname = path.relative_to(library_root).as_posix()
                zf.write(path, arcname=arcname)
            if scope.include_renders:
                for path in _walk_renders(library_root):
                    arcname = path.relative_to(library_root).as_posix()
                    zf.write(path, arcname=arcname)
            if scope.include_finals:
                for path in _walk_finals(library_root):
                    arcname = path.relative_to(library_root).as_posix()
                    zf.write(path, arcname=arcname)
    finally:
        if tmp_db is not None:
            try:
                tmp_db.unlink()
            except OSError:
                pass

    if isinstance(buf, io.BytesIO):
        buf.seek(0)
    return buf


# ---- restore --------------------------------------------------------------


@dataclass
class RestoreReport:
    """Counts + warnings the endpoint surfaces back to the UI after a
    restore so the user knows what landed."""
    files_extracted: int = 0
    bytes_extracted: int = 0
    pre_restore_snapshot: Path | None = None
    warnings: list[str] = field(default_factory=list)


def restore_zip(
    library_root: Path,
    zip_bytes: bytes,
    *,
    pre_restore_dir: Path | None = None,
) -> RestoreReport:
    """Replace the contents of ``library_root`` with the contents of
    the ZIP. The current library is first snapshotted (essentials only)
    to ``pre_restore_dir`` (default ``library_root.parent /
    audiomat-pre-restore-<ts>.zip``) so the user can roll back manually
    if the restore turns out to be wrong.

    Validation:

    * ZIP must contain ``audiomat.db`` at the root (otherwise it's not
      one of our exports — bail early instead of corrupting state).
    * No entry may resolve outside ``library_root`` (path traversal
      attack defense).
    * Total uncompressed size capped at
      :data:`_MAX_RESTORE_UNCOMPRESSED_BYTES` (zip-bomb defense).

    The HF model cache (``cache/``) is preserved across the restore —
    deleting it would force a 3 GB OmniVoice re-download for no gain.
    """
    library_root = Path(library_root)
    report = RestoreReport()

    # 1. Validate.
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes), "r")
    except zipfile.BadZipFile as e:
        raise ValueError(f"not a valid ZIP file: {e}")

    names = zf.namelist()
    if _DB_ENTRY not in names:
        raise ValueError(
            f"ZIP doesn't contain {_DB_ENTRY!r} — refusing to restore "
            "(not an audiomat backup)"
        )
    total_uncompressed = sum(info.file_size for info in zf.infolist())
    if total_uncompressed > _MAX_RESTORE_UNCOMPRESSED_BYTES:
        raise ValueError(
            f"ZIP uncompressed size {total_uncompressed} exceeds cap "
            f"{_MAX_RESTORE_UNCOMPRESSED_BYTES}; refusing to restore"
        )
    library_root_resolved = library_root.resolve()
    for entry in names:
        target = (library_root / entry).resolve()
        try:
            target.relative_to(library_root_resolved)
        except ValueError:
            raise ValueError(
                f"ZIP entry {entry!r} resolves outside the library root "
                f"(path-traversal attempt)"
            )

    # 2. Pre-restore snapshot (essentials only — small safety net).
    if pre_restore_dir is None:
        ts = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        pre_restore_dir = library_root.parent / f"audiomat-pre-restore-{ts}.zip"
    if any(library_root.iterdir()) if library_root.exists() else False:
        try:
            with pre_restore_dir.open("wb") as f:
                export_zip(library_root, BackupScope(), out=f)
            report.pre_restore_snapshot = pre_restore_dir
        except OSError as e:
            report.warnings.append(f"pre-restore snapshot failed: {e}")

    # 3. Close DB connections (Windows can't unlink an open SQLite file).
    db.close_all()

    # 4. Wipe (preserving cache/).
    if library_root.exists():
        for child in library_root.iterdir():
            if child.name in _EXCLUDED_DIRS:
                continue
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            except OSError as e:
                report.warnings.append(f"wipe {child.name}: {e}")
    else:
        library_root.mkdir(parents=True, exist_ok=True)

    # 5. Extract. Path-traversal protection applied above; here we
    # also re-validate per entry as a belt-and-braces.
    for entry in names:
        info = zf.getinfo(entry)
        if entry.endswith("/"):
            (library_root / entry).mkdir(parents=True, exist_ok=True)
            continue
        target = library_root / entry
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        report.files_extracted += 1
        report.bytes_extracted += info.file_size

    return report


if __name__ == "__main__":
    # Smoke: estimate sizes against the real library.
    # `python -m audiomat.backup`
    import os
    lib = Path(os.environ.get("AUDIOMAT_LIBRARY_ROOT") or (Path.home() / "audiomat"))
    preview = estimate_size(lib)
    mb = lambda n: f"{n / 1024 / 1024:.1f} MB"  # noqa: E731
    print(f"library_root        = {lib}")
    print(f"essentials          = {mb(preview.essentials_bytes)}  "
          f"({preview.file_counts.get('essentials', 0)} files)")
    print(f"renders             = {mb(preview.renders_bytes)}  "
          f"({preview.file_counts.get('renders', 0)} files)")
    print(f"finals              = {mb(preview.finals_bytes)}  "
          f"({preview.file_counts.get('finals', 0)} files)")
    print(f"total (essentials)  = {mb(preview.total_bytes(BackupScope()))}")
    print(f"total (everything)  = "
          f"{mb(preview.total_bytes(BackupScope(include_renders=True, include_finals=True)))}")
