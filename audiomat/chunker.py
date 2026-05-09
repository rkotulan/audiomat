"""Sentence → chunk batching for TTS.

The renderer feeds OmniVoice chunks of 90–200 chars at a time. Three reasons
for that range:

* Below ~90 chars OmniVoice often produces an "end-of-utterance artifact"
  (audible click / breath) inherited from the original Fish Speech S2 issue
  that shaped this number on the s2.cpp stack. Empirically still present on
  OmniVoice though softer.
* Above ~200 chars the chunk's audio crosses 12-13 s, which is fine on its
  own but pushes us towards OmniVoice's internal 30 s ``audio_chunk_threshold``
  where the model itself starts re-chunking — and that splits at less
  natural places than our sentence-aware logic.
* 90–200 lands every chunk on a sentence boundary in the books we tested
  (Skleněný muž, 25 332 chars across 149 chunks for chapter 1).
"""
from __future__ import annotations

DEFAULT_MIN_CHARS = 90
DEFAULT_MAX_CHARS = 200


def split_long_sentence(s: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[str]:
    """Break a single over-long sentence on commas (or semicolons treated as
    commas). Returns one or more sub-sentences, each ≤ ``max_chars`` where
    possible. If a comma-piece itself exceeds ``max_chars``, it is kept whole
    rather than split mid-clause.
    """
    if len(s) <= max_chars:
        return [s]
    parts = s.replace(";", ",").split(",")
    pieces: list[str] = []
    cur = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        cand = (cur + ", " + p).strip(", ") if cur else p
        if len(cand) > max_chars and cur:
            pieces.append(cur)
            cur = p
        else:
            cur = cand
    if cur:
        pieces.append(cur)
    return pieces


def make_chunks(
    sentences: list[str],
    min_chars: int = DEFAULT_MIN_CHARS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[str]:
    """Greedily merge consecutive sentences into chunks ≤ ``max_chars``.

    Pre-processes by splitting any single sentence longer than ``max_chars``
    via :func:`split_long_sentence`. Then walks the resulting sequence and
    accumulates sentences into a buffer, flushing whenever adding the next
    sentence would exceed the cap.

    The ``min_chars`` parameter is informational — we don't pad short chunks,
    because end-of-block fragments ("Pondělí[pause]" — 14 chars) must be
    allowed through. Callers who want to merge tiny final chunks into the
    previous one can do so explicitly.
    """
    expanded: list[str] = []
    for s in sentences:
        s = (s or "").strip()
        if not s:
            continue
        expanded.extend(split_long_sentence(s, max_chars=max_chars))

    chunks: list[str] = []
    buf = ""
    for s in expanded:
        if not buf:
            buf = s
            continue
        cand = buf + " " + s
        if len(cand) > max_chars:
            chunks.append(buf)
            buf = s
        else:
            buf = cand
    if buf:
        chunks.append(buf)
    return chunks


if __name__ == "__main__":
    # Smoke test — `python -m audiomat.chunker`
    sample = [
        "Krátká věta.",
        "Druhá taky krátká, ale s čárkou.",
        "Třetí věta je o trochu delší a měla by se s tou předchozí spojit.",
        "Čtvrtá věta je extrémně dlouhá, naprosto ohromující ve svém rozsahu, "
        "obsahuje spousty čárek a zaslouží si rozdělení, jinak by celý chunk "
        "měl přes dvě stě znaků a to už OmniVoice nezvládá optimálně.",
        "Pátá zase krátká.",
    ]
    chunks = make_chunks(sample)
    for i, c in enumerate(chunks):
        print(f"chunk {i:02d} ({len(c):3d}): {c}")
