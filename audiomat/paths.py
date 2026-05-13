"""Filesystem layout for the audiomat library.

audiomat keeps three root directories under one library:

* ``voices/<slug>/`` — shared voice library (voice.wav + voice.txt + meta.json),
  re-usable across projects.
* ``projects/<slug>/`` — per-book project (config.json + book.epub +
  chunks/ + final.m4b).
* ``cache/`` — HuggingFace model cache (mounted in Docker as a separate
  volume so it survives container rebuilds).

The library root is ``~/audiomat`` by default. Override via
``AUDIOMAT_LIBRARY_ROOT`` env var (Docker sets ``/data``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ENV_VAR = "AUDIOMAT_LIBRARY_ROOT"
DEFAULT_LIBRARY_NAME = "audiomat"


@dataclass(frozen=True)
class AudiomatPaths:
    """Resolved paths for one audiomat library instance."""

    library_root: Path

    @classmethod
    def default(cls) -> "AudiomatPaths":
        """Resolve the library root from env var or fall back to
        ``~/audiomat``. Does not create directories — call
        :meth:`ensure_dirs` to materialize.
        """
        env = os.environ.get(ENV_VAR)
        if env:
            return cls(library_root=Path(env).expanduser().resolve())
        return cls(library_root=Path.home() / DEFAULT_LIBRARY_NAME)

    @property
    def voices_root(self) -> Path:
        return self.library_root / "voices"

    @property
    def projects_root(self) -> Path:
        return self.library_root / "projects"

    @property
    def cache_root(self) -> Path:
        return self.library_root / "cache"

    @property
    def models_root(self) -> Path:
        """User-registered TTS model checkpoints (fine-tunes, HF-sourced
        snapshots). Each entry lives under ``models/<slug>/`` with a
        ``meta.json`` plus the model files (config.json + safetensors etc.)
        that ``OmniVoice.from_pretrained(<local_path>)`` consumes."""
        return self.library_root / "models"

    @property
    def secrets_path(self) -> Path:
        """Single JSON file with credentials (HF token, etc.). Permission
        bits set to 0o600 by the writer; gitignored by convention. Not
        encrypted — fs perms are the security boundary on a single-user
        host."""
        return self.library_root / "secrets.json"

    @property
    def settings_path(self) -> Path:
        """Non-sensitive user prefs (voice validation text, etc.). Sibling
        of secrets.json but kept separate so we don't have to think about
        file permissions when adding new prefs. See
        :mod:`audiomat.settings_store`."""
        return self.library_root / "settings.json"

    def voice_dir(self, slug: str) -> Path:
        return self.voices_root / slug

    def project_dir(self, slug: str) -> Path:
        return self.projects_root / slug

    def model_dir(self, slug: str) -> Path:
        return self.models_root / slug

    def ensure_dirs(self) -> None:
        """Create the four root directories if they don't exist."""
        for d in (self.voices_root, self.projects_root,
                  self.cache_root, self.models_root):
            d.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    p = AudiomatPaths.default()
    print(f"library_root  = {p.library_root}")
    print(f"voices_root   = {p.voices_root}")
    print(f"projects_root = {p.projects_root}")
    print(f"cache_root    = {p.cache_root}")
