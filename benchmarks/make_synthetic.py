"""Build synthetic "movies" with perfectly known subtitle timings.

Speech: sentences from a public-domain book (Sherlock Holmes, Project Gutenberg #1661) spoken by
the three Windows SAPI voices at random rates. Each sentence is placed on a timeline with random
pauses; some stretches contain only synthetic "music" (chords + percussion) and the whole track
has a pink-noise floor, so VAD has to work for its living. Ground truth = the exact speech span of
every sentence (leading/trailing TTS silence trimmed).

Outputs ``benchmarks/data/<name>.mkv`` (black 320x180 video + AAC audio) and
``benchmarks/data/<name>.truth.srt``.

Usage: python benchmarks/make_synthetic.py --name synth_short --minutes 12 --seed 1
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT.parent))

from subfix.audio import find_tool  # noqa: E402
from subfix.cues import wrap_lines  # noqa: E402
from subfix.srt_io import Subtitle, write_srt  # noqa: E402

SR = 16_000


def book_sentences(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    start = text.find("A SCANDAL IN BOHEMIA", text.find("*** START"))
    end = text.find("*** END")
    body = text[start:end] if start > 0 else text
    body = re.sub(r"\s+", " ", body.replace("_", ""))
    parts = re.split(r"(?:(?<=[.!?])|(?<=[.!?][\"”’']))\s+(?=[\"“A-Z])", body)
    out = []
    for p in parts:
        p = p.strip().strip('"“”').strip()
        if 18 <= len(p) <= 95 and not re.search(r"[A-Z]{4,}|\d{3,}|[\[\]*]", p):
            out.append(p)
    return out


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == SR and w.getnchannels() == 1
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768


def trim(x: np.ndarray, thresh: float = 0.01) -> np.ndarray:
    idx = np.flatnonzero(np.abs(x) > thresh)
    return x[idx[0] : idx[-1] + 1] if len(idx) else x[:0]


def pink_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    white = rng.standard_normal(n).astype(np.float32)
    spec = np.fft.rfft(white)
    f = np.arange(len(spec))
    f[0] = 1
    pink = np.fft.irfft(spec / np.sqrt(f), n).astype(np.float32)
    return pink / (np.abs(pink).max() + 1e-9)


def music(seconds: float, rng: np.random.Generator) -> np.ndarray:
    """Chord progression with a kick drum — loud, tonal, definitely not speech."""
    n = int(seconds * SR)
    t = np.arange(n) / SR
    out = np.zeros(n, dtype=np.float32)
    chord_len = 2.0
    roots = [220.0, 174.6, 196.0, 261.6]
    for k in range(int(np.ceil(seconds / chord_len))):
        a, b = int(k * chord_len * SR), min(n, int((k + 1) * chord_len * SR))
        root = roots[(k + rng.integers(4)) % 4]
        seg = t[a:b]
        for ratio in (1.0, 1.26, 1.5, 2.0):
            out[a:b] += 0.12 * np.sin(2 * np.pi * root * ratio * seg)
    beat = np.zeros(n, dtype=np.float32)
    for s in np.arange(0, seconds, 0.5):
        i = int(s * SR)
        m = min(n - i, int(0.15 * SR))
        beat[i : i + m] += (
            0.5 * np.sin(2 * np.pi * 60 * np.arange(m) / SR) * np.exp(-np.arange(m) / (0.04 * SR))
        )
    env = np.minimum(1, np.minimum(t, seconds - t) / 1.5)  # fade in/out
    return (out + beat) * env.astype(np.float32)


def build(name: str, minutes: float, seed: int, book: Path) -> None:
    rng = np.random.default_rng(seed)
    sentences = book_sentences(book)
    rng.shuffle(sentences)
    # ~60% speech density; average sentence ~3.5 s -> estimate how many we need
    n_needed = int(minutes * 60 / 5.2) + 20
    chosen = sentences[:n_needed]
    work = DATA / "tts" / name
    work.mkdir(parents=True, exist_ok=True)
    (work / "sentences.txt").write_text("\n".join(chosen), encoding="utf-8")
    print(f"[{name}] synthesizing {len(chosen)} sentences with SAPI ...", flush=True)
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "tts.ps1"),
            "-InFile",
            str(work / "sentences.txt"),
            "-OutDir",
            str(work / "wav"),
            "-Seed",
            str(seed),
        ],
        check=True,
    )

    total = int(minutes * 60 * SR)
    track = np.zeros(total, dtype=np.float32)
    subs: list[Subtitle] = []
    t = rng.uniform(2.0, 6.0)
    i = 0
    while i < len(chosen):
        # occasionally an "action scene": music only, no dialogue
        if rng.random() < 0.04:
            dur = rng.uniform(15, 45)
            if t + dur >= minutes * 60 - 5:
                break
            m = music(dur, rng) * rng.uniform(0.4, 0.9)
            a = int(t * SR)
            track[a : a + len(m)] += m
            t += dur + rng.uniform(0.5, 2.0)
        x = trim(read_wav(work / "wav" / f"{i:05d}.wav"))
        dur = len(x) / SR
        if t + dur >= minutes * 60 - 2:
            break
        a = int(t * SR)
        track[a : a + len(x)] += x * rng.uniform(0.5, 1.0)
        subs.append(Subtitle(round(t, 3), round(t + dur, 3), chosen[i]))
        gap = rng.choice(
            [rng.uniform(0.15, 0.6), rng.uniform(0.6, 2.5), rng.uniform(2.5, 8.0)],
            p=[0.45, 0.4, 0.15],
        )
        t += dur + gap
        i += 1
    # soft background music under ~20% of the dialogue + pink noise floor
    for _ in range(int(minutes / 5) + 1):
        dur = rng.uniform(30, 90)
        a = int(rng.uniform(0, max(1.0, minutes * 60 - dur)) * SR)
        m = music(dur, rng) * rng.uniform(0.08, 0.2)
        track[a : a + len(m)] += m[: total - a]
    track += 0.02 * pink_noise(total, rng)
    track = np.clip(track, -1, 1)

    DATA.mkdir(parents=True, exist_ok=True)
    wav_path = work / f"{name}.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((track * 32767).astype("<i2").tobytes())
    for s in subs:  # wrap long lines like real subtitles
        s.text = wrap_lines(s.text)
    write_srt(subs, DATA / f"{name}.truth.srt")
    ffmpeg = find_tool("ffmpeg")
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s=320x180:r=24:d={minutes * 60}",
            "-i",
            str(wav_path),
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "stillimage",
            "-crf",
            "40",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            str(DATA / f"{name}.mkv"),
        ],
        check=True,
    )
    print(f"[{name}] {len(subs)} cues, {minutes:.0f} min -> {DATA / (name + '.mkv')}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--minutes", type=float, default=12)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--book", type=Path, default=DATA / "src" / "holmes.txt")
    a = ap.parse_args()
    build(a.name, a.minutes, a.seed, a.book)


if __name__ == "__main__":
    main()
