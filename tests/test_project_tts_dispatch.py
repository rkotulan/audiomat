"""Tests for the v0.5 project-level TTS dispatch.

Covers:

* ``state.get_tts_for_project`` resolves project.tts_model exactly the
  way ``state.get_tts_for_voice`` v0.4 resolved voice.tts_model — same
  fallback contract on None / "default" / ghost slug.
* PATCH ``/api/projects/{slug}/tts-model`` validates the slug against
  the registry and normalises "default" / empty → None.
* ``ProjectRenderer._params_signature`` is sensitive to the engine
  slug — swapping OmniVoice ↔ Higgs invalidates cached chunks.

Heavy adapter loads are out of scope — we assert on the **type** of
the dispatcher's return value (cheap), never call ``.load()``.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from audiomat.db import get_conn
from audiomat.model_registry import TTSModel
from audiomat.project import BookInfo, Project, ProjectStatus, RenderParams
from audiomat.voice import Voice


def _seed_model(
    root: Path, slug: str, *,
    backend: str = "omnivoice",
    license: str = "permissive",
) -> TTSModel:
    mdir = root / slug
    mdir.mkdir(parents=True, exist_ok=True)
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


def _seed_voice(slug: str = "anna", *, tts_model: str | None = None) -> Voice:
    """Create a Voice DB row (no on-disk WAV). The dispatcher only
    reads the model field — Voice.save without files is enough."""
    v = Voice(
        name=slug.capitalize(), name_slug=slug,
        duration_s=8.0, sample_rate=24000, channels=1,
        transcript_chars=50, notes="", created="2026-05-01T00:00:00Z",
        tts_model=tts_model,
    )
    v.save()
    return v


def _seed_project(
    slug: str = "book1",
    voice_slug: str = "anna",
    *,
    tts_model: str | None = None,
) -> Project:
    proj = Project(
        name=slug,
        name_slug=slug,
        book=BookInfo(filename="book.epub", blocks_total=10),
        voice_ref=voice_slug.capitalize(),
        voice_ref_slug=voice_slug,
        params=RenderParams(),
        status=ProjectStatus(chapters_total=10),
        created="2026-05-01T00:00:00Z",
        version=1,
        tts_model=tts_model,
    )
    proj.save()
    return proj


# ---- get_tts_for_project routing --------------------------------------


class TestGetTtsForProject:
    def _reset_tts_cache(self):
        from audiomat import state
        with state._TTS_LOCK:
            state._TTS_INSTANCES.clear()

    def test_project_with_none_tts_model_gets_stock(self, isolated_library: Path):
        self._reset_tts_cache()
        get_conn()
        proj = _seed_project(tts_model=None)
        from audiomat.state import get_tts_for_project
        from audiomat.tts import OmniVoiceTTS
        inst = get_tts_for_project(proj)
        assert isinstance(inst, OmniVoiceTTS)

    def test_project_with_default_slug_gets_stock(self, isolated_library: Path):
        self._reset_tts_cache()
        get_conn()
        proj = _seed_project(tts_model="default")
        from audiomat.state import get_tts_for_project
        from audiomat.tts import OmniVoiceTTS
        inst = get_tts_for_project(proj)
        assert isinstance(inst, OmniVoiceTTS)

    def test_project_with_higgs_model_gets_higgs(self, isolated_library: Path):
        self._reset_tts_cache()
        get_conn()
        models_root = isolated_library / "models"
        models_root.mkdir(exist_ok=True)
        _seed_model(models_root, "higgs_demo",
                    backend="higgs", license="non_commercial")
        proj = _seed_project(tts_model="higgs_demo")
        from audiomat.state import get_tts_for_project
        from audiomat.tts_higgs import HiggsTTS
        inst = get_tts_for_project(proj)
        assert isinstance(inst, HiggsTTS)

    def test_project_with_omnivoice_finetune(self, isolated_library: Path):
        self._reset_tts_cache()
        get_conn()
        models_root = isolated_library / "models"
        models_root.mkdir(exist_ok=True)
        _seed_model(models_root, "omni_ft",
                    backend="omnivoice", license="permissive")
        proj = _seed_project(tts_model="omni_ft")
        from audiomat.state import get_tts_for_project
        from audiomat.tts import OmniVoiceTTS
        inst = get_tts_for_project(proj)
        assert isinstance(inst, OmniVoiceTTS)

    def test_missing_model_falls_back_to_stock(self, isolated_library: Path):
        """A model slug that's not in the registry shouldn't crash the
        render. v0.5 matches the v0.4 voice path — warn + fall back."""
        self._reset_tts_cache()
        get_conn()
        proj = _seed_project(tts_model="ghost_model")
        from audiomat.state import get_tts_for_project
        from audiomat.tts import OmniVoiceTTS
        inst = get_tts_for_project(proj)
        assert isinstance(inst, OmniVoiceTTS)


# ---- PATCH /api/projects/{slug}/tts-model -----------------------------


@pytest.fixture
def client(isolated_library: Path) -> TestClient:
    from audiomat.api import app
    return TestClient(app)


class TestPatchProjectTtsModel:
    def test_set_to_registered_slug(
        self, client: TestClient, isolated_library: Path,
    ):
        get_conn()
        _seed_voice("anna")
        _seed_project(voice_slug="anna")
        models_root = isolated_library / "models"
        models_root.mkdir(exist_ok=True)
        _seed_model(models_root, "higgs_demo",
                    backend="higgs", license="non_commercial")

        r = client.patch(
            "/api/projects/book1/tts-model",
            json={"tts_model": "higgs_demo"},
        )
        assert r.status_code == 200
        assert r.json()["tts_model"] == "higgs_demo"

    def test_default_slug_normalised_to_none(
        self, client: TestClient, isolated_library: Path,
    ):
        get_conn()
        _seed_voice("anna")
        _seed_project(voice_slug="anna", tts_model="higgs_demo")
        models_root = isolated_library / "models"
        models_root.mkdir(exist_ok=True)
        _seed_model(models_root, "higgs_demo",
                    backend="higgs", license="non_commercial")

        r = client.patch(
            "/api/projects/book1/tts-model",
            json={"tts_model": "default"},
        )
        assert r.status_code == 200
        # "default" is the wire-level "stock" but we store None on disk
        # so cache signature + DB row agree on one canonical form.
        assert r.json()["tts_model"] is None

    def test_empty_string_normalised_to_none(
        self, client: TestClient, isolated_library: Path,
    ):
        get_conn()
        _seed_voice("anna")
        _seed_project(voice_slug="anna", tts_model="higgs_demo")
        r = client.patch(
            "/api/projects/book1/tts-model",
            json={"tts_model": ""},
        )
        assert r.status_code == 200
        assert r.json()["tts_model"] is None

    def test_unknown_slug_400(self, client: TestClient, isolated_library: Path):
        get_conn()
        _seed_voice("anna")
        _seed_project(voice_slug="anna")
        r = client.patch(
            "/api/projects/book1/tts-model",
            json={"tts_model": "ghost"},
        )
        assert r.status_code == 400
        assert "ghost" in r.json()["detail"]

    def test_missing_project_404(self, client: TestClient, isolated_library: Path):
        r = client.patch(
            "/api/projects/ghost/tts-model",
            json={"tts_model": "default"},
        )
        assert r.status_code == 404


# ---- Cache signature invalidates on engine swap -----------------------


class TestRenderSignatureEngineSensitive:
    """``ProjectRenderer._params_signature`` must fold the engine slug
    into the hash so OmniVoice ↔ Higgs swap invalidates cached chunks.
    Otherwise the renderer would happily serve cached OmniVoice audio
    for a Higgs render request."""

    def _build_renderer(self, proj, voice):
        # ProjectRenderer can be built with a None TTS handle for
        # signature-only inspection — _params_signature doesn't touch
        # the tts object.
        from audiomat.render import ProjectRenderer
        return ProjectRenderer(proj, voice, tts=None, blocks=[])

    def test_signature_changes_when_engine_changes(
        self, isolated_library: Path,
    ):
        get_conn()
        v = _seed_voice("anna")
        # Make voice.wav exist so mtime probe doesn't return 0 — but
        # mtime is a constant within a single test so it doesn't shift
        # the diff we're hunting.
        v.dir.mkdir(parents=True, exist_ok=True)
        v.wav_path.write_bytes(b"\x00" * 1024)

        proj_stock = _seed_project(voice_slug="anna", tts_model=None)
        r_stock = self._build_renderer(proj_stock, v)
        sig_stock = r_stock._params_signature()

        proj_higgs = _seed_project(voice_slug="anna", tts_model="higgs_demo")
        # Reload through DB so the dataclass match real load path.
        proj_higgs = Project.load("book1")
        r_higgs = self._build_renderer(proj_higgs, v)
        sig_higgs = r_higgs._params_signature()

        assert sig_stock != sig_higgs

    def test_default_and_none_collapse_to_same_signature(
        self, isolated_library: Path,
    ):
        """``None`` and the literal ``"default"`` are both wire-level
        synonyms for "stock OmniVoice". The signature must not split
        them apart — otherwise toggling between a fresh-create (None)
        and a "Reset to default" PATCH (sometimes echoed as "default"
        by older clients) would double-invalidate."""
        get_conn()
        v = _seed_voice("anna")
        v.dir.mkdir(parents=True, exist_ok=True)
        v.wav_path.write_bytes(b"\x00" * 1024)

        proj_none = _seed_project(voice_slug="anna", tts_model=None)
        sig_none = self._build_renderer(proj_none, v)._params_signature()

        proj_default = _seed_project(voice_slug="anna", tts_model="default")
        sig_default = self._build_renderer(proj_default, v)._params_signature()

        assert sig_none == sig_default
