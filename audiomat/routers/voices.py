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

from audiomat.audio import (
    convert_voice_ref, extract_audio_window, probe_chapters, probe_wav,
)
from audiomat.project import Project
from audiomat.schemas import (
    AnalyzeOut, AnalyzeRequest, CandidateOut, ChapterOut,
    DraftUploadLongOut, ExtractWindowOut, ExtractWindowRequest,
    PreviewStagedVoiceOut, PreviewStagedVoiceRequest,
    TranscribeRequest, VoiceModelRequest, VoiceOut,
)
from audiomat.state import PATHS
from audiomat.voice import Voice


router = APIRouter(prefix="/api/voices", tags=["voices"])


# ---- specific voice routes (must come BEFORE /api/voices/{slug} catchall) ----


@router.get("", response_model=list[VoiceOut])
def list_voices():
    return [VoiceOut.from_voice(v) for v in Voice.list_all()]


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


# ---- long-source flow (multi-step wizard) ----


@router.post("/draft-upload-long", response_model=DraftUploadLongOut)
async def draft_voice_upload_long(audio: UploadFile = File(...)):
    """Upload an arbitrarily-long audio source (chapter mp3, audiobook
    m4b, …). No 20 s ceiling — caller will narrow it down via the
    /analyze + /extract-window flow.

    We immediately convert to 24 kHz mono WAV (so subsequent analyze /
    extract-window calls don't have to re-decode the source on every
    request) and probe for chapter markers. Returned path lives in an
    ``audiomat_voice_*`` tempdir; same staging area as draft-upload, so
    the same cleanup-on-commit logic applies in POST /api/voices.
    """
    suffix = Path(audio.filename or "voice").suffix or ".wav"
    tmpdir = Path(tempfile.mkdtemp(prefix="audiomat_voice_"))
    raw_path = tmpdir / f"raw{suffix}"
    converted_path = tmpdir / "voice_full.wav"

    with raw_path.open("wb") as f:
        shutil.copyfileobj(audio.file, f)

    try:
        info = convert_voice_ref(raw_path, converted_path)
        chapters = probe_chapters(raw_path)
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(400, f"audio conversion failed: {e}")
    finally:
        # Keep the original around briefly so probe_chapters can read it.
        # (The converted WAV doesn't carry chapter markers — concat strips
        # them — so we have to probe the original.)
        raw_path.unlink(missing_ok=True)

    return DraftUploadLongOut(
        audio_path=str(converted_path),
        duration_s=round(info.duration_s, 3),
        sample_rate=info.sample_rate,
        channels=info.channels,
        chapters=[
            ChapterOut(
                index=c.index, title=c.title,
                start_s=round(c.start_s, 3), end_s=round(c.end_s, 3),
                duration_s=round(c.duration_s, 3),
            )
            for c in chapters
        ],
    )


@router.post("/analyze", response_model=AnalyzeOut)
async def analyze_voice_source(req: AnalyzeRequest):
    """Run Silero VAD + scoring over a slice of the staged source and
    return the top 5 candidate windows. Each candidate gets a
    pre-trimmed preview WAV stashed alongside the source so the UI can
    play it back via /draft-audio.

    Two analyze modes:

    1. Default (``chapter_*`` unset) → analyze first ``analyze_minutes``
       of the source.
    2. Chapter-bounded (``chapter_start_s`` + ``chapter_end_s`` set) →
       analyze that exact range. Caps at ``analyze_minutes`` so a
       30-minute chapter doesn't sit on the GPU for two minutes.

    The returned candidate ``start_s`` / ``end_s`` are relative to the
    analyzed slice; ``analyzed_start_s`` lets the caller convert back to
    full-source coordinates when calling /extract-window.
    """
    full = Path(req.audio_path)
    if not full.exists():
        raise HTTPException(404, f"audio_path not found: {req.audio_path}")
    if full.parent.name and not full.parent.name.startswith("audiomat_voice_"):
        raise HTTPException(403, "audio_path must point inside an audiomat_voice_ tempdir")

    analyze_seconds = max(60.0, req.analyze_minutes * 60.0)
    if req.chapter_start_s is not None and req.chapter_end_s is not None:
        slice_start = max(0.0, float(req.chapter_start_s))
        slice_end = min(
            float(req.chapter_end_s),
            slice_start + analyze_seconds,
        )
    else:
        slice_start = 0.0
        slice_end = analyze_seconds

    slice_path = full.parent / f"slice_{int(slice_start)}_{int(slice_end)}.wav"
    try:
        if not slice_path.exists():
            extract_audio_window(full, slice_path, slice_start, slice_end)
    except Exception as e:
        raise HTTPException(500, f"slice extraction failed: {e}")

    from audiomat.voice_extract import find_candidates
    try:
        cands = find_candidates(slice_path)
    except Exception as e:
        raise HTTPException(500, f"VAD analysis failed: {type(e).__name__}: {e}")

    out_cands: list[CandidateOut] = []
    for i, c in enumerate(cands):
        # Render each candidate as its own preview WAV. start_s/end_s on
        # the candidate are slice-relative, so for the preview we pull
        # straight out of slice_path (cheap — <1 s of ffmpeg per cut).
        preview_path = full.parent / f"cand_{i:02d}_{c.start_s:.2f}-{c.end_s:.2f}.wav"
        try:
            extract_audio_window(slice_path, preview_path, c.start_s, c.end_s)
        except Exception as e:
            raise HTTPException(500, f"preview extraction failed: {e}")
        out_cands.append(CandidateOut(
            index=i,
            start_s=c.start_s, end_s=c.end_s,
            duration_s=round(c.duration_s, 3),
            score=c.score, preview_path=str(preview_path),
            breakdown=c.breakdown,
        ))

    return AnalyzeOut(
        candidates=out_cands,
        analyzed_start_s=round(slice_start, 3),
        analyzed_end_s=round(slice_end, 3),
        full_audio_path=str(full),
    )


@router.post("/preview-staged", response_model=PreviewStagedVoiceOut)
def preview_staged_voice(req: PreviewStagedVoiceRequest):
    """Render a TTS sample against a not-yet-saved voice — the user
    listens before committing the voice to the library, catching cases
    where a clean-looking clip happens to clone badly.

    Uses production defaults (num_step=48, guidance_scale=2.0, speed=1.0)
    and the stock OmniVoice model — fine-tunes attached via voice
    metadata only kick in after the voice is saved.

    Output WAV is written next to the staged voice in the same
    ``audiomat_voice_*`` tempdir; served via the existing /draft-audio
    endpoint (which has the same path-safety guard)."""
    src = Path(req.audio_path)
    if not src.exists():
        raise HTTPException(404, f"audio_path not found: {req.audio_path}")
    if not src.parent.name.startswith("audiomat_voice_"):
        raise HTTPException(403, "audio_path must point inside an audiomat_voice_ tempdir")
    transcript = req.transcript.strip()
    sample_text = req.sample_text.strip()
    if not transcript:
        raise HTTPException(400, "transcript is required")
    if not sample_text:
        raise HTTPException(400, "sample_text is required")
    if len(sample_text) > 1000:
        raise HTTPException(400,
            f"sample_text too long ({len(sample_text)} chars) — keep under 1000 "
            f"to bound the TTS render time")

    # Late imports keep the router import-light; soundfile + tts pull torch.
    import hashlib
    import time
    import soundfile as sf
    from audiomat.headers import prepare_for_tts
    from audiomat.num2text import normalize_lang
    from audiomat.state import get_tts

    # Stock OmniVoice for the staged preview — voice's tts_model field
    # is only meaningful after save (this is a brand-new voice).
    tts = get_tts(target=None)
    tts.load()
    sr = tts.sample_rate

    language = normalize_lang(req.language or "cs")
    clean = prepare_for_tts(sample_text, lang=language)

    # Cache by (transcript, sample_text, audio_path) so re-clicking
    # Render with no changes is instant. audio_path includes the
    # tempdir name so two separate uploads can't collide.
    key_src = f"{src.resolve().as_posix()}|{transcript}|{clean}"
    key = hashlib.md5(key_src.encode("utf-8")).hexdigest()[:16]
    out_path = src.parent / f"preview_{key}.wav"

    if out_path.exists() and out_path.stat().st_size > 1024:
        from audiomat.audio import probe_wav
        info = probe_wav(out_path)
        return PreviewStagedVoiceOut(
            audio_path=str(out_path),
            duration_s=round(info.duration_s, 2),
            gen_seconds=0.0,            # cache hit — no fresh wall-clock to report
        )

    try:
        t0 = time.time()
        audios = tts._model.generate(
            text=clean,
            language=language,
            ref_text=transcript,
            ref_audio=str(src),
            num_step=48,
            guidance_scale=2.0,
            speed=1.0,
        )
        gen_s = time.time() - t0
        sf.write(str(out_path), audios[0], sr, subtype="PCM_16")
    except Exception as e:
        raise HTTPException(500, f"TTS render failed: {type(e).__name__}: {e}")

    return PreviewStagedVoiceOut(
        audio_path=str(out_path),
        duration_s=round(audios[0].shape[-1] / sr, 2),
        gen_seconds=round(gen_s, 2),
    )


@router.post("/extract-window", response_model=ExtractWindowOut)
async def extract_voice_window(req: ExtractWindowRequest):
    """Cut the user's chosen ``[start_s, end_s]`` (slice-relative,
    matching what ``AnalyzeOut.candidates[*]`` returned) out of the
    full converted source and return a path the caller can pass to
    POST /api/voices.

    The output enforces the OmniVoice 5-10 s reference window. We also
    rename it ``voice.wav`` so the existing /api/voices commit flow
    treats it as the canonical voice ref (caller can then delete the
    larger ``voice_full.wav`` on commit; tempdir cleanup handles it).
    """
    full = Path(req.audio_path)
    if not full.exists():
        raise HTTPException(404, f"audio_path not found: {req.audio_path}")
    if not full.parent.name.startswith("audiomat_voice_"):
        raise HTTPException(403, "audio_path must point inside an audiomat_voice_ tempdir")

    abs_start = max(0.0, req.analyzed_start_s + req.start_s)
    abs_end = req.analyzed_start_s + req.end_s
    duration = abs_end - abs_start
    if duration < 3.0 or duration > 12.0:
        raise HTTPException(400,
            f"window must be 3-12 s (got {duration:.2f} s). "
            f"OmniVoice's tested range is 5-10 s with light slack at the edges.")

    out_path = full.parent / "voice.wav"
    try:
        info = extract_audio_window(full, out_path, abs_start, abs_end)
    except Exception as e:
        raise HTTPException(500, f"window extraction failed: {e}")

    return ExtractWindowOut(
        audio_path=str(out_path),
        duration_s=round(info.duration_s, 3),
        sample_rate=info.sample_rate,
        channels=info.channels,
    )


# ---- short-form commit (also handles the long-form's /extract-window output) ----


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
    try:
        v = Voice.load(slug)
    except FileNotFoundError:
        raise HTTPException(404, f"voice not found: {slug}")
    tts_model_clean = (req.tts_model or "").strip()
    if not tts_model_clean or tts_model_clean == "default":
        tts_model_clean = None
    v.tts_model = tts_model_clean
    v.save()
    return VoiceOut.from_voice(v)


@router.get("/{slug}", response_model=VoiceOut)
def get_voice(slug: str):
    try:
        return VoiceOut.from_voice(Voice.load(slug))
    except FileNotFoundError:
        raise HTTPException(404, f"voice not found: {slug}")


@router.delete("/{slug}")
def delete_voice(slug: str, replacement: str | None = None):
    """Delete a voice from the library.

    If the voice is referenced by one or more projects:

    * No ``?replacement=`` query → respond 409 with a structured detail
      ``{"message", "referencing_projects": [{slug, name}, ...]}`` so
      the UI can prompt the user to pick a swap target.
    * ``?replacement=<other-slug>`` → atomically reassign every
      referencing project to ``replacement`` and then delete the
      original. The voice swap goes through the same path as
      ``PATCH /projects/{slug}/voice`` (manifest signature handles
      chunk-cache invalidation on next render).

    The replacement must exist in the library and must not be the
    voice being deleted (no self-swap).
    """
    try:
        voice = Voice.load(slug)
    except FileNotFoundError:
        raise HTTPException(404, f"voice not found: {slug}")

    referencing_projects = [
        p for p in Project.list_all(PATHS.projects_root)
        if p.voice_ref_slug == slug
    ]

    if referencing_projects and replacement is None:
        raise HTTPException(409, {
            "message": (
                f"voice is used by {len(referencing_projects)} project(s); "
                "pass ?replacement=<slug> to atomically reassign and delete"
            ),
            "referencing_projects": [
                {"slug": p.name_slug, "name": p.name}
                for p in referencing_projects
            ],
        })

    replaced_in: list[str] = []
    if referencing_projects:
        if replacement == slug:
            raise HTTPException(400, "replacement cannot equal the voice being deleted")
        try:
            new_voice = Voice.load(replacement)
        except FileNotFoundError:
            raise HTTPException(404, f"replacement voice not found: {replacement}")
        for proj in referencing_projects:
            proj.voice_ref = new_voice.name
            proj.voice_ref_slug = new_voice.name_slug
            proj.save()
            replaced_in.append(proj.name_slug)

    voice.delete()
    return {
        "deleted": slug,
        "replacement": replacement if replaced_in else None,
        "replaced_in": replaced_in,
    }


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
