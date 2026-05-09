"""Project — one audiobook in one directory.

A project is everything that produces one M4B output:

* ``config.json`` — render params, voice reference, status (this module).
* ``book.epub`` (or ``book.txt``) — source content, copied on import.
* ``chunks/<NNN_stem>/`` — per-chapter chunks + manifest + per-chap WAV.
* ``final.m4b`` — concatenated AAC + chapter markers (after make_m4b).
* ``render_log.txt`` — appended-to status line on each significant event.

Naming is immutable in v0.1: once created, ``slug`` cannot change. Workaround:
download the artifacts and create a new project. (Filesystem GUID + display
name mapping is on the v0.2 wishlist.)
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from audiomat.slug import slugify


# ----------------------------------------------------------------------------
# Sub-dataclasses serialized into config.json
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
# Top-level Project
# ----------------------------------------------------------------------------


@dataclass
class Project:
    name: str
    name_slug: str
    dir: Path
    book: BookInfo
    voice_ref: str                     # display name of voice in library
    voice_ref_slug: str                # cached slug (matches voices/<slug>/)
    params: RenderParams
    status: ProjectStatus
    created: str = ""
    last_run: str = ""

    # -- Path accessors --

    @property
    def config_path(self) -> Path:
        return self.dir / "config.json"

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

    # -- IO --

    def save(self) -> None:
        cfg = {
            "name": self.name,
            "name_slug": self.name_slug,
            "created": self.created or _utcnow_iso(),
            "last_run": self.last_run,
            "book": asdict(self.book),
            "voice_ref": self.voice_ref,
            "voice_ref_slug": self.voice_ref_slug,
            "params": asdict(self.params),
            "status": asdict(self.status),
        }
        self.config_path.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls, dir: Path) -> "Project":
        cfg_path = dir / "config.json"
        if not cfg_path.exists():
            raise FileNotFoundError(cfg_path)
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        return cls(
            name=cfg["name"],
            name_slug=cfg.get("name_slug") or slugify(cfg["name"]),
            dir=dir,
            book=BookInfo(**cfg.get("book", {})),
            voice_ref=cfg.get("voice_ref", ""),
            voice_ref_slug=cfg.get("voice_ref_slug", ""),
            params=RenderParams(**cfg.get("params", {})),
            status=ProjectStatus(**cfg.get("status", {})),
            created=cfg.get("created", ""),
            last_run=cfg.get("last_run", ""),
        )

    @classmethod
    def list_all(cls, projects_root: Path) -> list["Project"]:
        if not projects_root.exists():
            return []
        out: list[Project] = []
        for d in sorted(projects_root.iterdir()):
            if not d.is_dir():
                continue
            if not (d / "config.json").exists():
                continue
            try:
                out.append(cls.load(d))
            except (json.JSONDecodeError, KeyError, ValueError, FileNotFoundError):
                continue
        return out

    @classmethod
    def find_by_name(cls, projects_root: Path, name: str) -> "Project | None":
        target_slug = slugify(name)
        for p in cls.list_all(projects_root):
            if p.name == name or p.name_slug == target_slug:
                return p
        return None

    @classmethod
    def create(
        cls,
        projects_root: Path,
        name: str,
        book_src: Path,
        voice_name: str,
        voice_slug: str | None = None,
        params: RenderParams | None = None,
        book_meta: dict | None = None,
        overwrite: bool = False,
    ) -> "Project":
        """Create a new project on disk.

        - Slug-collision-safe: rejects existing slug unless ``overwrite=True``.
        - Copies ``book_src`` into the project dir as ``book.<ext>``.
        - Initializes config.json with default render params and voice ref.
        - Caller is responsible for ensuring the voice exists in the library
          before render starts (this layer doesn't cross-check Voice).
        """
        slug = slugify(name)
        target = projects_root / slug
        if target.exists() and not overwrite:
            raise FileExistsError(f"project already exists: {target}")

        target.mkdir(parents=True, exist_ok=True)

        # Copy book in. Preserve original extension (epub / txt).
        book_src = Path(book_src)
        ext = book_src.suffix.lower() or ".epub"
        book_dst_name = f"book{ext}"
        shutil.copyfile(book_src, target / book_dst_name)

        book = BookInfo(filename=book_dst_name, **(book_meta or {}))
        proj = cls(
            name=name,
            name_slug=slug,
            dir=target,
            book=book,
            voice_ref=voice_name,
            voice_ref_slug=voice_slug or slugify(voice_name),
            params=params or RenderParams(),
            status=ProjectStatus(chapters_total=book.blocks_total),
            created=_utcnow_iso(),
            last_run="",
        )
        proj.save()
        return proj

    # -- Mutation helpers --

    def set_status(self, **changes) -> None:
        """Update fields on ``self.status`` and persist. Convenience for
        the renderer's "another chapter done" callback."""
        for k, v in changes.items():
            setattr(self.status, k, v)
        self.last_run = _utcnow_iso()
        self.save()

    def append_log(self, line: str) -> None:
        """Append a line to ``render_log.txt`` (creates if missing).
        Timestamp prefix is added automatically."""
        with self.render_log_path.open("a", encoding="utf-8") as f:
            f.write(f"[{_utcnow_iso()}] {line.rstrip()}\n")

    def delete(self) -> None:
        """Permanently remove the project directory. Caller decides whether
        to confirm with the user."""
        if self.dir.exists():
            shutil.rmtree(self.dir)


def _utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    # Round-trip smoke test — `python -m audiomat.project`
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        proj_root = Path(tmp) / "projects"
        proj_root.mkdir()

        # Fake a book.epub source
        fake_book = Path(tmp) / "in_book.epub"
        fake_book.write_bytes(b"\x00" * 256)

        proj = Project.create(
            proj_root,
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
        print(f"created: {proj.name} -> {proj.dir}")
        print(f"        config_path = {proj.config_path}")
        print(f"        book_path   = {proj.book_path} ({'exists' if proj.book_path.exists() else 'MISSING'})")
        print(f"        voice_ref   = {proj.voice_ref!r} (slug {proj.voice_ref_slug!r})")
        print(f"        params.num_step={proj.params.num_step}, gs={proj.params.guidance_scale}, speed={proj.params.speed}")

        # Mutate + reload
        proj.set_status(chapters_done=53, last_completed="053_some_chapter", phase="rendering")
        loaded = Project.load(proj.dir)
        print(f"reloaded status: phase={loaded.status.phase}, "
              f"{loaded.status.chapters_done}/{loaded.status.chapters_total}, "
              f"last={loaded.status.last_completed}")

        # list_all
        print(f"list_all: {[p.name for p in Project.list_all(proj_root)]}")
