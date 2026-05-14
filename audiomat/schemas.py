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


# --- Long-source voice picker (multi-step wizard) ---


class ChapterOut(BaseModel):
    """One chapter from a chaptered container (m4b). Returned by
    /api/voices/draft-upload-long when the upload is an audiobook with
    chapter markers. UI uses this to offer "analyze a specific chapter"
    instead of the default "first 10 minutes" of the file."""
    index: int
    title: str
    start_s: float
    end_s: float
    duration_s: float


class DraftUploadLongOut(BaseModel):
    """Response of /api/voices/draft-upload-long. Same shape as the
    short-form draft-upload but with no duration ceiling, and with an
    optional chapter list when the source has them.

    ``audio_path`` is the converted 24 kHz mono WAV of the WHOLE source
    (we keep it for later /extract-window calls that need to seek into
    arbitrary spots of the original)."""
    audio_path: str
    duration_s: float
    sample_rate: int
    channels: int
    chapters: list[ChapterOut]      # empty list when source has none


class AnalyzeRequest(BaseModel):
    """POST /api/voices/analyze body. Either analyze the first
    ``analyze_minutes`` of the source, or — if ``chapter_index`` is set
    — analyze that single chapter instead."""
    audio_path: str
    chapter_start_s: float | None = None    # set together with chapter_end_s
    chapter_end_s: float | None = None
    analyze_minutes: float = 10.0


class CandidateOut(BaseModel):
    """One scored candidate window. ``preview_path`` is a pre-trimmed
    WAV in the same staging tempdir, served via /draft-audio so the UI
    can play each candidate without re-trimming on every click."""
    index: int
    start_s: float                  # relative to the analyzed slice
    end_s: float
    duration_s: float
    score: float                    # 0-100 composite
    preview_path: str
    breakdown: dict


class AnalyzeOut(BaseModel):
    candidates: list[CandidateOut]
    analyzed_start_s: float          # offset of the analyzed slice within the source
    analyzed_end_s: float
    full_audio_path: str             # echoes the request, for chained calls


class ExtractWindowRequest(BaseModel):
    """POST /api/voices/extract-window body. Trim ``[start_s, end_s]``
    out of the (already-converted) staged source and return a path
    that can be passed to POST /api/voices to commit the voice.

    Times are in the analyzed slice's coordinate system — the backend
    adds back the slice offset before cutting from the full WAV."""
    audio_path: str                  # full converted WAV path
    analyzed_start_s: float          # slice offset (echo from AnalyzeOut)
    start_s: float
    end_s: float


class ExtractWindowOut(BaseModel):
    """Response shape matches DraftUploadResult so the front-end can
    feed it straight into the existing /api/voices commit flow."""
    audio_path: str
    duration_s: float
    sample_rate: int
    channels: int


class PreviewStagedVoiceRequest(BaseModel):
    """POST /api/voices/preview-staged body. Run a single TTS generation
    against a not-yet-saved voice (still living in an
    ``audiomat_voice_*`` tempdir) so the user can validate the clone
    quality before committing to the library.

    Uses the production OmniVoice params (num_step=48, gs=2.0, speed=1.0)
    — what the eventual project render will use by default — so what
    the user hears here matches what they'll get."""
    audio_path: str                 # staged voice.wav inside tempdir
    transcript: str                 # matching ref text (probably user-edited)
    sample_text: str                # what to render
    language: str = "cs"


class PreviewStagedVoiceOut(BaseModel):
    """Result of a staged-voice TTS preview. Audio served via the existing
    /api/voices/draft-audio endpoint (same staging-area protection)."""
    audio_path: str
    duration_s: float
    gen_seconds: float


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
    # Optimistic-lock counter — frontend echoes back as ``If-Match``
    # on PATCH so the backend can detect "another tab edited this
    # since you loaded it" via Project.save_with_version.
    version: int

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
            version=p.version,
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


class PreviewVoicesRequest(BaseModel):
    """POST /api/projects/{slug}/preview-voices body. Renders the same
    sample text from the project at each voice — params come from the
    project (so cells vary only by voice). Cap at 4 to keep the GPU
    bounded; the UI enforces the same cap on its checklist."""
    voice_slugs: list[str]


class ProjectVoiceRequest(BaseModel):
    """PATCH /api/projects/{slug}/voice body. Voice swap invalidates the
    chunk cache automatically via the manifest signature (which includes
    the voice slug — see ``ProjectRenderer._params_signature``)."""
    voice_slug: str


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


class VoiceValidationTextOut(BaseModel):
    """GET /api/settings/voice-validation-text response. ``is_default``
    lets the UI show a quiet "(reset to default)" affordance only when
    the user actually has an override stored."""
    text: str
    is_default: bool


class VoiceValidationTextRequest(BaseModel):
    """PUT /api/settings/voice-validation-text body. Server-side store
    persists across uploads + browsers (single library = single value).
    Use DELETE to clear the override."""
    text: str


# ----------------------------------------------------------------------------
# Backup / restore
# ----------------------------------------------------------------------------


class BackupSizeOut(BaseModel):
    """GET /api/backup/preview response. Lets the UI show a size badge
    on each toggle so the user knows what they're committing to before
    they click Download. Sizes in bytes — frontend formats."""
    essentials_bytes: int
    renders_bytes: int
    finals_bytes: int
    file_counts: dict           # {"essentials": int, "renders": int, "finals": int}


class RestoreOut(BaseModel):
    """POST /api/backup/restore response after a successful restore."""
    files_extracted: int
    bytes_extracted: int
    pre_restore_snapshot: str | None
    warnings: list[str]
