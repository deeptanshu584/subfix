"""Shared signal tools for audio-based synchronisation.

All audio-only sync methods score a candidate timing the same way: a subtitle cue displayed while
someone is speaking earns credit, a cue displayed over silence is penalised::

    score = Σ_t  sub(t) · (2·p_speech(t) − 1)

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
    score: float  # normalised alignment score in [-1, 1] (mean of 2p-1 under cues)
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


def reference_signal(speech: SpeechSignal, rate: int = RATE) -> np.ndarray:
    """``2p − 1`` on the sync grid: +1 = surely speech, −1 = surely silence."""
    return (2.0 * speech.at_rate(rate) - 1.0).astype(np.float64)


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

    Returns ``(lags, c)``.
    """
    n = len(ref) + len(sub) + 1
    nfft = 1 << (n - 1).bit_length()
    c = np.fft.irfft(np.fft.rfft(ref, nfft) * np.conj(np.fft.rfft(sub, nfft)), nfft)
    lags = np.arange(-max_lag, max_lag + 1)
    return lags, c[lags % nfft]


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
    """Mean of ``2p−1`` under all cue intervals (−1 … +1). Higher = better aligned."""
    sig = subtitle_signal(subs, len(ref), rate)
    on = sig.sum()
    return float((sig * ref).sum() / on) if on else -1.0
