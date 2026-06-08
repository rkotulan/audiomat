"""Tests for the v0.4 model registry additions: backend + license
fields, persistence, and dispatcher routing.

The registry is filesystem-based (one meta.json per registered model)
so these tests use plain tmpdirs — no DB fixture needed. The
dispatcher tests stub out the actual TTS classes since we don't want
to load 8 GB of weights from CI.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from audiomat.model_registry import TTSModel


# ---- meta.json round-trip -----------------------------------------


def _seed_model(
    root: Path, slug: str, *,
    backend: str = "omnivoice",
    license: str = "permissive",
    source_type: str = "hf",
) -> TTSModel:
    """Build + save a TTSModel with stub checkpoint files so is_valid
    returns True. Used by the persistence + dispatcher tests."""
    mdir = root / slug
    mdir.mkdir(parents=True)
    (mdir / "config.json").write_text('{"fake": true}\n', encoding="utf-8")
    (mdir / "model.safetensors").write_bytes(b"\x00" * 4096)
    m = TTSModel(
        name=slug,
        name_slug=slug,
        dir=mdir,
        source_type=source_type,
        source_ref="x/y" if source_type == "hf" else str(mdir),
        hf_revision="abc" if source_type == "hf" else None,
        size_bytes=4096,
        notes="",
        created="2026-05-14T00:00:00Z",
        backend=backend,
        license=license,
    )
    m.save()
    return m


class TestBackendLicensePersistence:
    def test_save_writes_both_fields(self, tmp_path: Path):
        m = _seed_model(tmp_path, "higgs_demo",
                        backend="higgs", license="non_commercial")
        meta = json.loads(m.meta_path.read_text(encoding="utf-8"))
        assert meta["backend"] == "higgs"
        assert meta["license"] == "non_commercial"

    def test_load_round_trips_both_fields(self, tmp_path: Path):
        _seed_model(tmp_path, "higgs_demo",
                    backend="higgs", license="non_commercial")
        loaded = TTSModel.load(tmp_path / "higgs_demo")
        assert loaded.backend == "higgs"
        assert loaded.license == "non_commercial"

    def test_load_v03_meta_without_new_fields_defaults_safely(self, tmp_path: Path):
        """Pre-v0.4 meta.json files don't have backend/license. Loader
        must default to omnivoice/permissive so existing operators'
        registered models keep loading after upgrade."""
        mdir = tmp_path / "old_entry"
        mdir.mkdir()
        (mdir / "config.json").write_text('{"fake": true}\n', encoding="utf-8")
        (mdir / "model.safetensors").write_bytes(b"\x00" * 4096)
        (mdir / "meta.json").write_text(json.dumps({
            "name": "Old Entry",
            "name_slug": "old_entry",
            "source_type": "hf",
            "source_ref": "x/y",
            "hf_revision": "abc",
            "size_bytes": 4096,
            "notes": "",
            "created": "2026-05-10T00:00:00Z",
        }), encoding="utf-8")
        loaded = TTSModel.load(mdir)
        assert loaded.backend == "omnivoice"
        assert loaded.license == "permissive"

    def test_default_dataclass_values_are_safe(self):
        """Constructing TTSModel without explicit backend/license must
        produce the safe defaults — protects against typos that omit
        the kwargs at registration sites we haven't updated yet."""
        m = TTSModel(
            name="x", name_slug="x", dir=Path("/tmp/x"),
            source_type="hf", source_ref="o/r",
        )
        assert m.backend == "omnivoice"
        assert m.license == "permissive"


# ---- register_local kwargs pass-through ---------------------------


class TestRegisterLocalForwardsKwargs:
    """register_local must forward backend/license to the stored model;
    the API path in routers/models.py takes these from
    RegisterLocalModelRequest and we want to verify the call chain
    doesn't drop them somewhere."""

    def test_register_local_persists_higgs_backend(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "config.json").write_text("{}", encoding="utf-8")
        (src / "model.safetensors").write_bytes(b"\x00" * 256)

        root = tmp_path / "registry"
        root.mkdir()
        m = TTSModel.register_local(
            root, name="my higgs", src_dir=src,
            backend="higgs", license="non_commercial",
        )
        # Round-trip through disk to prove it persisted.
        loaded = TTSModel.load(m.dir)
        assert loaded.backend == "higgs"
        assert loaded.license == "non_commercial"


# ---- state.get_tts dispatch ---------------------------------------


class TestGetTtsDispatch:
    """``state.get_tts`` returns an OmniVoiceTTS by default and a
    HiggsTTS when backend='higgs'. Both must use the same per-target
    LRU cache so a render doesn't accidentally create two instances
    for the same model.

    These tests never call ``.load()`` so no weights download.
    """

    def _reset(self):
        """Drop the process-wide singleton cache so tests don't
        cross-contaminate via the LRU dict."""
        from audiomat import state
        with state._TTS_LOCK:
            state._TTS_INSTANCES.clear()

    def test_default_is_omnivoice(self):
        self._reset()
        from audiomat.state import get_tts
        from audiomat.tts import OmniVoiceTTS
        inst = get_tts(target=None)
        assert isinstance(inst, OmniVoiceTTS)

    def test_explicit_higgs_backend_returns_higgs(self):
        self._reset()
        from audiomat.state import get_tts
        from audiomat.tts_higgs import HiggsTTS
        inst = get_tts(
            target="multimodalart/higgs-audio-v3-tts-4b-transformers",
            backend="higgs",
        )
        assert isinstance(inst, HiggsTTS)

    def test_higgs_arg_ignored_when_target_is_none(self):
        """target=None always means 'stock OmniVoice', regardless of
        what backend the caller passed — there's no Higgs stock to
        fall back to."""
        self._reset()
        from audiomat.state import get_tts
        from audiomat.tts import OmniVoiceTTS
        inst = get_tts(target=None, backend="higgs")
        assert isinstance(inst, OmniVoiceTTS)

    def test_same_target_returns_cached_instance(self):
        self._reset()
        from audiomat.state import get_tts
        a = get_tts(target="some/model", backend="omnivoice")
        b = get_tts(target="some/model", backend="omnivoice")
        assert a is b

    def test_different_targets_get_separate_instances(self):
        self._reset()
        from audiomat.state import get_tts
        a = get_tts(target="some/model-a")
        b = get_tts(target="some/model-b")
        assert a is not b


# ---- state.get_tts_for_voice routing ------------------------------


class _StubVoice:
    """Minimal duck-type matching the bits state.get_tts_for_voice
    reads off the Voice dataclass. Lets us avoid the DB-backed Voice
    just to test the dispatcher."""
    def __init__(self, slug: str, tts_model: str | None):
        self.name_slug = slug
        self.tts_model = tts_model


class TestGetTtsForVoiceRouting:
    """``state.get_tts_for_voice`` should:

    * use stock OmniVoice when the voice has no tts_model
    * route to HiggsTTS when the registered model.backend == 'higgs'
    * fall back to stock OmniVoice when the registered slug is gone

    Uses the project's ``isolated_library`` fixture which redirects
    ``AUDIOMAT_LIBRARY_ROOT`` at a tmp path and reloads ``state`` so
    ``PATHS.models_root`` points into the tmp tree (the frozen
    AudiomatPaths dataclass blocks direct monkeypatching)."""

    def _reset_tts_cache(self):
        from audiomat import state
        with state._TTS_LOCK:
            state._TTS_INSTANCES.clear()

    def test_voice_with_none_tts_model_gets_stock(self, isolated_library: Path):
        self._reset_tts_cache()
        from audiomat.state import get_tts_for_voice
        from audiomat.tts import OmniVoiceTTS
        inst = get_tts_for_voice(_StubVoice("v1", tts_model=None))
        assert isinstance(inst, OmniVoiceTTS)

    def test_voice_with_default_slug_gets_stock(self, isolated_library: Path):
        self._reset_tts_cache()
        from audiomat.state import get_tts_for_voice
        from audiomat.tts import OmniVoiceTTS
        inst = get_tts_for_voice(_StubVoice("v1", tts_model="default"))
        assert isinstance(inst, OmniVoiceTTS)

    def test_voice_pointing_at_higgs_model_routes_to_higgs(
        self, isolated_library: Path,
    ):
        self._reset_tts_cache()
        (isolated_library / "models").mkdir(exist_ok=True)
        _seed_model(isolated_library / "models", "higgs_demo",
                    backend="higgs", license="non_commercial")
        from audiomat.state import get_tts_for_voice
        from audiomat.tts_higgs import HiggsTTS
        inst = get_tts_for_voice(_StubVoice("v1", tts_model="higgs_demo"))
        assert isinstance(inst, HiggsTTS)

    def test_voice_pointing_at_omnivoice_finetune_routes_to_omnivoice(
        self, isolated_library: Path,
    ):
        self._reset_tts_cache()
        (isolated_library / "models").mkdir(exist_ok=True)
        _seed_model(isolated_library / "models", "omni_ft",
                    backend="omnivoice", license="permissive",
                    source_type="local")
        from audiomat.state import get_tts_for_voice
        from audiomat.tts import OmniVoiceTTS
        inst = get_tts_for_voice(_StubVoice("v1", tts_model="omni_ft"))
        assert isinstance(inst, OmniVoiceTTS)

    def test_missing_registry_entry_falls_back_to_stock(
        self, isolated_library: Path,
    ):
        """User deleted the registered model after a voice was assigned
        to it. Render should not blow up — it should fall back to stock
        OmniVoice and warn."""
        self._reset_tts_cache()
        from audiomat.state import get_tts_for_voice
        from audiomat.tts import OmniVoiceTTS
        inst = get_tts_for_voice(_StubVoice("v1", tts_model="ghost_model"))
        assert isinstance(inst, OmniVoiceTTS)
