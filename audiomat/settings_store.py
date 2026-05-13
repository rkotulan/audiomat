"""Non-sensitive user preferences — sibling of ``audiomat/secrets.py``.

Stored under ``<library_root>/settings.json`` (no special perms — these
aren't secrets). Lives next to ``secrets.json`` so a single bind-mount
on Docker covers both, but the two are kept separate so we don't have
to think about file permissions when adding new prefs.

Keys are namespaced by feature (``voice_validation_text``, etc.) so
unrelated prefs can co-exist without a per-pref file each.
"""
from __future__ import annotations

import json
from pathlib import Path


# Default validation paragraph for the voice creation flow. Czech beletrie
# with a quoted dialogue beat and one digit (exercises num2words "10" →
# "deset"). User-overridable via PUT /api/settings/voice-validation-text;
# the override is stored in settings.json and survives across uploads.
DEFAULT_VOICE_VALIDATION_TEXT = (
    "Bylo už 10 minut po půlnoci, když do dveří hospody vstoupil cizinec "
    "v promočeném kabátě. „Dobrý večer,\" pozdravil tiše. Hostinský zvedl "
    "hlavu od novin a změřil si ho pohledem."
)

VOICE_VALIDATION_TEXT_KEY = "voice_validation_text"

# Cap on stored text — keep the file small and bound the eventual TTS
# render time when the value is fed into POST /api/voices/preview-staged.
MAX_VOICE_VALIDATION_TEXT_CHARS = 1000


def _load_all(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_all(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    path.write_text(payload, encoding="utf-8")


def get_voice_validation_text(settings_path: Path) -> str:
    """Return the user's saved validation paragraph, or the default if
    they've never overridden it. Always returns a non-empty string —
    callers don't have to handle a None case."""
    stored = _load_all(settings_path).get(VOICE_VALIDATION_TEXT_KEY)
    if isinstance(stored, str) and stored.strip():
        return stored
    return DEFAULT_VOICE_VALIDATION_TEXT


def set_voice_validation_text(settings_path: Path, text: str) -> str:
    """Persist the user's preferred validation paragraph. Trims
    whitespace and rejects empty input (the default lives in code, so
    "clear my override" should be expressed as DELETE rather than PUT
    of an empty string).

    Returns the value that was actually stored — convenient for the
    endpoint to echo back."""
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("voice validation text cannot be empty — DELETE to reset to default")
    if len(cleaned) > MAX_VOICE_VALIDATION_TEXT_CHARS:
        raise ValueError(
            f"voice validation text too long ({len(cleaned)} chars, "
            f"max {MAX_VOICE_VALIDATION_TEXT_CHARS})"
        )
    data = _load_all(settings_path)
    data[VOICE_VALIDATION_TEXT_KEY] = cleaned
    _save_all(settings_path, data)
    return cleaned


def reset_voice_validation_text(settings_path: Path) -> str:
    """Drop the user's override so the default text takes over again.
    Idempotent — succeeds even if the key was never set. Returns the
    default text so the caller doesn't have to import it separately."""
    data = _load_all(settings_path)
    if VOICE_VALIDATION_TEXT_KEY in data:
        del data[VOICE_VALIDATION_TEXT_KEY]
        _save_all(settings_path, data)
    return DEFAULT_VOICE_VALIDATION_TEXT


if __name__ == "__main__":
    # Smoke test: round-trip the validation text through a tempfile.
    # `python -m audiomat.settings_store`
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        print(f"empty: text={get_voice_validation_text(path)[:40]!r}…")
        set_voice_validation_text(path, "Custom test paragraph in English.")
        print(f"set  : text={get_voice_validation_text(path)!r}")
        reset_voice_validation_text(path)
        print(f"reset: text={get_voice_validation_text(path)[:40]!r}…")
