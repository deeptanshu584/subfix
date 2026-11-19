"""Constant-offset synchronisation via FFT cross-correlation of speech activity.

Every cue is shifted by the same ``offset``. The best offset maximises the overlap of cue
intervals with VAD speech (see :mod:`subfix.sync.common`); the correlation for *all* offsets in
``±max_offset`` is computed with one FFT, then refined to sub-frame precision.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from ..srt_io import Subtitle, shift
from ..vad import SpeechSignal
from .common import (
    RATE,
    SyncResult,
    alignment_score,
    cross_correlate,
    dialogue,
    parabolic_peak,
    peak_confidence,
    reference_signal,
    subtitle_signal,
)


def best_offset(
    subs: Sequence[Subtitle],
    ref: np.ndarray,
    max_offset: float = 900.0,
    scale: float = 1.0,
    rate: int = RATE,
) -> tuple[float, float, float]:
    """Return ``(offset_seconds, normalised_score, confidence)`` for cues scaled by ``scale``.

    ``normalised_score`` is the mean of ``2p−1`` under the shifted cues (−1 … 1).
    """
    end = max(s.end for s in subs) * scale
    length = max(len(ref), int(end * rate) + 2)
    sig = subtitle_signal(subs, length, rate, scale)
    on = sig.sum() or 1.0
    max_lag = int(max_offset * rate)
    lags, c = cross_correlate(ref, sig, max_lag)
    i = int(np.argmax(c))
    pos = parabolic_peak(c, i)
    offset = (lags[0] + pos) / rate
    conf = peak_confidence(c, i, exclusion=int(2.0 * rate))
    return float(offset), float(c[i] / on), conf


def sync_offset(
    subs: Sequence[Subtitle], speech: SpeechSignal, max_offset: float = 900.0
) -> SyncResult:
    """Shift all cues by one constant offset."""
    ref = reference_signal(speech)
    est = dialogue(subs)
    offset, score, conf = best_offset(est, ref, max_offset)
    out = shift(subs, offset)
    return SyncResult(
        out,
        "offset",
        score=alignment_score(dialogue(out), ref),
        confidence=conf,
        params={"offset": offset, "scale": 1.0, "raw_score": score},
    )
