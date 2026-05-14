"""DELETE /api/voices/{slug} replacement path — atomic voice swap on
project references before delete.

Exercises the happy path + edge cases (missing replacement, self-swap,
replacement not in library). Uses TestClient + the isolated_library
fixture so no real ~/audiomat library is touched.
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


def _make_voice(library_root: Path, slug: str, display_name: str) -> None:
    """Create a minimal voice entry: row in the voices table + voice.wav
    + voice.txt on disk where Voice.dir expects them. voice.wav is a
    zero-filled stub — we never run TTS in these tests."""
    vdir = library_root / "voices" / slug
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "voice.wav").write_bytes(b"\x00" * 256)
    (vdir / "voice.txt").write_text("any transcript", encoding="utf-8")
    from audiomat.db import get_conn
    get_conn().execute(
        "INSERT INTO voices (name_slug, name, duration_s, sample_rate, "
        "channels, transcript_chars, notes, created, tts_model) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (slug, display_name, 8.0, 24000, 1, 14, "", "2026-05-13T00:00:00Z", None),
    )


def _make_project(library_root: Path, slug: str, voice_name: str, voice_slug: str) -> None:
    """Create a minimal project that points at ``voice_slug`` via
    ``voice_ref`` — DB row + a stub book.epub on disk so book_path
    checks don't fall over."""
    pdir = library_root / "projects" / slug
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "stub.epub").write_bytes(b"")
    from audiomat.db import get_conn
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects "
        "(name_slug, name, book_filename, book_blocks_total, "
        " voice_ref, voice_ref_slug, "
        " status_phase, status_chapters_done, status_chapters_total, "
        " created, last_run, params_json) "
        "VALUES (?, ?, 'stub.epub', 1, ?, ?, 'draft', 0, 0, "
        "'2026-05-13T00:00:00Z', '', '{}')",
        (slug, slug, voice_name, voice_slug),
    )


def test_delete_unused_voice_succeeds(isolated_library):
    _make_voice(isolated_library, "alice", "Alice")
    c = _client(isolated_library)
    r = c.delete("/api/voices/alice")
    assert r.status_code == 200
    assert r.json()["deleted"] == "alice"
    assert r.json()["replaced_in"] == []
    assert not (isolated_library / "voices" / "alice").exists()


def test_delete_in_use_without_replacement_returns_structured_409(isolated_library):
    _make_voice(isolated_library, "alice", "Alice")
    _make_voice(isolated_library, "bob", "Bob")
    _make_project(isolated_library, "myproject", "Alice", "alice")
    c = _client(isolated_library)
    r = c.delete("/api/voices/alice")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert isinstance(detail, dict), f"expected structured detail, got {detail!r}"
    assert "message" in detail
    assert detail["referencing_projects"] == [{"slug": "myproject", "name": "myproject"}]
    # Voice still exists in DB — failed delete must not be partial.
    from audiomat.voice import Voice
    assert Voice.exists("alice")
    assert (isolated_library / "voices" / "alice" / "voice.wav").exists()


def test_delete_with_replacement_swaps_projects_and_removes_voice(isolated_library):
    _make_voice(isolated_library, "alice", "Alice")
    _make_voice(isolated_library, "bob", "Bob")
    _make_project(isolated_library, "myproject", "Alice", "alice")
    c = _client(isolated_library)
    r = c.delete("/api/voices/alice?replacement=bob")
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["deleted"] == "alice"
    assert body["replacement"] == "bob"
    assert body["replaced_in"] == ["myproject"]
    # Voice gone (FS dir + DB row), project's voice ref rewritten.
    assert not (isolated_library / "voices" / "alice").exists()
    from audiomat.project import Project
    proj = Project.load("myproject")
    assert proj.voice_ref == "Bob"
    assert proj.voice_ref_slug == "bob"


def test_delete_with_self_replacement_rejected(isolated_library):
    _make_voice(isolated_library, "alice", "Alice")
    _make_project(isolated_library, "p", "Alice", "alice")
    c = _client(isolated_library)
    r = c.delete("/api/voices/alice?replacement=alice")
    assert r.status_code == 400
    assert (isolated_library / "voices" / "alice").exists()


def test_delete_with_missing_replacement_returns_404(isolated_library):
    _make_voice(isolated_library, "alice", "Alice")
    _make_project(isolated_library, "p", "Alice", "alice")
    c = _client(isolated_library)
    r = c.delete("/api/voices/alice?replacement=nonexistent")
    assert r.status_code == 404
    assert "replacement voice not found" in r.json()["detail"]
    assert (isolated_library / "voices" / "alice").exists()


def test_delete_unused_voice_ignores_replacement_param(isolated_library):
    """An unused voice can be deleted regardless of whether the caller
    sent a stray replacement. The replacement is consulted only when
    referencing projects exist — otherwise it's a harmless no-op."""
    _make_voice(isolated_library, "alice", "Alice")
    _make_voice(isolated_library, "bob", "Bob")
    c = _client(isolated_library)
    r = c.delete("/api/voices/alice?replacement=bob")
    assert r.status_code == 200
    assert r.json()["deleted"] == "alice"
    assert r.json()["replacement"] is None
    assert r.json()["replaced_in"] == []
    # Replacement voice untouched.
    from audiomat.voice import Voice
    assert Voice.exists("bob")
    assert (isolated_library / "voices" / "bob" / "voice.wav").exists()
