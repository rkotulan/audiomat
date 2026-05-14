"""Library backup + restore endpoints.

Three routes:

* ``GET /api/backup/preview`` — cheap stat-only walk; returns size and
  file count per tier so the UI can show "you're about to download X
  MB" before the user clicks Download.
* ``GET /api/backup/export`` — streams a ZIP. Query toggles
  ``include_renders`` and ``include_finals`` for the optional tiers.
* ``POST /api/backup/restore`` — multipart ZIP upload, replaces the
  current library with the contents (after auto-snapshotting essentials
  to a sibling ``audiomat-pre-restore-<ts>.zip``).

The actual file walks + ZIP IO live in :mod:`audiomat.backup`. This
file is just HTTP wiring + parameter validation.
"""
from __future__ import annotations

import datetime as dt
import io

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response

from audiomat.backup import (
    BackupScope,
    estimate_size,
    export_zip,
    restore_zip,
)
from audiomat.schemas import BackupSizeOut, RestoreOut
from audiomat.state import PATHS


router = APIRouter(prefix="/api/backup", tags=["backup"])


@router.get("/preview", response_model=BackupSizeOut)
def backup_preview():
    """Tier sizes + file counts. Cheap (one stat() per file)."""
    preview = estimate_size(PATHS.library_root)
    return BackupSizeOut(
        essentials_bytes=preview.essentials_bytes,
        renders_bytes=preview.renders_bytes,
        finals_bytes=preview.finals_bytes,
        file_counts=preview.file_counts,
    )


@router.get("/export")
def backup_export(
    include_renders: bool = False,
    include_finals: bool = False,
):
    """Stream a ZIP of the selected tiers. Filename embeds the current
    UTC timestamp + which tiers were included so multiple downloads
    don't collide in the user's Downloads folder.

    audiomat.db is checkpointed before serialization so the file in
    the ZIP is a self-contained snapshot. WAL/SHM sidecars are
    excluded — restoring the .db alone is correct."""
    scope = BackupScope(
        include_renders=include_renders,
        include_finals=include_finals,
    )
    buf = export_zip(PATHS.library_root, scope)
    payload = buf.getvalue() if isinstance(buf, io.BytesIO) else buf

    ts = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    tiers = ["essentials"]
    if include_renders:
        tiers.append("renders")
    if include_finals:
        tiers.append("finals")
    filename = f"audiomat-backup-{ts}-{'-'.join(tiers)}.zip"

    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.post("/restore", response_model=RestoreOut)
async def backup_restore(archive: UploadFile = File(...)):
    """Replace library contents with the uploaded ZIP. Destructive —
    the UI shows a confirm dialog before sending. A pre-restore
    snapshot of essentials is auto-saved next to the library root so
    rollback is possible if the wrong file was uploaded.

    Validation (in :func:`restore_zip`):
    - ZIP must contain ``audiomat.db`` at the root
    - No path-traversal entries (``../...``)
    - Total uncompressed size capped at 50 GB
    """
    raw = await archive.read()
    try:
        report = restore_zip(PATHS.library_root, raw)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RestoreOut(
        files_extracted=report.files_extracted,
        bytes_extracted=report.bytes_extracted,
        pre_restore_snapshot=(
            str(report.pre_restore_snapshot)
            if report.pre_restore_snapshot
            else None
        ),
        warnings=report.warnings,
    )
