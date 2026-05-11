"""Voice library — one cloned-voice asset per directory.

A voice is a triple of files inside ``voices/<slug>/``:

* ``voice.wav`` — 24 kHz mono 16-bit, 5–10 s recommended (OmniVoice's
  tested range; see CLAUDE.md gotchas).
* ``voice.txt`` — exact transcript of ``voice.wav`` (manually revised
  after Whisper auto-draft is the proven pattern).
* ``meta.json`` — wav properties + display name + notes (this module).

This file handles only data — file format conversion / Whisper
auto-transcription happens in higher-level orchestration (api.py / audio.py).
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from audiomat.slug import slugify


@dataclass
class Voice:
    """Voice library entry. Loaded from / saved to ``meta.json``."""

    name: str
    name_slug: str
    dir: Path
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

    @property
    def wav_path(self) -> Path:
        return self.dir / "voice.wav"

    @property
    def txt_path(self) -> Path:
        return self.dir / "voice.txt"

    @property
    def meta_path(self) -> Path:
        return self.dir / "meta.json"

    @property
    def is_valid(self) -> bool:
        """Quick health check: all three files exist and are non-empty."""
        return all(
            p.exists() and p.stat().st_size > 0
            for p in (self.wav_path, self.txt_path, self.meta_path)
        )

    def transcript(self) -> str:
        """Read voice.txt as a UTF-8 string, stripped."""
        return self.txt_path.read_text(encoding="utf-8").strip()

    # -- IO --

    def save(self) -> None:
        """Write meta.json. Caller is responsible for placing voice.wav and
        voice.txt into ``self.dir`` first.
        """
        meta = {
            "name": self.name,
            "name_slug": self.name_slug,
            "created": self.created or _utcnow_iso(),
            "duration_s": round(float(self.duration_s), 3),
            "sample_rate": int(self.sample_rate),
            "channels": int(self.channels),
            "transcript_chars": int(self.transcript_chars),
            "notes": self.notes,
            "tts_model": self.tts_model,
        }
        self.meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls, dir: Path) -> "Voice":
        """Load a voice from ``voices/<slug>/meta.json``."""
        meta_path = dir / "meta.json"
        if not meta_path.exists():
            raise FileNotFoundError(meta_path)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return cls(
            name=meta["name"],
            name_slug=meta.get("name_slug") or slugify(meta["name"]),
            dir=dir,
            duration_s=float(meta["duration_s"]),
            sample_rate=int(meta["sample_rate"]),
            channels=int(meta["channels"]),
            transcript_chars=int(meta.get("transcript_chars", 0)),
            notes=meta.get("notes", ""),
            created=meta.get("created", ""),
            tts_model=meta.get("tts_model"),
        )

    @classmethod
    def list_all(cls, voices_root: Path) -> list["Voice"]:
        """Enumerate voices in the library. Skips dirs without a valid
        meta.json (corrupt entries are silently dropped)."""
        if not voices_root.exists():
            return []
        out: list[Voice] = []
        for d in sorted(voices_root.iterdir()):
            if not d.is_dir():
                continue
            if not (d / "meta.json").exists():
                continue
            try:
                out.append(cls.load(d))
            except (json.JSONDecodeError, KeyError, ValueError, FileNotFoundError):
                continue
        return out

    @classmethod
    def find_by_name(cls, voices_root: Path, name: str) -> "Voice | None":
        """Look up by display name. Slug-equivalent names match (e.g. a
        config that says ``"Lucie Ježková"`` finds ``voices/Lucie_Jezkova/``)."""
        target_slug = slugify(name)
        for v in cls.list_all(voices_root):
            if v.name == name or v.name_slug == target_slug:
                return v
        return None

    @classmethod
    def create(
        cls,
        voices_root: Path,
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
        """Create a new voice library entry.

        ``wav_src`` is copied into the new voice dir as ``voice.wav`` —
        caller must convert format upstream (audio.py will provide the
        24 kHz mono 16-bit converter). Transcript is written verbatim.

        Slug collision is rejected unless ``overwrite=True``.
        """
        slug = slugify(name)
        target = voices_root / slug
        if target.exists() and not overwrite:
            raise FileExistsError(f"voice already exists: {target}")

        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(wav_src, target / "voice.wav")
        (target / "voice.txt").write_text(
            transcript_text.strip() + "\n",
            encoding="utf-8",
        )

        voice = cls(
            name=name,
            name_slug=slug,
            dir=target,
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
        """Remove the voice directory. Caller should first verify
        no active project references this voice (caller-side check)."""
        if self.dir.exists():
            shutil.rmtree(self.dir)


def _utcnow_iso() -> str:
    """ISO 8601 UTC timestamp with trailing Z. Used everywhere we record
    a creation / last-run wall-clock."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    # Smoke test: round-trip a Voice through save/load via tempfile.
    # `python -m audiomat.voice`
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "voices"
        root.mkdir()
        # Synthesize a fake voice (write empty wav + txt — enough for round-trip)
        slug = slugify("Lucie Ježková")
        (root / slug).mkdir()
        (root / slug / "voice.wav").write_bytes(b"\x00" * 1024)
        (root / slug / "voice.txt").write_text("Holohlavá holka.", encoding="utf-8")
        v = Voice(
            name="Lucie Ježková",
            name_slug=slug,
            dir=root / slug,
            duration_s=10.0,
            sample_rate=24000,
            channels=1,
            transcript_chars=16,
            notes="smoke test",
        )
        v.save()
        # Load + compare
        loaded = Voice.load(root / slug)
        print(f"saved   : {v.name} ({v.duration_s}s, {v.sample_rate}Hz)")
        print(f"loaded  : {loaded.name} ({loaded.duration_s}s, {loaded.sample_rate}Hz)")
        print(f"transcript: {loaded.transcript()!r}")
        print(f"is_valid : {loaded.is_valid}")
        # list_all
        print(f"list_all : {[v.name for v in Voice.list_all(root)]}")
        # find_by_name
        found = Voice.find_by_name(root, "Lucie Ježková")
        print(f"find     : {found.name if found else None}")
