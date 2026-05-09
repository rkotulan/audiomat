"""Faster-whisper transcription for voice references.

When a user uploads a voice WAV without a transcript, we auto-generate one
via faster-whisper (CTranslate2 backend, CPU). The resulting draft is sent
to the UI for the user to review before saving — Whisper-medium gets some
Czech words wrong (Egipský / zrsky / přez), so a human pass is recommended
but not required.

Heavy import (faster-whisper) is deferred to first :func:`transcribe` call
so api.py imports cheaply.
"""
from __future__ import annotations

from pathlib import Path


_MODEL = None
_MODEL_NAME = ""


def transcribe(
    wav_path: Path | str,
    language: str = "cs",
    model_name: str = "medium",
    beam_size: int = 5,
) -> str:
    """Transcribe a voice reference WAV via faster-whisper.

    Args:
        wav_path: input audio (any format ffmpeg supports, but we feed it
            converted WAVs).
        language: ISO 639-1 code. Czech (``"cs"``) is what we tested.
        model_name: ``tiny`` / ``base`` / ``small`` / ``medium`` /
            ``large-v3``. Default ``medium`` matches the production
            workflow (large-v3 is more accurate but ~3 GB and slower).
        beam_size: beam search width (default 5).

    Returns:
        Concatenated transcript with single-space joins between segments.
        Trimmed of leading / trailing whitespace.
    """
    global _MODEL, _MODEL_NAME
    from faster_whisper import WhisperModel

    if _MODEL is None or _MODEL_NAME != model_name:
        _MODEL = WhisperModel(model_name, device="cpu", compute_type="int8")
        _MODEL_NAME = model_name

    segments, _info = _MODEL.transcribe(
        str(wav_path),
        language=language,
        beam_size=beam_size,
        vad_filter=False,
        word_timestamps=False,
    )
    return " ".join(seg.text.strip() for seg in segments).strip()


def unload() -> None:
    """Release the loaded Whisper model. Useful when the API server is
    idle for a while and wants to free RAM."""
    global _MODEL, _MODEL_NAME
    _MODEL = None
    _MODEL_NAME = ""


if __name__ == "__main__":
    # Smoke test — verify imports work but skip the actual model load
    # (would download ~1.5 GB).
    print("transcribe module imports OK; faster-whisper deferred until first call")
