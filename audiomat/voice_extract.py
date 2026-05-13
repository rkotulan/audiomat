"""Long-source candidate finder for the voice library.

Given a multi-minute audio source (one chapter mp3, an audiobook m4b,
etc.), find the best 5-10 s windows that work as an OmniVoice reference
clip — speech-bounded by pauses, clean RMS, no clipping, single
speaker (the dominant one).

Pipeline:

1. Silero VAD over the source at 16 kHz → list of speech segments.
2. Identify "anchor boundaries" — segment edges adjacent to a pause
   ≥400 ms. These are sentence/phrase boundaries safe to splice on.
3. Enumerate (start_anchor, end_anchor) pairs whose duration is in the
   5-10 s window OmniVoice prefers (see CLAUDE.md "Iron-clad rules").
4. Score each candidate: speech density, RMS consistency, anti-clipping,
   SNR proxy. Weighted sum gives a 0-100 quality score.
5. Greedy top-N pick that suppresses overlapping candidates so the user
   sees a varied set, not five copies of the same window.

The Whisper transcript pass happens later (caller's job, via
``audiomat/transcribe.py``) once the user has picked a window.
"""
from __future__ import annotations

import math
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

# --- Tunables ---------------------------------------------------------------

VAD_SAMPLING_RATE = 16000           # Silero's native; we resample on read
VAD_THRESHOLD = 0.5                  # default voiced-probability cutoff
VAD_MIN_SPEECH_MS = 250              # ignore sub-phoneme blips
VAD_MIN_SILENCE_MS = 100             # gaps shorter than this stay inside speech
VAD_SPEECH_PAD_MS = 30               # tiny pad so we don't clip onsets

ANCHOR_GAP_MS = 400                  # gap ≥ this counts as a sentence break
TARGET_MIN_S = 5.0                   # OmniVoice ref window lower bound
TARGET_MAX_S = 10.0                  # …upper bound
TARGET_DENSITY = 0.90                # ideal voiced fraction inside window
RMS_CHUNK_MS = 100                   # RMS sampling resolution for consistency
TOP_N = 5                            # how many candidates to surface
MIN_NONOVERLAP_RATIO = 0.5           # picks must overlap < this fraction


# --- Cached VAD model -------------------------------------------------------

_VAD_MODEL = None


def _vad_model():
    """Load the Silero VAD model on first call.

    Uses the ONNX backend (``onnx=True``) instead of torch.jit because
    ``torch.jit.load`` on Windows breaks when the model file path
    contains non-ASCII characters (e.g. ``C:\\Users\\Táta\\…\\silero_vad.jit``)
    — torch's underlying fopen can't decode the path. The ONNX path
    goes through onnxruntime which handles unicode paths correctly.
    """
    global _VAD_MODEL
    if _VAD_MODEL is None:
        from silero_vad import load_silero_vad
        _VAD_MODEL = load_silero_vad(onnx=True)
    return _VAD_MODEL


# --- Data ------------------------------------------------------------------


@dataclass
class Candidate:
    """A scored 5-10 s window inside the source. Times are in seconds
    relative to the start of the analyzed audio (not the original file
    if a chapter / first-N-min slice was made upstream)."""
    start_s: float
    end_s: float
    score: float                      # composite, 0-100
    breakdown: dict[str, float] = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    def to_dict(self) -> dict:
        return asdict(self)


# --- Public entry ----------------------------------------------------------


def find_candidates(
    wav_path: Path | str,
    top_n: int = TOP_N,
    target_min_s: float = TARGET_MIN_S,
    target_max_s: float = TARGET_MAX_S,
) -> list[Candidate]:
    """Return up to ``top_n`` non-overlapping candidate windows ranked by
    composite quality score.

    Input WAV must be 16-bit PCM mono. Any sample rate is accepted —
    we resample down to 16 kHz internally for VAD and keep the native
    rate for the audio scoring pass.

    Returns an empty list if the source has no usable phrase-bounded
    windows in the [target_min_s, target_max_s] range.
    """
    wav_path = Path(wav_path)
    samples, sample_rate = _read_pcm(wav_path)
    segments = _vad_segments(samples, sample_rate)
    if not segments:
        return []
    anchors_start, anchors_end = _phrase_anchors(segments, ANCHOR_GAP_MS / 1000.0)
    raw = _enumerate_windows(
        anchors_start, anchors_end, target_min_s, target_max_s,
    )
    if not raw:
        return []
    speech_mask = _build_speech_mask(segments, len(samples), sample_rate)
    scored = [
        _score_window(s, e, samples, sample_rate, speech_mask)
        for (s, e) in raw
    ]
    scored.sort(key=lambda c: c.score, reverse=True)
    return _select_nonoverlapping(scored, top_n, MIN_NONOVERLAP_RATIO)


# --- VAD pass --------------------------------------------------------------


def _vad_segments(
    samples: np.ndarray,
    sample_rate: int,
) -> list[tuple[float, float]]:
    """Run Silero VAD on already-loaded PCM samples. ``samples`` is
    a float32 mono array in [-1, 1]; ``sample_rate`` is its native rate.

    Silero needs 16 kHz so we resample via ``torchaudio.functional.resample``
    (pure torch — no torchcodec / ffmpeg needed). Returns
    ``[(start_s, end_s), …]`` in seconds at the *original* timeline.
    """
    import torch
    import torchaudio.functional as TAF
    from silero_vad import get_speech_timestamps

    audio = torch.from_numpy(samples).float()
    if sample_rate != VAD_SAMPLING_RATE:
        audio = TAF.resample(audio, sample_rate, VAD_SAMPLING_RATE)
    ts = get_speech_timestamps(
        audio,
        _vad_model(),
        threshold=VAD_THRESHOLD,
        sampling_rate=VAD_SAMPLING_RATE,
        min_speech_duration_ms=VAD_MIN_SPEECH_MS,
        min_silence_duration_ms=VAD_MIN_SILENCE_MS,
        speech_pad_ms=VAD_SPEECH_PAD_MS,
        return_seconds=True,
    )
    return [(float(t["start"]), float(t["end"])) for t in ts]


# --- Anchor discovery ------------------------------------------------------


def _phrase_anchors(
    segments: list[tuple[float, float]],
    min_gap_s: float,
) -> tuple[list[float], list[float]]:
    """Boundaries adjacent to a pause ≥ ``min_gap_s``.

    A "start anchor" is a segment.start where the gap before it is
    long (so splicing in here lands on a fresh phrase). The first
    segment is always a start anchor.

    An "end anchor" is a segment.end where the gap after it is long
    (so splicing out here lands on a complete phrase). The last
    segment is always an end anchor.
    """
    if not segments:
        return [], []
    starts: list[float] = [segments[0][0]]
    ends: list[float] = []
    for i in range(len(segments) - 1):
        cur_end = segments[i][1]
        next_start = segments[i + 1][0]
        if next_start - cur_end >= min_gap_s:
            ends.append(cur_end)
            starts.append(next_start)
    ends.append(segments[-1][1])
    return starts, ends


def _enumerate_windows(
    anchors_start: list[float],
    anchors_end: list[float],
    target_min_s: float,
    target_max_s: float,
) -> list[tuple[float, float]]:
    """Pair every start-anchor with every later end-anchor whose
    distance falls inside the target window. Output is unsorted."""
    out: list[tuple[float, float]] = []
    for s in anchors_start:
        for e in anchors_end:
            d = e - s
            if d < target_min_s:
                continue
            if d > target_max_s:
                # End-anchors are sorted ascending — once we overshoot
                # we can break the inner loop early.
                break
            out.append((s, e))
    return out


# --- Scoring ---------------------------------------------------------------


def _read_pcm(wav_path: Path) -> tuple[np.ndarray, int]:
    """Read a 16-bit mono PCM WAV into a float32 array in [-1, 1].
    The voice draft pipeline always converts to mono 16-bit before
    calling us (audio.convert_voice_ref), so we don't bother handling
    multi-channel input here."""
    with wave.open(str(wav_path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"expected 16-bit PCM, got sampwidth={w.getsampwidth()}")
        if w.getnchannels() != 1:
            raise ValueError(f"expected mono, got channels={w.getnchannels()}")
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    return samples, rate


def _build_speech_mask(
    segments: list[tuple[float, float]],
    n_samples: int,
    sample_rate: int,
) -> np.ndarray:
    """Bool mask the same length as the audio. True where Silero said
    "speech". Used by the SNR proxy and density calc."""
    mask = np.zeros(n_samples, dtype=bool)
    for s, e in segments:
        i0 = max(0, int(s * sample_rate))
        i1 = min(n_samples, int(e * sample_rate))
        if i1 > i0:
            mask[i0:i1] = True
    return mask


def _score_window(
    start_s: float,
    end_s: float,
    samples: np.ndarray,
    sample_rate: int,
    speech_mask: np.ndarray,
) -> Candidate:
    """Score one window. Each component is normalized to [0, 1]; the
    composite is reported on a [0, 100] scale so the UI can render it
    as a familiar percent."""
    i0 = max(0, int(start_s * sample_rate))
    i1 = min(len(samples), int(end_s * sample_rate))
    win = samples[i0:i1]
    win_mask = speech_mask[i0:i1]

    density = float(win_mask.sum() / max(1, len(win_mask)))
    # Asymmetric triangle peaked at 0.88. Going under is recoverable
    # (the source just had a longer pause); going over 0.95 is bad
    # because OmniVoice expects natural micro-breaths in the reference.
    density_score = max(0.0, 1.0 - max(
        (TARGET_DENSITY - density) / 0.38,
        (density - TARGET_DENSITY) / 0.10,
    ))

    chunk_n = max(1, int(RMS_CHUNK_MS / 1000.0 * sample_rate))
    n_chunks = max(1, len(win) // chunk_n)
    rms_per_chunk = np.array([
        float(np.sqrt(np.mean(win[k * chunk_n:(k + 1) * chunk_n] ** 2)))
        for k in range(n_chunks)
    ])
    mean_rms = float(rms_per_chunk.mean())
    if mean_rms > 1e-6:
        cv = float(rms_per_chunk.std() / mean_rms)   # coefficient of variation
    else:
        cv = 1.0
    consistency_score = max(0.0, 1.0 - cv)

    peak = float(np.max(np.abs(win))) if len(win) else 0.0
    clipping_score = max(0.0, 1.0 - max(0.0, (peak - 0.90)) / 0.10)

    speech_samples = win[win_mask]
    silence_samples = win[~win_mask]
    rms_speech = float(np.sqrt(np.mean(speech_samples ** 2))) if len(speech_samples) else 0.0
    # Need at least 50 ms of silence to compute a meaningful SNR. Without
    # that, "silence_samples" is just edge-of-VAD spillover and rms_silence
    # collapses to ~0 → snr_db blows up to >100 and we end up rewarding
    # density=1.00 windows. Treat low-silence cases as neutral (0.5).
    silence_threshold_samples = int(0.05 * sample_rate)
    if len(silence_samples) < silence_threshold_samples:
        snr_db = float("nan")
        snr_score = 0.5
    else:
        rms_silence = float(np.sqrt(np.mean(silence_samples ** 2)))
        snr_db = 20.0 * math.log10((rms_speech + 1e-6) / (rms_silence + 1e-6))
        # Cap at 30 dB — anything above is diminishing returns and likely
        # the result of a near-silent silence floor (gated/denoised audio).
        snr_score = max(0.0, min(1.0, (snr_db - 10.0) / 20.0))

    composite = (
        1.5 * density_score
        + 1.0 * consistency_score
        + 1.0 * clipping_score
        + 1.5 * snr_score
    ) / 5.0
    return Candidate(
        start_s=round(start_s, 3),
        end_s=round(end_s, 3),
        score=round(composite * 100.0, 1),
        breakdown={
            "density": round(density, 3),
            "density_score": round(density_score, 3),
            "rms_cv": round(cv, 3),
            "consistency_score": round(consistency_score, 3),
            "peak": round(peak, 3),
            "clipping_score": round(clipping_score, 3),
            "snr_db": None if math.isnan(snr_db) else round(snr_db, 1),
            "snr_score": round(snr_score, 3),
        },
    )


def _select_nonoverlapping(
    candidates: list[Candidate],
    top_n: int,
    max_overlap_ratio: float,
) -> list[Candidate]:
    """Greedy top-N pick. Skips a candidate if more than
    ``max_overlap_ratio`` of its duration overlaps an already-picked
    one — keeps the surfaced set varied across the source."""
    picked: list[Candidate] = []
    for c in candidates:
        if all(_overlap_ratio(c, p) <= max_overlap_ratio for p in picked):
            picked.append(c)
            if len(picked) >= top_n:
                break
    return picked


def _overlap_ratio(a: Candidate, b: Candidate) -> float:
    lo = max(a.start_s, b.start_s)
    hi = min(a.end_s, b.end_s)
    overlap = max(0.0, hi - lo)
    shorter = min(a.duration_s, b.duration_s)
    return overlap / shorter if shorter > 0 else 0.0


# --- Smoke test ------------------------------------------------------------

if __name__ == "__main__":
    # `python -m audiomat.voice_extract <wav>`
    # WAV must be 16-bit mono. Easiest way to get one: feed any source
    # through audiomat.audio.convert_voice_ref first.
    import sys
    if len(sys.argv) < 2:
        print("usage: python -m audiomat.voice_extract <wav>")
        sys.exit(2)
    p = Path(sys.argv[1])
    if not p.exists():
        print(f"not found: {p}")
        sys.exit(2)
    print(f"Analyzing {p.name} …")
    cands = find_candidates(p)
    if not cands:
        print("(no candidates)")
        sys.exit(0)
    print(f"Top {len(cands)} candidates:")
    print(f"  {'#':>2}  {'start':>7}  {'end':>7}  {'dur':>5}  {'score':>5}  breakdown")
    for i, c in enumerate(cands, 1):
        bd = (
            f"den={c.breakdown['density']:.2f} "
            f"cv={c.breakdown['rms_cv']:.2f} "
            f"peak={c.breakdown['peak']:.2f} "
            f"snr={c.breakdown['snr_db']:.0f}dB"
        )
        print(
            f"  {i:>2}  {c.start_s:>7.2f}  {c.end_s:>7.2f}  "
            f"{c.duration_s:>5.2f}  {c.score:>5.1f}  {bd}"
        )
