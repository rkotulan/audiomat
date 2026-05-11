"""Shared pytest fixtures for audiomat tests.

The TestClient fixture redirects ``AudiomatPaths`` to a tmp dir so the
real ``~/audiomat`` library is never touched during a test run.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def isolated_library(tmp_path: Path, monkeypatch) -> Path:
    """Point AUDIOMAT_LIBRARY_ROOT at a tmp dir for the duration of the
    test. Use this whenever the test will exercise an endpoint that
    reads/writes voices/ or projects/.
    """
    monkeypatch.setenv("AUDIOMAT_LIBRARY_ROOT", str(tmp_path))
    # AudiomatPaths.default() is read at import time on audiomat.state — we
    # must reload it so the new env var takes effect.
    import importlib
    import audiomat.state
    importlib.reload(audiomat.state)
    yield tmp_path
