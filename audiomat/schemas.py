"""Pydantic request/response models for the FastAPI routers.

Keeping these out of the router files lets the routers stay focused on
HTTP wiring and lets schemas be re-used across multiple endpoints (e.g.
ProjectOut is returned by both create_project and update_project_book).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from audiomat.model_registry import TTSModel
from audiomat.project import Project
from audiomat.state import dataclass_to_dict
from audiomat.voice import Voice


# ----------------------------------------------------------------------------
# Voice schemas
# ----------------------------------------------------------------------------


class VoiceOut(BaseModel):
    name: str
    name_slug: str
    duration_s: float
    sample_rate: int
    channels: int
    transcript_chars: int
    notes: str
    created: str
    tts_model: str | None = None     # registry slug, None = stock default

    @classmethod
    def from_voice(cls, v: Voice) -> "VoiceOut":
        return cls(
            name=v.name, name_slug=v.name_slug,
            duration_s=v.duration_s, sample_rate=v.sample_rate,
            channels=v.channels, transcript_chars=v.transcript_chars,
            notes=v.notes, created=v.created, tts_model=v.tts_model,
        )


class TranscribeRequest(BaseModel):
    audio_path: str       # path produced by /api/voices/draft-upload
    language: str = "cs"


class VoiceModelRequest(BaseModel):
    """PATCH /voices/{slug}/model body. ``None`` / empty / "default"
    resets to the stock OmniVoice model."""
    tts_model: str | None = None


# ----------------------------------------------------------------------------
# Project schemas
# ----------------------------------------------------------------------------


class ProjectOut(BaseModel):
    name: str
    name_slug: str
    book: dict
    voice_ref: str
    voice_ref_slug: str
    params: dict
    status: dict
    created: str
    last_run: str
    has_final_m4b: bool

    @classmethod
    def from_project(cls, p: Project) -> "ProjectOut":
        return cls(
            name=p.name, name_slug=p.name_slug,
            book=dataclass_to_dict(p.book),
            voice_ref=p.voice_ref, voice_ref_slug=p.voice_ref_slug,
            params=dataclass_to_dict(p.params),
            status=dataclass_to_dict(p.status),
            created=p.created, last_run=p.last_run,
            has_final_m4b=p.final_path.exists(),
        )


class BlocksSkippedRequest(BaseModel):
    indices: list[int]


class BookMetaRequest(BaseModel):
    language: str | None = None


# ----------------------------------------------------------------------------
# Render schemas
# ----------------------------------------------------------------------------


class RenderRequest(BaseModel):
    """Optional body for POST /render. ``indices`` is a list of 1-based
    renderable chapter indices; if absent/empty the whole book renders."""
    indices: list[int] | None = None


class PreviewCustomRequest(BaseModel):
    num_step: int = 48
    guidance_scale: float = 2.0
    speed: float = 1.0
    # Optional matrix cell label ("Fast" / "Balanced" / "Crisp" / "Stable").
    # When present, the backend persists this tuning as a per-cell
    # override in previews/_tuned_cells.json — survives page refresh so
    # the matrix re-render shows the tuned cell at its custom params.
    # Omit (or None) to keep the call ephemeral (legacy behavior).
    label: str | None = None


# ----------------------------------------------------------------------------
# System schemas
# ----------------------------------------------------------------------------


ModelState = Literal["unloaded", "downloading", "loading", "ready"]


class ModelStatusOut(BaseModel):
    state: ModelState
    cache_bytes: int
    cache_target_bytes: int
    percent: float
    message: str | None = None
    # Display name of the model that's currently loading / loaded. Lets
    # SystemBanner say "Načítám Ježková v1…" instead of just "TTS model".
    # None when no model is loaded or loading.
    active_model: str | None = None


# ----------------------------------------------------------------------------
# TTS model registry schemas
# ----------------------------------------------------------------------------


class ModelOut(BaseModel):
    name: str
    name_slug: str
    source_type: Literal["local", "hf"]
    source_ref: str
    hf_revision: str | None = None
    size_bytes: int
    notes: str
    created: str

    @classmethod
    def from_model(cls, m: TTSModel) -> "ModelOut":
        return cls(
            name=m.name, name_slug=m.name_slug,
            source_type=m.source_type, source_ref=m.source_ref,
            hf_revision=m.hf_revision, size_bytes=m.size_bytes,
            notes=m.notes, created=m.created,
        )


class RegisterLocalModelRequest(BaseModel):
    """POST /api/models body. ``src_dir`` must be a path that the
    container can read (typically a bind-mounted host directory under
    /data/uploads/ or similar)."""
    name: str
    src_dir: str
    notes: str = ""
    overwrite: bool = False


class RegisterHFModelRequest(BaseModel):
    """POST /api/models/from-hf body. ``token`` overrides whatever is
    stored in secrets.json for this one call — useful for testing a
    token before persisting it."""
    name: str
    repo_id: str
    revision: str | None = None
    token: str | None = None
    notes: str = ""
    overwrite: bool = False


# ----------------------------------------------------------------------------
# Settings schemas
# ----------------------------------------------------------------------------


class HFTokenStatusOut(BaseModel):
    """GET /api/settings/hf response. The token itself is never sent
    back; only whether one is configured and where it came from."""
    has_token: bool
    source: Literal["env", "secrets_file"] | None = None


class HFTokenRequest(BaseModel):
    """PUT /api/settings/hf body. Empty / null token clears the stored
    value (use DELETE for the same effect)."""
    token: str | None = None
