"""OmniVoice TTS wrapper.

Single class :class:`OmniVoiceTTS` that owns the model handle and exposes
:meth:`generate` for single chunks. The heavy imports (torch, omnivoice)
happen lazily on first :meth:`load` call so other modules (api.py / cli)
can import this file cheaply.

Production defaults from CLAUDE.md Stage 3:

* model: ``k2-fsa/OmniVoice`` (Apache 2.0, public HF)
* device: ``cuda:0``
* dtype: float16
* num_step: 48
* guidance_scale: 2.0
* speed: 1.0

Reference voice constraints (enforced at load-voice time, see voice.py):

* 24 kHz mono 16-bit, 5–10 s recommended.
* ref_text MUST match ref_audio content (mismatch → output sped-up to match
  the wrong chars/sec ratio = unintelligible).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from audiomat.headers import prepare_for_tts
from audiomat.project import RenderParams
from audiomat.voice import Voice


DEFAULT_MODEL_ID = "k2-fsa/OmniVoice"
DEFAULT_LANGUAGE = "cs"
DEFAULT_DEVICE = "cuda:0"
DEFAULT_DTYPE = "float16"


@dataclass
class GenerationResult:
    """One chunk's worth of audio + the timing metadata the renderer
    needs for progress reporting."""
    audio: np.ndarray            # 1-D, dtype float32
    sample_rate: int
    duration_s: float            # = audio.shape[-1] / sample_rate
    gen_seconds: float           # wall-clock time to generate
    rtf: float                   # gen_seconds / duration_s


class OmniVoiceTTS:
    """Thread-unsafe (single GPU model handle). One instance per worker.

    Usage::

        tts = OmniVoiceTTS()
        tts.load()                            # ~5 s if HF cache warm
        result = tts.generate("Ahoj.", voice, params, language="cs")
        sf.write("out.wav", result.audio, result.sample_rate)
    """

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL_ID,
        device: str = DEFAULT_DEVICE,
        dtype: str = DEFAULT_DTYPE,
    ):
        self.model_id = model_id
        self.device = device
        self.dtype = dtype
        self._model = None
        self._sample_rate = 24000

    # -- lifecycle --

    def load(self) -> None:
        """Pull weights from HF cache and instantiate the model. Idempotent."""
        if self._model is not None:
            return
        import torch
        from omnivoice import OmniVoice

        dtype_map = {
            "float16": torch.float16,
            "bfloat16": torch.bfloat16,
            "float32": torch.float32,
        }
        torch_dtype = dtype_map[self.dtype]
        self._model = OmniVoice.from_pretrained(
            self.model_id,
            device_map=self.device,
            dtype=torch_dtype,
        )
        self._sample_rate = int(getattr(self._model, "sampling_rate", 24000))

    def unload(self) -> None:
        """Drop the model handle and free GPU memory. Idle API workers can
        call this after N seconds of inactivity to release VRAM for other
        processes."""
        self._model = None
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    # -- generation --

    def generate(
        self,
        text: str,
        voice: Voice,
        params: RenderParams,
        language: str = DEFAULT_LANGUAGE,
    ) -> GenerationResult:
        """Synthesize one chunk. Strips fish-speech markers from ``text``
        before generation. Reads voice transcript from disk on each call
        — lightweight enough that caching is not worth the bookkeeping
        (a chapter with 149 chunks reads voice.txt 149 times = ~1 ms).
        """
        import time

        if not text or not text.strip():
            raise ValueError("empty text")
        if not voice.is_valid:
            raise ValueError(f"voice has missing files: {voice.dir}")

        self.load()
        clean = prepare_for_tts(text, lang=language)
        ref_text = voice.transcript()

        t0 = time.time()
        audios = self._model.generate(
            text=clean,
            language=language,
            ref_text=ref_text,
            ref_audio=str(voice.wav_path),
            num_step=params.num_step,
            guidance_scale=params.guidance_scale,
            speed=params.speed,
        )
        gen_s = time.time() - t0

        wav = audios[0].astype(np.float32)
        dur = wav.shape[-1] / self._sample_rate
        return GenerationResult(
            audio=wav,
            sample_rate=self._sample_rate,
            duration_s=dur,
            gen_seconds=gen_s,
            rtf=(gen_s / dur if dur > 0 else float("nan")),
        )

    def generate_batch(
        self,
        texts: list[str],
        voice: Voice,
        params: RenderParams,
        language: str = DEFAULT_LANGUAGE,
    ) -> list[GenerationResult]:
        """Convenience wrapper that loops :meth:`generate`. OmniVoice
        natively supports ``text=list[str]`` for batch but our use case
        renders chapter-by-chapter and wants per-chunk progress callbacks,
        so explicit looping is cleaner than batched generation."""
        return [self.generate(t, voice, params, language=language) for t in texts]

    def vram_peak_gb(self) -> float | None:
        """Return peak VRAM allocated since last reset, in GB. None if
        CUDA isn't available."""
        try:
            import torch
            if not torch.cuda.is_available():
                return None
            return torch.cuda.max_memory_allocated() / (1024 ** 3)
        except ImportError:
            return None

    def reset_vram_stats(self) -> None:
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.reset_peak_memory_stats()
        except ImportError:
            pass


if __name__ == "__main__":
    # Smoke test — only verifies imports + class structure. Doesn't load
    # the actual model (would download 3 GB). For real-model verification
    # use scripts/tts_test_omnivoice.py in skleneny-muz-tts/.
    print(f"DEFAULT_MODEL_ID  = {DEFAULT_MODEL_ID}")
    print(f"DEFAULT_DEVICE    = {DEFAULT_DEVICE}")
    print(f"DEFAULT_DTYPE     = {DEFAULT_DTYPE}")
    print(f"DEFAULT_LANGUAGE  = {DEFAULT_LANGUAGE}")
    tts = OmniVoiceTTS()
    print(f"\ninstance: model_id={tts.model_id}, device={tts.device}, dtype={tts.dtype}")
    print(f"is_loaded={tts.is_loaded}, sample_rate={tts.sample_rate}")
    print("\n(real-model load deferred — would pull 3 GB from HF)")
