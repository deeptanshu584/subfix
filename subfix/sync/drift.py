"""Linear drift synchronisation: solve ``t' = scale · t + offset``.

Drift typically comes from frame-rate conversions (subtitles timed for a 25 fps PAL release played
against a 23.976 fps video → every minute the error grows by ~2.5 s). Strategy:

1. Candidate scales = 1.0, every ratio of common frame rates, plus a data-driven estimate from a
   robust (Theil–Sen) line fit through *local* offsets measured in sliding windows.
2. For each candidate: best offset by FFT cross-correlation, score normalised by cue time.
3. Golden-section refinement of the scale around the winner (offset re-optimised each step).
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence

import numpy as np

from ..log import log
from ..srt_io import Subtitle, shift
from ..vad import SpeechSignal
from .common import SyncResult, WindowSearch, alignment_score, dialogue, reference_signal
from .offset import best_offset

FPS = (23.976, 24.0, 25.0, 29.97, 30.0)


def candidate_scales() -> list[float]:
    """1.0 and all pairwise ratios of common frame rates."""
    ratios = {1.0}
    for a, b in itertools.permutations(FPS, 2):
        r = a / b
        if 0.75 < r < 1.35:
            ratios.add(round(r, 8))
    return sorted(ratios)


def local_offsets(
    subs: Sequence[Subtitle],
    ref: np.ndarray,
    window: float = 300.0,
    step: float = 150.0,
    max_offset: float = 900.0,
) -> list[tuple[float, float, float]]:
    """Estimate offsets of cues within sliding windows → ``[(window_mid, offset, confidence)]``."""
    if not subs:
        return []
    search = WindowSearch(ref, max_offset)
    out = []
    t_end = subs[-1].end
    t = subs[0].start
    while t < t_end:
        win = [s for s in subs if t <= s.start < t + window]
        if len(win) >= 8:
            _, off, conf = search.peaks(win, k=1)[0]
            mid = float(np.median([s.mid for s in win]))
            out.append((mid, off, conf))
        t += step
    return out


def theil_sen(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Robust line fit ``y = a + b x`` (median of pairwise slopes)."""
    if len(x) < 2:
        return (float(y[0]) if len(y) else 0.0), 0.0
    i, j = np.triu_indices(len(x), 1)
    dx = x[j] - x[i]
    ok = np.abs(dx) > 1e-9
    b = float(np.median((y[j] - y[i])[ok] / dx[ok])) if ok.any() else 0.0
    a = float(np.median(y - b * x))
    return a, b


def _golden(f, lo: float, hi: float, iters: int = 18) -> float:
    """Maximise unimodal ``f`` on [lo, hi]."""
    g = (np.sqrt(5) - 1) / 2
    a, b = lo, hi
    c, d = b - g * (b - a), a + g * (b - a)
    fc, fd = f(c), f(d)
    for _ in range(iters):
        if fc > fd:
            b, d, fd = d, c, fc
            c = b - g * (b - a)
            fc = f(c)
        else:
            a, c, fc = c, d, fd
            d = a + g * (b - a)
            fd = f(d)
    return (a + b) / 2


def sync_drift(
    subs: Sequence[Subtitle], speech: SpeechSignal, max_offset: float = 900.0
) -> SyncResult:
    """Fit ``scale`` and ``offset`` so that cues best overlap detected speech."""
    ref = reference_signal(speech)
    est = dialogue(subs)

    cands = candidate_scales()
    locs = local_offsets(est, ref, max_offset=max_offset)
    good = [(m, o) for m, o, c in locs if c > 1.0]
    if len(good) >= 3:
        x = np.array([m for m, _ in good])
        y = np.array([o for _, o in good])
        a, b = theil_sen(x, y)
        # local offsets were measured on unscaled times: t' = t + a + b t = (1+b) t + a
        if abs(b) < 0.3:
            cands.append(1.0 + b)
            log.debug("Theil-Sen drift estimate: scale=%.6f offset=%.3f", 1.0 + b, a)

    scored = []
    for s in cands:
        off, score, conf = best_offset(est, ref, max_offset, scale=s)
        scored.append((score, s, off, conf))
    scored.sort(reverse=True)
    best_score, best_s, _, _ = scored[0]
    log.debug(
        "best candidate scales: %s", [(round(s, 6), round(sc, 4)) for sc, s, _, _ in scored[:4]]
    )

    def f(s: float) -> float:
        return best_offset(est, ref, max_offset, scale=s)[1]

    span = 0.004 * best_s
    refined = _golden(f, best_s - span, best_s + span)
    if f(refined) >= best_score:
        best_s = refined
    off, score, conf = best_offset(est, ref, max_offset, scale=best_s)
    out = shift(subs, off, best_s)
    return SyncResult(
        out,
        "drift",
        score=alignment_score(dialogue(out), ref),
        confidence=conf,
        params={"offset": off, "scale": best_s, "raw_score": score},
    )
