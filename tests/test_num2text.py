"""Tests for audiomat.num2text — Czech number expansion + lang normalize."""
from __future__ import annotations

import pytest

from audiomat.num2text import expand_numbers, normalize_lang


class TestNormalizeLang:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("cs", "cs"),
            ("cs-CZ", "cs"),
            ("cs_CZ", "cs"),
            ("en-US", "en"),
            ("zh-Hant", "zh"),
            ("EN-GB", "en"),
            ("", "cs"),     # default fallback
            (None, "cs"),
        ],
    )
    def test_normalizes_bcp47_to_iso639(self, raw, expected):
        assert normalize_lang(raw) == expected


class TestExpandNumbersCzech:
    def test_year_in_prose(self):
        out = expand_numbers("Bylo to v roce 1959 a nikdo to nečekal.")
        assert "1959" not in out
        assert "tisíc" in out

    def test_small_count(self):
        out = expand_numbers("Pět let.")
        # No digits — pass through unchanged.
        assert out == "Pět let."

    def test_two_digit(self):
        out = expand_numbers("Vyžil 35 let.")
        assert "35" not in out

    def test_id_inside_word_left_alone(self):
        # "ID2126848814" — number is glued to a letter; should not expand.
        out = expand_numbers("ID2126848814 zůstává beze změny.")
        assert "2126848814" in out

    def test_long_number_left_alone(self):
        # Numbers longer than 9 digits stay as digits (assumed to be IDs).
        out = expand_numbers("Identifikátor 12345678901234.")
        assert "12345678901234" in out

    def test_glued_form_normalized(self):
        # num2words 'cs' produces "devětset" glued; we want "devět set".
        out = expand_numbers("Cena byla 900 korun.")
        assert "devětset" not in out
        assert "devět set" in out

    def test_dvieste_normalized(self):
        out = expand_numbers("Dvě stě 200 korun.")
        # 200 → "dvě stě"
        assert "dvěstě" not in out

    def test_unknown_lang_falls_back_gracefully(self):
        # 'xx' is not a num2words language → numbers stay as digits.
        out = expand_numbers("Year 1959 happened.", lang="xx")
        assert "1959" in out
