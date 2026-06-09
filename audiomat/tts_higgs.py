"""HiggsTTS adapter — multimodalart/higgs-audio-v3-tts-4b-transformers.

Higgs Audio v3 TTS as a drop-in alternative to :class:`OmniVoiceTTS`,
loaded via the multimodalart transformers port of Boson's 4B model.
Same public surface (``load`` / ``unload`` / ``generate`` / lifecycle
properties) so ``audiomat/state.py``'s ``get_tts_for_voice`` can hand
the renderer either backend interchangeably.

Why the multimodalart port instead of Boson's own repo
(``bosonai/higgs-audio-v3-tts-4b``)? Boson's repo ships only the
weights — the modeling code (the ``higgs_multimodal_qwen3``
architecture) doesn't land in upstream ``transformers`` until the
official Higgs Audio v3 launch (2026-06-04 per the LMSYS blog). The
multimodalart fork bundles the modeling code via the standard
``trust_remote_code=True`` mechanism so plain ``AutoModelForCausalLM``
loads work today. Weights are unchanged — the port is a packaging
shim, not a fine-tune.

Differences vs OmniVoiceTTS to keep in mind:

* **bf16 default**, not fp16. The model card recommends bfloat16 and
  fp16 underflows the activation range on parts of the audio decoder.
* **No diffusion knobs.** ``params.num_step / guidance_scale / speed``
  are OmniVoice-specific. HiggsTTS accepts a ``RenderParams`` instance
  to match the OmniVoice signature but ignores those fields. The chunk
  manifest signature in ``audiomat.render`` still includes them, so
  changing them on a Higgs voice triggers unnecessary re-renders —
  acceptable cost for v0.4; an optional optimisation would be
  per-backend signature whitelists in render.py.
* **License: non-commercial.** Audiomat itself stays MIT; the model
  registry's ``license`` flag (v0.4) is what surfaces the obligation
  to the user when they assign this model to a voice.
* **VRAM ~8.6 GB at bf16**, vs ~2.2 GB for OmniVoice. On a 12 GB GPU
  the user should drop ``AUDIOMAT_MAX_LOADED_MODELS`` to 1 if they
  plan to use both backends.

Lab validation: ``audiomat-lab/experiments/higgs_v3_2026-05-14/``
holds the side-by-side A/B against OmniVoice on Czech audiobook prose
(Rezavý les, Jitka Ježková voice). User-confirmed quality win on the
"boxovacím pytlem" regression target with the ``Jitka_Jezkova_slow``
reference clip.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from audiomat.headers import prepare_for_tts
from audiomat.num2text import normalize_lang
from audiomat.tts import GenerationResult
from audiomat.tts_capabilities import HIGGS_CAPABILITIES, TTSCapabilities

if TYPE_CHECKING:
    from audiomat.project import RenderParams
    from audiomat.voice import Voice


# Pin defaults — change here, not per-call. Revision pinned to a known-
# good commit hash on the multimodalart fork so an upstream rewrite
# (community-maintained — author may force-push) doesn't break us.
# ``None`` means "follow main HEAD"; suitable for lab work, not prod.
DEFAULT_MODEL_ID = "multimodalart/higgs-audio-v3-tts-4b-transformers"
DEFAULT_REVISION: str | None = None      # TODO: pin once stable
DEFAULT_DEVICE = "cuda:0"
DEFAULT_DTYPE = "bfloat16"
DEFAULT_LANGUAGE = "cs"


class HiggsTTS:
    """Thread-unsafe (single GPU model handle). One instance per worker.

    Interface mirrors :class:`audiomat.tts.OmniVoiceTTS` so the
    dispatcher in ``audiomat.state.get_tts_for_voice`` can substitute
    one for the other without callers needing a runtime ``isinstance``
    check.

    Usage::

        tts = HiggsTTS()
        tts.load()                            # ~10 s, downloads ~8 GB on first run
        result = tts.generate("Ahoj.", voice, params, language="cs")
        sf.write("out.wav", result.audio, result.sample_rate)
    """

    # v0.5: engine self-description for UI + render validation. Higgs
    # declares zero params (no diffusion knobs) and zero preset variants
    # so UI surfaces auto-skip the Fine-tune dialog and preview matrix.
    capabilities: TTSCapabilities = HIGGS_CAPABILITIES

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
        # Pass through to AutoModelForCausalLM.from_pretrained's
        # ``revision`` param. ``None`` = follow main HEAD; production
        # should pin a commit hash for reproducibility.
        self.model_revision = model_revision

        self._model = None
        self._tokenizer = None
        self._loading = False
        # Higgs outputs 24 kHz mono — same as OmniVoice, no resample
        # needed in the concat path. We read this from the model config
        # after load() in case a future revision shifts it.
        self._sample_rate = 24000
        self._last_used: float | None = None

    # ---- lifecycle ----------------------------------------------------

    @property
    def is_loading(self) -> bool:
        """True while :meth:`load` is fetching weights / instantiating.
        Lets the system status endpoint distinguish "idle, never used"
        from "actively pulling 8 GB"."""
        return self._loading

    def load(self) -> None:
        """Pull weights + tokenizer from HF cache and move to GPU. Idempotent.

        First call against an empty HF cache downloads ~8 GB. Subsequent
        process-cold loads are ~10 s on a fast disk. Loaded model footprint
        is ~8.6 GB VRAM at bfloat16 — verified against an RTX 5070
        (12 GB) leaving ~3 GB headroom for KV cache during longer chunks.
        """
        if self._model is not None:
            self._last_used = time.monotonic()
            return
        self._loading = True
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer

            dtype_map = {
                "float16": torch.float16,
                "bfloat16": torch.bfloat16,
                "float32": torch.float32,
            }
            torch_dtype = dtype_map[self.dtype]

            tokenizer_kwargs: dict = {}
            model_kwargs: dict = {
                "trust_remote_code": True,
                "dtype": torch_dtype,
            }
            if self.model_revision is not None:
                tokenizer_kwargs["revision"] = self.model_revision
                model_kwargs["revision"] = self.model_revision

            self._tokenizer = AutoTokenizer.from_pretrained(
                self.model_id, **tokenizer_kwargs,
            )
            self._model = AutoModelForCausalLM.from_pretrained(
                self.model_id, **model_kwargs,
            ).to(self.device).eval()

            # Pull from config when present; the multimodalart port
            # exposes ``model.config.sample_rate`` — fall back to 24 kHz
            # which is the documented Higgs Audio v3 native rate.
            self._sample_rate = int(
                getattr(self._model.config, "sample_rate", 24000)
            )
            self._last_used = time.monotonic()
        finally:
            self._loading = False

    def unload(self) -> None:
        """Drop the model handle and free GPU memory. The idle-unload
        background task in ``audiomat.state`` calls this after the
        configured timeout to release VRAM for other processes."""
        self._model = None
        self._tokenizer = None
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
        call. ``time.monotonic`` so the value doesn't jump when the
        system clock changes. Mirrors OmniVoiceTTS so the idle-unload
        loop in api.py works without backend-specific code paths."""
        if self._last_used is None:
            return None
        return time.monotonic() - self._last_used

    # ---- generation --------------------------------------------------

    def generate(
        self,
        text: str,
        voice: "Voice",
        params: "RenderParams",
        language: str = DEFAULT_LANGUAGE,
    ) -> GenerationResult:
        """Synthesize one chunk.

        Reads the voice WAV from disk on each call (same as OmniVoiceTTS
        — 149 reads per Chapter-1 sized job is ~1 ms total, not worth
        caching). The ``params`` argument is accepted to match the
        dispatcher interface but its diffusion-specific fields are
        ignored: Higgs has no num_step / guidance_scale / speed knobs.
        """
        if not text or not text.strip():
            raise ValueError("empty text")
        if not voice.is_valid:
            raise ValueError(f"voice has missing files: {voice.dir}")
        # ``params`` intentionally unused — silences the linter without
        # losing the parameter from the signature.
        _ = params

        import torch

        self.load()
        lang = normalize_lang(language)
        clean = prepare_for_tts(text, lang=lang)
        ref_text = voice.transcript()
        ref_tensor, ref_sr = _load_ref_audio(voice.wav_path)

        t0 = time.time()
        with torch.inference_mode():
            wav_tensor = self._model.generate_speech(
                clean,
                self._tokenizer,
                reference_audio=ref_tensor.to(self.device),
                reference_sample_rate=ref_sr,
                reference_text=ref_text,
            )
        gen_s = time.time() - t0
        self._last_used = time.monotonic()

        # generate_speech returns (T,) mono for single-utterance calls.
        # Cast to float32 numpy for soundfile / concat compatibility.
        wav = wav_tensor.detach().to(torch.float32).cpu().numpy()
        if wav.ndim > 1:
            wav = wav.squeeze()
        dur = wav.shape[-1] / self._sample_rate
        return GenerationResult(
            audio=wav.astype(np.float32),
            sample_rate=self._sample_rate,
            duration_s=dur,
            gen_seconds=gen_s,
            rtf=(gen_s / dur if dur > 0 else float("nan")),
        )

    def generate_batch(
        self,
        texts: list[str],
        voice: "Voice",
        params: "RenderParams",
        language: str = DEFAULT_LANGUAGE,
    ) -> list[GenerationResult]:
        """Loop :meth:`generate`. Same rationale as OmniVoiceTTS — the
        renderer wants per-chunk progress callbacks, so explicit looping
        is cleaner than batched generation through the model."""
        return [self.generate(t, voice, params, language=language) for t in texts]

    # ---- diagnostics -------------------------------------------------

    def vram_peak_gb(self) -> float | None:
        """Peak VRAM allocated since last reset, in GB. None if CUDA
        isn't available. Used by the system status endpoint."""
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


def _load_ref_audio(wav_path):
    """Read a voice reference WAV as a (1, T) bf16-friendly tensor +
    its sample rate. Uses ``soundfile`` instead of ``torchaudio.load``
    because torchaudio's load path depends on torchcodec which fails on
    Windows when the path contains non-ASCII characters (e.g.
    ``C:\\Users\\Táta\\...``) — same root cause that pushed us to ONNX
    Silero in v0.2."""
    import soundfile as sf
    import torch

    data, sr = sf.read(str(wav_path), always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    data = data.astype(np.float32)
    tensor = torch.from_numpy(data).unsqueeze(0)
    return tensor, int(sr)


if __name__ == "__main__":
    # Smoke test — verifies imports + class structure only. Does not
    # load the real model (would pull ~8 GB). For real-model
    # verification use audiomat-lab/experiments/higgs_v3_2026-05-14/
    # higgs_local_runner.py.
    print(f"DEFAULT_MODEL_ID  = {DEFAULT_MODEL_ID}")
    print(f"DEFAULT_REVISION  = {DEFAULT_REVISION}")
    print(f"DEFAULT_DEVICE    = {DEFAULT_DEVICE}")
    print(f"DEFAULT_DTYPE     = {DEFAULT_DTYPE}")
    print(f"DEFAULT_LANGUAGE  = {DEFAULT_LANGUAGE}")
    tts = HiggsTTS()
    print(f"\ninstance: model_id={tts.model_id}, device={tts.device}, dtype={tts.dtype}")
    print(f"          revision={tts.model_revision}")
    print(f"is_loaded={tts.is_loaded}, sample_rate={tts.sample_rate}")
    print(f"is_loading={tts.is_loading}, seconds_since_last_used={tts.seconds_since_last_used()}")
    print("\n(real-model load deferred — would pull ~8 GB from HF)")
