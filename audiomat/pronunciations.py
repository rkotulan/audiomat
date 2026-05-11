"""Per-project pronunciation dictionary.

Lets the user fix proper-noun pronunciation once and have it applied
across every chunk before TTS, instead of editing every chapter that
mentions the word. Stored at ``<project>/pronunciations.json`` as a
flat ``{source: target}`` map. Empty / missing file = no rewrites.

Application happens in :func:`audiomat.headers.prepare_for_tts` BEFORE
marker stripping and number expansion. Order matters:

  1. ``apply_pronunciations`` — replace source phrases with phonetic /
     localized targets. Runs first so the targets can themselves contain
     digits ("Mnichov 1972" → number-expanded later) and markers.
  2. ``strip_markers`` — translate fish-speech markers to "." cues.
  3. ``expand_numbers`` — num2words on standalone integers.

Matching is **word-boundary, case-sensitive** — long keys win when
multiple keys overlap (e.g. ``"München-City"`` is matched before
``"München"``). Case-insensitive matching would clobber CapitalIzed
variants we want to keep distinct; users who want it can add explicit
lowercase variants.

Cache invalidation is automatic: :meth:`ProjectRenderer._params_signature`
mixes in the SHA-256 of the serialized dict, so any add/edit/remove
flips the per-chunk sig and forces re-synth on the next render.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


PRONUNCIATIONS_FILENAME = "pronunciations.json"


def pronunciations_path(project_dir: Path) -> Path:
    return project_dir / PRONUNCIATIONS_FILENAME


def load_pronunciations(project_dir: Path) -> dict[str, str]:
    """Return the project's pronunciation dict. Empty dict if no file or
    file is unreadable / malformed (rather than raising — TTS pipeline
    must keep working even with a broken dict)."""
    p = pronunciations_path(project_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Coerce values to str; reject empty keys to keep regex sane
    return {
        str(k): str(v) for k, v in data.items()
        if isinstance(k, str) and k.strip() and isinstance(v, (str, int, float))
    }


def save_pronunciations(project_dir: Path, mapping: dict[str, str]) -> None:
    """Replace the project's dict on disk. Empty mapping deletes the
    file entirely (so the renderer skips the apply pass cheaply).
    Atomic via .tmp + rename."""
    p = pronunciations_path(project_dir)
    if not mapping:
        p.unlink(missing_ok=True)
        return
    cleaned = {
        str(k): str(v) for k, v in mapping.items()
        if isinstance(k, str) and k.strip()
    }
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(cleaned, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(p)


def apply_pronunciations(text: str, mapping: dict[str, str]) -> str:
    """Apply word-boundary substitutions to ``text``. Empty mapping is a
    no-op. Keys are processed longest-first so longer phrases win when
    they overlap shorter ones (``"New York"`` before ``"New"``).

    Word boundaries (``\\b``) are Unicode-aware in Python's ``re``, so
    Czech diacritics are handled correctly: ``"München"`` matches as a
    whole token even though ``ü`` isn't ASCII.
    """
    if not mapping or not text:
        return text
    keys_long_first = sorted(mapping.keys(), key=len, reverse=True)
    # Build one combined alternation regex so a single sub() pass handles
    # everything — preserves left-to-right precedence among same-length
    # keys without re-running over already-substituted text.
    #
    # We use lookbehind/lookahead for "not adjacent to a word char"
    # instead of plain ``\b``: \b matches a word↔non-word transition,
    # which fails for keys that themselves end in a non-word char (e.g.
    # ``"Dr."`` ending in ``.``). Lookarounds catch both ``Dr. Novák``
    # (the dot abuts a space — no \b — but ``(?!\w)`` is satisfied) and
    # ``Dr.X`` (rejected because X is a word char).
    alternation = "|".join(re.escape(k) for k in keys_long_first)
    pattern = re.compile(rf"(?<!\w)(?:{alternation})(?!\w)", re.UNICODE)

    def _replace(match: re.Match) -> str:
        return mapping[match.group(0)]

    return pattern.sub(_replace, text)


def signature(mapping: dict[str, str]) -> str:
    """16-char hex of the mapping's canonical serialization. Used by
    :meth:`ProjectRenderer._params_signature` so a dict edit invalidates
    cached chunks (otherwise the renderer would happily return old
    audio synthesized from the un-substituted text)."""
    if not mapping:
        return "0" * 16
    canonical = json.dumps(mapping, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
