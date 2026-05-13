"""Round-trip tests for the voice-validation-text setting."""
from __future__ import annotations

from fastapi.testclient import TestClient


def _client(isolated_library):
    import importlib
    import audiomat.api
    importlib.reload(audiomat.api)
    return TestClient(audiomat.api.app)


def test_get_returns_default_when_unset(isolated_library):
    c = _client(isolated_library)
    r = c.get("/api/settings/voice-validation-text")
    assert r.status_code == 200
    body = r.json()
    assert body["is_default"] is True
    assert "Bylo už 10 minut" in body["text"]   # canonical Czech default


def test_put_persists_and_get_returns_override(isolated_library):
    c = _client(isolated_library)
    custom = "The quick brown fox jumps over the lazy dog. Numbers: 42, 1999."
    r = c.put("/api/settings/voice-validation-text", json={"text": custom})
    assert r.status_code == 200
    assert r.json()["text"] == custom
    assert r.json()["is_default"] is False

    r2 = c.get("/api/settings/voice-validation-text")
    assert r2.status_code == 200
    assert r2.json()["text"] == custom
    assert r2.json()["is_default"] is False


def test_put_rejects_empty_text(isolated_library):
    """Empty input shouldn't blank out the user's preference — to clear,
    use DELETE. Spec contract documented in settings_store."""
    c = _client(isolated_library)
    r = c.put("/api/settings/voice-validation-text", json={"text": "   "})
    assert r.status_code == 400


def test_put_rejects_oversized_text(isolated_library):
    """Cap at 1000 chars to bound TTS render time downstream."""
    c = _client(isolated_library)
    r = c.put("/api/settings/voice-validation-text", json={"text": "x" * 1500})
    assert r.status_code == 400


def test_delete_resets_to_default(isolated_library):
    c = _client(isolated_library)
    c.put("/api/settings/voice-validation-text", json={"text": "Custom override"})
    r = c.delete("/api/settings/voice-validation-text")
    assert r.status_code == 200
    assert r.json()["is_default"] is True
    assert "Bylo už 10 minut" in r.json()["text"]


def test_delete_idempotent_when_never_set(isolated_library):
    c = _client(isolated_library)
    r = c.delete("/api/settings/voice-validation-text")
    assert r.status_code == 200
    assert r.json()["is_default"] is True
