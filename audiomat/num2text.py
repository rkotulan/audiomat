"""Number-to-text expansion for the TTS pipeline.

Wraps `num2words` and post-processes Czech output so it matches the style
that produced good Skleněný muž S2-Pro / OmniVoice renders ("devět set"
with a space, not "devětset").

Used by :func:`audiomat.headers.prepare_for_tts` — every chunk passes
through here before reaching the TTS model.
"""
from __future__ import annotations

import re


# Standalone integer tokens. Word boundaries on both sides so we don't
# touch numbers inside identifiers ("ID2126848814" stays as-is) or hex
# colour codes etc. Up to 9 digits → years and small counts; bigger
# numbers stay literal so we don't synth a 30-second number-name salvo.
_NUMBER_RE = re.compile(r'(?<!\w)\d{1,9}(?!\w)')

# num2words 'cs' produces 'devětset', 'dvěstě', 'třista', … glued together.
# CLAUDE.md style sample uses spaced forms ("devět set", "dvě stě"). The
# OmniVoice / S2-Pro models pronounce both; spaced just sounds more natural
# in CZ literary fiction.
_CZ_NORMALIZATIONS = (
    (re.compile(r'\bdvěstě\b'), 'dvě stě'),
    (re.compile(r'\btřista\b'), 'tři sta'),
    (re.compile(r'\bčtyřista\b'), 'čtyři sta'),
    (re.compile(r'(\w*)(devět|osm|sedm|šest|pět)set\b'),
     lambda m: f"{m.group(1)}{m.group(2)} set"),
)


def normalize_lang(lang: str | None) -> str:
    """Normalize a BCP 47 language tag (``cs-CZ``, ``en-US``) to the
    ISO 639-1 primary subtag (``cs``, ``en``) that ``num2words`` expects.
    Empty / None defaults to ``"cs"``.
    """
    if not lang:
        return "cs"
    return lang.split("-")[0].split("_")[0].lower()


def expand_numbers(text: str, lang: str = "cs") -> str:
    """Replace standalone integers in ``text`` with their spelled-out
    word equivalents using ``num2words``. On unsupported languages
    (``num2words`` knows ~55 — see ``num2words.CONVERTER_CLASSES``)
    the digits are left as-is and the TTS model handles them; typically
    digit-by-digit reading.

    BCP 47 tags from EPUB metadata (``cs-CZ``, ``en-US``) are accepted —
    we strip to the primary subtag.

    Numbers attached to words ("Mgr.5"), inside identifiers, or longer
    than 9 digits (presumably IDs) are left alone.
    """
    try:
        from num2words import num2words
    except ImportError as e:
        raise RuntimeError(
            "num2words not installed — pip install num2words"
        ) from e

    code = normalize_lang(lang)

    def repl(match: re.Match) -> str:
        try:
            return num2words(int(match.group()), lang=code)
        except (ValueError, NotImplementedError):
            return match.group()

    out = _NUMBER_RE.sub(repl, text)

    if code == "cs":
        for pattern, replacement in _CZ_NORMALIZATIONS:
            out = pattern.sub(replacement, out)

    return out


if __name__ == "__main__":
    # Smoke test — `python -m audiomat.num2text`
    samples = [
        "Bylo to v roce 1959 a nikdo to nečekal.",
        "Bylo to v roce 1996, čtrnáctého prosince.",
        "Pět metrů dolů a dvě stě nahoru.",
        "Kapitola 5",
        "ID2126848814 zůstává beze změny.",
        "100 let staré.",
        "Vyžil 35 let.",
    ]
    for s in samples:
        print(f"{s}\n  -> {expand_numbers(s)}\n")
