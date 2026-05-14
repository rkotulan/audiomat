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
    ``voice_ref``. We only need config.json — no book / chunks needed
    for delete-replacement tests."""
    pdir = library_root / "projects" / slug
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "config.json").write_text(json.dumps({
        "name": slug,
        "name_slug": slug,
        "book": {
            "filename": "stub.epub",
            "blocks_total": 1,
            "blocks_skipped": [],
            "title": None, "author": None, "language": "cs",
        },
        "voice_ref": voice_name,
        "voice_ref_slug": voice_slug,
        "params": {
            "num_step": 48, "guidance_scale": 2.0, "speed": 1.0,
            "min_chars": 90, "max_chars": 200, "target_lufs": -16.0,
            "silence_gap_ms": 200, "section_headers": [],
        },
        "status": {
            "chapters_done": 0, "chapters_total": 0,
            "last_completed": None, "phase": "draft",
        },
        "created": "2026-05-13T00:00:00Z",
        "last_run": "",
    }, ensure_ascii=False), encoding="utf-8")
    # book file referenced by config has to exist for Project.list_all
    # not to choke on the load — empty stub is fine.
    (pdir / "stub.epub").write_bytes(b"")


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
    # Voice file gone, project rewritten.
    assert not (isolated_library / "voices" / "alice").exists()
    cfg = json.loads(
        (isolated_library / "projects" / "myproject" / "config.json")
        .read_text(encoding="utf-8")
    )
    assert cfg["voice_ref"] == "Bob"
    assert cfg["voice_ref_slug"] == "bob"


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
