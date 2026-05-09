"""Filesystem-safe slugification of human-readable names.

audiomat uses slugs to map a project / voice / chapter display name (which
may contain Czech diacritics, spaces, punctuation, …) into an ASCII
filename component. Slugs are stable: same input → same output.
"""
from __future__ import annotations

import re
import unicodedata


# Explicit Czech-aware transliteration. NFKD-decompose handles most diacritics
# generically (á → a + combining acute), but a few Czech-specific letters
# (especially "ř" and "ů") have idiosyncratic decompositions that map to
# something we don't want, so we hard-code the right ASCII fallback first.
_CZECH_FALLBACK = str.maketrans({
    "á": "a", "č": "c", "ď": "d", "é": "e", "ě": "e", "í": "i", "ň": "n",
    "ó": "o", "ř": "r", "š": "s", "ť": "t", "ú": "u", "ů": "u", "ý": "y",
    "ž": "z",
    "Á": "A", "Č": "C", "Ď": "D", "É": "E", "Ě": "E", "Í": "I", "Ň": "N",
    "Ó": "O", "Ř": "R", "Š": "S", "Ť": "T", "Ú": "U", "Ů": "U", "Ý": "Y",
    "Ž": "Z",
})


def slugify(text: str, max_len: int = 60) -> str:
    """Convert any unicode string to an ASCII filesystem-safe slug.

    - Czech diacritics → bare ASCII (ž→z, ř→r, ů→u, …).
    - Other diacritics stripped via NFKD decomposition.
    - All non-alphanumerics collapsed to single underscores.
    - Truncated at ``max_len``, never ending on an underscore.
    - Empty / all-non-alphanumeric input returns ``"untitled"``.

    Examples:
        >>> slugify("Skleněný muž")
        'Skleneny_muz'
        >>> slugify("Lucie Ježková")
        'Lucie_Jezkova'
        >>> slugify("ÄLLA-MÅ_läs")
        'ALLA_MA_las'
        >>> slugify("   ???   ")
        'untitled'
    """
    if not text:
        return "untitled"
    # Czech-aware first pass (preserves the right ASCII for ě, ř, …)
    text = text.translate(_CZECH_FALLBACK)
    # Strip remaining diacritics on other Latin / Cyrillic letters
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    # Collapse non-alphanumerics
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    if not text:
        return "untitled"
    if len(text) > max_len:
        text = text[:max_len].rstrip("_")
    return text or "untitled"


def chapter_stem(text: str, max_len: int = 30) -> str:
    """Slug for chapter directory names. Stops at the first ``[break]`` or
    ``[pause]`` marker if present — the segment before is the chapter
    "header" (e.g. ``"Zima dva tisíce devatenáct"``, ``"Hill Taxík …"``).

    Used by the renderer to derive ``001_Zima_dva_tisice_devatenact/`` from
    a chapter's leading text.
    """
    text = re.split(r"\[(?:break|pause|emphasis|laughing)\]", text, maxsplit=1)[0]
    return slugify(text, max_len=max_len)


if __name__ == "__main__":
    # Smoke test — `python -m audiomat.slug`
    samples = [
        "Skleněný muž",
        "Lucie Ježková",
        "Zima dva tisíce devatenáct[break]Co to sakra bylo?",
        "Pondělí",
        "  ???  ",
        "ÄLLA-MÅ_läs",
    ]
    for s in samples:
        print(f"{s!r:55s} -> {slugify(s)!r:35s}  chapter: {chapter_stem(s)!r}")
