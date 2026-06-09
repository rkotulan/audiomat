"""TTS engine capability descriptors — self-published metadata per adapter.

Each TTS adapter (:class:`audiomat.tts.OmniVoiceTTS`,
:class:`audiomat.tts_higgs.HiggsTTS`, …) declares a class-level
``capabilities: TTSCapabilities`` attribute that documents:

* which render-time **params** it exposes (sliders + Fine-tune dialog),
* what **preset variants** make sense for an A/B matrix,
* what **features** it supports (multi-speaker, non-verbal tags, …),
* its **license** obligations for the UI,
* informational performance hints (typical RTF, peak VRAM).

UI surfaces and the render path read from this descriptor instead of
branching on ``backend == "higgs"`` literals. Adding a new engine = new
adapter + new descriptor; zero UI code touched.

The descriptor is a frozen dataclass with primitive fields only so it
round-trips through ``dataclasses.asdict`` → JSON cleanly for the
``/api/models`` endpoint. Tuples (immutable + dataclass-friendly)
become JSON arrays on the wire.

Format hint design — UI labels for a slider value need consistent
rounding across Python tests + the TypeScript frontend. We avoid
Python-only ``f"{v:.1f}"`` strings and instead expose two structured
fields:

* :attr:`ParamSpec.decimals` — how many fractional digits to show.
* :attr:`ParamSpec.suffix` — short unit string ("×", "%", "") appended
  after the number.

Both can be rendered identically in Python (:meth:`ParamSpec.format`)
and JS (``value.toFixed(decimals) + suffix``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


# Type aliases — keep the literals in one place so a future "audio_event"
# backend can extend them without grepping the codebase.
ParamKind = Literal["int", "float"]
LicenseKind = Literal["permissive", "non_commercial"]


@dataclass(frozen=True)
class ParamSpec:
    """One tunable knob the engine exposes.

    Drives the Fine-tune dialog sliders, the Progress card param badges,
    and the render-path validation. All fields are primitive so the
    spec serializes to JSON without custom encoders.
    """

    # Internal key used in ``RenderParams`` / ``project.params_json``.
    # Stable — renaming this is a schema-breaking change.
    name: str

    # User-facing slider label. Defaults to ``name`` if not overridden.
    label: str

    # Short helper text shown under the slider. One sentence; the dialog
    # has limited vertical room.
    hint: str

    kind: ParamKind                # "int" | "float" — drives JS coercion
    min: float
    max: float
    step: float
    default: float

    # Display formatting. ``decimals=0`` + ``suffix=""`` renders "48",
    # ``decimals=2`` + ``suffix="×"`` renders "1.00×". Cross-language:
    # both Python (:meth:`format`) and JS (``v.toFixed(d) + suffix``).
    decimals: int = 0
    suffix: str = ""

    def format(self, value: float) -> str:
        """Render ``value`` exactly as the UI would, for log messages
        and Python-side preview text."""
        if self.decimals == 0 and self.kind == "int":
            shown = str(int(round(value)))
        else:
            shown = f"{float(value):.{self.decimals}f}"
        return shown + self.suffix

    def coerce(self, value):  # type: ignore[no-untyped-def]
        """Cast ``value`` to the spec's primitive type. Raises
        :class:`ValueError` if the input isn't numeric. Out-of-range
        values are returned as-is for the caller (typically
        :meth:`TTSCapabilities.validate_params`) to flag explicitly."""
        if self.kind == "int":
            return int(round(float(value)))
        return float(value)


@dataclass(frozen=True)
class PresetVariant:
    """One named A/B cell in the preview matrix.

    The render path applies ``params`` on top of the project's current
    params (so missing keys fall through to the project default). An
    engine with zero variants suppresses the matrix UI entirely — the
    Preview tab renders the "skip" explainer instead.
    """

    key: str                # stable id ("fast" | "balanced" | …)
    label: str              # UI label ("Fast" | "Balanced" | …)
    params: dict            # {"num_step": 32, "guidance_scale": 2.0, ...}


@dataclass(frozen=True)
class TTSCapabilities:
    """Engine self-description. One per adapter class.

    Class-level constant on each adapter — never instance state, so
    callers can read it without instantiating the adapter (Models page
    listing doesn't need to allocate model handles)."""

    # Identity — what the UI shows for this engine.
    display_name: str               # "OmniVoice", "Higgs Audio v3"
    short_label: str                # chip text on Progress + badges

    # License obligation surfaced in the UI (amber NC badge, render
    # confirmation copy). audiomat code itself stays MIT regardless.
    license_kind: LicenseKind       # "permissive" | "non_commercial"
    license_name: str               # "Apache-2.0", "Higgs Community License"

    # Param surface — drives sliders, Progress badges, render validation.
    # Empty tuple = engine has no tunable knobs.
    params: tuple[ParamSpec, ...] = ()

    # Preset matrix cells. Empty = no A/B makes sense → UI skips matrix.
    preset_variants: tuple[PresetVariant, ...] = ()

    # Reference clip expectations — informational, displayed in the
    # voice picker hint area. Engine adapters enforce these at
    # generate() time (warning or refusal).
    ref_min_seconds: float = 5.0
    ref_max_seconds: float = 10.0
    ref_sample_rate: int = 22050    # native rate the engine expects

    # Generated audio rate (output of ``generate``). Concat path uses
    # this; mismatched rates would force per-chunk resample.
    output_sample_rate: int = 24000

    # Feature flags — gate future UI affordances. Multi-speaker drives
    # the cast assignment surface in the planned annotation workflow;
    # non-verbal tags gate the ``[laugh]`` / ``[sigh]`` insert helper.
    supports_multi_speaker: bool = False
    supports_non_verbal_tags: bool = False
    supports_emotion_descriptor: bool = False

    # Operational hints — shown on the Models page detail card. Both
    # are approximate measurements from our reference RTX 5070 (12 GB);
    # they're guidance for the user, not hard contracts.
    typical_rtf: float = 0.0        # gen_seconds / audio_duration_s
    typical_vram_gb: float = 0.0

    # ---- derived helpers -------------------------------------------------

    @property
    def has_tunable_params(self) -> bool:
        """True if the engine exposes ≥1 user-tunable param. Drives
        whether the Fine-tune button + slider dialog render at all."""
        return len(self.params) > 0

    @property
    def has_preset_matrix(self) -> bool:
        """True if A/B preview matrix makes sense. Requires ≥2 presets
        (one cell = degenerate) and ≥1 param to vary across them."""
        return len(self.preset_variants) >= 2 and self.has_tunable_params

    def find_param(self, name: str) -> ParamSpec | None:
        """Linear scan — param lists are 3-5 items at most."""
        for p in self.params:
            if p.name == name:
                return p
        return None

    def default_params(self) -> dict:
        """``{spec.name: spec.default}`` for every declared param. Used
        when switching a project's engine: the project's ``params_json``
        gets reset to these defaults so stale OmniVoice knobs don't
        accidentally apply to a Higgs render."""
        return {p.name: p.coerce(p.default) for p in self.params}

    def validate_params(self, raw: dict) -> dict:
        """Coerce + range-check ``raw`` against the declared param specs.

        Returns a fresh dict containing only the engine's known params,
        with each value coerced to the spec's type. Unknown keys are
        dropped silently (forward-compat with future param additions).
        Missing keys are filled from spec defaults.

        Raises :class:`ValueError` with a human-readable message on the
        first out-of-range value. Callers (the render endpoint) catch
        this and translate to 422.
        """
        out: dict = {}
        for spec in self.params:
            if spec.name in raw and raw[spec.name] is not None:
                try:
                    v = spec.coerce(raw[spec.name])
                except (TypeError, ValueError) as e:
                    raise ValueError(
                        f"{spec.name}: not a number ({raw[spec.name]!r})"
                    ) from e
                if v < spec.min or v > spec.max:
                    raise ValueError(
                        f"{spec.name}={spec.format(v)} out of range "
                        f"[{spec.format(spec.min)}, {spec.format(spec.max)}]"
                    )
                out[spec.name] = v
            else:
                out[spec.name] = spec.coerce(spec.default)
        return out


# ----------------------------------------------------------------------------
# Pre-built specs — declared at module scope so the adapter files can
# import a single name instead of repeating verbose ParamSpec(...) calls.
# ----------------------------------------------------------------------------


# OmniVoice's three render knobs. Values match the production defaults
# documented in CLAUDE.md Stage 3 + the existing PREVIEW_MATRIX in
# audiomat/routers/preview.py — Phase 6 will switch that matrix to read
# from here.
OMNIVOICE_PARAM_NUM_STEP = ParamSpec(
    name="num_step",
    label="num_step",
    hint="Diffusion steps. Higher = smoother, ~1.5× slower per +16.",
    kind="int",
    min=16, max=64, step=16, default=48,
    decimals=0, suffix="",
)
OMNIVOICE_PARAM_GUIDANCE_SCALE = ParamSpec(
    name="guidance_scale",
    label="guidance_scale",
    hint="Conditioning strength. 2.0 default; 3.0+ may over-emphasize.",
    kind="float",
    min=1.0, max=4.0, step=0.1, default=2.0,
    decimals=1, suffix="",
)
OMNIVOICE_PARAM_SPEED = ParamSpec(
    name="speed",
    label="speed",
    hint="Speech tempo. 1.0 natural, 0.85 relaxed, 1.15 brisk.",
    kind="float",
    min=0.7, max=1.3, step=0.05, default=1.0,
    decimals=2, suffix="×",
)


# OmniVoice preset matrix. Values mirror the v0.4 PREVIEW_MATRIX in
# audiomat/routers/preview.py exactly — Phase 7 swaps that hardcoded
# list for ``OMNIVOICE_CAPABILITIES.preset_variants``.
OMNIVOICE_PRESETS: tuple[PresetVariant, ...] = (
    PresetVariant(
        key="fast", label="Fast",
        params={"num_step": 32, "guidance_scale": 2.0, "speed": 1.0},
    ),
    PresetVariant(
        key="balanced", label="Balanced",
        params={"num_step": 48, "guidance_scale": 2.0, "speed": 1.0},
    ),
    PresetVariant(
        key="crisp", label="Crisp",
        params={"num_step": 48, "guidance_scale": 2.5, "speed": 1.0},
    ),
    PresetVariant(
        key="stable", label="Stable",
        params={"num_step": 64, "guidance_scale": 2.0, "speed": 1.0},
    ),
)


OMNIVOICE_CAPABILITIES = TTSCapabilities(
    display_name="OmniVoice",
    short_label="OmniVoice",
    license_kind="permissive",
    license_name="Apache-2.0",
    params=(
        OMNIVOICE_PARAM_NUM_STEP,
        OMNIVOICE_PARAM_GUIDANCE_SCALE,
        OMNIVOICE_PARAM_SPEED,
    ),
    preset_variants=OMNIVOICE_PRESETS,
    ref_min_seconds=5.0,
    ref_max_seconds=10.0,
    ref_sample_rate=22050,
    output_sample_rate=24000,
    supports_multi_speaker=False,
    supports_non_verbal_tags=False,
    supports_emotion_descriptor=False,
    typical_rtf=0.25,
    typical_vram_gb=2.3,
)


# Higgs Audio v3 — autoregressive LM, no diffusion knobs. No preset
# matrix; the Preview tab renders the "skip — voice uses Higgs" card
# instead. Higher VRAM (~8.6 GB at bf16) + slower RTF (~0.77 on RTX
# 5070) than OmniVoice; UI shows these as expectation-setting hints.
HIGGS_CAPABILITIES = TTSCapabilities(
    display_name="Higgs Audio v3",
    short_label="Higgs",
    license_kind="non_commercial",
    license_name="Higgs Community License",
    params=(),
    preset_variants=(),
    ref_min_seconds=5.0,
    ref_max_seconds=15.0,
    ref_sample_rate=24000,
    output_sample_rate=24000,
    supports_multi_speaker=True,
    supports_non_verbal_tags=True,
    supports_emotion_descriptor=False,
    typical_rtf=0.77,
    typical_vram_gb=8.6,
)


if __name__ == "__main__":
    # Smoke — `python -m audiomat.tts_capabilities`
    import dataclasses as _dc
    import json as _json

    for caps in (OMNIVOICE_CAPABILITIES, HIGGS_CAPABILITIES):
        print(f"\n=== {caps.display_name} ===")
        print(f"license          : {caps.license_name} ({caps.license_kind})")
        print(f"params           : {[p.name for p in caps.params] or '(none)'}")
        print(f"presets          : {[v.label for v in caps.preset_variants] or '(none)'}")
        print(f"has_preset_matrix: {caps.has_preset_matrix}")
        print(f"multi-speaker    : {caps.supports_multi_speaker}")
        print(f"non-verbal tags  : {caps.supports_non_verbal_tags}")
        print(f"defaults         : {caps.default_params()}")
        # Round-trip through JSON to prove it serializes cleanly.
        payload = _dc.asdict(caps)
        _json.dumps(payload)
        print(f"json round-trip  : ok ({len(_json.dumps(payload))} bytes)")
