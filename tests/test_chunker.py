"""Tests for audiomat.chunker — sentence → chunk batching."""
from __future__ import annotations

from audiomat.chunker import make_chunks, split_long_sentence


class TestSplitLongSentence:
    def test_short_sentence_passes_through(self):
        s = "Krátká věta."
        assert split_long_sentence(s) == [s]

    def test_long_sentence_split_on_commas(self):
        s = (
            "Toto je opravdu velmi dlouhá věta, která má spoustu klauzulí, "
            "oddělených čárkami, a měla by se rozdělit na menší kousky podle "
            "čárek, protože překračuje limit naprosto jasně."
        )
        # Lower max_chars to force the splitter to actually split.
        pieces = split_long_sentence(s, max_chars=80)
        assert len(pieces) >= 2
        for p in pieces:
            assert len(p) <= 80 or "," not in p, (
                f"piece exceeds max_chars and has no comma to split: {p!r}"
            )

    def test_semicolon_treated_as_comma(self):
        s = (
            "Krátká část; další část; třetí část; čtvrtá část; pátá část; "
            "šestá část; sedmá část; osmá část; devátá část; desátá část."
        )
        pieces = split_long_sentence(s, max_chars=80)
        assert len(pieces) >= 2

    def test_no_split_when_unbreakable(self):
        # No commas/semicolons → cannot split → return as-is even if long.
        s = "x" * 250
        assert split_long_sentence(s, max_chars=200) == [s]


class TestMakeChunks:
    def test_empty_input(self):
        assert make_chunks([]) == []

    def test_blank_sentences_filtered(self):
        assert make_chunks(["", "  ", "\n"]) == []

    def test_short_sentences_merged(self):
        sentences = ["Ahoj.", "Jak se máš?", "Dobře."]
        chunks = make_chunks(sentences, min_chars=90, max_chars=200)
        # Three short sentences (5+12+7 = 24 chars total) merge into 1 chunk.
        assert len(chunks) == 1
        assert "Ahoj" in chunks[0]
        assert "Dobře" in chunks[0]

    def test_chunk_size_respected(self):
        # Many medium sentences should produce multiple chunks all ≤ max_chars.
        sentence = "Toto je věta dlouhá přibližně padesát znaků celkem."
        sentences = [sentence] * 20
        chunks = make_chunks(sentences, min_chars=90, max_chars=200)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c) <= 200, f"chunk exceeds max: {len(c)} chars"

    def test_long_sentence_pre_split(self):
        # One huge sentence with commas → should be split BEFORE merging.
        long_s = (
            "Velmi dlouhá věta, plná čárek, která pokračuje a pokračuje, "
            "klauzule za klauzulí, dokud nedosáhne mnoha set znaků, čímž "
            "donutí chunker, aby ji rozdělil na menší smysluplné kusy."
        )
        chunks = make_chunks([long_s], min_chars=90, max_chars=150)
        assert len(chunks) >= 2

    def test_short_final_chunk_allowed(self):
        # End-of-block fragments (e.g. "Pondělí") must pass through even
        # below min_chars — we don't pad.
        chunks = make_chunks(["Pondělí"], min_chars=90, max_chars=200)
        assert chunks == ["Pondělí"]
