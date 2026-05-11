"""OmniVoice TTS wrapper.

Single class :class:`OmniVoiceTTS` that owns the model handle and exposes
:meth:`generate` for single chunks. The heavy imports (torch, omnivoice)
happen lazily on first :meth:`load` call so other modules (api.py / cli)
can import this file cheaply.

Production defaults from CLAUDE.md Stage 3:

* model: ``k2-fsa/OmniVoice`` (Apache 2.0, public HF)
* revision: pinned to a known-good commit SHA (see ``DEFAULT_REVISION``);
  protects against silent upstream weight changes between runs and gives
  reproducible builds. Bump deliberately after end-to-end testing.
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

import time
from dataclasses import dataclass

import numpy as np

from audiomat.headers import prepare_for_tts
from audiomat.num2text import normalize_lang
from audiomat.project import RenderParams
from audiomat.voice import Voice


DEFAULT_MODEL_ID = "k2-fsa/OmniVoice"
# Pinned HF revision so a silent upstream weight change can't shift
# voice quality / cache invariance between runs. Bump deliberately
# after testing a new snapshot end-to-end on a real CZ render. Override
# at runtime via OmniVoiceTTS(model_revision=...).
#
# Snapshot date: 2026-05-07 (k2-fsa/OmniVoice main HEAD at the time of
# this pin). https://huggingface.co/k2-fsa/OmniVoice/commits/main
DEFAULT_REVISION = "999c332499c708b116876ff5fe1aa5dd15f422ce"
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
        model_revision: str | None = DEFAULT_REVISION,
    ):
        self.model_id = model_id
        self.device = device
        self.dtype = dtype
        # Pass through to OmniVoice.from_pretrained's `revision` param.
        # None = follow main HEAD (not recommended for prod; reproducibility
        # suffers). Default = the pinned snapshot.
        self.model_revision = model_revision
        self._model = None
        self._loading = False     # True while load() is in progress
        self._sample_rate = 24000
        # Wall-clock timestamp of the last load() or generate() call.
        # The idle-unload background task uses this to decide when to
        # release VRAM. None = never used since process start.
        self._last_used: float | None = None

    # -- lifecycle --

    @property
    def is_loading(self) -> bool:
        """True while :meth:`load` is fetching weights / instantiating.
        Distinct from :attr:`is_loaded` so the system status endpoint can
        tell 'idle, never used' apart from 'actively pulling 3 GB'."""
        return self._loading

    def load(self) -> None:
        """Pull weights from HF cache and instantiate the model. Idempotent."""
        if self._model is not None:
            self._last_used = time.monotonic()
            return
        self._loading = True
        try:
            import torch
            from omnivoice import OmniVoice

            dtype_map = {
                "float16": torch.float16,
                "bfloat16": torch.bfloat16,
                "float32": torch.float32,
            }
            torch_dtype = dtype_map[self.dtype]
            from_pretrained_kwargs: dict = {
                "device_map": self.device,
                "dtype": torch_dtype,
            }
            if self.model_revision is not None:
                from_pretrained_kwargs["revision"] = self.model_revision
            self._model = OmniVoice.from_pretrained(
                self.model_id,
                **from_pretrained_kwargs,
            )
            self._sample_rate = int(getattr(self._model, "sampling_rate", 24000))
            self._last_used = time.monotonic()
        finally:
            self._loading = False

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

    def seconds_since_last_used(self) -> float | None:
        """Wall-clock seconds since the most recent load() / generate()
        call, or None if the model has never been used. Driven by
        time.monotonic so it doesn't jump on system clock changes.
        Used by the idle-unload background task in api.py."""
        if self._last_used is None:
            return None
        return time.monotonic() - self._last_used

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
        # OmniVoice and num2words both want ISO 639-1 ("cs"); EPUB metadata
        # routinely supplies BCP 47 ("cs-CZ") which OmniVoice silently falls
        # back to language-agnostic mode on. Normalize at the entrance.
        lang = normalize_lang(language)
        clean = prepare_for_tts(text, lang=lang)
        ref_text = voice.transcript()

        t0 = time.time()
        audios = self._model.generate(
            text=clean,
            language=lang,
            ref_text=ref_text,
            ref_audio=str(voice.wav_path),
            num_step=params.num_step,
            guidance_scale=params.guidance_scale,
            speed=params.speed,
        )
        gen_s = time.time() - t0
        self._last_used = time.monotonic()

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
    print(f"DEFAULT_REVISION  = {DEFAULT_REVISION}")
    print(f"DEFAULT_DEVICE    = {DEFAULT_DEVICE}")
    print(f"DEFAULT_DTYPE     = {DEFAULT_DTYPE}")
    print(f"DEFAULT_LANGUAGE  = {DEFAULT_LANGUAGE}")
    tts = OmniVoiceTTS()
    print(f"\ninstance: model_id={tts.model_id}, device={tts.device}, dtype={tts.dtype}")
    print(f"          revision={tts.model_revision}")
    print(f"is_loaded={tts.is_loaded}, sample_rate={tts.sample_rate}")
    print("\n(real-model load deferred — would pull 3 GB from HF)")
