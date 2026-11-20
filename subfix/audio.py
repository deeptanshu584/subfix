"""Audio extraction with ffmpeg and memory-mapped access to the cached 16 kHz mono WAV.

A 3-hour movie becomes a ~345 MB int16 WAV in the cache; it is never fully loaded into memory —
consumers read float32 windows through :meth:`AudioFile.read` / :meth:`AudioFile.iter_chunks`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import progress as progress_hooks
from .config import CACHE_VERSION, SAMPLE_RATE, cache_dir
from .log import log, progress


class AudioError(RuntimeError):
    """Raised when audio cannot be extracted or read."""


def _winget_links() -> list[Path]:
    if sys.platform != "win32":
        return []
    base = os.environ.get("LOCALAPPDATA")
    return [Path(base) / "Microsoft" / "WinGet" / "Links"] if base else []


def find_tool(name: str) -> str:
    """Locate ``ffmpeg``/``ffprobe``: ``$SUBFIX_FFMPEG`` dir → PATH → WinGet links folder."""

    def _check(c: Path) -> str | None:
        for cand in (c, c.with_suffix(".exe")):
            if cand.is_file():
                return str(cand)
        return None

    env = os.environ.get("SUBFIX_FFMPEG")
    if env:
        p = Path(env)
        hit = _check(p.parent / name if p.is_file() else p / name)
        if hit:
            return hit
    found = shutil.which(name)
    if found:
        return found
    for d in _winget_links():
        hit = _check(d / name)
        if hit:
            return hit
    raise AudioError(
        f"{name} not found. Install ffmpeg (Windows: `winget install Gyan.FFmpeg.Essentials`, "
        f"macOS: `brew install ffmpeg`, Linux: `apt install ffmpeg`) or set SUBFIX_FFMPEG."
    )


def probe(path: Path) -> dict:
    """Return ffprobe's JSON description of ``path`` (format + streams)."""
    exe = find_tool("ffprobe")
    try:
        out = subprocess.run(
            [
                exe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise AudioError(f"ffprobe could not read {path}: {exc.stderr.strip()[-300:]}") from exc
    return json.loads(out or "{}")


def audio_streams(path: Path) -> list[dict]:
    """Audio streams of a media file (each dict has ``index``, ``codec_name``, ``tags`` ...)."""
    return [s for s in probe(path).get("streams", []) if s.get("codec_type") == "audio"]


def media_duration(path: Path) -> float:
    """Duration in seconds (0.0 if unknown)."""
    try:
        return float(probe(path).get("format", {}).get("duration", 0.0))
    except (ValueError, AudioError):
        return 0.0


_CENTER_LAYOUTS = ("3.0", "3.1", "4.0", "4.1", "5.0", "5.1", "6.1", "7.0", "7.1")


def has_center_channel(stream: dict) -> bool:
    """True if an ffprobe audio stream has a front-centre (dialogue) channel, e.g. 5.1."""
    layout = str(stream.get("channel_layout", ""))
    return layout.startswith(_CENTER_LAYOUTS) or (not layout and stream.get("channels", 0) >= 6)


def file_fingerprint(path: Path, extra: str = "") -> str:
    """Cheap, stable cache key: absolute path + size + mtime + first/last 64 KiB."""
    path = Path(path).resolve()
    st = path.stat()
    h = hashlib.sha1()
    h.update(f"{path}|{st.st_size}|{int(st.st_mtime)}|{extra}|v{CACHE_VERSION}".encode())
    with open(path, "rb") as f:
        h.update(f.read(65536))
        if st.st_size > 131072:
            f.seek(-65536, os.SEEK_END)
            h.update(f.read(65536))
    return h.hexdigest()[:20]


def extract_audio(
    media: Path,
    audio_track: int = 0,
    out_path: Path | None = None,
    force: bool = False,
) -> Path:
    """Extract track ``audio_track`` of ``media`` to a cached 16 kHz mono PCM WAV.

    Returns the WAV path. Re-runs on the same file are instant (cache hit).
    """
    media = Path(media)
    if not media.is_file():
        raise AudioError(f"File not found: {media}")
    streams = audio_streams(media)
    if not streams:
        raise AudioError(f"{media.name} has no audio track")
    if audio_track >= len(streams):
        raise AudioError(
            f"{media.name} has {len(streams)} audio track(s); --audio-track {audio_track} "
            f"does not exist (tracks are numbered from 0)"
        )
    # Surround mixes put dialogue on the centre channel: mostly-centre audio gives VAD and
    # Whisper far less music/effects to fight with than a plain downmix.
    center = has_center_channel(streams[audio_track])
    mix = ["-af", "pan=mono|c0=0.8*FC+0.1*FL+0.1*FR"] if center else ["-ac", "1"]
    if out_path is None:
        key = file_fingerprint(media, extra=f"a{audio_track}" + ("|fc" if center else ""))
        out_path = cache_dir("audio") / f"{media.stem[:40]}.{key}.wav"
    if out_path.is_file() and not force:
        log.debug("audio cache hit: %s", out_path)
        progress_hooks.report("extract", 1.0, "cached")
        return out_path

    ffmpeg = find_tool("ffmpeg")
    duration = media_duration(media)
    tmp = out_path.with_suffix(".part.wav")
    cmd = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(media),
        "-map",
        f"0:a:{audio_track}",
        "-vn",
        "-sn",
        "-dn",
        *mix,
        "-ar",
        str(SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        "-progress",
        "pipe:1",
        "-nostats",
        str(tmp),
    ]
    log.info(
        "Extracting audio from [bold]%s[/]%s ...",
        media.name,
        " (centre/dialogue channel)" if center else "",
    )
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    with progress() as bar:
        task = bar.add_task("Extracting audio", total=max(duration, 1.0))
        assert proc.stdout is not None
        for line in proc.stdout:
            if progress_hooks.cancelled():
                proc.kill()
                proc.wait()
                tmp.unlink(missing_ok=True)
                progress_hooks.check_cancel()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    done = min(duration, int(line.split("=")[1]) / 1e6)
                except ValueError:
                    continue
                bar.update(task, completed=done)
                progress_hooks.report("extract", done / duration if duration else None)
        stderr = proc.stderr.read() if proc.stderr else ""
        proc.wait()
    if proc.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        hint = ""
        if "matches no streams" in stderr:
            hint = f" (the file has no audio track #{audio_track})"
        raise AudioError(f"ffmpeg failed{hint}: {stderr.strip()[-500:]}")
    os.replace(tmp, out_path)
    progress_hooks.report("extract", 1.0)
    return out_path


def _wav_data_offset(path: Path) -> tuple[int, int, int, int]:
    """Parse a PCM WAV header → (data_offset, data_bytes, sample_rate, channels)."""
    with open(path, "rb") as f:
        riff = f.read(12)
        if len(riff) < 12 or riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
            raise AudioError(f"Not a WAV file: {path}")
        sr = channels = bits = None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                raise AudioError(f"WAV has no data chunk: {path}")
            cid, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
            if cid == b"fmt ":
                fmt = f.read(size)
                channels, sr = struct.unpack("<HI", fmt[2:8])
                bits = struct.unpack("<H", fmt[14:16])[0]
            elif cid == b"data":
                if bits != 16:
                    raise AudioError("Only 16-bit PCM WAV is supported")
                offset = f.tell()
                total = path.stat().st_size - offset
                # ffmpeg writes size 0xFFFFFFFF when streaming; trust the file size then.
                data_bytes = total if size in (0, 0xFFFFFFFF) or size > total else size
                return offset, data_bytes, int(sr or 0), int(channels or 1)
            else:
                f.seek(size + (size & 1), os.SEEK_CUR)


@dataclass
class AudioFile:
    """Memory-mapped 16 kHz mono int16 WAV."""

    path: Path
    samples: np.ndarray  # int16 memmap
    sample_rate: int = SAMPLE_RATE

    @classmethod
    def open(cls, path: Path) -> AudioFile:
        offset, nbytes, sr, ch = _wav_data_offset(Path(path))
        if ch != 1:
            raise AudioError("Expected mono audio")
        n = nbytes // 2
        data = (
            np.memmap(path, dtype="<i2", mode="r", offset=offset, shape=(n,))
            if n
            else np.zeros(0, dtype="<i2")
        )
        return cls(Path(path), data, sr)

    @property
    def duration(self) -> float:
        return len(self.samples) / self.sample_rate

    def read(self, start: float = 0.0, end: float | None = None) -> np.ndarray:
        """Return float32 samples in [-1, 1] for ``[start, end)`` seconds (a copy)."""
        a = max(0, int(round(start * self.sample_rate)))
        b = (
            len(self.samples)
            if end is None
            else min(len(self.samples), int(end * self.sample_rate))
        )
        return (np.asarray(self.samples[a:b], dtype=np.float32) / 32768.0).astype(np.float32)

    def iter_chunks(self, chunk_s: float = 600.0) -> Iterator[tuple[float, np.ndarray]]:
        """Yield ``(start_seconds, float32 samples)`` for consecutive non-overlapping chunks."""
        t = 0.0
        while t < self.duration:
            yield t, self.read(t, t + chunk_s)
            t += chunk_s


def load_audio(media: Path, audio_track: int = 0) -> AudioFile:
    """Extract (cached) and open the audio of ``media``; 16 kHz mono WAVs are used directly."""
    media = Path(media)
    if media.suffix.lower() == ".wav":
        try:
            af = AudioFile.open(media)
            if af.sample_rate == SAMPLE_RATE:
                return af
        except AudioError:
            pass
    return AudioFile.open(extract_audio(media, audio_track))
