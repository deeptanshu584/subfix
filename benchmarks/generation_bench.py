"""Benchmark subtitle *generation*: speed (× real time), word error rate, peak memory.

Runs ``subfix generate --no-cache`` as a subprocess (so model loading and audio extraction are
included and peak memory is measured for the whole process tree), then compares the generated
text with the ground-truth subtitles (WER over normalised tokens).

Usage: python benchmarks/generation_bench.py [--device cuda|cpu] [--model NAME] [--datasets ...]
Appends/updates entries in benchmarks/generation_results.json (read by run_benchmark.py).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import psutil
from rapidfuzz.distance import Levenshtein

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from subfix.audio import media_duration  # noqa: E402
from subfix.srt_io import load_subtitles  # noqa: E402
from subfix.sync.smart import tokenize  # noqa: E402

DATA = ROOT / "data"
DATASETS = {
    "synth_short": (DATA / "synth_short.mkv", DATA / "synth_short.truth.srt"),
    "synth_long": (DATA / "synth_long.mkv", DATA / "synth_long.truth.srt"),
    "tos": (DATA / "real" / "tears_of_steel_720p.mov", DATA / "real" / "TOS-en.srt"),
}
OUT = ROOT / "generation_results.json"


def words(path: Path) -> list[str]:
    return [w for s in load_subtitles(path).subs for w, _ in tokenize(s.text)]


def run_one(name: str, device: str, model: str | None) -> dict:
    media, truth = DATASETS[name]
    out = ROOT / "work" / f"{name}.{device}.generated.srt"
    out.parent.mkdir(parents=True, exist_ok=True)
    exe = str(Path(sys.executable).with_name("subfix.exe" if sys.platform == "win32" else "subfix"))
    cmd = [exe, "generate", str(media), "--no-cache", "-q", "--device", device, "-o", str(out)]
    if model:
        cmd += ["--model", model]
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd)
    p = psutil.Process(proc.pid)
    peak = 0
    while proc.poll() is None:
        try:
            rss = p.memory_info().rss + sum(c.memory_info().rss for c in p.children(True))
            peak = max(peak, rss)
        except psutil.Error:
            pass
        time.sleep(0.2)
    wall = time.perf_counter() - t0
    if proc.returncode != 0:
        raise SystemExit(f"generate failed for {name}")
    ref, hyp = words(truth), words(out)
    dur = media_duration(media)
    return {
        "dataset": name,
        "device": device,
        "model": model or "default",
        "audio_min": dur / 60,
        "wall_s": wall,
        "x_realtime": dur / wall,
        "peak_rss_mb": peak / 2**20,
        "wer": Levenshtein.distance(ref, hyp) / max(1, len(ref)),
        "ref_words": len(ref),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--model", default=None)
    ap.add_argument("--datasets", default="synth_short,synth_long,tos")
    a = ap.parse_args()
    results = json.loads(OUT.read_text()) if OUT.is_file() else []
    for name in a.datasets.split(","):
        r = run_one(name, a.device, a.model)
        print(json.dumps(r), flush=True)
        results = [
            x
            for x in results
            if (x["dataset"], x["device"], x["model"]) != (name, a.device, r["model"])
        ]
        results.append(r)
        OUT.write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
