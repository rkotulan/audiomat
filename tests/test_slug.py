"""Tests for audiomat.slug — Czech-aware Unicode → ASCII slugifier."""
from __future__ import annotations

import pytest

from audiomat.slug import chapter_stem, slugify


class TestSlugify:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Skleněný muž", "Skleneny_muz"),
            ("Lucie Ježková", "Lucie_Jezkova"),
            ("Rezavý les v1", "Rezavy_les_v1"),
            ("Pondělí ráno", "Pondeli_rano"),
            # Other Latin diacritics handled via NFKD fallback.
            ("ÄLLA-MÅ_läs", "ALLA_MA_las"),
        ],
    )
    def test_czech_and_latin(self, raw, expected):
        assert slugify(raw) == expected

    def test_empty_returns_untitled(self):
        assert slugify("") == "untitled"

    def test_only_punctuation_returns_untitled(self):
        assert slugify("???!@#$%^&*()") == "untitled"
        assert slugify("   ???   ") == "untitled"

    def test_truncated_at_max_len(self):
        long = "Skleněný" * 20
        out = slugify(long, max_len=30)
        assert len(out) <= 30
        assert not out.endswith("_"), "trailing underscore after truncation"

    def test_idempotent_on_safe_input(self):
        s = "Already_clean_123"
        assert slugify(s) == s
        assert slugify(slugify(s)) == s

    def test_no_underscore_at_start_or_end(self):
        out = slugify("___ Skleněný ___")
        assert not out.startswith("_")
        assert not out.endswith("_")


class TestChapterStem:
    def test_stops_at_break_marker(self):
        # Section headers use [break] / [pause] to separate the leading
        # header phrase from the body.
        s = "Zima dva tisíce devatenáct[break]Co to sakra bylo?"
        out = chapter_stem(s)
        assert out == "Zima_dva_tisice_devatenact"
        assert "Co" not in out

    def test_stops_at_pause_marker(self):
        s = "Hill[pause]Taxík zastavil před domem."
        assert chapter_stem(s) == "Hill"

    def test_no_marker_full_stem(self):
        s = "Pondělí"
        assert chapter_stem(s) == "Pondeli"

    def test_respects_max_len(self):
        s = "Velmi dlouhý nadpis kapitoly bez markeru který by měl být zkrácen"
        out = chapter_stem(s, max_len=20)
        assert len(out) <= 20
