"""Tests for audiomat.headers — pause injection + marker stripping."""
from __future__ import annotations

from audiomat.headers import (
    inject_header_pause,
    prepare_for_tts,
    strip_markers,
)


class TestInjectHeaderPause:
    def test_character_header_injects_pause(self):
        out = inject_header_pause(
            ["Hill Taxík zastavil před domem."],
            section_headers=("Hill",),
        )
        assert out[0] == "Hill[pause][break]Taxík zastavil před domem."

    def test_prefix_collision_not_matched(self):
        # "Hilltop" must not be matched by "Hill" — next char must be a space.
        out = inject_header_pause(
            ["Hilltop ošetřovatelka řekla."],
            section_headers=("Hill",),
        )
        assert out[0] == "Hilltop ošetřovatelka řekla."

    def test_time_marker_before_lety(self):
        out = inject_header_pause(["Před sedmnácti lety se to stalo."])
        assert "[pause][break]" in out[0]
        assert out[0].startswith("Před sedmnácti lety[pause][break]")

    def test_season_year_literary_form(self):
        out = inject_header_pause(["Zima dva tisíce devatenáct rok zlomu."])
        assert "[pause][break]" in out[0]

    def test_season_year_numeric_form(self):
        out = inject_header_pause(["Podzim 1973 byl mokrý a chladný."])
        assert out[0].startswith("Podzim 1973[pause][break]")

    def test_no_match_passes_through(self):
        s = "Obyčejný odstavec bez hlavičky."
        out = inject_header_pause([s])
        assert out == [s]

    def test_already_paused_left_alone(self):
        out = inject_header_pause(
            ["[break]Už mám pauzu na začátku."],
            section_headers=("Skleněný muž",),
        )
        assert out[0] == "[break]Už mám pauzu na začátku."

    def test_non_czech_lang_pass_through(self):
        s = "Hill arrived at the door."
        out = inject_header_pause([s], lang="en", section_headers=("Hill",))
        assert out == [s]

    def test_returns_new_list(self):
        sentences = ["Pondělí ráno."]
        out = inject_header_pause(sentences, section_headers=("Pondělí",))
        assert out is not sentences

    def test_empty_input(self):
        assert inject_header_pause([]) == []


class TestStripMarkers:
    def test_break_to_period(self):
        assert strip_markers("Hello[break]world") == "Hello. world"

    def test_pause_to_period(self):
        assert strip_markers("Hello[pause]world") == "Hello. world"

    def test_no_double_period(self):
        # "Hello.[break]world" should not yield "Hello.. world" — collapse.
        out = strip_markers("Hello.[break]world")
        assert ".." not in out

    def test_floating_period_collapsed(self):
        # " . " should become just "." (no leading space).
        out = strip_markers("word [break] next")
        assert " ." not in out
        assert out == "word. next"

    def test_emphasis_marker(self):
        assert strip_markers("really[emphasis]important") == "really. important"


class TestPrepareForTts:
    def test_full_pipeline_strips_and_expands(self):
        out = prepare_for_tts("V roce 1959[break]bylo všechno jiné.")
        # 1959 → spelled out; [break] → ". "
        assert "1959" not in out
        assert "[break]" not in out

    def test_english_only_strips(self):
        # English numbers also expand via num2words 'en'.
        out = prepare_for_tts("In 2020 things happened.", lang="en")
        assert "2020" not in out
        assert "twenty" in out.lower()
