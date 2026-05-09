"""Audio I/O glue: format conversion, chunk concatenation, M4B build.

This is the only module that talks to ffmpeg. We bundle ffmpeg via the
``imageio-ffmpeg`` Python package, so no system-level ffmpeg install is
required.

Three responsibilities:

1. **Voice ref normalization** — any input WAV/MP3/OGG/etc. → 24 kHz mono
   16-bit PCM (OmniVoice's native rate).
2. **Per-chapter concat + loudness** — N chunk WAVs → 1 chapter WAV with
   200 ms inter-chunk silence + dynaudnorm (intra-chunk RMS smoothing)
   + loudnorm (target LUFS, -1.5 dBTP, 11 LRA).
3. **M4B build** — chapter WAVs → AAC mono + chapter markers + ID3-style
   metadata + optional cover art.
"""
from __future__ import annotations

import re
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg


# ----------------------------------------------------------------------------
# ffmpeg helpers
# ----------------------------------------------------------------------------


_FFMPEG_PATH: str | None = None


def ffmpeg_path() -> str:
    """Return the path to the bundled ffmpeg binary. Resolved once and cached."""
    global _FFMPEG_PATH
    if _FFMPEG_PATH is None:
        _FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
    return _FFMPEG_PATH


def _run(cmd: list[str], context: str) -> None:
    """Run an ffmpeg command. Raises RuntimeError with the tail of stderr
    on non-zero exit. UTF-8 with replace decoding so Czech filenames /
    metadata don't crash the error path."""
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0:
        tail = (res.stderr or res.stdout or "")[-2000:]
        raise RuntimeError(f"ffmpeg failed ({context}):\n{tail}")


# ----------------------------------------------------------------------------
# 1) Voice reference normalization
# ----------------------------------------------------------------------------


VOICE_TARGET_RATE = 24000     # OmniVoice native
VOICE_TARGET_CHANNELS = 1


@dataclass
class AudioInfo:
    """Output of :func:`probe`. Fields read from a WAV header (we only
    accept WAV here — MP3/OGG/etc. get converted first)."""
    duration_s: float
    sample_rate: int
    channels: int


def probe_wav(path: Path | str) -> AudioInfo:
    """Read a WAV header and return duration / sample rate / channel count.
    Used to populate Voice.meta after conversion."""
    with wave.open(str(path), "rb") as w:
        frames = w.getnframes()
        rate = w.getframerate()
        channels = w.getnchannels()
    duration = frames / rate if rate > 0 else 0.0
    return AudioInfo(duration_s=duration, sample_rate=rate, channels=channels)


def convert_voice_ref(
    in_path: Path | str,
    out_path: Path | str,
    sample_rate: int = VOICE_TARGET_RATE,
    channels: int = VOICE_TARGET_CHANNELS,
) -> AudioInfo:
    """Convert any voice-ref input to 24 kHz mono 16-bit PCM WAV.

    OmniVoice can technically take any input format and resample
    internally, but pre-converting:

    1. Lets us probe duration/SR cleanly (we read WAV headers, not OGG).
    2. Avoids per-chunk resampling overhead (149 chapter-1 chunks would
       resample 149×).
    3. Catches malformed input early at upload time, not at first render.

    Returns the post-conversion :class:`AudioInfo` so the caller can
    populate Voice.meta directly."""
    in_path = Path(in_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not in_path.exists():
        raise FileNotFoundError(in_path)

    cmd = [
        ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(in_path),
        "-ac", str(channels),
        "-ar", str(sample_rate),
        "-sample_fmt", "s16",
        "-c:a", "pcm_s16le",
        str(out_path),
    ]
    _run(cmd, f"convert_voice_ref {in_path.name} → {out_path.name}")
    return probe_wav(out_path)


# ----------------------------------------------------------------------------
# 2) Per-chapter concat + loudness normalization
# ----------------------------------------------------------------------------


def _silence_wav(out_path: Path, gap_ms: int, sample_rate: int) -> Path:
    """Generate a single silent WAV of the requested duration. Cached on
    disk by path — caller picks a stable filename like
    ``chunks_root/_silence_200ms_24000.wav``."""
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi",
        "-i", f"anullsrc=channel_layout=mono:sample_rate={sample_rate}",
        "-t", f"{gap_ms / 1000}",
        "-c:a", "pcm_s16le",
        str(out_path),
    ]
    _run(cmd, f"silence {gap_ms}ms @ {sample_rate}Hz")
    return out_path


def concat_chunks_loudnorm(
    chunk_paths: list[Path],
    out_path: Path,
    sample_rate: int = VOICE_TARGET_RATE,
    silence_gap_ms: int = 200,
    target_lufs: float = -16.0,
) -> None:
    """Concatenate chunk WAVs with inter-chunk silence and apply loudness
    normalization (dynaudnorm pre-flatten + loudnorm to target LUFS).

    The chain ``dynaudnorm=p=0.95:m=15:s=12,loudnorm=I=<lufs>:TP=-1.5:LRA=11``
    is the same setup the s2.cpp / OmniVoice production renders use:
    dynaudnorm smooths intra-chunk RMS swings before loudnorm so the LUFS
    pass doesn't ramp gain audibly across chunk joins.

    All chunk WAVs must already be at ``sample_rate`` (the silence pad is
    generated at ``sample_rate``, and ffmpeg concat demuxer requires
    matching codec params across all inputs).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    silence_path = out_path.parent / f"_silence_{silence_gap_ms}ms_{sample_rate}.wav"
    _silence_wav(silence_path, silence_gap_ms, sample_rate)

    list_path = out_path.parent / f"_concat_{out_path.stem}.txt"
    with list_path.open("w", encoding="utf-8", newline="\n") as f:
        for i, w in enumerate(chunk_paths):
            posix = w.resolve().as_posix().replace("'", "'\\''")
            f.write(f"file '{posix}'\n")
            if i < len(chunk_paths) - 1:
                sil = silence_path.resolve().as_posix().replace("'", "'\\''")
                f.write(f"file '{sil}'\n")

    cmd = [
        ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-af", f"dynaudnorm=p=0.95:m=15:s=12,loudnorm=I={target_lufs}:TP=-1.5:LRA=11",
        "-c:a", "pcm_s16le",
        str(out_path),
    ]
    try:
        _run(cmd, f"concat+loudnorm → {out_path.name}")
    finally:
        list_path.unlink(missing_ok=True)


# ----------------------------------------------------------------------------
# 3) M4B build (chapter WAVs → AAC + chapter markers + metadata)
# ----------------------------------------------------------------------------


_CHAPTER_STEM_RE = re.compile(r"^(\d{3}[a-z]?)_(.+)$")


def _wav_duration_ms(p: Path) -> int:
    """Read WAV duration in integer milliseconds. Used to compute chapter
    marker boundaries for the M4B."""
    with wave.open(str(p), "rb") as w:
        frames = w.getnframes()
        rate = w.getframerate()
    return int(round(frames * 1000 / rate))


def _chapter_title_from_stem(stem: str) -> str:
    """``001a_Zima_2019`` → ``001a Zima 2019``. Falls back to underscore-
    replace for any stem that doesn't match the NNN[a-z]_ pattern."""
    m = _CHAPTER_STEM_RE.match(stem)
    if m:
        num, rest = m.groups()
        return f"{num} {rest.replace('_', ' ')}"
    return stem.replace("_", " ")


@dataclass
class M4BMetadata:
    """ID3-style metadata embedded into the M4B for audiobook players."""
    title: str
    artist: str = ""
    album: str = ""
    narrator: str = ""
    genre: str = "Audiobook"
    cover: Path | None = None
    bitrate: str = "64k"        # AAC, mono speech (not music)


def collect_chapter_wavs(chunks_root: Path) -> list[tuple[str, Path, int]]:
    """Walk ``<chunks_root>/<NNN_stem>/<NNN_stem>.wav`` in alphabetical
    order, returning ``(stem, wav_path, duration_ms)`` for each non-empty
    final per-chapter WAV. Empty / missing WAVs are skipped with no error.
    """
    items: list[tuple[str, Path, int]] = []
    if not chunks_root.exists():
        return items
    for d in sorted(chunks_root.iterdir()):
        if not d.is_dir():
            continue
        wav = d / f"{d.name}.wav"
        if not wav.exists() or wav.stat().st_size < 1024:
            continue
        items.append((d.name, wav, _wav_duration_ms(wav)))
    return items


def _write_m4b_metadata(path: Path, items: list[tuple[str, Path, int]],
                        meta: M4BMetadata) -> None:
    """Write the ffmpeg-compatible metadata file with chapter markers."""
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(";FFMETADATA1\n")
        f.write(f"title={meta.title}\n")
        if meta.artist:
            f.write(f"artist={meta.artist}\n")
            f.write(f"album_artist={meta.artist}\n")
        if meta.album:
            f.write(f"album={meta.album}\n")
        f.write(f"genre={meta.genre}\n")
        if meta.narrator:
            f.write(f"composer={meta.narrator}\n")
            f.write(f"comment=Narrated by {meta.narrator}\n")
        cur_ms = 0
        for stem, _wav, dur in items:
            f.write("\n[CHAPTER]\n")
            f.write("TIMEBASE=1/1000\n")
            f.write(f"START={cur_ms}\n")
            f.write(f"END={cur_ms + dur}\n")
            f.write(f"title={_chapter_title_from_stem(stem)}\n")
            cur_ms += dur


def _write_concat_list(path: Path, items: list[tuple[str, Path, int]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for _stem, wav, _dur in items:
            posix = wav.resolve().as_posix().replace("'", "'\\''")
            f.write(f"file '{posix}'\n")


def build_m4b(
    chunks_root: Path,
    out_path: Path,
    meta: M4BMetadata,
) -> tuple[int, int]:
    """Build an M4B from per-chapter WAVs in ``chunks_root``.

    Encodes to AAC mono at ``meta.bitrate`` (default 64k — fine for speech)
    and embeds chapter markers + metadata. Returns ``(chapter_count,
    total_ms)`` for the caller's progress message.

    Cover art is optional — if provided as ``meta.cover``, it's attached as
    the front cover (audiobook player thumbnail).
    """
    items = collect_chapter_wavs(chunks_root)
    if not items:
        raise FileNotFoundError(f"no chapter WAVs in {chunks_root}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path = out_path.parent / f"_m4b_metadata_{out_path.stem}.txt"
    list_path = out_path.parent / f"_m4b_concat_{out_path.stem}.txt"
    _write_m4b_metadata(meta_path, items, meta)
    _write_concat_list(list_path, items)

    cmd = [
        ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-i", str(meta_path),
    ]
    if meta.cover and meta.cover.exists():
        cmd += ["-i", str(meta.cover)]
        cmd += ["-map", "0:a", "-map", "2:v", "-disposition:v", "attached_pic"]
    cmd += [
        "-map_metadata", "1",
        "-c:a", "aac", "-b:a", meta.bitrate, "-ac", "1",
        "-movflags", "+faststart",
        "-f", "mp4",
        str(out_path),
    ]
    try:
        _run(cmd, f"build_m4b → {out_path.name}")
    finally:
        meta_path.unlink(missing_ok=True)
        list_path.unlink(missing_ok=True)

    total_ms = sum(d for _, _, d in items)
    return len(items), total_ms


if __name__ == "__main__":
    # Smoke test — imports + ffmpeg path resolution. Doesn't actually run
    # any conversion.
    print(f"ffmpeg path: {ffmpeg_path()}")
    print(f"VOICE_TARGET_RATE     = {VOICE_TARGET_RATE}")
    print(f"VOICE_TARGET_CHANNELS = {VOICE_TARGET_CHANNELS}")
    print(f"_chapter_title_from_stem('001a_Zima_2019') = {_chapter_title_from_stem('001a_Zima_2019')!r}")
    print(f"_chapter_title_from_stem('xyz')            = {_chapter_title_from_stem('xyz')!r}")
