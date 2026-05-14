"""Project — one audiobook in one DB row + one filesystem directory.

A project is everything that produces one M4B output. v0.3 split:

* **DB row** (``projects`` table) — name, book metadata, voice ref,
  render params, status, created/last_run timestamps, optimistic-lock
  ``version`` counter. Skipped block indices live in
  ``project_blocks_skipped`` (one row each, ON DELETE CASCADE).
* **On-disk directory** (``projects/<slug>/``) — book.epub (or .txt),
  chunks/, previews/, chapters/<stem>.txt overrides,
  pronunciations.json, render_log.txt, final.m4b. Binaries + per-
  chapter text overrides stay on disk: too binary / multi-line / huge
  for SQL.

Naming is immutable: once created, ``slug`` cannot change. Workaround:
download the artifacts and create a new project.

Concurrency model — two write paths:

* :meth:`save` — full upsert that increments ``version``. Used by user
  PATCH endpoints (gated by an :meth:`save_with_version` call when an
  ``If-Match`` header is present) and by :meth:`Project.create`.
* :meth:`save_status` — narrow UPDATE that touches only ``status_*``
  + ``last_run``. Does **not** bump version. Used by the render loop
  (``set_status``) so background progress reporting doesn't fire 409s
  on the user mid-edit.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from audiomat.db import get_conn
from audiomat.slug import slugify


# ----------------------------------------------------------------------------
# Sub-dataclasses (book metadata + render params + status snapshot)
# ----------------------------------------------------------------------------


@dataclass
class BookInfo:
    """Source book metadata + audiomat-level skip list."""
    filename: str = "book.epub"
    blocks_total: int = 0
    blocks_skipped: list[int] = field(default_factory=list)
    title: str | None = None
    author: str | None = None
    language: str | None = None


@dataclass
class RenderParams:
    """All knobs the user can tune from the UI's Advanced tab.

    Defaults are the production-validated config from CLAUDE.md Stage 3:
    step 48, gs 2.0, 90–200 char chunks, -16 LUFS, 200 ms inter-chunk gap.
    """
    num_step: int = 48
    guidance_scale: float = 2.0
    speed: float = 1.0
    min_chars: int = 90
    max_chars: int = 200
    target_lufs: float = -16.0
    silence_gap_ms: int = 200
    section_headers: list[str] = field(default_factory=list)


@dataclass
class ProjectStatus:
    """Render progress snapshot. Phase transitions:
    draft → preview → rendering → complete (or failed)."""
    chapters_done: int = 0
    chapters_total: int = 0
    last_completed: str | None = None
    phase: str = "draft"


# ----------------------------------------------------------------------------
# Optimistic-lock exception
# ----------------------------------------------------------------------------


class ProjectVersionMismatch(Exception):
    """Raised by :meth:`Project.save_with_version` when the DB row has
    been bumped by someone else since the caller read ``expected``.

    PATCH endpoints translate this into a 409 with ``actual`` so the
    UI can show "this project was changed in another tab — refresh and
    retry"."""
    def __init__(self, slug: str, expected: int, actual: int):
        super().__init__(
            f"project {slug!r} version mismatch: expected {expected}, got {actual}"
        )
        self.slug = slug
        self.expected = expected
        self.actual = actual


# ----------------------------------------------------------------------------
# Top-level Project
# ----------------------------------------------------------------------------


@dataclass
class Project:
    name: str
    name_slug: str
    book: BookInfo
    voice_ref: str                     # display name of voice in library
    voice_ref_slug: str                # cached slug (matches voices/<slug>/)
    params: RenderParams
    status: ProjectStatus
    created: str = ""
    last_run: str = ""
    version: int = 1                   # optimistic-lock counter (DB-side)

    # ---- Path accessors (computed from PATHS.projects_root + slug) ----

    @property
    def dir(self) -> Path:
        from audiomat.state import PATHS
        return PATHS.project_dir(self.name_slug)

    @property
    def book_path(self) -> Path:
        return self.dir / self.book.filename

    @property
    def chunks_dir(self) -> Path:
        return self.dir / "chunks"

    @property
    def final_path(self) -> Path:
        return self.dir / "final.m4b"

    @property
    def render_log_path(self) -> Path:
        return self.dir / "render_log.txt"

    # ---- DB adapters ----

    @classmethod
    def from_row(cls, row: sqlite3.Row, blocks_skipped: list[int]) -> "Project":
        """Build a Project from a sqlite3.Row + the pre-fetched list of
        skipped block indices for that project. Callers (load /
        list_all / find_by_name) handle the blocks-skipped lookup so
        this constructor stays cheap."""
        params_dict = json.loads(row["params_json"])
        # Drop unknown keys gracefully so a future schema bump that
        # adds RenderParams fields doesn't crash old rows.
        valid_param_keys = set(RenderParams.__dataclass_fields__)
        params = RenderParams(**{k: v for k, v in params_dict.items()
                                  if k in valid_param_keys})
        return cls(
            name=row["name"],
            name_slug=row["name_slug"],
            book=BookInfo(
                filename=row["book_filename"],
                blocks_total=int(row["book_blocks_total"]),
                blocks_skipped=blocks_skipped,
                title=row["book_title"],
                author=row["book_author"],
                language=row["book_language"],
            ),
            voice_ref=row["voice_ref"],
            voice_ref_slug=row["voice_ref_slug"],
            params=params,
            status=ProjectStatus(
                chapters_done=int(row["status_chapters_done"]),
                chapters_total=int(row["status_chapters_total"]),
                last_completed=row["status_last_completed"],
                phase=row["status_phase"],
            ),
            created=row["created"] or "",
            last_run=row["last_run"] or "",
            version=int(row["version"]),
        )

    def _to_params(self) -> tuple:
        """Tuple of values matching the column order in INSERT/UPSERT."""
        return (
            self.name_slug, self.name,
            self.book.filename,
            self.book.title, self.book.author, self.book.language,
            int(self.book.blocks_total),
            self.voice_ref, self.voice_ref_slug,
            self.status.phase,
            int(self.status.chapters_done), int(self.status.chapters_total),
            self.status.last_completed,
            self.created or _utcnow_iso(),
            self.last_run,
            json.dumps(asdict(self.params), ensure_ascii=False),
        )

    # ---- IO ----

    def save(self) -> None:
        """Blind UPSERT. Always bumps ``version`` on UPDATE. Used by:

        * :meth:`Project.create` (initial INSERT)
        * Voice replacement flow in voices router (no If-Match contract)
        * Any other path where the caller doesn't track a previous version

        For user-driven PATCH endpoints, prefer :meth:`save_with_version`
        which detects the two-tab lost-update scenario.
        """
        conn = get_conn()
        with conn:
            conn.execute(
                "INSERT INTO projects "
                "(name_slug, name, book_filename, book_title, book_author, "
                " book_language, book_blocks_total, voice_ref, voice_ref_slug, "
                " status_phase, status_chapters_done, status_chapters_total, "
                " status_last_completed, created, last_run, params_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name_slug) DO UPDATE SET "
                "  name=excluded.name, "
                "  book_filename=excluded.book_filename, "
                "  book_title=excluded.book_title, "
                "  book_author=excluded.book_author, "
                "  book_language=excluded.book_language, "
                "  book_blocks_total=excluded.book_blocks_total, "
                "  voice_ref=excluded.voice_ref, "
                "  voice_ref_slug=excluded.voice_ref_slug, "
                "  status_phase=excluded.status_phase, "
                "  status_chapters_done=excluded.status_chapters_done, "
                "  status_chapters_total=excluded.status_chapters_total, "
                "  status_last_completed=excluded.status_last_completed, "
                "  last_run=excluded.last_run, "
                "  params_json=excluded.params_json, "
                "  version=projects.version+1",
                self._to_params(),
            )
            self._sync_blocks_skipped(conn)
            self.version = conn.execute(
                "SELECT version FROM projects WHERE name_slug=?",
                (self.name_slug,),
            ).fetchone()["version"]

    def save_with_version(self, expected_version: int) -> int:
        """Conditional UPDATE used by user PATCH endpoints that honor
        an ``If-Match`` header.

        Performs an atomic ``UPDATE … WHERE version=expected_version``.
        If 0 rows were affected, looks up the current version and
        raises :class:`ProjectVersionMismatch` so the caller can
        translate to 409. Returns the new ``version`` on success.
        """
        conn = get_conn()
        with conn:
            cur = conn.execute(
                "UPDATE projects SET "
                "  name=?, book_filename=?, book_title=?, book_author=?, "
                "  book_language=?, book_blocks_total=?, "
                "  voice_ref=?, voice_ref_slug=?, "
                "  status_phase=?, status_chapters_done=?, "
                "  status_chapters_total=?, status_last_completed=?, "
                "  last_run=?, params_json=?, "
                "  version=version+1 "
                "WHERE name_slug=? AND version=?",
                (
                    self.name, self.book.filename, self.book.title,
                    self.book.author, self.book.language,
                    int(self.book.blocks_total),
                    self.voice_ref, self.voice_ref_slug,
                    self.status.phase,
                    int(self.status.chapters_done),
                    int(self.status.chapters_total),
                    self.status.last_completed,
                    self.last_run, json.dumps(asdict(self.params), ensure_ascii=False),
                    self.name_slug, expected_version,
                ),
            )
            if cur.rowcount == 0:
                actual_row = conn.execute(
                    "SELECT version FROM projects WHERE name_slug=?",
                    (self.name_slug,),
                ).fetchone()
                if actual_row is None:
                    raise FileNotFoundError(f"project not found: {self.name_slug}")
                raise ProjectVersionMismatch(
                    self.name_slug, expected_version, int(actual_row["version"])
                )
            self._sync_blocks_skipped(conn)
            self.version = expected_version + 1
            return self.version

    def save_status(self) -> None:
        """Narrow UPDATE for ``status_*`` + ``last_run``. Does NOT bump
        version — render-loop progress events shouldn't conflict with
        the user's edit-version contract.

        Use this from the renderer's per-chunk / per-chapter callbacks.
        """
        conn = get_conn()
        with conn:
            conn.execute(
                "UPDATE projects SET "
                "  status_phase=?, status_chapters_done=?, "
                "  status_chapters_total=?, status_last_completed=?, "
                "  last_run=? "
                "WHERE name_slug=?",
                (
                    self.status.phase,
                    int(self.status.chapters_done),
                    int(self.status.chapters_total),
                    self.status.last_completed,
                    self.last_run or _utcnow_iso(),
                    self.name_slug,
                ),
            )

    def _sync_blocks_skipped(self, conn: sqlite3.Connection) -> None:
        """Replace project_blocks_skipped rows for this project with
        the current ``self.book.blocks_skipped`` list. Called inside
        the same transaction as the projects-table write so PATCH /
        create are atomic across both tables."""
        conn.execute(
            "DELETE FROM project_blocks_skipped WHERE project_slug=?",
            (self.name_slug,),
        )
        if self.book.blocks_skipped:
            conn.executemany(
                "INSERT INTO project_blocks_skipped (project_slug, block_index) "
                "VALUES (?, ?)",
                [(self.name_slug, int(idx)) for idx in self.book.blocks_skipped],
            )

    @classmethod
    def load(cls, slug: str) -> "Project":
        """Load a single project by slug. Raises FileNotFoundError if
        no row matches — same exception type the v0.2 FS load raised
        so existing catches stay correct."""
        conn = get_conn()
        row = conn.execute(
            "SELECT * FROM projects WHERE name_slug=?", (slug,)
        ).fetchone()
        if row is None:
            raise FileNotFoundError(f"project not found: {slug}")
        skipped = [
            int(r["block_index"]) for r in conn.execute(
                "SELECT block_index FROM project_blocks_skipped "
                "WHERE project_slug=? ORDER BY block_index",
                (slug,),
            )
        ]
        return cls.from_row(row, skipped)

    @classmethod
    def exists(cls, slug: str) -> bool:
        """Cheap PK-only check. Used by router validation paths."""
        return get_conn().execute(
            "SELECT 1 FROM projects WHERE name_slug=?", (slug,)
        ).fetchone() is not None

    @classmethod
    def list_all(cls) -> list["Project"]:
        """Enumerate all projects in name-slug order. One query for the
        rows + one batched query for blocks_skipped (vs. N+1 by-row
        lookups), so the cost stays O(1) round-trips no matter how
        many projects exist."""
        conn = get_conn()
        rows = conn.execute("SELECT * FROM projects ORDER BY name_slug").fetchall()
        if not rows:
            return []
        skipped_by_slug: dict[str, list[int]] = {row["name_slug"]: [] for row in rows}
        for r in conn.execute(
            "SELECT project_slug, block_index FROM project_blocks_skipped "
            "ORDER BY project_slug, block_index"
        ):
            slug = r["project_slug"]
            if slug in skipped_by_slug:
                skipped_by_slug[slug].append(int(r["block_index"]))
        return [cls.from_row(r, skipped_by_slug[r["name_slug"]]) for r in rows]

    @classmethod
    def find_by_name(cls, name: str) -> "Project | None":
        """Lookup by display name OR name_slug. Returns None if not
        found (no exception — different from load() because callers
        typically branch on existence)."""
        target_slug = slugify(name)
        row = get_conn().execute(
            "SELECT * FROM projects WHERE name=? OR name_slug=?",
            (name, target_slug),
        ).fetchone()
        if row is None:
            return None
        skipped = [
            int(r["block_index"]) for r in get_conn().execute(
                "SELECT block_index FROM project_blocks_skipped "
                "WHERE project_slug=? ORDER BY block_index",
                (row["name_slug"],),
            )
        ]
        return cls.from_row(row, skipped)

    @classmethod
    def create(
        cls,
        name: str,
        book_src: Path,
        voice_name: str,
        voice_slug: str | None = None,
        params: RenderParams | None = None,
        book_meta: dict | None = None,
        overwrite: bool = False,
    ) -> "Project":
        """Create a new project: copies the book file into the project
        directory and INSERTs a row. Slug-collision-safe — rejects an
        existing slug unless ``overwrite=True``.

        The voice ref is denormalized (display name + slug both stored)
        so deleting the voice later doesn't break the project's
        identity — re-pointing happens via PATCH /projects/{slug}/voice
        or DELETE /voices/{slug}?replacement=…
        """
        slug = slugify(name)
        if cls.exists(slug) and not overwrite:
            raise FileExistsError(f"project already exists: {slug}")

        from audiomat.state import PATHS
        target = PATHS.project_dir(slug)
        target.mkdir(parents=True, exist_ok=True)

        book_src = Path(book_src)
        ext = book_src.suffix.lower() or ".epub"
        book_dst_name = f"book{ext}"
        shutil.copyfile(book_src, target / book_dst_name)

        book = BookInfo(filename=book_dst_name, **(book_meta or {}))
        proj = cls(
            name=name,
            name_slug=slug,
            book=book,
            voice_ref=voice_name,
            voice_ref_slug=voice_slug or slugify(voice_name),
            params=params or RenderParams(),
            status=ProjectStatus(chapters_total=book.blocks_total),
            created=_utcnow_iso(),
            last_run="",
            version=1,
        )
        proj.save()
        return proj

    # ---- Mutation helpers ----

    def set_status(self, **changes) -> None:
        """Update fields on ``self.status`` and persist via the narrow
        save_status() path (no version bump). Convenience for the
        renderer's "another chapter done" callback."""
        for k, v in changes.items():
            setattr(self.status, k, v)
        self.last_run = _utcnow_iso()
        self.save_status()

    def append_log(self, line: str) -> None:
        """Append a line to ``render_log.txt`` (creates if missing).
        Timestamp prefix is added automatically."""
        with self.render_log_path.open("a", encoding="utf-8") as f:
            f.write(f"[{_utcnow_iso()}] {line.rstrip()}\n")

    def delete(self) -> None:
        """Permanently remove the row + the project directory. The
        project_blocks_skipped + chunk_manifest rows cascade via FK.
        Caller decides whether to confirm with the user."""
        get_conn().execute(
            "DELETE FROM projects WHERE name_slug=?", (self.name_slug,)
        )
        if self.dir.exists():
            shutil.rmtree(self.dir)


def _utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    # Round-trip smoke — `python -m audiomat.project`
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AUDIOMAT_LIBRARY_ROOT"] = tmp
        import importlib
        import audiomat.state
        importlib.reload(audiomat.state)
        import audiomat.db
        audiomat.db.close_all()

        fake_book = Path(tmp) / "in_book.epub"
        fake_book.write_bytes(b"\x00" * 256)

        proj = Project.create(
            name="Skleněný muž",
            book_src=fake_book,
            voice_name="Lucie Ježková",
            book_meta={
                "blocks_total": 166,
                "blocks_skipped": [0, 1, 2],
                "title": "Skleněný muž",
                "author": "Anders de la Motte",
                "language": "cs",
            },
        )
        print(f"created: {proj.name} -> {proj.dir}  (version={proj.version})")
        print(f"        voice_ref   = {proj.voice_ref!r} (slug {proj.voice_ref_slug!r})")
        print(f"        params      = num_step={proj.params.num_step}, gs={proj.params.guidance_scale}")
        print(f"        skipped     = {proj.book.blocks_skipped}")

        # set_status (no version bump)
        before_v = proj.version
        proj.set_status(chapters_done=53, last_completed="053_some_chapter", phase="rendering")
        loaded = Project.load(proj.name_slug)
        print(f"after set_status: version={loaded.version} (was {before_v}; should equal)")
        print(f"        status      = phase={loaded.status.phase}, "
              f"{loaded.status.chapters_done}/{loaded.status.chapters_total}")

        # save_with_version success path
        loaded.params.speed = 0.95
        new_v = loaded.save_with_version(expected_version=loaded.version)
        print(f"after PATCH: version={new_v} (was {loaded.version - 1})")

        # save_with_version conflict path
        try:
            loaded.params.speed = 1.05
            loaded.save_with_version(expected_version=1)  # stale
        except ProjectVersionMismatch as e:
            print(f"got expected mismatch: {e}")

        print(f"list_all: {[p.name for p in Project.list_all()]}")
        proj_reload = Project.load(proj.name_slug)
        proj_reload.delete()
        print(f"after delete: list_all={Project.list_all()}")
        audiomat.db.close_all()
