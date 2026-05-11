"""Voice library endpoints — list / get / create / delete / audio.

Voice creation is two-stage: ``POST /draft-upload`` stages a converted
WAV in a tempdir; ``POST /`` (root) commits it into the library along
with the user-edited transcript.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from audiomat.audio import convert_voice_ref
from audiomat.project import Project
from audiomat.schemas import TranscribeRequest, VoiceModelRequest, VoiceOut
from audiomat.state import PATHS
from audiomat.voice import Voice


router = APIRouter(prefix="/api/voices", tags=["voices"])


# ---- specific voice routes (must come BEFORE /api/voices/{slug} catchall) ----


@router.get("", response_model=list[VoiceOut])
def list_voices():
    return [VoiceOut.from_voice(v) for v in Voice.list_all(PATHS.voices_root)]


@router.post("/auto-transcribe")
def auto_transcribe(req: TranscribeRequest):
    """Run faster-whisper on a previously-uploaded audio file. Returns the
    draft transcript so the UI can show it for user editing before save.
    Heavy lift — first call downloads ~1.5 GB whisper-medium."""
    from audiomat.transcribe import transcribe
    path = Path(req.audio_path)
    if not path.exists():
        raise HTTPException(404, f"audio not found: {req.audio_path}")
    try:
        text = transcribe(path, language=req.language)
    except Exception as e:
        raise HTTPException(500, f"transcribe failed: {type(e).__name__}: {e}")
    return {"transcript": text}


@router.get("/draft-audio")
def draft_audio(path: str):
    """Serve a previously-uploaded staged voice WAV so the UI can play it
    back during the review stage. Security: only serves files inside an
    ``audiomat_voice_*`` tempdir (created by /draft-upload). Any other
    path is 404'd to prevent arbitrary filesystem reads.

    NOTE: this route MUST be registered before ``/{slug}``, otherwise
    FastAPI matches the path-suffix as a slug — a nasty silent routing
    bug we hit during v0.0.1 testing.
    """
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, f"draft audio not found at {p}")
    if not p.parent.name.startswith("audiomat_voice_"):
        raise HTTPException(403, f"path outside staging area: parent={p.parent.name!r}")
    return FileResponse(p, media_type="audio/wav")


@router.post("/draft-upload")
async def draft_voice_upload(audio: UploadFile = File(...)):
    """Stage 1 of voice creation: upload + ffmpeg-convert to 24 kHz mono.
    Returns the temp path + audio info so the UI can preview / auto-
    transcribe before final commit. Caller must call POST /api/voices to finalize.
    """
    suffix = Path(audio.filename or "voice").suffix or ".wav"
    tmpdir = Path(tempfile.mkdtemp(prefix="audiomat_voice_"))
    raw_path = tmpdir / f"raw{suffix}"
    converted_path = tmpdir / "voice.wav"

    with raw_path.open("wb") as f:
        shutil.copyfileobj(audio.file, f)

    try:
        info = convert_voice_ref(raw_path, converted_path)
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(400, f"audio conversion failed: {e}")
    finally:
        raw_path.unlink(missing_ok=True)

    if info.duration_s > 20:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(400,
            f"voice ref is {info.duration_s:.1f}s (>20s) — OmniVoice degrades "
            f"and is 2.6× slower per chunk above 20s. Trim to 5–10s.")

    return {
        "audio_path": str(converted_path),
        "duration_s": round(info.duration_s, 3),
        "sample_rate": info.sample_rate,
        "channels": info.channels,
        "warning": (
            f"voice ref is {info.duration_s:.1f}s — OmniVoice recommends 3–10s."
            if info.duration_s > 15 else ""
        ),
    }


@router.post("", response_model=VoiceOut)
async def create_voice(
    name: str = Form(...),
    audio_path: str = Form(...),
    transcript: str = Form(...),
    notes: str = Form(""),
    overwrite: bool = Form(False),
    tts_model: str | None = Form(None),
):
    """Stage 2 of voice creation: commit a previously-uploaded audio +
    transcript into the library. ``audio_path`` comes from
    ``/api/voices/draft-upload``.

    ``tts_model`` (optional) — slug of a registered TTS model the voice
    should default to at preview / render time. Null / empty / "default"
    means use the stock OmniVoice."""
    if not name.strip():
        raise HTTPException(400, "name is required")
    if not transcript.strip():
        raise HTTPException(400, "transcript is required")

    src = Path(audio_path)
    if not src.exists():
        raise HTTPException(404, f"audio_path not found: {audio_path}")

    # Normalize the model field: empty string / "default" → None so the
    # stored meta.json doesn't carry a meaningless slug.
    tts_model_clean = (tts_model or "").strip()
    if not tts_model_clean or tts_model_clean == "default":
        tts_model_clean = None

    from audiomat.audio import probe_wav
    info = probe_wav(src)

    try:
        voice = Voice.create(
            voices_root=PATHS.voices_root,
            name=name.strip(),
            wav_src=src,
            transcript_text=transcript,
            duration_s=info.duration_s,
            sample_rate=info.sample_rate,
            channels=info.channels,
            notes=notes,
            overwrite=overwrite,
            tts_model=tts_model_clean,
        )
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    finally:
        # Clean up the staging tmpdir
        if src.parent.name.startswith("audiomat_voice_"):
            shutil.rmtree(src.parent, ignore_errors=True)

    return VoiceOut.from_voice(voice)


@router.patch("/{slug}/model", response_model=VoiceOut)
def update_voice_model(slug: str, req: VoiceModelRequest):
    """Change which TTS model a voice points at. Cheap operation: just
    rewrites meta.json. Doesn't invalidate any per-chapter cache — the
    manifest signature already includes voice slug + params (see
    ProjectRenderer._params_signature) so a model swap will trigger
    re-synth on next render automatically."""
    target = PATHS.voice_dir(slug)
    if not (target / "meta.json").exists():
        raise HTTPException(404, f"voice not found: {slug}")
    v = Voice.load(target)
    tts_model_clean = (req.tts_model or "").strip()
    if not tts_model_clean or tts_model_clean == "default":
        tts_model_clean = None
    v.tts_model = tts_model_clean
    v.save()
    return VoiceOut.from_voice(v)


@router.get("/{slug}", response_model=VoiceOut)
def get_voice(slug: str):
    target = PATHS.voice_dir(slug)
    if not (target / "meta.json").exists():
        raise HTTPException(404, f"voice not found: {slug}")
    return VoiceOut.from_voice(Voice.load(target))


@router.delete("/{slug}")
def delete_voice(slug: str):
    target = PATHS.voice_dir(slug)
    if not target.exists():
        raise HTTPException(404, f"voice not found: {slug}")
    # Refuse delete if any project references this voice
    referencing = [p.name for p in Project.list_all(PATHS.projects_root)
                   if p.voice_ref_slug == slug]
    if referencing:
        raise HTTPException(409,
            f"voice is used by {len(referencing)} project(s): {', '.join(referencing)}")
    Voice.load(target).delete()
    return {"deleted": slug}


@router.get("/{slug}/audio")
def voice_audio(slug: str):
    """Serve voice.wav inline so the UI's <audio controls> can play it.
    No filename= → no Content-Disposition: attachment → browser plays
    instead of downloading. The frontend uses <a download> on a separate
    button to force download."""
    target = PATHS.voice_dir(slug) / "voice.wav"
    if not target.exists():
        raise HTTPException(404, "voice.wav not found")
    return FileResponse(target, media_type="audio/wav")
