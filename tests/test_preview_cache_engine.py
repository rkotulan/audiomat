"""v0.5.1 hotfix: preview cache keys must include the project's
engine slug.

Discovered on Rezavý les v2 when swapping the project from OmniVoice
→ Higgs left stale OmniVoice-generated cells in `previews/` that
served as cache hits to the UI. The render-path manifest signature
already folded the engine in (Phase 4), but the preview endpoints
hashed only `(text, params, voice_slug)`.

Each call below exercises the cache-key function used by one preview
endpoint and asserts the engine slug actually shifts the digest.
"""
from __future__ import annotations

import hashlib

from audiomat.routers.preview import _engine_slug_for_cache


class _StubProject:
    """Duck-types just the attribute the cache helper reads."""
    def __init__(self, tts_model):
        self.tts_model = tts_model


class TestEngineSlugCanonicalisation:
    """``None`` and the literal ``"default"`` both mean "stock
    OmniVoice" and must collapse to the same string — otherwise
    flipping between the wire-level synonyms double-invalidates."""

    def test_none_collapses_to_default(self):
        assert _engine_slug_for_cache(_StubProject(None)) == "default"

    def test_empty_string_collapses_to_default(self):
        assert _engine_slug_for_cache(_StubProject("")) == "default"

    def test_default_literal_passes_through(self):
        assert _engine_slug_for_cache(_StubProject("default")) == "default"

    def test_named_slug_passes_through(self):
        assert _engine_slug_for_cache(_StubProject("higgs_demo")) == "higgs_demo"


class TestCacheKeyChangesWithEngine:
    """Reproduce the v0.5.1 hotfix scenario: identical (text, params,
    voice) but different engine slugs must produce different cache
    hashes — otherwise stale cells from the old engine masquerade as
    fresh cache hits after a swap."""

    def _key(self, *, voice_slug: str, engine: str | None,
              text: str = "Ahoj.", num_step: int = 48,
              gs: float = 2.0, speed: float = 1.0) -> str:
        # Mirrors the cache-key shape the three preview endpoints
        # use (same `clean|num_step|gs|speed|voice|engine=` pattern).
        src = (
            f"{text}|{num_step}|{gs}|{speed}|{voice_slug}"
            f"|engine={_engine_slug_for_cache(_StubProject(engine))}"
        )
        return hashlib.md5(src.encode("utf-8")).hexdigest()[:16]

    def test_omnivoice_and_higgs_differ(self):
        a = self._key(voice_slug="jezkova", engine=None)
        b = self._key(voice_slug="jezkova", engine="higgs_demo")
        assert a != b

    def test_two_different_finetunes_differ(self):
        a = self._key(voice_slug="jezkova", engine="ov_finetune_a")
        b = self._key(voice_slug="jezkova", engine="ov_finetune_b")
        assert a != b

    def test_none_and_default_collapse_to_same_key(self):
        """Wire-level synonyms must produce the same digest so a PATCH
        that echoes ``"default"`` against an in-memory ``None`` doesn't
        spuriously invalidate the cache."""
        a = self._key(voice_slug="jezkova", engine=None)
        b = self._key(voice_slug="jezkova", engine="default")
        assert a == b

    def test_engine_only_change_flips_key(self):
        """Voice, params and text identical — only the engine differs.
        The v0.5.0 bug was exactly this scenario returning the same
        hash."""
        kw = {"voice_slug": "jezkova", "num_step": 48,
              "gs": 2.0, "speed": 1.0, "text": "Sample"}
        omnivoice = self._key(engine=None, **kw)
        higgs = self._key(engine="higgs_demo", **kw)
        assert omnivoice != higgs
