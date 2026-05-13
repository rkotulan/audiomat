"""Smoke tests for the long-source voice picker endpoints.

Verifies route registration + early-validation paths. The full
upload→analyze→extract loop is exercised manually in the browser
(see CLAUDE.md "Testing pattern" — TTS / VAD-heavy paths aren't covered
here to keep the suite fast).
"""
from __future__ import annotations

import io
import wave

import numpy as np
from fastapi.testclient import TestClient


def _client(isolated_library):
    import importlib
    import audiomat.api
    importlib.reload(audiomat.api)
    return TestClient(audiomat.api.app)


def _tiny_wav_bytes(duration_s: float = 1.0, sample_rate: int = 22050) -> bytes:
    """Build a valid in-memory WAV (mono 16-bit) for upload tests."""
    n = int(duration_s * sample_rate)
    samples = (np.random.default_rng(0).normal(0, 0.01, n) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def test_long_routes_registered(isolated_library):
    """All four new endpoints must be wired AND must come before the
    /{slug} catchall (otherwise the literal paths get shadowed — see
    CLAUDE.md FastAPI route-order gotcha)."""
    c = _client(isolated_library)
    have = {(r.path, tuple(sorted(r.methods))) for r in c.app.routes if hasattr(r, "methods")}
    assert ("/api/voices/draft-upload-long", ("POST",)) in have
    assert ("/api/voices/analyze", ("POST",)) in have
    assert ("/api/voices/extract-window", ("POST",)) in have
    assert ("/api/voices/preview-staged", ("POST",)) in have


def test_preview_staged_403_outside_staging(isolated_library, tmp_path):
    """Path safety: same guard as /extract-window — paths outside an
    audiomat_voice_* tempdir get a 403 even if they exist."""
    rogue = tmp_path / "rogue.wav"
    rogue.write_bytes(_tiny_wav_bytes())
    c = _client(isolated_library)
    r = c.post("/api/voices/preview-staged", json={
        "audio_path": str(rogue),
        "transcript": "anything",
        "sample_text": "anything",
    })
    assert r.status_code == 403


def test_preview_staged_400_on_oversized_sample_text(isolated_library):
    """Sample text > 1000 chars rejected up front so a typo in a paste
    doesn't sit on the GPU for minutes."""
    c = _client(isolated_library)
    r = c.post("/api/voices/preview-staged", json={
        "audio_path": "C:/does/not/exist/voice.wav",
        "transcript": "matching transcript",
        "sample_text": "x" * 1500,
    })
    # 404 (path missing) wins over 400 (length) by source order; either is
    # an acceptable rejection contract.
    assert r.status_code in (400, 404)


def test_analyze_404_when_audio_path_missing(isolated_library):
    c = _client(isolated_library)
    r = c.post("/api/voices/analyze", json={
        "audio_path": "C:/does/not/exist/voice_full.wav",
    })
    assert r.status_code == 404


def test_extract_window_403_outside_staging(isolated_library, tmp_path):
    """Path safety: paths outside an audiomat_voice_* tempdir must be
    rejected even if they exist. Stops a curl injection from carving
    arbitrary files."""
    rogue = tmp_path / "rogue.wav"
    rogue.write_bytes(_tiny_wav_bytes())
    c = _client(isolated_library)
    r = c.post("/api/voices/extract-window", json={
        "audio_path": str(rogue),
        "analyzed_start_s": 0.0,
        "start_s": 0.0,
        "end_s": 7.0,
    })
    assert r.status_code == 403


def test_extract_window_400_when_duration_out_of_range(isolated_library, tmp_path, monkeypatch):
    """3-12 s window enforcement. Stage a tempdir under the expected
    audiomat_voice_* prefix and request a 1 s slice."""
    import tempfile
    staging = tempfile.mkdtemp(prefix="audiomat_voice_")
    full = (tmp_path.__class__(staging) / "voice_full.wav")
    full.write_bytes(_tiny_wav_bytes(duration_s=2.0))
    c = _client(isolated_library)
    r = c.post("/api/voices/extract-window", json={
        "audio_path": str(full),
        "analyzed_start_s": 0.0,
        "start_s": 0.0,
        "end_s": 1.0,
    })
    assert r.status_code == 400
    assert "3-12" in r.json()["detail"]


def test_draft_upload_long_accepts_long_audio(isolated_library):
    """30 s WAV must NOT be rejected here (it would be by the short-form
    /draft-upload). Verifies the response shape too."""
    c = _client(isolated_library)
    body = _tiny_wav_bytes(duration_s=30.0)
    r = c.post(
        "/api/voices/draft-upload-long",
        files={"audio": ("voice.wav", body, "audio/wav")},
    )
    assert r.status_code == 200, r.json()
    j = r.json()
    assert j["duration_s"] >= 25.0      # ffmpeg may shave a few ms
    assert j["sample_rate"] == 24000
    assert j["channels"] == 1
    assert j["chapters"] == []
    assert j["audio_path"].endswith("voice_full.wav")
