"""Tests for the optimistic-lock PATCH path on projects.

Verifies the two-tab lost-update scenario the v0.3 migration was built
to solve: client A holds version=N, client B PATCHes to version=N+1,
client A's PATCH with ``If-Match: N`` fails with 409 instead of
silently overwriting B's change.

Tests fake the project entry via Project.create() — no book parsing
needed because we only touch the PATCH endpoints (which mutate
metadata, not the rendered output).
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient


def _client(isolated_library):
    import importlib
    import audiomat.api
    importlib.reload(audiomat.api)
    return TestClient(audiomat.api.app)


def _seed_voice(slug: str = "voice_a") -> None:
    from audiomat.db import get_conn
    get_conn().execute(
        "INSERT INTO voices (name_slug, name, duration_s, sample_rate, "
        "channels, transcript_chars, notes, created, tts_model) "
        "VALUES (?, 'Voice A', 8.0, 24000, 1, 14, '', "
        "'2026-05-13T00:00:00Z', NULL)",
        (slug,),
    )


def _seed_project(library_root: Path, slug: str = "p1") -> int:
    """Create a project via Project.create so the on-disk book stub
    and DB row land in the right place. Returns the initial version."""
    _seed_voice()
    from audiomat.project import Project
    book = library_root / "_book.txt"
    book.write_text("Some text.", encoding="utf-8")
    proj = Project.create(
        name=slug, book_src=book, voice_name="Voice A",
        voice_slug="voice_a", book_meta={"language": "cs"},
    )
    return proj.version


def test_get_project_returns_version(isolated_library):
    _seed_project(isolated_library)
    c = _client(isolated_library)
    r = c.get("/api/projects/p1")
    assert r.status_code == 200
    assert r.json()["version"] == 1


def test_patch_without_if_match_blind_save_bumps_version(isolated_library):
    """Backward-compat: clients that don't send If-Match still get the
    v0.2 behavior (last-write-wins). The version still increments so
    a subsequent If-Match-aware client sees the change."""
    _seed_project(isolated_library)
    c = _client(isolated_library)
    r = c.patch("/api/projects/p1/params", json={"speed": 0.9})
    assert r.status_code == 200, r.json()
    assert r.json()["version"] == 2


def test_patch_with_matching_if_match_succeeds(isolated_library):
    _seed_project(isolated_library)
    c = _client(isolated_library)
    r = c.patch(
        "/api/projects/p1/params",
        json={"speed": 0.9},
        headers={"If-Match": "1"},
    )
    assert r.status_code == 200, r.json()
    assert r.json()["version"] == 2
    assert r.json()["params"]["speed"] == 0.9


def test_patch_with_stale_if_match_returns_409(isolated_library):
    """The lost-update fix: client A is on v1 after first read.
    Client B successfully PATCHes (v1 → v2). Client A's PATCH with
    If-Match: 1 must NOT silently overwrite B's change."""
    _seed_project(isolated_library)
    c = _client(isolated_library)
    # Client B: blind PATCH bumps to v2.
    c.patch("/api/projects/p1/params", json={"speed": 0.9})
    # Client A: stale If-Match=1.
    r = c.patch(
        "/api/projects/p1/params",
        json={"speed": 1.1},
        headers={"If-Match": "1"},
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["expected_version"] == 1
    assert detail["current_version"] == 2
    # Project still reflects B's change, not A's.
    after = c.get("/api/projects/p1").json()
    assert after["params"]["speed"] == 0.9
    assert after["version"] == 2


def test_blocks_skipped_patch_honors_if_match(isolated_library):
    _seed_project(isolated_library)
    c = _client(isolated_library)
    c.patch("/api/projects/p1/blocks-skipped", json={"indices": [3]})
    r = c.patch(
        "/api/projects/p1/blocks-skipped",
        json={"indices": [5]},
        headers={"If-Match": "1"},
    )
    assert r.status_code == 409


def test_voice_patch_honors_if_match(isolated_library):
    _seed_project(isolated_library)
    # Need a second voice to swap to.
    from audiomat.db import get_conn
    get_conn().execute(
        "INSERT INTO voices (name_slug, name, duration_s, sample_rate, "
        "channels, transcript_chars, notes, created, tts_model) "
        "VALUES ('voice_b', 'Voice B', 8.0, 24000, 1, 14, '', "
        "'2026-05-13T00:00:00Z', NULL)"
    )
    c = _client(isolated_library)
    # Trigger a bump so v1 is stale.
    c.patch("/api/projects/p1/params", json={"speed": 0.95})
    r = c.patch(
        "/api/projects/p1/voice",
        json={"voice_slug": "voice_b"},
        headers={"If-Match": "1"},
    )
    assert r.status_code == 409


def test_book_patch_honors_if_match(isolated_library):
    _seed_project(isolated_library)
    c = _client(isolated_library)
    c.patch("/api/projects/p1/book", json={"language": "en"})
    r = c.patch(
        "/api/projects/p1/book",
        json={"language": "de"},
        headers={"If-Match": "1"},
    )
    assert r.status_code == 409
