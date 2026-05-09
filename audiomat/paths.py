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

    def voice_dir(self, slug: str) -> Path:
        return self.voices_root / slug

    def project_dir(self, slug: str) -> Path:
        return self.projects_root / slug

    def ensure_dirs(self) -> None:
        """Create the three root directories if they don't exist."""
        for d in (self.voices_root, self.projects_root, self.cache_root):
            d.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    p = AudiomatPaths.default()
    print(f"library_root  = {p.library_root}")
    print(f"voices_root   = {p.voices_root}")
    print(f"projects_root = {p.projects_root}")
    print(f"cache_root    = {p.cache_root}")
