"""Unit tests for HiggsTTS adapter — interface parity with OmniVoiceTTS.

The real-model load is ~8 GB and ~10 s; we don't pay that cost in
pytest. These tests verify:

* The class has the same public surface as ``OmniVoiceTTS`` so the
  dispatcher in ``audiomat.state.get_tts_for_voice`` can swap
  backends without an isinstance branch.
* Defaults match what the multimodalart port expects.
* Lifecycle properties give the right answers before ``load`` runs.

End-to-end generate() validation lives in the lab —
``audiomat-lab/experiments/higgs_v3_2026-05-14/higgs_local_runner.py``
exercises the real model on real Czech audiobook prose.
"""
from __future__ import annotations

import inspect

from audiomat.tts import OmniVoiceTTS
from audiomat.tts_higgs import (
    DEFAULT_DEVICE,
    DEFAULT_DTYPE,
    DEFAULT_LANGUAGE,
    DEFAULT_MODEL_ID,
    HiggsTTS,
)


class TestDefaults:
    def test_model_id_targets_multimodalart_port(self):
        # Boson's own repo ships weights-only and isn't loadable through
        # AutoModelForCausalLM until the official Higgs Audio v3 launch
        # adds the architecture to upstream transformers. Until then we
        # must point at the multimodalart trust_remote_code packaging.
        assert DEFAULT_MODEL_ID == "multimodalart/higgs-audio-v3-tts-4b-transformers"

    def test_bfloat16_default_dtype(self):
        # The model card recommends bfloat16; fp16 underflows part of
        # the audio decoder activation range.
        assert DEFAULT_DTYPE == "bfloat16"

    def test_cuda_default_device(self):
        # The model is too big to run usefully on CPU; default to GPU.
        assert DEFAULT_DEVICE == "cuda:0"

    def test_default_language_matches_audiomat(self):
        assert DEFAULT_LANGUAGE == "cs"


class TestInterfaceParity:
    """HiggsTTS must expose the same public methods/properties as
    OmniVoiceTTS so ``state.get_tts_for_voice`` can hand either
    backend to the renderer without backend-specific branches."""

    PUBLIC = (
        # Lifecycle
        "load",
        "unload",
        "is_loaded",
        "is_loading",
        "sample_rate",
        "seconds_since_last_used",
        # Generation
        "generate",
        "generate_batch",
        # Diagnostics
        "vram_peak_gb",
        "reset_vram_stats",
    )

    def test_every_public_member_present(self):
        missing = [m for m in self.PUBLIC if not hasattr(HiggsTTS, m)]
        assert not missing, f"HiggsTTS missing: {missing}"

    def test_generate_signature_matches(self):
        higgs_sig = inspect.signature(HiggsTTS.generate)
        omni_sig = inspect.signature(OmniVoiceTTS.generate)
        # Parameter names must match positionally — renderer calls
        # tts.generate(text=..., voice=..., params=..., language=...).
        assert list(higgs_sig.parameters) == list(omni_sig.parameters), (
            f"signatures diverged:\n  Higgs: {higgs_sig}\n  Omni:  {omni_sig}"
        )

    def test_generate_batch_signature_matches(self):
        h = list(inspect.signature(HiggsTTS.generate_batch).parameters)
        o = list(inspect.signature(OmniVoiceTTS.generate_batch).parameters)
        assert h == o

    def test_constructor_keyword_args_match(self):
        # Both backends accept (model_id, device, dtype, model_revision)
        # so a registered Voice's tts_model -> revision pin path works
        # without conditional kwargs.
        h = list(inspect.signature(HiggsTTS.__init__).parameters)
        o = list(inspect.signature(OmniVoiceTTS.__init__).parameters)
        assert h == o


class TestLifecycleBeforeLoad:
    """Verify the pre-load state reads sensibly. These checks don't
    fire the load() call so they don't pull any weights."""

    def test_is_loaded_false_before_load(self):
        assert HiggsTTS().is_loaded is False

    def test_is_loading_false_before_load(self):
        assert HiggsTTS().is_loading is False

    def test_sample_rate_24khz_default(self):
        # Higgs Audio v3 native rate. The real load() refreshes this from
        # model.config; before load() we report the documented default
        # so progress UIs don't show "0 Hz" while the model warms.
        assert HiggsTTS().sample_rate == 24000

    def test_seconds_since_last_used_none_when_never_used(self):
        assert HiggsTTS().seconds_since_last_used() is None

    def test_unload_is_safe_when_never_loaded(self):
        # idle-unload background task may try to unload before anyone
        # used the backend; this must not raise.
        HiggsTTS().unload()


class TestConfigurability:
    def test_revision_pin_round_trip(self):
        tts = HiggsTTS(model_revision="abc123")
        assert tts.model_revision == "abc123"

    def test_default_revision_is_none(self):
        # Lab default — production should pin a hash. The license-flag
        # registry entry added in Phase 2 carries the pinned revision.
        assert HiggsTTS().model_revision is None


class TestVramHelpers:
    def test_vram_peak_returns_float_or_none(self):
        # We can't assume CUDA is present in CI, so just check it
        # doesn't crash and returns a sane type.
        v = HiggsTTS().vram_peak_gb()
        assert v is None or isinstance(v, float)

    def test_reset_vram_stats_is_safe_without_cuda(self):
        HiggsTTS().reset_vram_stats()
