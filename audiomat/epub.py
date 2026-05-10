"""EPUB → blocks parser.

Walks an EPUB's spine, extracts text from each XHTML chapter, splits into
sentences, and returns a list of :class:`Block` objects compatible with
the rest of the pipeline (chunker / headers / renderer).

We deliberately keep this minimal for v0.1:

* No marker inference (``[break]``/``[pause]`` are not auto-inserted; the
  user can edit the resulting JSON manually if they want manual cues).
* No TOC mapping — chapters come out in spine order. Display titles, if
  needed, are derived from the first line of each block by the caller.
* Sentence splitter is a regex with a small Czech abbreviation block-list.
  Good enough for typical literary fiction; minor mis-splits are absorbed
  by the chunker's max_chars guard.

Heavy-tail languages or technical books with lots of numbered abbreviations
("Obr. 1.", "Tab. 5.") will produce noisier sentences. We'll plug in a
proper sentence boundary detector (``pysbd`` or ``stanza``) in a later
release if it turns out to matter.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Block:
    """One chapter / spine item, post-text-extraction.

    Compatible with the JSON structure used by the existing
    skleneny-muz-tts pipeline (``chapters_json/__saved_Skleněný muž.json``):
    has ``text`` (raw concatenated text) and ``sentences`` (split list).
    """
    text: str
    sentences: list[str]
    keep: bool = True
    source_id: str | None = None        # spine item ID, for debugging


@dataclass
class EpubMetadata:
    title: str | None = None
    author: str | None = None
    language: str | None = None
    publisher: str | None = None
    extras: dict[str, str] = field(default_factory=dict)


# Czech-aware sentence splitter. Splits on . ! ? followed by whitespace
# followed by an uppercase letter (incl. Czech) or an opening quotation mark
# (Czech books use „..." and «...»). Won't split inside abbreviations from
# the block-list below.
_SENTENCE_BREAK = re.compile(
    r'(?<=[.!?])\s+(?=[„«»"(A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ])',
    re.UNICODE,
)

# Czech + English abbreviations that look like sentence ends but aren't.
# Order: longer entries first so partial-overlap protection is greedy.
_PROTECTED_ABBREVS = (
    "Ph.D.", "MSc.", "B.Sc.", "M.Sc.",
    "MUDr.", "JUDr.", "PaedDr.", "PhDr.", "RNDr.", "MgA.",
    "Mgr.", "Ing.", "Bc.", "Dr.",
    "atd.", "apod.", "např.", "tzv.", "tzn.", "popř.", "resp.",
    "tj.", "j.č.", "č.", "č.j.", "obr.", "tab.", "str.",
    "Mr.", "Mrs.", "Ms.", "St.", "vs.", "etc.", "i.e.", "e.g.",
)
# Sentinel for the "." inside a protected abbreviation. ASCII control char
# that won't appear in real book text.
_DOT_SENTINEL = ""


def split_sentences(text: str) -> list[str]:
    """Split a paragraph / block of text into sentences.

    Czech-aware via uppercase-letter / quotation-mark lookahead. Abbreviations
    in :data:`_PROTECTED_ABBREVS` are protected by temporarily replacing the
    internal ``.`` with a sentinel, so that ``"Mgr. Novák přišel."`` stays
    one sentence rather than two.
    """
    if not text or not text.strip():
        return []
    protected = text
    for abbr in _PROTECTED_ABBREVS:
        protected = protected.replace(abbr, abbr.replace(".", _DOT_SENTINEL))
    parts = _SENTENCE_BREAK.split(protected)
    return [p.replace(_DOT_SENTINEL, ".").strip() for p in parts if p.strip()]


def _extract_text(html_bytes: bytes) -> str:
    """Pull readable text out of an XHTML spine item, dropping script / style
    and preserving paragraph-level whitespace.

    Heading tags (``h1``…``h6``) get a ``[pause][break]`` marker appended
    so the downstream pipeline treats them as section headers and the TTS
    model breathes between the chapter title and the body sentence.
    Without this, "Šedá dívka Rezavý les je prastarý…" came out glued —
    the section header and first paragraph were textually adjacent in
    extracted plain text.

    Parsing choice: ``lxml`` (HTML mode) is intentionally used over
    ``lxml-xml`` even though EPUB spine items are technically XHTML.
    HTML mode is more forgiving on real-world EPUBs that contain minor
    markup quirks. The warning that bs4 emits in this case is silenced
    locally so it doesn't flood uvicorn logs.
    """
    import warnings

    from bs4 import BeautifulSoup, NavigableString
    from bs4 import XMLParsedAsHTMLWarning

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(html_bytes, "lxml")
    for tag in soup(("script", "style", "noscript")):
        tag.decompose()
    # Append a marker to every heading. headers.strip_markers() will
    # translate this into a sentence-end cue (". ") before the model
    # sees the text.
    for h in soup.find_all(("h1", "h2", "h3", "h4", "h5", "h6")):
        h.append(NavigableString("[pause][break]"))
    # Replace block-level tags with newlines so paragraph breaks survive
    # ``get_text``. Inline tags (em / strong / a) are kept as plain text.
    for br in soup.find_all("br"):
        br.replace_with("\n")
    text = soup.get_text(separator="\n", strip=True)
    # Collapse runs of blank lines to single newlines, runs of spaces to one.
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def parse_epub(path: Path | str) -> tuple[EpubMetadata, list[Block]]:
    """Read an EPUB file and return (metadata, blocks).

    Each block corresponds to one spine item (typically a chapter).
    Empty / whitespace-only spine items are skipped; the caller decides
    which leading blocks (cover, copyright, TOC) to drop via
    ``Block.keep = False`` based on their length / content heuristics.
    """
    import ebooklib
    from ebooklib import epub

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    book = epub.read_epub(str(path))

    def _meta(ns: str, name: str) -> str | None:
        items = book.get_metadata(ns, name)
        if not items:
            return None
        # ebooklib returns [(value, attrs), ...]
        value = items[0][0]
        return value.strip() if isinstance(value, str) else None

    meta = EpubMetadata(
        title=_meta("DC", "title"),
        author=_meta("DC", "creator"),
        language=_meta("DC", "language"),
        publisher=_meta("DC", "publisher"),
    )

    blocks: list[Block] = []
    for spine_id, _linear in book.spine:
        item = book.get_item_with_id(spine_id)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        text = _extract_text(item.get_content())
        if not text:
            continue
        sentences = split_sentences(text)
        if not sentences:
            continue
        blocks.append(Block(
            text=text,
            sentences=sentences,
            keep=True,
            source_id=spine_id,
        ))

    return meta, blocks


if __name__ == "__main__":
    # Smoke test for the pure splitter — `python -m audiomat.epub`
    samples = [
        # Plain Czech narrative
        "Bylo to v zimě. Sníh padal celou noc. Ráno bylo bílo.",
        # Abbreviation protection
        "Mgr. Novák přišel pozdě. Dr. Svoboda už čekal.",
        # Czech opening quotation mark starts a new sentence
        "Řekla to potichu. „Co tady děláš?\" zeptala se.",
        # Number/ordinal at end (will mis-split — known limitation)
        "Bylo to v roce 1996. Pak začala další éra.",
    ]
    for s in samples:
        print(f"input: {s}")
        for i, sent in enumerate(split_sentences(s), 1):
            print(f"  {i}: {sent}")
        print()
