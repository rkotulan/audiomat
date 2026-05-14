"""Tests for per-chapter text override storage + merge.

Covers:
  * audiomat.overrides round-trip (save / load / has / delete / list)
  * apply_overrides merges file content into a Block list, leaves keep
    + source_id intact, re-runs the sentence splitter
  * Empty / blank text rejected at save
  * overridden_indices skips malformed filenames
"""
from __future__ import annotations

from pathlib import Path

import pytest

from audiomat.epub import Block
from audiomat.overrides import (
    apply_overrides,
    delete_override,
    has_override,
    load_override,
    overridden_indices,
    overrides_dir,
    save_override,
)


def _block(text: str, *, keep: bool = True, source_id: str | None = None) -> Block:
    from audiomat.epub import split_sentences
    return Block(text=text, sentences=split_sentences(text), keep=keep, source_id=source_id)


class TestOverrideRoundTrip:
    def test_no_override_initially(self, tmp_path: Path):
        assert not has_override(tmp_path, 0)
        assert load_override(tmp_path, 0) is None

    def test_save_then_load(self, tmp_path: Path):
        save_override(tmp_path, 3, "Hello world.")
        assert has_override(tmp_path, 3)
        assert load_override(tmp_path, 3) == "Hello world."

    def test_save_creates_overrides_dir(self, tmp_path: Path):
        assert not overrides_dir(tmp_path).exists()
        save_override(tmp_path, 0, "x")
        assert overrides_dir(tmp_path).exists()

    def test_save_overwrites_existing(self, tmp_path: Path):
        save_override(tmp_path, 0, "first")
        save_override(tmp_path, 0, "second")
        assert load_override(tmp_path, 0) == "second"

    def test_save_rejects_empty(self, tmp_path: Path):
        with pytest.raises(ValueError):
            save_override(tmp_path, 0, "")
        with pytest.raises(ValueError):
            save_override(tmp_path, 0, "   \n  ")
        # File must not be created on rejection
        assert not has_override(tmp_path, 0)

    def test_delete_returns_true_when_present(self, tmp_path: Path):
        save_override(tmp_path, 0, "x")
        assert delete_override(tmp_path, 0) is True
        assert not has_override(tmp_path, 0)

    def test_delete_returns_false_when_absent(self, tmp_path: Path):
        assert delete_override(tmp_path, 99) is False

    def test_save_rejects_negative_index(self, tmp_path: Path):
        with pytest.raises(ValueError):
            save_override(tmp_path, -1, "x")


class TestOverriddenIndices:
    def test_empty_when_no_dir(self, tmp_path: Path):
        assert overridden_indices(tmp_path) == set()

    def test_empty_when_dir_empty(self, tmp_path: Path):
        overrides_dir(tmp_path).mkdir()
        assert overridden_indices(tmp_path) == set()

    def test_collects_all_indices(self, tmp_path: Path):
        save_override(tmp_path, 0, "a")
        save_override(tmp_path, 5, "b")
        save_override(tmp_path, 42, "c")
        assert overridden_indices(tmp_path) == {0, 5, 42}

    def test_ignores_malformed_filenames(self, tmp_path: Path):
        d = overrides_dir(tmp_path)
        d.mkdir()
        save_override(tmp_path, 1, "ok")
        # Drop unrelated junk into the dir
        (d / "block_NOT_A_NUMBER.txt").write_text("x", encoding="utf-8")
        (d / "random.txt").write_text("x", encoding="utf-8")
        (d / "notes.md").write_text("x", encoding="utf-8")
        assert overridden_indices(tmp_path) == {1}


class TestApplyOverrides:
    def test_no_overrides_returns_input_unchanged(self, tmp_path: Path):
        blocks = [_block("First."), _block("Second.")]
        out = apply_overrides(blocks, tmp_path)
        assert out == blocks

    def test_overrides_dir_missing(self, tmp_path: Path):
        # apply_overrides must not raise if overrides/ dir doesn't exist.
        blocks = [_block("Whatever.")]
        assert apply_overrides(blocks, tmp_path) == blocks

    def test_replaces_overridden_block_text(self, tmp_path: Path):
        blocks = [_block("Original first."), _block("Second untouched.")]
        save_override(tmp_path, 0, "Edited first sentence. And second.")
        out = apply_overrides(blocks, tmp_path)
        assert out[0].text == "Edited first sentence. And second."
        assert out[1].text == "Second untouched."

    def test_re_splits_sentences(self, tmp_path: Path):
        blocks = [_block("One sentence.")]
        save_override(tmp_path, 0, "First. Second. Third.")
        out = apply_overrides(blocks, tmp_path)
        assert len(out[0].sentences) == 3

    def test_preserves_keep_and_source_id(self, tmp_path: Path):
        blocks = [_block("Original.", keep=False, source_id="spine_1")]
        save_override(tmp_path, 0, "Override.")
        out = apply_overrides(blocks, tmp_path)
        assert out[0].keep is False
        assert out[0].source_id == "spine_1"

    def test_returns_new_list(self, tmp_path: Path):
        blocks = [_block("x")]
        save_override(tmp_path, 0, "y")
        out = apply_overrides(blocks, tmp_path)
        assert out is not blocks


# ----------------------------------------------------------------------------
# End-to-end via the FastAPI router
# ----------------------------------------------------------------------------


def _make_project(library_root: Path, name: str = "TestBook") -> str:
    """Create a minimal TXT-backed project so the chapter routes have
    something to operate on without needing a real EPUB."""
    from audiomat.project import Project
    from audiomat.voice import Voice

    voices_root = library_root / "voices"
    projects_root = library_root / "projects"
    voices_root.mkdir(parents=True, exist_ok=True)
    projects_root.mkdir(parents=True, exist_ok=True)

    # Create a stub voice (path setup only — not used for TTS in tests).
    voice_dir = voices_root / "TestVoice"
    voice_dir.mkdir()
    (voice_dir / "voice.wav").write_bytes(b"\x00" * 1024)
    (voice_dir / "voice.txt").write_text("test transcript", encoding="utf-8")
    (voice_dir / "meta.json").write_text(
        '{"name": "TestVoice", "name_slug": "TestVoice", "duration_s": 1.0, '
        '"sample_rate": 24000, "channels": 1, "transcript_chars": 15, '
        '"notes": "", "created": "2026-01-01T00:00:00Z"}',
        encoding="utf-8",
    )

    book_src = library_root / "_src.txt"
    book_src.write_text(
        "First paragraph with several sentences. Second sentence here. "
        "Third one too.",
        encoding="utf-8",
    )

    proj = Project.create(
        name=name,
        book_src=book_src,
        voice_name="TestVoice",
        voice_slug="TestVoice",
        book_meta={"language": "cs"},
    )
    return proj.name_slug


def _client(isolated_library: Path):
    import importlib
    import audiomat.api
    importlib.reload(audiomat.api)
    from fastapi.testclient import TestClient
    return TestClient(audiomat.api.app)


class TestChapterTextEndpoints:
    def test_get_returns_original_text(self, isolated_library: Path):
        slug = _make_project(isolated_library)
        c = _client(isolated_library)
        chapters = c.get(f"/api/projects/{slug}/chapters").json()
        stem = chapters["chapters"][0]["stem"]

        r = c.get(f"/api/projects/{slug}/chapters/{stem}/text")
        assert r.status_code == 200
        body = r.json()
        assert body["has_override"] is False
        assert "First paragraph" in body["text"]
        assert body["text"] == body["original_text"]
        assert body["estimated_chunks"] >= 1

    def test_put_then_get_returns_override(self, isolated_library: Path):
        slug = _make_project(isolated_library)
        c = _client(isolated_library)
        stem = c.get(f"/api/projects/{slug}/chapters").json()["chapters"][0]["stem"]

        r = c.put(
            f"/api/projects/{slug}/chapters/{stem}/text",
            json={"text": "My edited text."},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["has_override"] is True
        assert body["text"] == "My edited text."
        assert "First paragraph" in body["original_text"]

    def test_list_chapters_reflects_has_override(self, isolated_library: Path):
        slug = _make_project(isolated_library)
        c = _client(isolated_library)
        stem = c.get(f"/api/projects/{slug}/chapters").json()["chapters"][0]["stem"]

        c.put(f"/api/projects/{slug}/chapters/{stem}/text",
               json={"text": "Override."})

        chapters = c.get(f"/api/projects/{slug}/chapters").json()["chapters"]
        # First (and only) chapter should now report has_override True.
        assert chapters[0]["has_override"] is True

    def test_delete_reverts_to_original(self, isolated_library: Path):
        slug = _make_project(isolated_library)
        c = _client(isolated_library)
        stem = c.get(f"/api/projects/{slug}/chapters").json()["chapters"][0]["stem"]
        c.put(f"/api/projects/{slug}/chapters/{stem}/text",
               json={"text": "Override."})

        r = c.delete(f"/api/projects/{slug}/chapters/{stem}/text")
        assert r.status_code == 200
        body = r.json()
        assert body["has_override"] is False
        assert "First paragraph" in body["text"]

    def test_delete_404_when_no_override(self, isolated_library: Path):
        slug = _make_project(isolated_library)
        c = _client(isolated_library)
        stem = c.get(f"/api/projects/{slug}/chapters").json()["chapters"][0]["stem"]

        r = c.delete(f"/api/projects/{slug}/chapters/{stem}/text")
        assert r.status_code == 404

    def test_put_rejects_empty_text(self, isolated_library: Path):
        slug = _make_project(isolated_library)
        c = _client(isolated_library)
        stem = c.get(f"/api/projects/{slug}/chapters").json()["chapters"][0]["stem"]

        r = c.put(
            f"/api/projects/{slug}/chapters/{stem}/text",
            json={"text": "   "},
        )
        assert r.status_code == 400

    def test_get_404_on_unknown_stem(self, isolated_library: Path):
        slug = _make_project(isolated_library)
        c = _client(isolated_library)
        r = c.get(f"/api/projects/{slug}/chapters/999_nonexistent/text")
        assert r.status_code == 404

    def test_path_traversal_rejected(self, isolated_library: Path):
        slug = _make_project(isolated_library)
        c = _client(isolated_library)
        # FastAPI rejects raw / in path params before our handler runs;
        # verify a ".." attempt also fails (handled by _validate_stem).
        r = c.get(f"/api/projects/{slug}/chapters/..foo/text")
        assert r.status_code == 400
