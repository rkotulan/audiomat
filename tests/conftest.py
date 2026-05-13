"""Shared pytest fixtures for audiomat tests.

The TestClient fixture redirects ``AudiomatPaths`` to a tmp dir so the
real ``~/audiomat`` library is never touched during a test run.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture
def isolated_library(tmp_path: Path, monkeypatch) -> Path:
    """Point AUDIOMAT_LIBRARY_ROOT at a tmp dir for the duration of the
    test. Use this whenever the test will exercise an endpoint that
    reads/writes voices/ or projects/.

    Reloads ``audiomat.state`` (so ``PATHS`` re-resolves against the new
    env var) **and** every cached ``audiomat.routers.*`` module (so
    routers that bound ``PATHS`` at import time rebind to the fresh
    one). Without the router reload, endpoints that hit the library
    filesystem keep talking to the first test's tmp dir — the source of
    a previously-silent cross-test bug.
    """
    monkeypatch.setenv("AUDIOMAT_LIBRARY_ROOT", str(tmp_path))
    import importlib
    import audiomat.state
    importlib.reload(audiomat.state)
    for name in list(sys.modules):
        if name.startswith("audiomat.routers."):
            importlib.reload(sys.modules[name])
    yield tmp_path
