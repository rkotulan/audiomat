"""Tests for v0.5 Phase 2 — capabilities surfaced via /api/models.

Covers:

* ``ModelOut.from_model`` embeds the matching backend's capabilities,
* ``ModelOut.from_stock_omnivoice`` synthesizes the stock entry,
* ``GET /api/models`` lists stock first + registered models after,
* ``GET /api/models/default`` returns the stock card without hitting
  the registry,
* ``resolve_backend`` / ``caps_for_model_slug`` helpers fall back
  gracefully on unknown slugs (matches the v0.4
  ``state.get_tts_for_voice`` "ghost model" contract).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from audiomat.model_registry import (
    DEFAULT_MODEL_SLUG,
    TTSModel,
    caps_for_model_slug,
    resolve_backend,
)
from audiomat.schemas import ModelOut
from audiomat.tts_capabilities import HIGGS_CAPABILITIES, OMNIVOICE_CAPABILITIES


def _seed_model(
    root: Path, slug: str, *,
    backend: str = "omnivoice",
    license: str = "permissive",
) -> TTSModel:
    """Build + save a TTSModel with stub checkpoint files so is_valid
    returns True. Mirrors test_model_registry_backend.py's helper."""
    mdir = root / slug
    mdir.mkdir(parents=True)
    (mdir / "config.json").write_text('{"fake": true}\n', encoding="utf-8")
    (mdir / "model.safetensors").write_bytes(b"\x00" * 4096)
    m = TTSModel(
        name=slug,
        name_slug=slug,
        dir=mdir,
        source_type="hf",
        source_ref="x/y",
        hf_revision="abc",
        size_bytes=4096,
        notes="",
        created="2026-05-14T00:00:00Z",
        backend=backend,
        license=license,
    )
    m.save()
    return m


# ---- ModelOut.from_model embeds caps -----------------------------------


class TestModelOutCapsEmbed:
    def test_omnivoice_model_carries_omnivoice_caps(self, tmp_path: Path):
        m = _seed_model(tmp_path, "ov_finetune", backend="omnivoice")
        out = ModelOut.from_model(m)
        assert out.capabilities["display_name"] == "OmniVoice"
        assert out.capabilities["license_kind"] == "permissive"
        assert len(out.capabilities["params"]) == 3
        assert len(out.capabilities["preset_variants"]) == 4

    def test_higgs_model_carries_higgs_caps(self, tmp_path: Path):
        m = _seed_model(tmp_path, "higgs_demo",
                        backend="higgs", license="non_commercial")
        out = ModelOut.from_model(m)
        assert out.capabilities["display_name"] == "Higgs Audio v3"
        assert out.capabilities["license_kind"] == "non_commercial"
        assert out.capabilities["params"] == []
        assert out.capabilities["preset_variants"] == []
        assert out.capabilities["supports_multi_speaker"] is True


# ---- ModelOut.from_stock_omnivoice ------------------------------------


class TestStockSyntheticEntry:
    def test_slug_is_reserved_default_word(self):
        stock = ModelOut.from_stock_omnivoice()
        assert stock.name_slug == DEFAULT_MODEL_SLUG

    def test_carries_omnivoice_caps(self):
        stock = ModelOut.from_stock_omnivoice()
        assert stock.backend == "omnivoice"
        assert stock.license == "permissive"
        assert stock.capabilities["display_name"] == "OmniVoice"

    def test_revision_matches_pinned_tts_revision(self):
        """If we ever bump the OmniVoice pin, the stock card should
        report the new SHA — otherwise UI would mislead users about
        reproducibility."""
        from audiomat.tts import DEFAULT_REVISION
        stock = ModelOut.from_stock_omnivoice()
        assert stock.hf_revision == DEFAULT_REVISION


# ---- resolve_backend + caps_for_model_slug -----------------------------


class TestBackendResolution:
    def test_none_slug_returns_omnivoice(self, tmp_path: Path):
        assert resolve_backend(tmp_path, None) == "omnivoice"
        assert resolve_backend(tmp_path, "") == "omnivoice"
        assert resolve_backend(tmp_path, "default") == "omnivoice"

    def test_registered_higgs_returns_higgs(self, tmp_path: Path):
        _seed_model(tmp_path, "higgs_demo",
                    backend="higgs", license="non_commercial")
        assert resolve_backend(tmp_path, "higgs_demo") == "higgs"

    def test_ghost_slug_falls_back_to_omnivoice(self, tmp_path: Path):
        """Voice was bound to a model that's since been deleted. Match
        ``state.get_tts_for_voice`` semantics: never raise; fall back to
        stock so renders don't blow up."""
        assert resolve_backend(tmp_path, "ghost") == "omnivoice"


class TestCapsForSlug:
    def test_none_returns_omnivoice_caps(self, tmp_path: Path):
        caps = caps_for_model_slug(tmp_path, None)
        assert caps is OMNIVOICE_CAPABILITIES

    def test_higgs_slug_returns_higgs_caps(self, tmp_path: Path):
        _seed_model(tmp_path, "higgs_demo",
                    backend="higgs", license="non_commercial")
        caps = caps_for_model_slug(tmp_path, "higgs_demo")
        assert caps is HIGGS_CAPABILITIES

    def test_ghost_returns_omnivoice_caps(self, tmp_path: Path):
        """Unknown slug → graceful fall back, same as resolve_backend.
        Render-path validation should keep working even if voice points
        at a deleted model."""
        caps = caps_for_model_slug(tmp_path, "ghost")
        assert caps is OMNIVOICE_CAPABILITIES


# ---- GET /api/models lists stock + registered -------------------------


@pytest.fixture
def client(isolated_library: Path) -> TestClient:
    """FastAPI client bound to the isolated_library tmp tree. Imports
    api.app lazily so the lifespan migration doesn't run against an
    unrelated DB."""
    from audiomat.api import app
    return TestClient(app)


class TestListModelsEndpoint:
    def test_empty_registry_still_lists_stock(
        self, client: TestClient, isolated_library: Path,
    ):
        r = client.get("/api/models")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        assert body[0]["name_slug"] == DEFAULT_MODEL_SLUG
        assert body[0]["capabilities"]["display_name"] == "OmniVoice"

    def test_stock_comes_before_registered(
        self, client: TestClient, isolated_library: Path,
    ):
        models_root = isolated_library / "models"
        models_root.mkdir(exist_ok=True)
        _seed_model(models_root, "higgs_demo",
                    backend="higgs", license="non_commercial")
        r = client.get("/api/models")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 2
        assert body[0]["name_slug"] == DEFAULT_MODEL_SLUG
        assert body[1]["name_slug"] == "higgs_demo"
        assert body[1]["capabilities"]["license_kind"] == "non_commercial"


class TestGetModelEndpoint:
    def test_default_slug_returns_synthetic_stock(
        self, client: TestClient, isolated_library: Path,
    ):
        r = client.get(f"/api/models/{DEFAULT_MODEL_SLUG}")
        assert r.status_code == 200
        body = r.json()
        assert body["name_slug"] == DEFAULT_MODEL_SLUG
        assert body["capabilities"]["display_name"] == "OmniVoice"

    def test_registered_slug_returns_registry_entry(
        self, client: TestClient, isolated_library: Path,
    ):
        models_root = isolated_library / "models"
        models_root.mkdir(exist_ok=True)
        _seed_model(models_root, "higgs_demo",
                    backend="higgs", license="non_commercial")
        r = client.get("/api/models/higgs_demo")
        assert r.status_code == 200
        body = r.json()
        assert body["backend"] == "higgs"
        assert body["capabilities"]["short_label"] == "Higgs"

    def test_unknown_slug_404(self, client: TestClient, isolated_library: Path):
        r = client.get("/api/models/ghost")
        assert r.status_code == 404


# ---- Reserved slug protection -----------------------------------------


class TestReservedSlug:
    def test_register_local_refuses_default_slug(self, tmp_path: Path):
        """``DEFAULT_MODEL_SLUG`` is the wire-level handle for the stock
        entry. Re-registering it would shadow the synthetic listing in
        ``list_models`` — registry already protects, this test pins
        the contract."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "config.json").write_text("{}", encoding="utf-8")
        (src / "model.safetensors").write_bytes(b"\x00" * 256)
        root = tmp_path / "registry"
        root.mkdir()
        with pytest.raises(ValueError, match="reserved"):
            TTSModel.register_local(
                root, name="default", src_dir=src,
            )
