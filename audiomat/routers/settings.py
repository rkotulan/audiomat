"""User settings — currently just the Hugging Face token.

Persists under ``<library_root>/secrets.json`` with mode 0o600 (POSIX).
On Windows the chmod is a no-op but the file inherits user-only ACLs
from the library root.

The HF token never appears in any GET response — only ``has_token``
and ``source`` (env vs file vs absent). Mirrors how good password
fields work in well-behaved settings UIs.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from audiomat.hf_client import list_user_repos
from audiomat.schemas import (
    HFTokenRequest, HFTokenStatusOut,
    VoiceValidationTextOut, VoiceValidationTextRequest,
)
from audiomat.secrets import get_hf_token, hf_token_source, set_hf_token
from audiomat.settings_store import (
    DEFAULT_VOICE_VALIDATION_TEXT,
    get_voice_validation_text,
    reset_voice_validation_text,
    set_voice_validation_text,
)
from audiomat.state import PATHS


router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/hf", response_model=HFTokenStatusOut)
def get_hf_settings():
    """Report whether an HF token is configured and where it came from.
    Used by the Settings page to show "Using HF_TOKEN env var" vs
    "Stored in secrets.json" vs "Not configured"."""
    return HFTokenStatusOut(
        has_token=get_hf_token(PATHS.secrets_path) is not None,
        source=hf_token_source(PATHS.secrets_path),
    )


@router.put("/hf", response_model=HFTokenStatusOut)
def set_hf_settings(req: HFTokenRequest):
    """Store (or clear, when ``token`` is null/empty) the HF token.
    Does not touch the HF_TOKEN env var — if that's set, it takes
    precedence over whatever we store here and the response will reflect
    ``source: env``."""
    set_hf_token(PATHS.secrets_path, req.token)
    return HFTokenStatusOut(
        has_token=get_hf_token(PATHS.secrets_path) is not None,
        source=hf_token_source(PATHS.secrets_path),
    )


@router.delete("/hf", response_model=HFTokenStatusOut)
def clear_hf_settings():
    """Clear the stored token. The HF_TOKEN env var (if set) is left
    untouched and will still be reported via GET."""
    set_hf_token(PATHS.secrets_path, None)
    return HFTokenStatusOut(
        has_token=get_hf_token(PATHS.secrets_path) is not None,
        source=hf_token_source(PATHS.secrets_path),
    )


@router.get("/voice-validation-text", response_model=VoiceValidationTextOut)
def get_voice_validation_text_setting():
    """Return the user's preferred validation paragraph for voice creation.
    Falls back to the built-in Czech default when no override is stored —
    callers always get a usable string. ``is_default`` lets the UI show a
    "(reset)" link only when there's actually something to reset."""
    text = get_voice_validation_text(PATHS.settings_path)
    return VoiceValidationTextOut(
        text=text,
        is_default=(text == DEFAULT_VOICE_VALIDATION_TEXT),
    )


@router.put("/voice-validation-text", response_model=VoiceValidationTextOut)
def set_voice_validation_text_setting(req: VoiceValidationTextRequest):
    """Store the user's preferred validation paragraph. Persists to
    ``settings.json`` so the next voice creation (any browser, after a
    restart) starts with this text instead of the Czech default."""
    try:
        stored = set_voice_validation_text(PATHS.settings_path, req.text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return VoiceValidationTextOut(
        text=stored,
        is_default=(stored == DEFAULT_VOICE_VALIDATION_TEXT),
    )


@router.delete("/voice-validation-text", response_model=VoiceValidationTextOut)
def reset_voice_validation_text_setting():
    """Drop the user's override and return to the built-in default."""
    text = reset_voice_validation_text(PATHS.settings_path)
    return VoiceValidationTextOut(text=text, is_default=True)


@router.post("/hf/validate")
def validate_hf_token(req: HFTokenRequest):
    """Sanity-check a token against the HF API without storing it.
    Lets the Settings UI show "✓ valid" / "✗ rejected" before the user
    commits. Validates by calling ``whoami`` (via list_user_repos which
    does the same probe first)."""
    if not req.token or not req.token.strip():
        raise HTTPException(400, "token is required")
    try:
        repos = list_user_repos(req.token)
    except RuntimeError as e:
        raise HTTPException(401, str(e))
    return {"valid": True, "repo_count": len(repos)}
