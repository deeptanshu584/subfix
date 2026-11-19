"""Shared signal tools for audio-based synchronisation.

All audio-only sync methods score a candidate timing the same way: a subtitle cue displayed while
someone is speaking earns credit, a cue displayed over silence is penalised::

    score = Σ_t  sub(t) · (p_speech(t) − θ)        θ = film's mean speech probability

where ``sub(t)`` is 1 while any cue is on screen. Both signals live on a 10 ms grid, so a full
search over all offsets is one FFT cross-correlation.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from ..srt_io import Subtitle
from ..vad import SpeechSignal

#: Grid rate for sync signals (frames per second) → 10 ms resolution.
RATE = 100

_NON_DIALOGUE = re.compile(r"^\s*([\[(].*[\])]|[♪♫#*].*|-+)\s*$", re.DOTALL)


@dataclass
class SyncResult:
    """Outcome of a sync method."""

    subs: list[Subtitle]
    method: str
    score: float  # mean of (p − θ) under the cues; higher = better aligned
    confidence: float  # method-specific, roughly 0..1+
    params: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        p = self.params
        if self.method == "offset":
            return f"offset {p['offset']:+.3f} s"
        if self.method == "drift":
            return f"scale {p['scale']:.6f}, offset {p['offset']:+.3f} s"
        if self.method == "piecewise":
            segs = p.get("segments", [])
            return (
                f"{len(segs)} segment(s): "
                + ", ".join(f"{o:+.2f}s" for _, _, o in segs[:6])
                + (" …" if len(segs) > 6 else "")
            )
        if self.method == "smart":
            return (
                f"{p.get('matched_lines', 0)}/{p.get('total_lines', 0)} lines matched to "
                f"transcript ({p.get('match_ratio', 0):.0%})"
            )
        return self.method


def is_dialogue(sub: Subtitle) -> bool:
    """Heuristic: exclude sound descriptions, music notes and absurdly long cues from estimation."""
    return sub.duration < 12.0 and not _NON_DIALOGUE.match(sub.text)


def dialogue(subs: Sequence[Subtitle]) -> list[Subtitle]:
    d = [s for s in subs if is_dialogue(s) and s.end > s.start]
    return d if len(d) >= max(3, len(subs) // 5) else [s for s in subs if s.end > s.start]


#: Value of the reference signal outside the audio: worse than silence, so cues are never
#: "parked" before the start or after the end of the film to dodge a weak VAD signal.
OUT_OF_RANGE = -0.5


def speech_threshold(speech: SpeechSignal) -> float:
    """Adaptive "is this speech?" level: the mean speech probability of the film.

    Silero is confident on clean dialogue (p≈1) but on dialogue under music/effects it often
    stays at 0.1–0.4 while true non-speech is ≈0.0. Centring on the film's own mean (clamped
    to [0.05, 0.5]) works for both: synthetic clean speech → ≈0.45, Tears of Steel → ≈0.09.
    """
    m = float(speech.probs.mean()) if len(speech.probs) else 0.5
    return float(np.clip(m, 0.05, 0.5))


def reference_signal(speech: SpeechSignal, rate: int = RATE) -> np.ndarray:
    """``p − θ`` on the sync grid (θ = :func:`speech_threshold`): >0 speech-like, <0 not."""
    return (speech.at_rate(rate) - speech_threshold(speech)).astype(np.float64)


def subtitle_signal(
    subs: Sequence[Subtitle], length: int, rate: int = RATE, scale: float = 1.0
) -> np.ndarray:
    """Indicator (0/1) of "a cue is on screen" on the sync grid for times ``scale * t``."""
    sig = np.zeros(length, dtype=np.float64)
    for s in subs:
        a = int(round(s.start * scale * rate))
        b = int(round(s.end * scale * rate))
        a, b = max(0, a), min(length, b)
        if b > a:
            sig[a:b] = 1.0
    return sig


def cross_correlate(
    ref: np.ndarray, sub: np.ndarray, max_lag: int
) -> tuple[np.ndarray, np.ndarray]:
    """``c[k] = Σ_t sub(t) · ref(t + k)`` for ``k ∈ [-max_lag, max_lag]`` via FFT.

    ``ref`` is extended with :data:`OUT_OF_RANGE` on both sides, so shifting cues outside the
    audio is penalised instead of scoring zero. Returns ``(lags, c)``.
    """
    tail = max(0, len(sub) + max_lag - len(ref))
    ref_p = np.concatenate([np.full(max_lag, OUT_OF_RANGE), ref, np.full(tail + 1, OUT_OF_RANGE)])
    n = len(ref_p) + len(sub) + 1
    nfft = 1 << (n - 1).bit_length()
    c = np.fft.irfft(np.fft.rfft(ref_p, nfft) * np.conj(np.fft.rfft(sub, nfft)), nfft)
    lags = np.arange(-max_lag, max_lag + 1)
    return lags, c[lags + max_lag]


class WindowSearch:
    """Fast offset search for *subsets* of cues (sliding windows).

    The reference is downsampled (default 25 Hz) and padded once; each window then correlates
    only against ``[window − max_offset, window + max_offset]`` instead of the whole film, so a
    100-minute film costs a few hundred small FFTs instead of a few hundred film-length ones.
    """

    def __init__(self, ref: np.ndarray, max_offset: float, factor: int = 4, rate: int = RATE):
        n = len(ref) // factor * factor
        self.rate = rate / factor
        coarse = ref[:n].reshape(-1, factor).mean(axis=1)
        self.max_lag = int(max_offset * self.rate)
        pad = np.full(self.max_lag, OUT_OF_RANGE)
        self.ref_ext = np.concatenate([pad, coarse, pad])
        self.n = len(coarse)

    def correlate(self, subs: Sequence[Subtitle]) -> tuple[np.ndarray, np.ndarray, float]:
        """Return ``(lags_seconds, normalised_correlation, cue_frames)`` for ``subs``."""
        a = max(0, int(subs[0].start * self.rate) - 1)
        b = int(max(s.end for s in subs) * self.rate) + 2
        sig = np.zeros(b - a)
        for s in subs:
            i, j = int(round(s.start * self.rate)) - a, int(round(s.end * self.rate)) - a
            sig[max(0, i) : max(0, j)] = 1.0
        on = sig.sum() or 1.0
        crop = self.ref_ext[a : b + 2 * self.max_lag]
        if len(crop) < len(sig) + 2 * self.max_lag:
            crop = np.concatenate(
                [crop, np.full(len(sig) + 2 * self.max_lag - len(crop), OUT_OF_RANGE)]
            )
        nfft = 1 << (len(crop) + len(sig)).bit_length()
        c = np.fft.irfft(np.fft.rfft(crop, nfft) * np.conj(np.fft.rfft(sig, nfft)), nfft)
        c = c[: 2 * self.max_lag + 1] / on
        lags = np.arange(-self.max_lag, self.max_lag + 1) / self.rate
        return lags, c, on

    def peaks(self, subs: Sequence[Subtitle], k: int = 3, suppress: float = 1.5):
        """Top-``k`` ``(score, offset_s, confidence)`` with non-maximum suppression."""
        lags, c, _ = self.correlate(subs)
        sup = int(suppress * self.rate)
        cc = c.copy()
        out = []
        for _ in range(k):
            i = int(np.argmax(cc))
            if not np.isfinite(cc[i]):
                break
            conf = peak_confidence(c, i, exclusion=int(2.0 * self.rate))
            out.append((float(c[i]), float(lags[i]), conf))
            cc[max(0, i - sup) : i + sup + 1] = -np.inf
        return out


def parabolic_peak(c: np.ndarray, i: int) -> float:
    """Sub-sample peak position around index ``i`` via a parabola through 3 points."""
    if 0 < i < len(c) - 1:
        y0, y1, y2 = c[i - 1], c[i], c[i + 1]
        denom = y0 - 2 * y1 + y2
        if denom < 0:
            return i + 0.5 * (y0 - y2) / denom
    return float(i)


def peak_confidence(c: np.ndarray, i: int, exclusion: int) -> float:
    """How much the best peak stands out: (peak − 2nd best outside ±exclusion) / spread."""
    if len(c) < 3:
        return 0.0
    mask = np.ones(len(c), dtype=bool)
    mask[max(0, i - exclusion) : i + exclusion + 1] = False
    if not mask.any():
        return 1.0
    second = c[mask].max()
    spread = c.std() + 1e-9
    return float((c[i] - second) / spread)


def alignment_score(subs: Sequence[Subtitle], ref: np.ndarray, rate: int = RATE) -> float:
    """Mean of the reference signal under all cue intervals. Higher = better aligned."""
    sig = subtitle_signal(subs, len(ref), rate)
    on = sig.sum()
    return float((sig * ref).sum() / on) if on else -1.0
