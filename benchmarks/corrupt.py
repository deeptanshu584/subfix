"""Deterministic corruptions of correctly-timed subtitles + timing-error metrics."""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from subfix.srt_io import Subtitle, shift

FPS_PAIRS = [(25.0, 23.976), (23.976, 25.0), (24.0, 25.0), (25.0, 24.0), (23.976, 24.0)]


@dataclass
class Corruption:
    kind: str
    subs: list[Subtitle]
    description: str


def corrupt_offset(subs: Sequence[Subtitle], rng: random.Random) -> Corruption:
    off = rng.choice([-1, 1]) * rng.uniform(1.5, 45.0)
    return Corruption("offset", shift(subs, off), f"offset {off:+.2f}s")


def corrupt_drift(subs: Sequence[Subtitle], rng: random.Random) -> Corruption:
    a, b = rng.choice(FPS_PAIRS)
    scale = a / b  # subtitles timed for a fps-`b` release shown on fps-`a` video
    off = rng.uniform(-8.0, 8.0)
    return Corruption("drift", shift(subs, off, scale), f"scale {scale:.5f} offset {off:+.2f}s")


def corrupt_piecewise(subs: Sequence[Subtitle], rng: random.Random) -> Corruption:
    """2–4 sections with different offsets, as when the video has scenes/ads cut or inserted.

    A jump that moves a section *earlier* than the end of the previous one means the video
    contains footage the subtitle file has no lines for (an extended scene, an ad break): those
    overlapping lines are dropped, exactly as they would be missing from a real subtitle file.
    """
    n = len(subs)
    k = rng.randint(2, 4)
    cuts = sorted(rng.sample(range(n // 8, n - n // 8), k - 1))
    bounds = [0, *cuts, n]
    off = rng.uniform(-6, 6)
    out: list[Subtitle] = []
    parts = []
    for i in range(k):
        if i:
            off += rng.choice([-1, 1]) * rng.uniform(2.0, 60.0)
        seg = shift(subs[bounds[i] : bounds[i + 1]], off)
        if out:
            seg = [s for s in seg if s.start > out[-1].end + 0.1]
        out.extend(seg)
        parts.append(f"{off:+.1f}s")
    dropped = n - len(out)
    desc = f"{k} sections: " + ", ".join(parts) + (f" ({dropped} lines absent)" if dropped else "")
    return Corruption("piecewise", out, desc)


CORRUPTORS = {
    "offset": corrupt_offset,
    "drift": corrupt_drift,
    "piecewise": corrupt_piecewise,
}


def timing_errors(pred: Sequence[Subtitle], truth: Sequence[Subtitle]) -> np.ndarray:
    """Absolute start-time error (ms) per cue; cues correspond by text + order."""
    truth_by_text: dict[str, list[float]] = {}
    for t in truth:
        truth_by_text.setdefault(t.text, []).append(t.start)
    errs = []
    for p in pred:
        starts = truth_by_text.get(p.text)
        if starts:
            errs.append(min(abs(p.start - s) for s in starts) * 1000)
        else:
            errs.append(float("nan"))
    return np.array(errs, dtype=float)


def summarize(errs: np.ndarray) -> dict[str, float]:
    e = errs[~np.isnan(errs)]
    if not len(e):
        return {"mean": float("nan"), "median": float("nan"), "p90": float("nan"), "within200": 0}
    return {
        "mean": float(e.mean()),
        "median": float(np.median(e)),
        "p90": float(np.percentile(e, 90)),
        "within200": float((e <= 200).mean() * 100),
        "within500": float((e <= 500).mean() * 100),
    }
