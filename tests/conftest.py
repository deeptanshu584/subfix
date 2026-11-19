"""Shared fixtures: synthetic speech signals and matching subtitles (no audio, no GPU)."""

from __future__ import annotations

import random

import numpy as np
import pytest

from subfix.srt_io import Subtitle
from subfix.vad import HOP_SECONDS, SpeechSignal


def make_dialogue(n: int = 150, seed: int = 0) -> list[Subtitle]:
    """Random but realistic cue layout: 1–5 s lines, 0.2–6 s pauses, occasional long gaps."""
    rng = random.Random(seed)
    words = [
        "alpha",
        "bravo",
        "charlie",
        "delta",
        "echo",
        "foxtrot",
        "golf",
        "hotel",
        "india",
        "juliet",
        "kilo",
        "lima",
        "mike",
        "november",
        "oscar",
        "papa",
        "quebec",
        "romeo",
        "sierra",
        "tango",
        "uniform",
        "victor",
        "whiskey",
        "xray",
    ]
    t = 3.0
    subs = []
    for i in range(n):
        d = rng.uniform(1.0, 5.0)
        text = " ".join(rng.choice(words) for _ in range(rng.randint(3, 8))) + f" {i}"
        subs.append(Subtitle(round(t, 3), round(t + d, 3), text))
        t += d + (rng.uniform(15, 40) if rng.random() < 0.03 else rng.uniform(0.2, 6.0))
    return subs


def speech_from(subs: list[Subtitle], noise: float = 0.1, seed: int = 0) -> SpeechSignal:
    """A VAD-like probability signal that is high exactly where ``subs`` are spoken."""
    rng = np.random.default_rng(seed)
    dur = max(s.end for s in subs) + 30
    n = int(dur / HOP_SECONDS)
    p = np.full(n, 0.05)
    for s in subs:
        p[int(s.start / HOP_SECONDS) : int(s.end / HOP_SECONDS)] = 0.95
    p = np.clip(p + rng.normal(0, noise, n), 0, 1).astype(np.float32)
    return SpeechSignal(p)


@pytest.fixture
def dialogue() -> list[Subtitle]:
    return make_dialogue()


@pytest.fixture
def speech(dialogue) -> SpeechSignal:
    return speech_from(dialogue)
