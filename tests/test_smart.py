"""Text alignment logic of smart sync, with a fake transcript (no Whisper needed)."""

import random

import numpy as np

from subfix.srt_io import Subtitle, shift
from subfix.sync.smart import (
    align_tokens,
    fuzzy_align,
    local_slope,
    longest_increasing_chain,
    running_median,
    sync_smart,
    tokenize,
)
from subfix.transcribe import Segment, Transcript, Word


def fake_transcript(subs, jitter=0.05, drop=0.1, seed=0) -> Transcript:
    """Words timed by spreading each cue's duration over its words, with noise and dropouts."""
    rng = random.Random(seed)
    segs = []
    for s in subs:
        toks = s.text.split()
        step = (s.end - s.start) / len(toks)
        words = []
        for k, w in enumerate(toks):
            if rng.random() < drop:
                continue
            t = s.start + k * step + rng.uniform(-jitter, jitter)
            words.append(Word(t, t + step * 0.9, " " + w))
        segs.append(Segment(s.start, s.end, s.text, words))
    return Transcript("en", "transcribe", "fake", segs)


def test_tokenize_cleans_markup():
    toks = [t for t, _ in tokenize("- <i>Mr. Holmes,</i> [door slams] it's GOOD-bye! ♪ la ♪")]
    assert toks == ["mister", "holmes", "it's", "goodbye"]


def test_tokenize_cjk_chars():
    assert [t for t, _ in tokenize("你好世界")] == ["你", "好", "世", "界"]


def test_lis_chain():
    pairs = [(0, 0), (1, 5), (2, 1), (3, 2), (4, 3), (5, 0)]
    assert longest_increasing_chain(pairs) == [(0, 0), (2, 1), (3, 2), (4, 3)]


def test_fuzzy_align_handles_spelling_variants():
    a = ["the", "colour", "of", "his", "favourite", "jacket"]
    b = ["the", "color", "of", "his", "favorite", "jacket"]
    pairs = fuzzy_align(a, b)
    assert pairs == [(i, i) for i in range(6)]


def test_align_tokens_with_insertions():
    a = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]
    b = ["uh", "one", "two", "three", "um", "four", "five", "six", "seven", "eight", "nine", "ten"]
    pairs = dict(align_tokens(a, b))
    assert pairs[0] == 1 and pairs[3] == 5 and pairs[9] == 11


def test_running_median_preserves_steps():
    x = np.array([0.0] * 10 + [5.0] * 10)
    x[4] = 3.0  # outlier
    y = running_median(x, 3)
    assert y[4] == 0.0 and y[9] == 0.0 and y[10] == 5.0


def test_local_slope_ignores_steps():
    t = np.arange(200, dtype=float) * 5
    d = 0.01 * t + np.where(t > 500, 20.0, 0.0)
    assert abs(local_slope(t, d) - 0.01) < 1e-6


def _sentences(n=120, seed=1):
    rng = random.Random(seed)
    vocab = [
        "watson",
        "holmes",
        "baker",
        "street",
        "london",
        "letter",
        "crime",
        "violin",
        "pipe",
        "evening",
        "doctor",
        "carriage",
        "window",
        "morning",
        "client",
        "mystery",
        "inspector",
        "door",
        "fire",
        "lamp",
        "coat",
    ]
    subs, t = [], 2.0
    for _ in range(n):
        words = [rng.choice(vocab) for _ in range(rng.randint(4, 9))]
        d = 0.35 * len(words)
        subs.append(Subtitle(t, t + d, " ".join(words)))
        t += d + rng.uniform(0.3, 4.0)
    return subs


def test_smart_recovers_offset_and_cuts():
    truth = _sentences()
    tr = fake_transcript(truth)
    n = len(truth)
    corrupted = shift(truth[: n // 2], 4.0) + shift(truth[n // 2 :], 25.0)
    r = sync_smart(corrupted, tr)
    err = np.array([abs(p.start - t.start) for p, t in zip(r.subs, truth, strict=True)])
    assert np.median(err) < 0.08
    assert (err < 0.2).mean() > 0.95
    assert r.params["match_ratio"] > 0.9


def test_smart_recovers_drift():
    truth = _sentences(seed=3)
    tr = fake_transcript(truth, seed=3)
    r = sync_smart(shift(truth, 1.5, 25 / 23.976), tr)
    err = np.array([abs(p.start - t.start) for p, t in zip(r.subs, truth, strict=True)])
    assert (err < 0.2).mean() > 0.95


def test_smart_rejects_unrelated_text():
    truth = _sentences()
    other = [Subtitle(s.start, s.end, "zzz qqq xxx www") for s in truth]
    try:
        sync_smart(other, fake_transcript(truth))
    except ValueError as exc:
        assert "match" in str(exc)
    else:
        raise AssertionError("expected ValueError")
