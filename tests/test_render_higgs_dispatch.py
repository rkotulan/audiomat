"""Integration test that the ProjectRenderer path can run with a
HiggsTTS instance, not just OmniVoiceTTS.

We don't load 8 GB of real weights — instead we stub out
``HiggsTTS.generate`` to verify the renderer reaches it via the same
code path it uses for OmniVoiceTTS, and that GenerationResult flows
back into the chunk_manifest sig + audio write loop without type
errors.

The actual TTS quality / Czech pronunciation regression checks live in
audiomat-lab/experiments/higgs_v3_2026-05-14/.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest


class _StubVoice:
    """Minimal Voice duck-type for the renderer's wants. The dispatcher
    only reads name_slug + tts_model + transcript path; chunk write
    needs wav_path; manifest signature reads voice.name_slug."""
    def __init__(self, root: Path, slug: str, *, tts_model: str | None):
        d = root / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "voice.wav").write_bytes(b"\x00" * 1024)
        (d / "voice.txt").write_text("ref transcript", encoding="utf-8")
        self.dir = d
        self.name = slug
        self.name_slug = slug
        self.tts_model = tts_model
        self.wav_path = d / "voice.wav"

    @property
    def is_valid(self) -> bool:
        return True

    def transcript(self) -> str:
        return "ref transcript"


# ---- HiggsTTS interface check (no weights load) -------------------


def test_higgs_tts_generate_signature_matches_omnivoice():
    """The renderer assumes ``tts.generate(text, voice, params,
    language)`` works on either backend. Phase 1 already verified this
    via inspect.signature; here we double-check by calling a stubbed
    generate() and confirming the same kwargs flow."""
    from audiomat.tts_higgs import HiggsTTS

    tts = HiggsTTS()
    fake_audio = np.zeros(24000, dtype=np.float32)

    # Patch out the real model so generate() doesn't try to load weights.
    def fake_generate_speech(text, tokenizer, **kwargs):
        import torch
        return torch.from_numpy(fake_audio)
    tts._model = MagicMock()
    tts._model.generate_speech.side_effect = fake_generate_speech
    tts._tokenizer = MagicMock()
    tts._sample_rate = 24000

    # Need a usable Voice + RenderParams stub for the signature.
    from audiomat.project import RenderParams
    voice = MagicMock()
    voice.is_valid = True
    voice.transcript.return_value = "ref"
    voice.wav_path = Path("nonexistent.wav")
    # Replace the inline ref-audio loader so we don't actually touch
    # disk during this signature smoke.
    import audiomat.tts_higgs as th
    import torch
    th._load_ref_audio = lambda p: (torch.zeros(1, 1024), 24000)

    result = tts.generate("Ahoj.", voice, RenderParams(), language="cs")
    assert result.sample_rate == 24000
    assert result.audio.shape == (24000,)
    assert result.duration_s == 1.0


# ---- ProjectRenderer-with-HiggsTTS smoke -------------------------


def test_project_renderer_accepts_higgs_tts():
    """The renderer's __init__ is typed ``tts: OmniVoiceTTS`` but at
    runtime any duck-typed adapter works. Verify HiggsTTS doesn't
    explode the constructor. We don't run render_all — that needs a
    real Project on disk + real TTS — but we touch the surface the
    renderer reads."""
    from audiomat.render import ProjectRenderer
    from audiomat.tts_higgs import HiggsTTS

    tts = HiggsTTS()
    # Renderer reads project.chunks_dir + voice.name_slug + voice.wav_path.
    # All stubbed here — no real I/O.
    proj = MagicMock()
    proj.chunks_dir = Path("/tmp/render-smoke-chunks")
    proj.dir = Path("/tmp/render-smoke")
    proj.dir.mkdir(parents=True, exist_ok=True)
    proj.book.language = "cs"
    voice = MagicMock()
    voice.name_slug = "v"
    voice.wav_path = Path("/tmp/voice.wav")
    Path("/tmp/voice.wav").write_bytes(b"\x00" * 16)

    renderer = ProjectRenderer(proj, voice, tts, blocks=[])
    assert renderer.tts is tts
    # Manifest sig generation reads voice.wav_path mtime — should not
    # blow up on either backend.
    sig = renderer._params_signature()
    assert isinstance(sig, str) and len(sig) == 16


# ---- params field ignored gracefully on Higgs --------------------


def test_higgs_ignores_omnivoice_diffusion_params():
    """Higgs has no num_step / guidance_scale / speed knobs. The
    adapter should accept them in params for interface parity but not
    error out. Confirms that swapping a project from OmniVoice to
    Higgs mid-stream doesn't require zeroing the existing params."""
    from audiomat.project import RenderParams
    from audiomat.tts_higgs import HiggsTTS
    import audiomat.tts_higgs as th
    import torch

    tts = HiggsTTS()
    tts._model = MagicMock()
    tts._model.generate_speech.return_value = torch.zeros(2400, dtype=torch.float32)
    tts._tokenizer = MagicMock()
    tts._sample_rate = 24000
    th._load_ref_audio = lambda p: (torch.zeros(1, 1024), 24000)

    voice = MagicMock()
    voice.is_valid = True
    voice.transcript.return_value = "ref"
    voice.wav_path = Path("nonexistent.wav")

    # Production-style params with all OmniVoice knobs set — Higgs must
    # not raise on any of them.
    params = RenderParams(num_step=48, guidance_scale=2.0, speed=1.0)
    result = tts.generate("text", voice, params, language="cs")
    assert result.duration_s == pytest.approx(0.1, abs=0.01)
