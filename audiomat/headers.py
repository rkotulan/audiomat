"""Section header pause injection.

In some books (especially Czech literary fiction) chapters open with a
section header — a POV character name, a time marker — that the source
text glues directly to the body without any visible pause. Without an
explicit pause cue the TTS model breezes through it: "Skleněný muž
probouzí se" instead of "Skleněný muž — pause — probouzí se".

We fix this by injecting ``[pause][break]`` markers right after the header
in the first sentence. The renderer then strips/translates these markers
to a sentence-end cue that OmniVoice produces an audible pause for.

Currently Czech-only (``lang="cs"``); other languages return the input
unchanged. Section header detection is two-pronged:

1. **Time markers** — generic regex covering the common Czech idioms
   ("Před X lety", "O dva dny později", "Zima dva tisíce devatenáct", …).
2. **POV / character names** — book-specific. Because there's no way to
   detect proper-noun POV markers automatically, the caller passes a tuple
   of strings to match prefix-wise. For Skleněný muž the list is
   ``("Skleněný muž", "Askerová", "Hellman", "Hill")``.
"""
from __future__ import annotations

import re


# Czech-only for now. Pattern matches the *whole* header phrase at the
# start of the first sentence, must be followed by whitespace, ``[`` (a
# marker), or end-of-string — never followed by a letter (would be a
# false match like "lety" inside "letýtko" if that were a word).
TIME_HEADER_RE_CS = re.compile(
    r"^("
    r"Před\s+(?:\S+\s+)+lety"
    r"|O\s+(?:\S+\s+)+(?:dní|dny|den|týdny|týdnů|let|měsíc|měsíce|měsíců)\s+(?:později|nato)"
    r"|Týden\s+poté"
    # Season-year header. Two forms supported:
    # 1. Literary spelled-out: "Zima dva tisíce devatenáct"
    # 2. Raw 4-digit year:     "Podzim 1973", "Léto 1968"
    r"|(?:Zima|Léto|Jaro|Podzim)\s+(?:dva\s+tisíce\s+\S+?|\d{4})"
    r")(?=\s|\[|$)",
    re.UNICODE,
)


def inject_header_pause(
    sentences: list[str],
    lang: str = "cs",
    section_headers: tuple[str, ...] = (),
) -> list[str]:
    """Return a new list of sentences where the first sentence's section
    header (if any) is followed by ``[pause][break]``.

    Args:
        sentences: original sentence list. Not mutated.
        lang: ISO 639-1 code. Only ``"cs"`` triggers any change today.
        section_headers: tuple of POV / character header phrases to match
            prefix-wise on the first sentence. Order matters — longer
            entries should come first to avoid prefix collisions
            ("Hill" must not match "Hilltop"). For each entry, the next
            char after the header must be a space (not a letter).

    Returns:
        New list with possibly one modified first sentence. Always returns
        a new list — never the same instance as ``sentences``.
    """
    if lang != "cs":
        return list(sentences)
    if not sentences:
        return list(sentences)
    first = (sentences[0] or "").lstrip()
    if not first:
        return list(sentences)
    if first.startswith(("[pause]", "[break]")):
        return list(sentences)

    new_first: str | None = None

    # 1) Character / POV headers
    for h in section_headers:
        if first.startswith(h):
            rest = first[len(h):]
            if rest.startswith(" "):
                body = rest[1:]
                if not body.startswith(("[pause]", "[break]")):
                    new_first = f"{h}[pause][break]{body}"
                break  # found exact-match header
            elif rest.startswith(("[pause]", "[break]")):
                break  # already cued, leave alone
            # else: prefix collision, try next entry

    # 2) Time-marker headers
    if new_first is None:
        m = TIME_HEADER_RE_CS.match(first)
        if m:
            head = first[:m.end()]
            body = first[m.end():].lstrip()
            if not body.startswith(("[pause]", "[break]")):
                new_first = f"{head}[pause][break]{body}"

    if new_first is None:
        return list(sentences)
    return [new_first] + list(sentences[1:])


# Marker translation for backends that don't speak fish-speech tags.
# OmniVoice doesn't parse [break]/[pause]/[emphasis]/[laughing] — translates
# them to ". " for sentence-end cues. Used by tts.py before generate().
_MARKER_RE = re.compile(r"\[(?:break|pause|emphasis|laughing)\]")


def strip_markers(text: str) -> str:
    """Replace ``[break]``/``[pause]``/``[emphasis]``/``[laughing]`` markers
    with ``". "`` (sentence-end cue), then collapse whitespace and any
    resulting double-period or floating-period sequences. OmniVoice /
    Chatterbox / XTTS-v2 all take the resulting text well.
    """
    out = _MARKER_RE.sub(". ", text)
    out = re.sub(r"\s+", " ", out)
    # When a marker was preceded by whitespace (e.g. an HTML newline
    # between a heading and the next paragraph), the substitution leaves
    # " . " — drop the leading space so the model sees "word." not
    # "word .". Real prose never has " ." so this is unambiguous.
    out = re.sub(r"\s+\.", ".", out)
    out = re.sub(r"\.\s*\.", ".", out)
    return out.strip()


def prepare_for_tts(text: str, lang: str = "cs") -> str:
    """Full pre-TTS prep: strip fish-speech markers + expand numbers to
    words. Use this everywhere a chunk goes to the model — it's the single
    canonical text-cleaning step in the pipeline.

    Number expansion is critical for natural pronunciation: TTS models
    typically read raw digits as digit-by-digit ("nineteen-fifty-nine" →
    "one nine five nine") which sounds robotic. ``audiomat.num2text``
    converts ``"1959"`` → ``"tisíc devět set padesát devět"`` (CZ)
    via ``num2words`` + Czech normalizations.
    """
    from audiomat.num2text import expand_numbers
    return expand_numbers(strip_markers(text), lang=lang)


if __name__ == "__main__":
    # Smoke test — `python -m audiomat.headers`
    samples = [
        # (input first sentence, section_headers, expected change?)
        ("Skleněný muž probouzí se neochotně.", ("Skleněný muž", "Hill"), True),
        ("Hill Taxík zastavil před domem.",      ("Skleněný muž", "Hill"), True),
        ("Hilltop ošetřovatelka řekla.",         ("Skleněný muž", "Hill"), False),  # prefix collision
        ("Před sedmnácti lety se to stalo.",     (),                       True),
        ("Zima dva tisíce devatenáct rok zlomu.",(),                       True),
        ("Pondělí ráno bylo chladné.",            ("Pondělí",),            True),
        ("[break]Už mám pauzu na začátku.",      ("Skleněný muž",),        False),
        ("Obyčejný odstavec bez hlavičky.",       ("Skleněný muž",),        False),
    ]
    for text, headers, expect_change in samples:
        out = inject_header_pause([text], section_headers=headers)
        changed = out[0] != text
        ok = "OK " if changed == expect_change else "FAIL"
        print(f"{ok} {changed!s:5s} | {text!r:60s} -> {out[0]!r}")

    print("\n--- strip_markers ---")
    print(strip_markers("Hello[break]world. [pause]again"))
