"""Voice activity detection: a per-frame speech-probability signal for a whole movie.

Uses the Silero VAD v6 ONNX model bundled with faster-whisper (no PyTorch). Audio is processed in
10-minute chunks read from the memory-mapped WAV, so memory stays flat for 3-hour films. The
resulting signal (one value per 32 ms) is cached as ``.npy`` next to the extracted audio.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import progress as progress_hooks
from .audio import AudioFile, file_fingerprint
from .config import CACHE_VERSION, cache_dir
from .log import log, progress

FRAME_SAMPLES = 512  # Silero window at 16 kHz
HOP_SECONDS = FRAME_SAMPLES / 16_000  # 0.032 s


@dataclass
class SpeechSignal:
    """Speech probability per frame, ``probs[i]`` covers ``[i*hop, (i+1)*hop)``."""

    probs: np.ndarray
    hop: float = HOP_SECONDS

    @property
    def duration(self) -> float:
        return len(self.probs) * self.hop

    def at_rate(self, rate_hz: float) -> np.ndarray:
        """Resample to ``rate_hz`` frames/s by linear interpolation of frame centres."""
        n = int(np.ceil(self.duration * rate_hz))
        t = (np.arange(n) + 0.5) / rate_hz
        centres = (np.arange(len(self.probs)) + 0.5) * self.hop
        return np.interp(t, centres, self.probs).astype(np.float32)

    def segments(
        self, threshold: float = 0.5, min_speech: float = 0.2, min_silence: float = 0.3
    ) -> list[tuple[float, float]]:
        """Speech intervals with hysteresis-free thresholding and gap/short-burst cleanup."""
        active = self.probs >= threshold
        if not active.any():
            return []
        edges = np.diff(np.concatenate([[0], active.astype(np.int8), [0]]))
        starts = np.flatnonzero(edges == 1) * self.hop
        ends = np.flatnonzero(edges == -1) * self.hop
        merged: list[list[float]] = []
        for s, e in zip(starts, ends, strict=True):
            if merged and s - merged[-1][1] < min_silence:
                merged[-1][1] = float(e)
            else:
                merged.append([float(s), float(e)])
        return [(s, e) for s, e in merged if e - s >= min_speech]

    def speech_fraction(self, threshold: float = 0.5) -> float:
        return float((self.probs >= threshold).mean()) if len(self.probs) else 0.0


def _silero_session():
    """Our own onnxruntime session (more threads than faster-whisper's default of 1)."""
    import os

    import onnxruntime
    from faster_whisper.utils import get_assets_path

    opts = onnxruntime.SessionOptions()
    opts.inter_op_num_threads = 1
    opts.intra_op_num_threads = max(1, min(4, (os.cpu_count() or 2) // 2))
    opts.enable_cpu_mem_arena = False
    opts.log_severity_level = 4
    path = os.path.join(get_assets_path(), "silero_vad_v6.onnx")
    return onnxruntime.InferenceSession(path, providers=["CPUExecutionProvider"], sess_options=opts)


def _run_silero(session, audio: np.ndarray, context: int = 64) -> np.ndarray:
    """Silero v6 over ``audio`` (len multiple of 512). Mirrors faster_whisper.vad.SileroVADModel."""
    frames = audio.reshape(-1, FRAME_SAMPLES)
    ctx = np.zeros((frames.shape[0], context), dtype=np.float32)
    ctx[1:] = frames[:-1, -context:]
    batched = np.concatenate([ctx, frames], axis=1)
    h = np.zeros((1, 1, 128), dtype=np.float32)
    c = np.zeros((1, 1, 128), dtype=np.float32)
    outs = []
    for i in range(0, batched.shape[0], 10_000):
        out, h, c = session.run(None, {"input": batched[i : i + 10_000], "h": h, "c": c})
        outs.append(out)
    return np.concatenate(outs, axis=0).reshape(-1).astype(np.float32)


def compute_speech_signal(audio: AudioFile, chunk_s: float = 600.0) -> SpeechSignal:
    """Run Silero VAD over the whole file in chunks (no caching)."""
    session = _silero_session()
    chunk_samples = int(chunk_s * audio.sample_rate) // FRAME_SAMPLES * FRAME_SAMPLES
    n = len(audio.samples)
    parts: list[np.ndarray] = []
    with progress() as bar:
        task = bar.add_task("Detecting speech (VAD)", total=n // max(1, audio.sample_rate))
        for a in range(0, n, chunk_samples):
            b = min(n, a + chunk_samples)
            x = np.asarray(audio.samples[a:b], dtype=np.float32) / 32768.0
            pad = (-len(x)) % FRAME_SAMPLES
            if pad:
                x = np.concatenate([x, np.zeros(pad, dtype=np.float32)])
            progress_hooks.check_cancel()
            parts.append(_run_silero(session, x))
            bar.update(task, completed=b // audio.sample_rate)
            progress_hooks.report("vad", b / max(1, n))
    probs = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
    return SpeechSignal(probs)


def speech_signal(audio: AudioFile, use_cache: bool = True) -> SpeechSignal:
    """Cached :func:`compute_speech_signal` keyed by the audio file's fingerprint."""
    key = f"{Path(audio.path).stem[:40]}.{file_fingerprint(audio.path)}"
    cache = cache_dir("vad") / f"{key}.silero6.v{CACHE_VERSION}.npy"
    if use_cache and cache.is_file():
        log.debug("VAD cache hit: %s", cache)
        progress_hooks.report("vad", 1.0, "cached")
        return SpeechSignal(np.load(cache))
    sig = compute_speech_signal(audio)
    if use_cache:
        tmp = cache.with_suffix(".tmp.npy")
        np.save(tmp, sig.probs)
        tmp.replace(cache)
    log.info("Speech detected in %.0f%% of the audio", 100 * sig.speech_fraction())
    return sig
