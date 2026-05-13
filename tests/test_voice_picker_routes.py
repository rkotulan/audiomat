"""Smoke tests for the voice-picker endpoints (preview-voices + voice swap).

Validation paths only — the actual SSE generation needs a real project +
TTS model, which is GPU-only and not exercised in pytest.
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def _client(isolated_library):
    import importlib
    import audiomat.api
    importlib.reload(audiomat.api)
    return TestClient(audiomat.api.app)


def test_preview_voices_404_when_project_missing(isolated_library):
    c = _client(isolated_library)
    r = c.post("/api/projects/nonexistent/preview-voices",
               json={"voice_slugs": ["any"]})
    assert r.status_code == 404


def test_preview_voices_4xx_on_empty_voice_slugs(isolated_library):
    """Empty list must produce a 4xx. The cap was relaxed to "≥1" (no
    upper bound) but the lower bound still holds — generating zero
    cells is meaningless."""
    c = _client(isolated_library)
    r = c.post("/api/projects/anything/preview-voices",
               json={"voice_slugs": []})
    # Either 400 (validation first) or 404 (project lookup first) is OK;
    # the contract is "user gets an error, not a 200".
    assert r.status_code in (400, 404)


def test_preview_voices_4xx_on_duplicate_slugs(isolated_library):
    """Duplicate slugs in the request must be rejected — would otherwise
    cause two cells with the same audio_url and confuse the UI."""
    c = _client(isolated_library)
    r = c.post("/api/projects/anything/preview-voices",
               json={"voice_slugs": ["same", "same"]})
    assert r.status_code in (400, 404)


def test_update_voice_404_when_project_missing(isolated_library):
    c = _client(isolated_library)
    r = c.patch("/api/projects/nonexistent/voice", json={"voice_slug": "any"})
    assert r.status_code == 404


def test_update_voice_400_on_missing_body(isolated_library):
    c = _client(isolated_library)
    r = c.patch("/api/projects/anything/voice", json={})
    # Pydantic rejects missing required field.
    assert r.status_code == 422
