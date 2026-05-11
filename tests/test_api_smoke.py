"""Smoke tests for the FastAPI app — routes that don't need a GPU.

Verifies the routers are wired correctly and the route-order gotcha
(literal paths before {slug} catchalls) hasn't regressed. Does NOT
exercise TTS generation — those would require the OmniVoice model
and a CUDA GPU.
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def _client(isolated_library):
    """Build a TestClient against a freshly-isolated library. The
    fixture reloads audiomat.state so PATHS picks up the env-var
    override; we then re-import audiomat.api so its routers see the
    new state module."""
    import importlib
    import audiomat.api
    importlib.reload(audiomat.api)
    return TestClient(audiomat.api.app)


def test_app_imports_and_registers_all_routes(isolated_library):
    """The full /api surface must register without errors."""
    c = _client(isolated_library)
    expected = {
        "/api/system/model-status",
        "/api/voices",
        "/api/voices/auto-transcribe",
        "/api/voices/draft-audio",
        "/api/voices/draft-upload",
        "/api/voices/{slug}",
        "/api/voices/{slug}/audio",
        "/api/projects",
        "/api/projects/{slug}",
        "/api/projects/{slug}/blocks-skipped",
        "/api/projects/{slug}/book",
        "/api/projects/{slug}/params",
        "/api/projects/{slug}/preview-matrix",
        "/api/projects/{slug}/preview-custom",
        "/api/projects/{slug}/preview-audio/{filename}",
        "/api/projects/{slug}/chapters",
        "/api/projects/{slug}/chapters/{stem}",
        "/api/projects/{slug}/chapter-audio/{stem}",
        "/api/projects/{slug}/render",
        "/api/projects/{slug}/cancel-render",
        "/api/projects/{slug}/progress",
        "/api/projects/{slug}/build-m4b",
        "/api/projects/{slug}/m4b",
    }
    have = {r.path for r in c.app.routes if hasattr(r, "methods")}
    missing = expected - have
    assert not missing, f"missing routes: {missing}"


def test_list_voices_empty(isolated_library):
    c = _client(isolated_library)
    r = c.get("/api/voices")
    assert r.status_code == 200
    assert r.json() == []


def test_list_projects_empty(isolated_library):
    c = _client(isolated_library)
    r = c.get("/api/projects")
    assert r.status_code == 200
    assert r.json() == []


def test_get_nonexistent_voice_404(isolated_library):
    c = _client(isolated_library)
    r = c.get("/api/voices/nonexistent_xyz")
    assert r.status_code == 404
    assert "voice not found" in r.json()["detail"]


def test_get_nonexistent_project_404(isolated_library):
    c = _client(isolated_library)
    r = c.get("/api/projects/nonexistent_xyz")
    assert r.status_code == 404


def test_draft_audio_route_not_shadowed_by_slug(isolated_library):
    """Critical regression test for CLAUDE.md gotcha: GET
    /api/voices/draft-audio must dispatch to the literal handler, NOT to
    GET /api/voices/{slug=draft-audio}. Symptom of regression: 404 with
    'voice not found: draft-audio' instead of 'draft audio not found'."""
    c = _client(isolated_library)
    r = c.get("/api/voices/draft-audio?path=/does/not/exist")
    assert r.status_code == 404
    assert "draft audio not found" in r.json()["detail"], (
        f"expected literal handler, got: {r.json()}"
    )


def test_model_status_unloaded_at_startup(isolated_library):
    """Without ever calling generate(), the singleton must remain
    unloaded — the status endpoint must NOT trigger a model load."""
    c = _client(isolated_library)
    r = c.get("/api/system/model-status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "unloaded"
    assert body["cache_target_bytes"] > 0


def test_render_404_when_no_active_job(isolated_library):
    c = _client(isolated_library)
    r = c.get("/api/projects/anything/progress")
    assert r.status_code == 404


def test_cancel_render_404_when_no_active_job(isolated_library):
    c = _client(isolated_library)
    r = c.post("/api/projects/anything/cancel-render")
    assert r.status_code == 404
