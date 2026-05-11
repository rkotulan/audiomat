"""TTS model registry endpoints.

Stock ``k2-fsa/OmniVoice`` is the implicit default (no entry in the
registry). User-added entries — local-copied checkpoints or HF-sourced
snapshots — live at ``<library_root>/models/<slug>/``. Each gets a
slug-keyed REST surface mirroring the voices router.
"""
from __future__ import annotations

import json as _json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from audiomat.hf_client import (
    HFRepoInfo,
    list_user_repos,
    redownload as hf_redownload,
    stream_download_to_registry,
)
from audiomat.model_registry import TTSModel
from audiomat.schemas import (
    ModelOut,
    RegisterHFModelRequest,
    RegisterLocalModelRequest,
)
from audiomat.secrets import get_hf_token
from audiomat.state import PATHS, clear_tts


router = APIRouter(prefix="/api/models", tags=["models"])


# --- list / detail ----------------------------------------------------------


@router.get("", response_model=list[ModelOut])
def list_models():
    """Enumerate registered TTS models. Skips corrupt entries silently
    (same shape as /voices)."""
    return [ModelOut.from_model(m) for m in TTSModel.list_all(PATHS.models_root)]


@router.get("/hf/my-repos", response_model=list[HFRepoInfo])
def list_my_hf_repos():
    """Resolve the user's HF token and list every repo they own.
    Backs the "Browse my HF models" picker in the Models UI so users
    don't have to copy/paste repo ids by hand. Requires a configured
    token (settings.json or HF_TOKEN env)."""
    token = get_hf_token(PATHS.secrets_path)
    if not token:
        raise HTTPException(
            400, "no Hugging Face token configured — set one under Settings"
        )
    try:
        return list_user_repos(token)
    except RuntimeError as e:
        raise HTTPException(401, str(e))


@router.get("/{slug}", response_model=ModelOut)
def get_model(slug: str):
    target = PATHS.model_dir(slug)
    if not (target / "meta.json").exists():
        raise HTTPException(404, f"model not found: {slug}")
    return ModelOut.from_model(TTSModel.load(target))


# --- register: local copy ---------------------------------------------------


@router.post("", response_model=ModelOut)
def register_local_model(req: RegisterLocalModelRequest):
    """Copy a local checkpoint directory into the registry. ``src_dir``
    must be readable from inside the container — typical pattern is to
    bind-mount the host's training output dir as ``/data/uploads/<x>``
    and point ``src_dir`` at it."""
    src = Path(req.src_dir).expanduser()
    if not src.exists():
        raise HTTPException(404, f"src_dir not found: {req.src_dir}")
    if not src.is_dir():
        raise HTTPException(400, f"src_dir must be a directory: {req.src_dir}")
    try:
        model = TTSModel.register_local(
            PATHS.models_root,
            name=req.name,
            src_dir=src,
            notes=req.notes,
            overwrite=req.overwrite,
        )
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return ModelOut.from_model(model)


# --- register: HF snapshot (streamed) ---------------------------------------


def _sse_download(
    gen,
) -> EventSourceResponse:
    """Wrap a DownloadProgress generator into SSE. Each yielded event
    becomes an ``event:/data:`` pair the frontend consumes via the same
    fetch + manual parser pattern used elsewhere (preview-matrix,
    m4b-build)."""
    def to_sse():
        for ev in gen:
            yield {
                "event": ev.kind,
                "data": _json.dumps({
                    "downloaded_bytes": ev.downloaded_bytes,
                    "total_bytes": ev.total_bytes,
                    "percent": round(ev.percent, 2),
                    "message": ev.message,
                    "model_slug": ev.model_slug,
                }),
            }
    return EventSourceResponse(to_sse())


@router.post("/from-hf")
def register_hf_model(req: RegisterHFModelRequest):
    """Stream-download an HF model snapshot into the registry. SSE
    events:

      * ``started``  — total_bytes (may be 0 if HF didn't expose sizes)
      * ``progress`` — downloaded_bytes + percent (~every 0.5 %)
      * ``complete`` — model_slug of the newly registered entry
      * ``error``    — message

    Token resolution order: explicit ``token`` in body > stored token
    in secrets.json > HF_TOKEN env var > anonymous (rate-limited public
    repos only)."""
    token = req.token or get_hf_token(PATHS.secrets_path)
    if not req.name.strip():
        raise HTTPException(400, "name is required")
    if not req.repo_id.strip():
        raise HTTPException(400, "repo_id is required")
    gen = stream_download_to_registry(
        models_root=PATHS.models_root,
        name=req.name.strip(),
        repo_id=req.repo_id.strip(),
        revision=req.revision,
        token=token,
        notes=req.notes,
        overwrite=req.overwrite,
    )
    return _sse_download(gen)


# --- redownload -------------------------------------------------------------


@router.post("/{slug}/redownload")
def redownload_model(slug: str):
    """Re-pull an HF-sourced model in place. Useful if local files were
    corrupted or just to refresh from the pinned revision. Errors if
    the model is local-sourced (no remote to refresh from). Same SSE
    shape as ``/from-hf``."""
    existing = TTSModel.find_by_slug(PATHS.models_root, slug)
    if existing is None:
        raise HTTPException(404, f"model not found: {slug}")
    if existing.source_type != "hf":
        raise HTTPException(
            400,
            f"model {slug!r} is source_type={existing.source_type!r} — "
            f"only hf-sourced models can be redownloaded",
        )
    token = get_hf_token(PATHS.secrets_path)

    # Build a one-shot generator that calls redownload + emits the same
    # event shape as stream_download_to_registry.
    def gen():
        from audiomat.hf_client import DownloadProgress

        yield DownloadProgress(kind="started", total_bytes=existing.size_bytes)
        try:
            # We don't have per-byte progress for redownload (it goes
            # through download_to_registry's callback path). Cheap
            # approximation: emit one "progress 50 %" so the bar moves
            # off zero, then complete.
            yield DownloadProgress(
                kind="progress",
                downloaded_bytes=existing.size_bytes // 2,
                total_bytes=existing.size_bytes,
                percent=50.0,
            )
            fresh = hf_redownload(
                PATHS.models_root, existing, token=token, progress_cb=None
            )
            yield DownloadProgress(
                kind="complete",
                model_slug=fresh.name_slug,
                downloaded_bytes=fresh.size_bytes,
                total_bytes=fresh.size_bytes,
                percent=100.0,
            )
        except Exception as e:
            yield DownloadProgress(
                kind="error",
                message=f"{type(e).__name__}: {e}",
            )

    return _sse_download(gen())


# --- delete -----------------------------------------------------------------


@router.delete("/{slug}")
def delete_model(slug: str):
    """Remove a registered model from disk + unload any in-memory
    instance pointing at its path. The voices that referenced this
    model fall back to the stock OmniVoice on next render — caller is
    responsible for fixing up voice.tts_model fields if they want
    something else."""
    existing = TTSModel.find_by_slug(PATHS.models_root, slug)
    if existing is None:
        raise HTTPException(404, f"model not found: {slug}")
    # Unload any matching TTS instance first so the file delete doesn't
    # race with a model holding the weights open (Windows file locks).
    clear_tts(target=existing.from_pretrained_target)
    existing.delete()
    return {"deleted": slug}
