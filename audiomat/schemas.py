"""Pydantic request/response models for the FastAPI routers.

Keeping these out of the router files lets the routers stay focused on
HTTP wiring and lets schemas be re-used across multiple endpoints (e.g.
ProjectOut is returned by both create_project and update_project_book).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

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

    @classmethod
    def from_voice(cls, v: Voice) -> "VoiceOut":
        return cls(
            name=v.name, name_slug=v.name_slug,
            duration_s=v.duration_s, sample_rate=v.sample_rate,
            channels=v.channels, transcript_chars=v.transcript_chars,
            notes=v.notes, created=v.created,
        )


class TranscribeRequest(BaseModel):
    audio_path: str       # path produced by /api/voices/draft-upload
    language: str = "cs"


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
