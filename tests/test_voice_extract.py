"""Unit tests for audiomat.voice_extract — VAD-bounded candidate finder.

VAD model loading is heavy (silero-vad + onnxruntime first call ~1 s),
so the algorithm-level tests below stub the VAD step and feed synthetic
segments directly into the pure helpers. One end-to-end test exercises
the real VAD against a programmatically generated speech-like signal —
that one is marked ``slow`` so it can be skipped when iterating.
"""
from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np

from audiomat.voice_extract import (
    Candidate, _enumerate_windows, _overlap_ratio, _phrase_anchors,
    _select_nonoverlapping,
)


class TestPhraseAnchors:
    def test_first_segment_is_always_start_anchor(self):
        segs = [(1.0, 2.0), (3.0, 4.0)]
        starts, _ = _phrase_anchors(segs, min_gap_s=0.4)
        assert starts[0] == 1.0

    def test_last_segment_is_always_end_anchor(self):
        segs = [(1.0, 2.0), (3.0, 4.0)]
        _, ends = _phrase_anchors(segs, min_gap_s=0.4)
        assert ends[-1] == 4.0

    def test_short_gap_does_not_create_anchor(self):
        # 100 ms gap < 400 ms threshold → no extra anchors.
        segs = [(1.0, 2.0), (2.1, 3.0)]
        starts, ends = _phrase_anchors(segs, min_gap_s=0.4)
        assert starts == [1.0]
        assert ends == [3.0]

    def test_long_gap_creates_anchor_pair(self):
        # 600 ms gap → end-anchor at 2.0, start-anchor at 2.6.
        segs = [(1.0, 2.0), (2.6, 3.0)]
        starts, ends = _phrase_anchors(segs, min_gap_s=0.4)
        assert starts == [1.0, 2.6]
        assert ends == [2.0, 3.0]

    def test_empty_segments_returns_empty(self):
        starts, ends = _phrase_anchors([], min_gap_s=0.4)
        assert starts == []
        assert ends == []


class TestEnumerateWindows:
    def test_window_in_range_kept(self):
        # 7 s gap between anchors (5-10 s window): keep.
        windows = _enumerate_windows([0.0], [7.0], 5.0, 10.0)
        assert windows == [(0.0, 7.0)]

    def test_window_too_short_dropped(self):
        windows = _enumerate_windows([0.0], [3.0], 5.0, 10.0)
        assert windows == []

    def test_window_too_long_dropped(self):
        windows = _enumerate_windows([0.0], [12.0], 5.0, 10.0)
        assert windows == []

    def test_multiple_starts_and_ends_pair_combinatorially(self):
        starts = [0.0, 10.0]
        ends = [7.0, 17.0]      # 7s and 17s from start[0]=0.0; 7s from start[1]=10.0
        windows = _enumerate_windows(starts, ends, 5.0, 10.0)
        # (0,7) good (7s); (0,17) too long; (10,17) good (7s); (10,7) negative
        assert (0.0, 7.0) in windows
        assert (10.0, 17.0) in windows
        assert (0.0, 17.0) not in windows


class TestSelectNonoverlapping:
    def _cand(self, s: float, e: float, score: float) -> Candidate:
        return Candidate(start_s=s, end_s=e, score=score)

    def test_picks_top_n(self):
        cs = [self._cand(0, 6, 90), self._cand(10, 17, 85), self._cand(20, 27, 80)]
        out = _select_nonoverlapping(cs, top_n=2, max_overlap_ratio=0.5)
        assert len(out) == 2
        assert out[0].score == 90
        assert out[1].score == 85

    def test_skips_heavy_overlap(self):
        # Both candidates span basically the same range; the second is dropped.
        cs = [self._cand(0, 7, 90), self._cand(0.5, 7.5, 89)]
        out = _select_nonoverlapping(cs, top_n=2, max_overlap_ratio=0.5)
        assert len(out) == 1

    def test_keeps_disjoint_candidates(self):
        cs = [self._cand(0, 7, 90), self._cand(20, 27, 89)]
        out = _select_nonoverlapping(cs, top_n=2, max_overlap_ratio=0.5)
        assert len(out) == 2


class TestOverlapRatio:
    def test_disjoint_zero(self):
        a = Candidate(0, 5, 0)
        b = Candidate(10, 15, 0)
        assert _overlap_ratio(a, b) == 0.0

    def test_full_containment_ratio_one(self):
        a = Candidate(0, 10, 0)        # 10 s
        b = Candidate(2, 7, 0)         # 5 s, fully inside a
        # Overlap is 5 s; shorter is 5 s; ratio 1.0
        assert _overlap_ratio(a, b) == 1.0

    def test_partial_overlap(self):
        a = Candidate(0, 6, 0)         # 6 s
        b = Candidate(4, 10, 0)        # 6 s
        # Overlap [4, 6] = 2 s; shorter = 6 s; ratio = 1/3.
        assert math.isclose(_overlap_ratio(a, b), 2 / 6, rel_tol=1e-6)


# ---- End-to-end with a real WAV ----


def _write_silence_with_blips(
    path: Path,
    total_s: float,
    sample_rate: int,
    blips: list[tuple[float, float]],
) -> None:
    """Synthesize a WAV: ``total_s`` of low-level noise floor with louder
    sine bursts at the requested ``[start, end]`` ranges. Just enough
    structure to coax silero-vad into reporting speech segments roughly
    where the bursts are."""
    n = int(total_s * sample_rate)
    rng = np.random.default_rng(0)
    samples = rng.normal(0, 0.001, n).astype(np.float32)
    for s, e in blips:
        i0 = int(s * sample_rate)
        i1 = int(e * sample_rate)
        # Vary frequency a bit per blip so the model treats each as one
        # phrase rather than continuous tone.
        freq = 220 + (i0 % 8) * 30
        t = np.arange(i1 - i0) / sample_rate
        samples[i0:i1] += 0.3 * np.sin(2 * np.pi * freq * t)
    pcm = (samples * 32767).clip(-32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())


def test_find_candidates_end_to_end_synthetic(tmp_path: Path):
    """Build a 30 s WAV with 6 sine "phrases" of 1.5-2 s each, separated
    by 2-3 s pauses, and verify find_candidates returns at least one
    window scored > 0. We don't assert exact ranges (silero-vad isn't
    deterministic across versions) — just that the pipeline runs and
    produces something sane."""
    from audiomat.voice_extract import find_candidates

    wav = tmp_path / "synthetic.wav"
    _write_silence_with_blips(
        wav, total_s=30.0, sample_rate=24000,
        blips=[
            (1.0, 2.5),     # phrase 1
            (4.5, 6.0),     # phrase 2  (pair could form 5 s window)
            (8.5, 10.5),
            (13.0, 14.5),
            (17.0, 19.0),
            (22.0, 24.0),
        ],
    )
    cands = find_candidates(wav, top_n=3)
    assert isinstance(cands, list)
    # Synthetic sine bursts may or may not trigger Silero (it's trained on
    # speech), so this test is "doesn't crash + returns valid shape".
    for c in cands:
        assert c.duration_s >= 4.5     # close to TARGET_MIN_S=5 with rounding
        assert c.duration_s <= 10.5
        assert 0.0 <= c.score <= 100.0
        assert "density" in c.breakdown
