"""Tests for the v0.5 TTSCapabilities descriptor + adapter declarations.

The descriptor is plain dataclasses + module-level constants — no IO,
no model loads — so these tests run in the default pytest path without
the ``isolated_library`` fixture.
"""
from __future__ import annotations

import dataclasses as dc
import json

import pytest

from audiomat.tts_capabilities import (
    HIGGS_CAPABILITIES,
    OMNIVOICE_CAPABILITIES,
    OMNIVOICE_PARAM_GUIDANCE_SCALE,
    OMNIVOICE_PARAM_NUM_STEP,
    OMNIVOICE_PARAM_SPEED,
    OMNIVOICE_PRESETS,
    ParamSpec,
    PresetVariant,
    TTSCapabilities,
)


# ---- ParamSpec format + coerce -----------------------------------------


class TestParamSpecFormat:
    def test_int_renders_without_decimals(self):
        assert OMNIVOICE_PARAM_NUM_STEP.format(48) == "48"
        assert OMNIVOICE_PARAM_NUM_STEP.format(32.0) == "32"

    def test_float_renders_with_declared_decimals(self):
        assert OMNIVOICE_PARAM_GUIDANCE_SCALE.format(2.0) == "2.0"
        assert OMNIVOICE_PARAM_GUIDANCE_SCALE.format(2.25) == "2.2"

    def test_suffix_appended(self):
        assert OMNIVOICE_PARAM_SPEED.format(1.0) == "1.00×"
        assert OMNIVOICE_PARAM_SPEED.format(0.85) == "0.85×"


class TestParamSpecCoerce:
    def test_int_coerce_rounds(self):
        assert OMNIVOICE_PARAM_NUM_STEP.coerce(31.6) == 32
        assert OMNIVOICE_PARAM_NUM_STEP.coerce("48") == 48

    def test_float_coerce_preserves(self):
        assert OMNIVOICE_PARAM_GUIDANCE_SCALE.coerce(2.5) == 2.5
        assert OMNIVOICE_PARAM_GUIDANCE_SCALE.coerce("3.0") == 3.0


# ---- TTSCapabilities helpers -------------------------------------------


class TestCapabilitiesHelpers:
    def test_default_params_returns_all_spec_defaults(self):
        defaults = OMNIVOICE_CAPABILITIES.default_params()
        assert defaults == {"num_step": 48, "guidance_scale": 2.0, "speed": 1.0}

    def test_default_params_empty_when_no_params(self):
        assert HIGGS_CAPABILITIES.default_params() == {}

    def test_has_tunable_params(self):
        assert OMNIVOICE_CAPABILITIES.has_tunable_params is True
        assert HIGGS_CAPABILITIES.has_tunable_params is False

    def test_has_preset_matrix_requires_both_params_and_variants(self):
        assert OMNIVOICE_CAPABILITIES.has_preset_matrix is True
        assert HIGGS_CAPABILITIES.has_preset_matrix is False

    def test_find_param_hit_and_miss(self):
        spec = OMNIVOICE_CAPABILITIES.find_param("num_step")
        assert spec is OMNIVOICE_PARAM_NUM_STEP
        assert OMNIVOICE_CAPABILITIES.find_param("doesntexist") is None


# ---- TTSCapabilities.validate_params -----------------------------------


class TestValidateParams:
    def test_valid_input_round_trips_and_coerces_types(self):
        out = OMNIVOICE_CAPABILITIES.validate_params(
            {"num_step": "32", "guidance_scale": 2.5, "speed": 1.0}
        )
        assert out == {"num_step": 32, "guidance_scale": 2.5, "speed": 1.0}
        assert isinstance(out["num_step"], int)
        assert isinstance(out["guidance_scale"], float)

    def test_missing_keys_filled_from_defaults(self):
        out = OMNIVOICE_CAPABILITIES.validate_params({"num_step": 32})
        assert out == {"num_step": 32, "guidance_scale": 2.0, "speed": 1.0}

    def test_unknown_keys_dropped_silently(self):
        out = OMNIVOICE_CAPABILITIES.validate_params(
            {"num_step": 32, "totally_made_up_param": 999}
        )
        assert "totally_made_up_param" not in out

    def test_out_of_range_raises_value_error(self):
        with pytest.raises(ValueError, match=r"num_step=8 out of range"):
            OMNIVOICE_CAPABILITIES.validate_params({"num_step": 8})

    def test_non_numeric_raises_value_error(self):
        with pytest.raises(ValueError, match=r"num_step: not a number"):
            OMNIVOICE_CAPABILITIES.validate_params({"num_step": "abc"})

    def test_higgs_validates_empty_dict_to_empty(self):
        """Higgs declares zero params — validation returns empty dict
        regardless of input. Lets the render path call validate_params
        unconditionally without backend branching."""
        assert HIGGS_CAPABILITIES.validate_params({"num_step": 48}) == {}
        assert HIGGS_CAPABILITIES.validate_params({}) == {}


# ---- Preset declarations match v0.4 PREVIEW_MATRIX --------------------


class TestPresetParity:
    """v0.4 hardcoded the matrix in audiomat/routers/preview.py — v0.5
    moves it onto the descriptor. These tests guard against drift."""

    def test_four_omnivoice_presets(self):
        assert len(OMNIVOICE_PRESETS) == 4
        keys = [v.key for v in OMNIVOICE_PRESETS]
        assert keys == ["fast", "balanced", "crisp", "stable"]

    def test_preset_param_values_match_v0_4(self):
        """Locks in the exact PREVIEW_MATRIX values from preview.py so
        Phase 7's swap doesn't accidentally shift any cell."""
        params_by_key = {v.key: v.params for v in OMNIVOICE_PRESETS}
        assert params_by_key["fast"] == {
            "num_step": 32, "guidance_scale": 2.0, "speed": 1.0
        }
        assert params_by_key["balanced"] == {
            "num_step": 48, "guidance_scale": 2.0, "speed": 1.0
        }
        assert params_by_key["crisp"] == {
            "num_step": 48, "guidance_scale": 2.5, "speed": 1.0
        }
        assert params_by_key["stable"] == {
            "num_step": 64, "guidance_scale": 2.0, "speed": 1.0
        }


# ---- JSON round-trip (Phase 2 will rely on this for /api/models) ------


class TestJsonRoundTrip:
    def test_omnivoice_caps_dataclass_to_dict_serializes(self):
        payload = dc.asdict(OMNIVOICE_CAPABILITIES)
        # Should not raise — tuples become lists, nested dataclasses
        # become dicts, primitives pass through.
        wire = json.dumps(payload)
        back = json.loads(wire)
        assert back["display_name"] == "OmniVoice"
        assert back["license_kind"] == "permissive"
        assert len(back["params"]) == 3
        assert len(back["preset_variants"]) == 4
        assert back["params"][0]["name"] == "num_step"

    def test_higgs_caps_dataclass_to_dict_serializes(self):
        payload = dc.asdict(HIGGS_CAPABILITIES)
        wire = json.dumps(payload)
        back = json.loads(wire)
        assert back["display_name"] == "Higgs Audio v3"
        assert back["license_kind"] == "non_commercial"
        assert back["params"] == []
        assert back["preset_variants"] == []
        assert back["supports_multi_speaker"] is True


# ---- Adapter class-level attribute -------------------------------------


class TestAdapterCapabilitiesAttr:
    """Both adapters expose ``.capabilities`` as a class attribute
    pointing at the matching descriptor — so callers don't need to
    instantiate a model handle just to read its self-description."""

    def test_omnivoice_class_has_capabilities(self):
        from audiomat.tts import OmniVoiceTTS
        assert OmniVoiceTTS.capabilities is OMNIVOICE_CAPABILITIES

    def test_higgs_class_has_capabilities(self):
        from audiomat.tts_higgs import HiggsTTS
        assert HiggsTTS.capabilities is HIGGS_CAPABILITIES

    def test_capabilities_accessible_without_instantiation(self):
        """Reading caps off the class must not trigger lazy torch / hf
        imports — Models page enumeration would be painfully slow if it
        had to allocate handles for every registered model."""
        from audiomat.tts import OmniVoiceTTS
        from audiomat.tts_higgs import HiggsTTS
        # Accessing .capabilities on the class object only — no ``()``.
        assert OmniVoiceTTS.capabilities.display_name == "OmniVoice"
        assert HiggsTTS.capabilities.display_name == "Higgs Audio v3"


# ---- ParamSpec frozen-dataclass invariant ------------------------------


class TestImmutability:
    def test_param_spec_is_frozen(self):
        with pytest.raises(dc.FrozenInstanceError):
            OMNIVOICE_PARAM_NUM_STEP.default = 999  # type: ignore[misc]

    def test_capabilities_is_frozen(self):
        with pytest.raises(dc.FrozenInstanceError):
            OMNIVOICE_CAPABILITIES.display_name = "Hax"  # type: ignore[misc]

    def test_preset_variant_is_frozen(self):
        with pytest.raises(dc.FrozenInstanceError):
            OMNIVOICE_PRESETS[0].key = "lol"  # type: ignore[misc]


# ---- Capabilities feature-flag contract --------------------------------


class TestFeatureFlagContract:
    """Lock the v0.5 contract: only Higgs sets multi-speaker / non-verbal
    today. If a future engine adds these, this test will fail loudly so
    we update downstream code (cast assignment UI, non-verbal helper)."""

    def test_omnivoice_no_advanced_features(self):
        assert OMNIVOICE_CAPABILITIES.supports_multi_speaker is False
        assert OMNIVOICE_CAPABILITIES.supports_non_verbal_tags is False
        assert OMNIVOICE_CAPABILITIES.supports_emotion_descriptor is False

    def test_higgs_advertises_multi_speaker_and_non_verbal(self):
        assert HIGGS_CAPABILITIES.supports_multi_speaker is True
        assert HIGGS_CAPABILITIES.supports_non_verbal_tags is True
        # Emotion descriptor stays False until we validate it in lab
        # (see audiomat-lab/CLAUDE.md "multi-voice prototype" TODO).
        assert HIGGS_CAPABILITIES.supports_emotion_descriptor is False
