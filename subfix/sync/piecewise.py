"""Piecewise-constant offsets (cuts, inserted scenes, ads) via Viterbi dynamic programming.

Model: cue ``i`` is shifted by offset ``o_i`` taken from a candidate set. Consecutive cues prefer
the same offset; switching costs ``split_penalty``. We maximise::

    Σ_i  E[i, o_i]  −  split_penalty · #{i : o_i ≠ o_{i−1}}

where ``E[i, o] = Σ_{t ∈ cue_i + o} (p_speech(t) − θ)`` is evaluated in O(1) with prefix sums.
A hard constraint forbids switches that would make a cue start before the previous one ends
(subtitles keep their order), handled with a prefix-max over offsets sorted ascending, so each DP
step is O(K) and the whole thing O(N·K).

Candidate offsets: the top correlation peaks of short sliding windows (any section long enough to
matter shows up as a peak in at least one window), each expanded to a ±0.6 s neighbourhood at
10 ms resolution.

Diagram (offset per cue)::

    offset ▲        ┌──────────┐
           │ ───────┘          └──────────── (scene cut → offset changes twice)
           └────────────────────────────────► cue index
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from ..log import log
from ..srt_io import Subtitle
from ..vad import SpeechSignal
from .common import (
    OUT_OF_RANGE,
    RATE,
    SyncResult,
    WindowSearch,
    alignment_score,
    dialogue,
    is_dialogue,
    reference_signal,
)


def candidate_offsets(
    subs: Sequence[Subtitle],
    ref: np.ndarray,
    max_offset: float,
    window: float = 90.0,
    step: float = 30.0,
    peaks_per_window: int = 6,
    rate: int = RATE,
) -> list[float]:
    """Coarse offset candidates from the strongest correlation peaks of sliding windows."""
    search = WindowSearch(ref, max_offset, rate=rate)
    found: list[tuple[float, float]] = []  # (score, offset)
    t0, t1 = subs[0].start, subs[-1].end
    starts = [*np.arange(t0, max(t0 + 1e-6, t1 - window * 0.5), step)] or [t0]
    windows = [[s for s in subs if a <= s.start < a + window] for a in starts]
    windows.append(list(subs))  # global correlation too
    for win in windows:
        if len(win) < 4:
            continue
        for score, off, _ in search.peaks(win, k=peaks_per_window):
            if score > 0:
                found.append((score, off))
    # cluster offsets within 0.4 s, keep the best-scoring representative
    found.sort(reverse=True)
    reps: list[float] = []
    for _, o in found:
        if all(abs(o - r) > 0.4 for r in reps):
            reps.append(o)
    return reps


def _expand(centres: Sequence[float], radius: float = 0.6, rate: int = RATE) -> np.ndarray:
    grid = set()
    r = int(radius * rate)
    for c in centres:
        ci = int(round(c * rate))
        grid.update(range(ci - r, ci + r + 1))
    return np.array(sorted(grid), dtype=np.int64)


def viterbi_offsets(
    subs: Sequence[Subtitle],
    ref: np.ndarray,
    offsets_frames: np.ndarray,
    split_penalty: float,
    weights: np.ndarray | None = None,
    rate: int = RATE,
    overlap_tolerance: float = 0.5,
    extra: np.ndarray | None = None,
) -> np.ndarray:
    """Return the best offset (in frames) for every cue. ``offsets_frames`` must be sorted.

    ``extra`` (shape ``(n_cues, n_offsets)``) is added to the VAD emission — smart sync uses it
    to inject text-alignment evidence.
    """
    n, k = len(subs), len(offsets_frames)
    starts = np.array([s.start for s in subs])
    ends = np.array([s.end for s in subs])
    pad = int(np.abs(offsets_frames).max()) + 2
    tail = np.full(pad + int(ends.max() * rate) + 2, OUT_OF_RANGE)
    padded = np.concatenate([np.full(pad, OUT_OF_RANGE), ref, tail])
    csum = np.concatenate([[0.0], np.cumsum(padded)])

    a_idx = np.round(starts * rate).astype(np.int64)[:, None] + offsets_frames[None, :] + pad
    b_idx = np.round(ends * rate).astype(np.int64)[:, None] + offsets_frames[None, :] + pad
    a_idx = np.clip(a_idx, 0, len(padded))
    b_idx = np.clip(b_idx, 0, len(padded))
    emis = csum[b_idx] - csum[a_idx]  # (n, k)
    if weights is not None:
        emis *= weights[:, None]
    if extra is not None:
        emis = emis + extra

    back = np.zeros((n, k), dtype=np.int32)
    v = emis[0].copy()
    ar = np.arange(k)
    tol = int(overlap_tolerance * rate)
    for i in range(1, n):
        # prefix max over offsets (ascending) of previous scores
        pm_idx = np.maximum.accumulate(np.where(v == np.maximum.accumulate(v), ar, 0))
        pm_val = v[pm_idx]
        gap = int(round(starts[i] * rate)) - int(round(ends[i - 1] * rate))
        # allowed previous offset o' <= gap + o + tol  (cue i must not start before cue i-1 ends)
        j = np.searchsorted(offsets_frames, gap + offsets_frames + tol, side="right") - 1
        valid = j >= 0
        jj = np.clip(j, 0, k - 1)
        switch = np.where(valid, pm_val[jj] - split_penalty, -np.inf)
        stay = v
        take_switch = switch > stay
        back[i] = np.where(take_switch, pm_idx[jj], ar)
        v = emis[i] + np.where(take_switch, switch, stay)

    path = np.zeros(n, dtype=np.int64)
    path[-1] = int(np.argmax(v))
    for i in range(n - 1, 0, -1):
        path[i - 1] = back[i, path[i]]
    return offsets_frames[path]


def signal_strength(ref: np.ndarray) -> float:
    """Typical per-frame value of clear speech in this film's reference signal."""
    pos = ref[ref > 0]
    return float(pos.mean()) if len(pos) else 0.5


def _segments(offs: np.ndarray, subs: Sequence[Subtitle]) -> list[tuple[int, int, float]]:
    """Runs of identical offset → ``[(first_cue, last_cue_exclusive, offset_s)]``."""
    segs = []
    a = 0
    for i in range(1, len(offs) + 1):
        if i == len(offs) or offs[i] != offs[a]:
            segs.append((a, i, float(offs[a]) / RATE))
            a = i
    return segs


def sync_piecewise(
    subs: Sequence[Subtitle],
    speech: SpeechSignal,
    max_offset: float = 900.0,
    split_penalty_s: float = 6.0,
) -> SyncResult:
    """Assign each cue an offset from a small candidate set with a penalty for every switch.

    ``split_penalty_s``: a new section must "earn" this many seconds of extra speech overlap.
    """
    subs = sorted(subs, key=lambda s: s.start)
    ref = reference_signal(speech)
    est = dialogue(subs)
    centres = candidate_offsets(est, ref, max_offset)
    if not centres:
        centres = [0.0]
    grid = _expand(centres)
    log.debug("piecewise: %d offset clusters, %d grid states", len(centres), len(grid))
    weights = np.array([1.0 if is_dialogue(s) else 0.2 for s in subs])
    # Express the penalty in "seconds of well-aligned speech" for *this* film: a frame of clear
    # speech is worth ~0.5 on clean audio but only ~0.2 under a loud score.
    strength = signal_strength(ref)
    offs = viterbi_offsets(subs, ref, grid, split_penalty_s * RATE * strength, weights)
    segs = _segments(offs, subs)
    out = [
        Subtitle(s.start + o / RATE, s.end + o / RATE, s.text)
        for s, o in zip(subs, offs, strict=True)
    ]
    seg_desc = [(subs[a].start, subs[b - 1].end, o) for a, b, o in segs]
    # confidence: mean per-cue gain of the chosen offset vs. the median score of alternatives
    score = alignment_score(dialogue(out), ref)
    return SyncResult(
        out,
        "piecewise",
        score=score,
        confidence=float(max(0.0, score) * 10),
        params={"segments": seg_desc, "offset": seg_desc[0][2] if seg_desc else 0.0},
    )
