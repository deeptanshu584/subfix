"""Offset / drift / piecewise math on synthetic speech signals."""

import numpy as np
import pytest

from subfix.srt_io import Subtitle, shift
from subfix.sync.common import cross_correlate, parabolic_peak
from subfix.sync.drift import candidate_scales, sync_drift, theil_sen
from subfix.sync.offset import sync_offset
from subfix.sync.piecewise import sync_piecewise, viterbi_offsets


def start_errors(pred, truth):
    return np.array([abs(p.start - t.start) for p, t in zip(pred, truth, strict=True)])


def test_cross_correlate_finds_known_lag():
    rng = np.random.default_rng(1)
    ref = rng.standard_normal(5000)
    sub = np.roll(ref, -137)[:4000]  # sub(t) = ref(t + 137)
    lags, c = cross_correlate(ref, sub, 500)
    assert lags[np.argmax(c)] == 137


def test_parabolic_peak_subsample():
    x = np.arange(10, dtype=float)
    c = -((x - 4.3) ** 2)
    assert parabolic_peak(c, 4) == pytest.approx(4.3, abs=1e-6)


@pytest.mark.parametrize("offset", [-42.37, -3.2, 0.0, 0.45, 17.8, 300.0])
def test_offset_recovered(dialogue, speech, offset):
    corrupted = shift(dialogue, offset)
    r = sync_offset(corrupted, speech)
    assert r.params["offset"] == pytest.approx(-offset, abs=0.03)
    assert np.median(start_errors(r.subs, dialogue)) < 0.03


def test_theil_sen_robust_to_outliers():
    x = np.arange(20, dtype=float)
    y = 2.0 + 0.5 * x
    y[[3, 11]] = [50, -40]
    a, b = theil_sen(x, y)
    assert b == pytest.approx(0.5, abs=1e-6) and a == pytest.approx(2.0, abs=1e-6)


def test_candidate_scales_contains_pal_ntsc():
    sc = candidate_scales()
    assert 1.0 in sc
    assert any(abs(s - 25 / 23.976) < 1e-6 for s in sc)
    assert any(abs(s - 23.976 / 25) < 1e-6 for s in sc)


@pytest.mark.parametrize("scale,offset", [(25 / 23.976, 2.0), (23.976 / 25, -5.0), (1.0008, 1.0)])
def test_drift_recovered(dialogue, speech, scale, offset):
    corrupted = shift(dialogue, offset, scale)
    r = sync_drift(corrupted, speech)
    err = start_errors(r.subs, dialogue)
    assert np.median(err) < 0.05, r.describe()
    assert (err < 0.2).mean() > 0.95


def test_piecewise_recovers_sections(dialogue, speech):
    n = len(dialogue)
    a, b = n // 3, 2 * n // 3
    corrupted = shift(dialogue[:a], 2.0) + shift(dialogue[a:b], 9.5) + shift(dialogue[b:], 31.0)
    r = sync_piecewise(corrupted, speech)
    err = start_errors(r.subs, dialogue)
    assert (err < 0.2).mean() > 0.97
    assert len(r.params["segments"]) == 3


def test_piecewise_single_offset_stays_single(dialogue, speech):
    r = sync_piecewise(shift(dialogue, -7.0), speech)
    assert len(r.params["segments"]) == 1


def test_viterbi_respects_order_constraint():
    # two cues 1 s apart; offset grid {0, +5 s}: switching cue 2 to an *earlier* offset than cue 1
    # would reorder them and must be impossible.
    subs = [Subtitle(10, 11, "a"), Subtitle(12, 13, "b")]
    ref = -np.ones(3000)
    ref[1500:1600] = 1  # speech at 15–16 s favours cue a at +5 s ...
    ref[1200:1300] = 1  # ... and cue b at 0 s, which would put b before a.
    offs = viterbi_offsets(subs, ref, np.array([0, 500]), split_penalty=0.0)
    assert offs[1] >= offs[0] - 100 or offs[0] == offs[1]
