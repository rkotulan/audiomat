"""Tests for per-project pronunciation dictionary.

Covers the four pieces that have to be correct for this to be safe:

  * :func:`apply_pronunciations` — word-boundary correctness, longest-
    first ordering, Czech / unicode keys, regex-special chars escaped.
  * load/save round-trip; tolerant of malformed JSON.
  * :func:`signature` is stable + flips on any edit.
  * GET/PUT endpoints round-trip via TestClient and reject bad shapes.
"""
from __future__ import annotations

import json
from pathlib import Path

from audiomat.pronunciations import (
    PRONUNCIATIONS_FILENAME,
    apply_pronunciations,
    load_pronunciations,
    save_pronunciations,
    signature,
)


# ----------------------------------------------------------------------------
# apply_pronunciations
# ----------------------------------------------------------------------------


class TestApplyPronunciations:
    def test_empty_mapping_noop(self):
        assert apply_pronunciations("Hello world", {}) == "Hello world"

    def test_empty_text_noop(self):
        assert apply_pronunciations("", {"x": "y"}) == ""

    def test_basic_replacement(self):
        out = apply_pronunciations("Šel jsem do München.", {"München": "Mnichova"})
        assert out == "Šel jsem do Mnichova."

    def test_word_boundary_respects(self):
        # "Mün" must not match inside "München" — word boundary.
        out = apply_pronunciations("München", {"Mün": "WRONG"})
        assert out == "München"

    def test_word_boundary_works_inside_phrase(self):
        out = apply_pronunciations(
            "Cesta z Mnichova do Berlína.",
            {"Mnichova": "Mníchova"},
        )
        assert "Mníchova" in out
        assert "Mnichova" not in out

    def test_longest_match_wins(self):
        # "New York" must match before "New" — otherwise we'd substitute
        # "New" first and leave " York" alone.
        out = apply_pronunciations(
            "I love New York.",
            {"New": "Nový", "New York": "Nový Jork"},
        )
        assert "Nový Jork" in out
        assert "Nový York" not in out

    def test_unicode_diacritics_match(self):
        out = apply_pronunciations(
            "Říká Žofie zítra.",
            {"Žofie": "Sofie"},
        )
        assert "Sofie" in out

    def test_regex_special_chars_escaped(self):
        # Dots in source must NOT be regex wildcards.
        out = apply_pronunciations(
            "Dr. Novák přišel a R. Novák odešel.",
            {"Dr.": "Doktor"},
        )
        assert "Doktor Novák" in out
        assert "R. Novák" in out  # "R." must NOT match "Dr." pattern

    def test_multi_word_source(self):
        out = apply_pronunciations(
            "Skleněný muž je tady.",
            {"Skleněný muž": "muž ze skla"},
        )
        assert out == "muž ze skla je tady."

    def test_case_sensitive(self):
        # v1 ships case-sensitive matching — users add explicit variants.
        out = apply_pronunciations(
            "München and münchen.",
            {"München": "Mnichov"},
        )
        assert out == "Mnichov and münchen."


# ----------------------------------------------------------------------------
# Storage round-trip
# ----------------------------------------------------------------------------


class TestStorageRoundTrip:
    def test_empty_when_no_file(self, tmp_path: Path):
        assert load_pronunciations(tmp_path) == {}

    def test_save_then_load(self, tmp_path: Path):
        save_pronunciations(tmp_path, {"München": "Mnichov", "Žofie": "Sofie"})
        loaded = load_pronunciations(tmp_path)
        assert loaded == {"München": "Mnichov", "Žofie": "Sofie"}

    def test_save_empty_deletes_file(self, tmp_path: Path):
        save_pronunciations(tmp_path, {"x": "y"})
        assert (tmp_path / PRONUNCIATIONS_FILENAME).exists()
        save_pronunciations(tmp_path, {})
        assert not (tmp_path / PRONUNCIATIONS_FILENAME).exists()

    def test_load_corrupt_returns_empty(self, tmp_path: Path):
        (tmp_path / PRONUNCIATIONS_FILENAME).write_text("not json", encoding="utf-8")
        assert load_pronunciations(tmp_path) == {}

    def test_load_non_dict_returns_empty(self, tmp_path: Path):
        (tmp_path / PRONUNCIATIONS_FILENAME).write_text("[1, 2, 3]", encoding="utf-8")
        assert load_pronunciations(tmp_path) == {}

    def test_load_filters_empty_keys(self, tmp_path: Path):
        (tmp_path / PRONUNCIATIONS_FILENAME).write_text(
            json.dumps({"valid": "ok", "": "ignored", "  ": "also ignored"}),
            encoding="utf-8",
        )
        loaded = load_pronunciations(tmp_path)
        assert loaded == {"valid": "ok"}

    def test_save_strips_empty_keys(self, tmp_path: Path):
        save_pronunciations(tmp_path, {"valid": "ok", "": "junk"})
        loaded = load_pronunciations(tmp_path)
        assert loaded == {"valid": "ok"}


# ----------------------------------------------------------------------------
# Signature
# ----------------------------------------------------------------------------


class TestSignature:
    def test_empty_is_constant(self):
        assert signature({}) == signature({})
        assert signature({}) == "0" * 16

    def test_changes_with_value(self):
        a = signature({"München": "Mnichov"})
        b = signature({"München": "Mníchov"})  # 1-char diff
        assert a != b

    def test_changes_with_key(self):
        a = signature({"München": "Mnichov"})
        b = signature({"Munich": "Mnichov"})
        assert a != b

    def test_order_independent(self):
        a = signature({"a": "1", "b": "2"})
        b = signature({"b": "2", "a": "1"})
        assert a == b

    def test_returns_16_hex_chars(self):
        s = signature({"x": "y"})
        assert len(s) == 16
        assert all(c in "0123456789abcdef" for c in s)


# ----------------------------------------------------------------------------
# Endpoints via TestClient
# ----------------------------------------------------------------------------


def _make_project(library_root: Path, name: str = "PronTest") -> str:
    from audiomat.project import Project
    from audiomat.voice import Voice

    voices_root = library_root / "voices"
    projects_root = library_root / "projects"
    voices_root.mkdir(parents=True, exist_ok=True)
    projects_root.mkdir(parents=True, exist_ok=True)

    voice_dir = voices_root / "V"
    voice_dir.mkdir()
    (voice_dir / "voice.wav").write_bytes(b"\x00" * 1024)
    (voice_dir / "voice.txt").write_text("transcript", encoding="utf-8")
    (voice_dir / "meta.json").write_text(
        '{"name": "V", "name_slug": "V", "duration_s": 1.0, '
        '"sample_rate": 24000, "channels": 1, "transcript_chars": 10, '
        '"notes": "", "created": "2026-01-01T00:00:00Z"}',
        encoding="utf-8",
    )

    book_src = library_root / "_book.txt"
    book_src.write_text("Some text.", encoding="utf-8")
    proj = Project.create(
        name=name, book_src=book_src,
        voice_name="V", voice_slug="V", book_meta={"language": "cs"},
    )
    return proj.name_slug


def _client(isolated_library: Path):
    import importlib
    import audiomat.api
    importlib.reload(audiomat.api)
    from fastapi.testclient import TestClient
    return TestClient(audiomat.api.app)


class TestPronunciationEndpoints:
    def test_get_empty_initially(self, isolated_library: Path):
        slug = _make_project(isolated_library)
        c = _client(isolated_library)
        r = c.get(f"/api/projects/{slug}/pronunciations")
        assert r.status_code == 200
        assert r.json() == {}

    def test_put_then_get_round_trip(self, isolated_library: Path):
        slug = _make_project(isolated_library)
        c = _client(isolated_library)
        payload = {"München": "Mnichov", "Žofie": "Sofie"}
        r = c.put(f"/api/projects/{slug}/pronunciations", json=payload)
        assert r.status_code == 200
        assert r.json() == payload
        assert c.get(f"/api/projects/{slug}/pronunciations").json() == payload

    def test_put_empty_clears(self, isolated_library: Path):
        slug = _make_project(isolated_library)
        c = _client(isolated_library)
        c.put(f"/api/projects/{slug}/pronunciations", json={"a": "b"})
        c.put(f"/api/projects/{slug}/pronunciations", json={})
        assert c.get(f"/api/projects/{slug}/pronunciations").json() == {}

    def test_put_rejects_non_string_value(self, isolated_library: Path):
        slug = _make_project(isolated_library)
        c = _client(isolated_library)
        r = c.put(f"/api/projects/{slug}/pronunciations",
                   json={"x": 42})        # 42 is int, not str
        assert r.status_code == 400

    def test_get_404_on_unknown_project(self, isolated_library: Path):
        c = _client(isolated_library)
        r = c.get("/api/projects/nonexistent/pronunciations")
        assert r.status_code == 404
