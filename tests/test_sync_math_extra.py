"""Signal-level edge cases for audio sync: correlation, peaks, thresholds, VAD helpers, drift."""

from __future__ import annotations

import random

import numpy as np
import pytest
from conftest import make_dialogue, speech_from

from subfix.srt_io import Subtitle, shift
from subfix.sync.common import (
    OUT_OF_RANGE,
    SyncResult,
    alignment_score,
    cross_correlate,
    is_dialogue,
    parabolic_peak,
    peak_confidence,
    reference_signal,
    speech_threshold,
    subtitle_signal,
)
from subfix.sync.common import (
    dialogue as dialogue_filter,
)
from subfix.sync.drift import _golden, candidate_scales, local_offsets, sync_drift, theil_sen
from subfix.sync.offset import best_offset, sync_offset
from subfix.vad import HOP_SECONDS, SpeechSignal


def weak_speech_from(
    subs: list[Subtitle], lo: float = 0.01, hi: float = 0.25, noise: float = 0.05, seed: int = 0
) -> SpeechSignal:
    """Dialogue under music: Silero only reaches p≈0.25 on speech, ≈0.01 elsewhere."""
    rng = np.random.default_rng(seed)
    n = int((max(s.end for s in subs) + 30) / HOP_SECONDS)
    p = np.full(n, lo)
    for s in subs:
        p[int(s.start / HOP_SECONDS) : int(s.end / HOP_SECONDS)] = hi
    return SpeechSignal(np.clip(p + rng.normal(0, noise, n), 0, 1).astype(np.float32))


def start_errors(pred, truth):
    return np.array([abs(p.start - t.start) for p, t in zip(pred, truth, strict=True)])


# --------------------------------------------------------------------------- cross_correlate


def test_out_of_range_worse_than_silence():
    # All silence: every in-range lag scores the same; lags that push the cue outside the audio
    # must score strictly worse, so the argmax keeps cues inside the film.
    ref = np.full(1000, -0.05)
    sub = np.zeros(1000)
    sub[100:200] = 1.0
    lags, c = cross_correlate(ref, sub, 500)
    inside = (lags >= -100) & (lags <= 500)
    assert np.allclose(c[inside], -0.05 * 100)
    assert np.all(c[~inside] < c[inside].min() - 1e-9)
    assert c[lags == -500][0] == pytest.approx(100 * OUT_OF_RANGE)
    assert -100 <= lags[np.argmax(c)] <= 500


def test_out_of_range_tail_when_sub_longer_than_ref():
    ref = np.full(300, -0.05)
    sub = np.zeros(400)
    sub[250:290] = 1.0  # cue ends 10 frames before the audio does
    lags, c = cross_correlate(ref, sub, 50)
    assert len(c) == 101
    assert c[lags == 10][0] == pytest.approx(-0.05 * 40)
    assert c[lags == 11][0] < c[lags == 10][0]  # one frame past the end is penalised
    assert c[lags == 30][0] == pytest.approx(20 * -0.05 + 20 * OUT_OF_RANGE)  # half outside
    assert c[lags == 50][0] == pytest.approx(40 * OUT_OF_RANGE)  # entirely outside


def test_best_offset_keeps_cues_inside_silent_audio():
    ref = np.full(6000, -0.05)  # 60 s of silence
    subs = [Subtitle(2.0, 4.0, "a"), Subtitle(50.0, 55.0, "b")]
    off, _, _ = best_offset(subs, ref, max_offset=30.0)
    assert -2.0 - 1e-6 <= off <= 5.0 + 1e-6


# --------------------------------------------------------------------------- peaks


@pytest.mark.parametrize("i", [0, 9])
def test_parabolic_peak_at_edges_returns_index(i):
    c = np.linspace(0, 1, 10) if i == 9 else np.linspace(1, 0, 10)
    assert parabolic_peak(c, i) == float(i)


def test_parabolic_peak_flat_and_minimum():
    assert parabolic_peak(np.ones(5), 2) == 2.0
    assert parabolic_peak(np.array([1.0, 0.0, 1.0]), 1) == 1.0  # convex: no refinement
    assert parabolic_peak(np.array([0.0, 1.0, 0.0]), 1) == 1.0  # symmetric peak


def test_peak_confidence_distinct_vs_flat():
    rng = np.random.default_rng(0)
    noisy = rng.normal(0, 0.1, 1000)
    peaked = noisy.copy()
    peaked[500] = 5.0
    distinct = peak_confidence(peaked, 500, exclusion=20)
    flat = peak_confidence(np.ones(1000), 500, exclusion=20)
    ambiguous = peaked.copy()
    ambiguous[100] = 5.0  # a twin peak outside the exclusion zone
    assert distinct > 5.0
    assert flat == pytest.approx(0.0, abs=1e-6)
    assert peak_confidence(ambiguous, 500, exclusion=20) == pytest.approx(0.0, abs=1e-6)


def test_peak_confidence_degenerate_inputs():
    assert peak_confidence(np.array([1.0, 2.0]), 1, exclusion=1) == 0.0
    assert peak_confidence(np.array([0.0, 3.0, 1.0, 0.5]), 1, exclusion=10) == 1.0


# --------------------------------------------------------------------------- signals


def test_subtitle_signal_scale_and_clipping():
    subs = [Subtitle(1.0, 2.0, "a"), Subtitle(-1.0, 0.5, "neg"), Subtitle(9.0, 20.0, "late")]
    sig = subtitle_signal(subs, 1000, scale=2.0)
    assert sig.shape == (1000,)
    assert sig[200:400].all() and not sig[400:999].any()
    assert sig[:100].all() and not sig[100:200].any()  # [-2, 1) s clipped to [0, 1) s
    assert subtitle_signal(subs, 1000, scale=1.0)[900:].all()  # [9, 20) s clipped to 10 s
    assert subtitle_signal([], 50).sum() == 0


def test_alignment_score_ordering(dialogue, speech):
    ref = reference_signal(speech)
    aligned = alignment_score(dialogue, ref)
    small = alignment_score(shift(dialogue, 0.5), ref)
    big = alignment_score(shift(dialogue, 7.0), ref)
    assert aligned > small > big
    assert aligned > 0.3
    assert alignment_score([], ref) == -1.0


@pytest.mark.parametrize(
    "level,theta",
    [(0.0, 0.05), (0.01, 0.05), (0.2, 0.2), (0.45, 0.45), (0.9, 0.5), (1.0, 0.5)],
)
def test_speech_threshold_clamped(level, theta):
    sp = SpeechSignal(np.full(500, level, dtype=np.float32))
    assert speech_threshold(sp) == pytest.approx(theta, abs=1e-6)
    ref = reference_signal(sp)
    assert ref.dtype == np.float64
    assert np.allclose(ref, level - theta, atol=1e-6)


def test_speech_threshold_empty_signal():
    assert speech_threshold(SpeechSignal(np.zeros(0, dtype=np.float32))) == 0.5


def test_reference_signal_sign_on_weak_vad():
    subs = make_dialogue(40, seed=1)
    ref = reference_signal(weak_speech_from(subs, noise=0.0))
    on = subtitle_signal(subs, len(ref)).astype(bool)
    assert (ref[on] > 0).mean() > 0.97
    assert (ref[~on] < 0).mean() > 0.97


# --------------------------------------------------------------------------- SpeechSignal


def test_at_rate_length_and_values():
    probs = np.array([0.0] * 10 + [1.0] * 10, dtype=np.float32)
    sp = SpeechSignal(probs, hop=0.05)  # 1.0 s
    out = sp.at_rate(100)
    assert len(out) == 100 and out.dtype == np.float32
    assert np.all(out[:45] == 0.0) and np.all(out[55:] == 1.0)
    assert np.all(np.diff(out) >= 0)  # linear ramp between frame centres
    assert len(SpeechSignal(np.ones(3125, dtype=np.float32)).at_rate(100)) in (10000, 10001)


def test_segments_merge_short_gaps_and_drop_short_bursts():
    probs = np.array([0, 1, 1, 1, 0, 1, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0], dtype=np.float32)
    sp = SpeechSignal(probs, hop=0.1)
    segs = sp.segments(threshold=0.5, min_speech=0.2, min_silence=0.3)
    assert len(segs) == 1
    assert segs[0] == pytest.approx((0.1, 0.7))
    # with no minimum length the short burst survives as its own segment
    segs = sp.segments(threshold=0.5, min_speech=0.0, min_silence=0.3)
    assert [pytest.approx(s) for s in segs] == [(0.1, 0.7), (1.2, 1.3)]


def test_segments_and_fraction_edge_cases():
    quiet = SpeechSignal(np.full(100, 0.1, dtype=np.float32))
    assert quiet.segments() == [] and quiet.speech_fraction() == 0.0
    loud = SpeechSignal(np.ones(100, dtype=np.float32), hop=0.01)
    assert loud.segments() == [pytest.approx((0.0, 1.0))]
    assert loud.speech_fraction() == 1.0
    assert SpeechSignal(np.zeros(0, dtype=np.float32)).speech_fraction() == 0.0


# --------------------------------------------------------------------------- dialogue filter


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Hello there.", True),
        ("[MUSIC]", False),
        ("[door slams]\n[footsteps]", False),
        ("(sighs)", False),
        ("♪ la la la ♪", False),
        ("# singing #", False),
        ("-", False),
        ("[John] I said no.", True),
    ],
)
def test_is_dialogue(text, expected):
    assert is_dialogue(Subtitle(1.0, 3.0, text)) is expected


def test_is_dialogue_rejects_very_long_cue():
    assert not is_dialogue(Subtitle(0.0, 15.0, "credits roll"))


def test_dialogue_falls_back_when_mostly_non_dialogue():
    subs = [Subtitle(i, i + 1, "[MUSIC]") for i in range(10)] + [Subtitle(20, 21, "Hi")]
    subs.append(Subtitle(30, 30, "zero length"))
    out = dialogue_filter(subs)
    assert len(out) == 11 and all(s.end > s.start for s in out)


# --------------------------------------------------------------------------- drift helpers


def test_theil_sen_too_few_points():
    assert theil_sen(np.array([]), np.array([])) == (0.0, 0.0)
    assert theil_sen(np.array([5.0]), np.array([2.5])) == (2.5, 0.0)
    a, b = theil_sen(np.array([3.0, 3.0, 3.0]), np.array([1.0, 2.0, 9.0]))
    assert (a, b) == (2.0, 0.0)
    a, b = theil_sen(np.array([0.0, 10.0]), np.array([1.0, 6.0]))
    assert (a, b) == pytest.approx((1.0, 0.5))


def test_candidate_scales_symmetric_sorted_bounded():
    sc = candidate_scales()
    assert sc == sorted(set(sc))
    assert all(0.75 < s < 1.35 for s in sc)
    for s in sc:
        assert min(abs(s * t - 1.0) for t in sc) < 1e-6, s
    assert any(abs(s - 30 / 23.976) < 1e-6 for s in sc)


def test_golden_section_finds_maximum():
    assert _golden(lambda x: -((x - 0.3) ** 2), 0.0, 1.0) == pytest.approx(0.3, abs=1e-3)
    assert _golden(lambda x: x, 0.0, 1.0) == pytest.approx(1.0, abs=1e-3)


def test_local_offsets_track_constant_offset(dialogue, speech):
    ref = reference_signal(speech)
    locs = local_offsets(shift(dialogue, 6.0), ref)
    assert len(locs) >= 3
    assert all(off == pytest.approx(-6.0, abs=0.05) for _, off, _ in locs)
    assert np.median([conf for _, _, conf in locs]) > 1.0
    assert local_offsets(dialogue[:5], ref) == []
    assert local_offsets([], ref) == []


# --------------------------------------------------------------------------- end-to-end


@pytest.mark.parametrize("offset", [-20.0, 4.3, 120.0])
def test_sync_offset_weak_vad_dialogue_under_music(offset):
    truth = make_dialogue()
    speech = weak_speech_from(truth)
    assert 0.05 < speech_threshold(speech) < 0.25
    r = sync_offset(shift(truth, offset), speech)
    assert r.params["offset"] == pytest.approx(-offset, abs=0.05)
    assert np.median(start_errors(r.subs, truth)) < 0.05


@pytest.mark.parametrize("offset", [-58.0, 58.0])
def test_sync_offset_near_max_offset(offset):
    truth = shift(make_dialogue(seed=3), 100.0)  # leave room for negative shifts
    r = sync_offset(shift(truth, offset), speech_from(truth), max_offset=60.0)
    assert r.params["offset"] == pytest.approx(-offset, abs=0.05)


@pytest.mark.parametrize("offset", [-2.5, 1.7, 6.0])
def test_sync_offset_few_cues(offset):
    truth = make_dialogue(5, seed=2)
    r = sync_offset(shift(truth, offset), speech_from(truth))
    assert r.params["offset"] == pytest.approx(-offset, abs=0.05)
    assert len(r.subs) == 5 and r.method == "offset"
    assert r.describe().startswith("offset ")


def test_sync_drift_with_sound_description_cues(dialogue, speech):
    rng = random.Random(5)
    noise = ["[MUSIC]", "♪ la la ♪", "(door slams)", "[THUNDER RUMBLING]"]
    extra = [
        Subtitle(s.end + 0.1, s.end + 0.1 + rng.uniform(1, 3), rng.choice(noise))
        for s in dialogue
        if rng.random() < 0.15
    ]
    mixed = sorted(dialogue + extra, key=lambda s: s.start)
    scale, offset = 25 / 23.976, 2.0
    r = sync_drift(shift(mixed, offset, scale), speech)
    assert isinstance(r, SyncResult) and len(r.subs) == len(mixed)
    assert r.params["scale"] == pytest.approx(1 / scale, rel=2e-4)
    spoken = [s for s in r.subs if is_dialogue(s)]
    err = start_errors(spoken, dialogue)
    assert np.median(err) < 0.05 and (err < 0.2).mean() > 0.95
    # sound-description cues follow the same mapping instead of being dropped
    fixed = {s.text: s for s in r.subs if not is_dialogue(s)}
    assert set(fixed) <= set(noise)
