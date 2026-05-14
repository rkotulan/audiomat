"""Voice library — one cloned-voice asset per slug.

Backed by a row in the ``voices`` table (audiomat.db) plus two files
on disk inside ``voices/<slug>/``:

* ``voice.wav`` — 24 kHz mono 16-bit, 5–10 s recommended (OmniVoice's
  tested range; see CLAUDE.md gotchas).
* ``voice.txt`` — exact transcript of ``voice.wav`` (manually revised
  after Whisper auto-draft is the proven pattern).

In v0.2 the metadata lived in ``voices/<slug>/meta.json`` next to the
WAV/txt. v0.3 moved it into SQLite for atomic writes and to remove the
"walk N directories on every list" cost. Binaries stay on disk (BLOBs
make backup / inspection painful at hundreds of MB).

This module handles only data — file format conversion / Whisper
auto-transcription happens in higher-level orchestration (api.py /
audio.py).
"""
from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from audiomat.db import get_conn
from audiomat.slug import slugify


@dataclass
class Voice:
    """Voice library entry. Persisted in the ``voices`` table; the
    ``dir``/``wav_path``/``txt_path`` properties point to the
    on-disk binaries which live next to where ``meta.json`` used to."""

    name: str
    name_slug: str
    duration_s: float
    sample_rate: int
    channels: int
    transcript_chars: int
    notes: str = ""
    created: str = ""           # ISO 8601 UTC, e.g. "2026-05-09T18:00:00Z"
    # Optional TTS model slug from the model registry (audiomat/model_registry.py).
    # None / "" / "default" → use the stock k2-fsa/OmniVoice model.
    # Anything else → look up registry, fall back to default if missing.
    # Lets a fine-tuned model travel with the voice that was trained on
    # (e.g. a Ježková clone voice automatically picks the jezkova-v1
    # fine-tune at preview / render time).
    tts_model: str | None = None

    # ---- on-disk paths (computed from PATHS.voices_root + slug) ----

    @property
    def dir(self) -> Path:
        """Directory holding voice.wav + voice.txt for this voice.
        Resolves against the live ``PATHS`` so test fixtures with
        AUDIOMAT_LIBRARY_ROOT overrides work transparently."""
        from audiomat.state import PATHS
        return PATHS.voice_dir(self.name_slug)

    @property
    def wav_path(self) -> Path:
        return self.dir / "voice.wav"

    @property
    def txt_path(self) -> Path:
        return self.dir / "voice.txt"

    @property
    def is_valid(self) -> bool:
        """Quick health check: WAV + transcript both exist and are
        non-empty. Doesn't verify the DB row — callers that hold a
        Voice instance already know it loaded successfully."""
        return all(
            p.exists() and p.stat().st_size > 0
            for p in (self.wav_path, self.txt_path)
        )

    def transcript(self) -> str:
        """Read voice.txt as a UTF-8 string, stripped."""
        return self.txt_path.read_text(encoding="utf-8").strip()

    # ---- DB adapters ----

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Voice":
        """Build a Voice from a sqlite3.Row (row_factory is set globally
        in db.get_conn). Used by load() / list_all() / find_by_name()."""
        return cls(
            name=row["name"],
            name_slug=row["name_slug"],
            duration_s=float(row["duration_s"]),
            sample_rate=int(row["sample_rate"]),
            channels=int(row["channels"]),
            transcript_chars=int(row["transcript_chars"]),
            notes=row["notes"] or "",
            created=row["created"] or "",
            tts_model=row["tts_model"],
        )

    # ---- IO ----

    def save(self) -> None:
        """UPSERT into voices. Doesn't touch voice.wav / voice.txt —
        caller is responsible for placing those next to where ``dir``
        points before calling save() (Voice.create() does both)."""
        get_conn().execute(
            "INSERT INTO voices "
            "(name_slug, name, duration_s, sample_rate, channels, "
            " transcript_chars, notes, created, tts_model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(name_slug) DO UPDATE SET "
            "  name=excluded.name, "
            "  duration_s=excluded.duration_s, "
            "  sample_rate=excluded.sample_rate, "
            "  channels=excluded.channels, "
            "  transcript_chars=excluded.transcript_chars, "
            "  notes=excluded.notes, "
            "  tts_model=excluded.tts_model",
            (
                self.name_slug, self.name,
                round(float(self.duration_s), 3),
                int(self.sample_rate), int(self.channels),
                int(self.transcript_chars), self.notes,
                self.created or _utcnow_iso(), self.tts_model,
            ),
        )

    @classmethod
    def load(cls, slug: str) -> "Voice":
        """Load a single voice by slug. Raises FileNotFoundError when no
        matching row exists — keeps the same exception type the v0.2
        FS-based load used so callers don't need to change the catch."""
        row = get_conn().execute(
            "SELECT name_slug, name, duration_s, sample_rate, channels, "
            "       transcript_chars, notes, created, tts_model "
            "FROM voices WHERE name_slug=?",
            (slug,),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(f"voice not found: {slug}")
        return cls.from_row(row)

    @classmethod
    def exists(cls, slug: str) -> bool:
        """Cheap existence check that doesn't materialize a Voice. Used
        by routers that just need to validate before a UPDATE / DELETE."""
        return get_conn().execute(
            "SELECT 1 FROM voices WHERE name_slug=?", (slug,)
        ).fetchone() is not None

    @classmethod
    def list_all(cls) -> list["Voice"]:
        """Enumerate voices in name-slug order. Returns [] when the
        library is empty (fresh install)."""
        rows = get_conn().execute(
            "SELECT name_slug, name, duration_s, sample_rate, channels, "
            "       transcript_chars, notes, created, tts_model "
            "FROM voices ORDER BY name_slug"
        ).fetchall()
        return [cls.from_row(r) for r in rows]

    @classmethod
    def find_by_name(cls, name: str) -> "Voice | None":
        """Look up by display name OR name_slug — both forms hit. Lets a
        config that says ``"Lucie Ježková"`` find the row stored under
        slug ``Lucie_Jezkova`` even though the display name is unique-ish
        but not the PK."""
        target_slug = slugify(name)
        row = get_conn().execute(
            "SELECT name_slug, name, duration_s, sample_rate, channels, "
            "       transcript_chars, notes, created, tts_model "
            "FROM voices WHERE name=? OR name_slug=?",
            (name, target_slug),
        ).fetchone()
        return cls.from_row(row) if row else None

    @classmethod
    def create(
        cls,
        name: str,
        wav_src: Path,
        transcript_text: str,
        duration_s: float,
        sample_rate: int,
        channels: int,
        notes: str = "",
        overwrite: bool = False,
        tts_model: str | None = None,
    ) -> "Voice":
        """Create a new voice library entry. Copies ``wav_src`` to the
        canonical voices/<slug>/voice.wav location, writes the
        transcript verbatim, and inserts the meta row. Slug collision
        is rejected unless ``overwrite=True``.

        Atomicity: the FS copy happens BEFORE the INSERT so a failed
        insert (e.g. unique-slug conflict on a parallel create) doesn't
        leave a stale WAV behind — the COPY+INSERT pair runs inside a
        single transaction so both succeed or both fail."""
        slug = slugify(name)
        if cls.exists(slug) and not overwrite:
            raise FileExistsError(f"voice already exists: {slug}")

        from audiomat.state import PATHS
        target_dir = PATHS.voice_dir(slug)
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(wav_src, target_dir / "voice.wav")
        (target_dir / "voice.txt").write_text(
            transcript_text.strip() + "\n",
            encoding="utf-8",
        )

        voice = cls(
            name=name,
            name_slug=slug,
            duration_s=duration_s,
            sample_rate=sample_rate,
            channels=channels,
            transcript_chars=len(transcript_text.strip()),
            notes=notes,
            created=_utcnow_iso(),
            tts_model=tts_model,
        )
        voice.save()
        return voice

    def delete(self) -> None:
        """Remove the row + the voice directory. Caller should first
        verify no active project references this voice (caller-side
        check — see DELETE /api/voices/{slug} in the router for the
        replacement flow)."""
        get_conn().execute(
            "DELETE FROM voices WHERE name_slug=?", (self.name_slug,)
        )
        if self.dir.exists():
            shutil.rmtree(self.dir)


def _utcnow_iso() -> str:
    """ISO 8601 UTC timestamp with trailing Z. Used everywhere we record
    a creation / last-run wall-clock."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    # Smoke test: round-trip a Voice through save/load against a tmp
    # library. `python -m audiomat.voice`
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AUDIOMAT_LIBRARY_ROOT"] = tmp
        # Reload state + db so PATHS picks up the env var and the DB
        # opens against the tmp library root.
        import importlib
        import audiomat.state
        importlib.reload(audiomat.state)
        import audiomat.db
        audiomat.db.close_all()

        # Stage WAV + txt where Voice.create expects to copy from.
        src_wav = Path(tmp) / "src_voice.wav"
        src_wav.write_bytes(b"\x00" * 1024)

        v = Voice.create(
            name="Lucie Ježková",
            wav_src=src_wav,
            transcript_text="Holohlavá holka.",
            duration_s=10.0, sample_rate=24000, channels=1,
            notes="smoke test",
        )
        print(f"created : {v.name} ({v.duration_s}s, slug={v.name_slug})")
        loaded = Voice.load(v.name_slug)
        print(f"loaded  : {loaded.name} ({loaded.duration_s}s, {loaded.sample_rate}Hz)")
        print(f"transcript: {loaded.transcript()!r}")
        print(f"is_valid : {loaded.is_valid}")
        print(f"list_all : {[x.name for x in Voice.list_all()]}")
        found = Voice.find_by_name("Lucie Ježková")
        print(f"find    : {found.name if found else None}")
        loaded.delete()
        print(f"after delete: list_all={Voice.list_all()}")
        # Release the DB handle so the tempdir can be cleaned up on
        # Windows (open SQLite file blocks rmtree).
        audiomat.db.close_all()
