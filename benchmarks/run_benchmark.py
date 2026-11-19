"""Benchmark: corrupt correctly-timed subtitles, run every sync mode, measure timing error.

Datasets (benchmarks/data, see make_synthetic.py and RESULTS.md):
  synth_short  12-min synthetic TTS film, exact ground truth
  synth_long  100-min synthetic TTS film, exact ground truth
  tos         Tears of Steel (CC-BY, Blender Foundation), official human-timed English subtitles
  tos_de      same film, German subtitles (cross-language: audio is English)

For every dataset × corruption (offset / drift / piecewise) × seed, every method is run on the
same corrupted file. Error = |predicted start − true start| per cue.

Usage: python benchmarks/run_benchmark.py [--seeds 5] [--datasets synth_short,tos]
Writes benchmarks/results.json and benchmarks/RESULTS.md.
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT))

from corrupt import CORRUPTORS, summarize, timing_errors  # noqa: E402

from subfix.log import setup_logging  # noqa: E402
from subfix.pipeline import Context, run_sync  # noqa: E402
from subfix.srt_io import load_subtitles  # noqa: E402

DATA = ROOT / "data"
DATASETS = {
    "synth_short": (DATA / "synth_short.mkv", DATA / "synth_short.truth.srt", "12-min synthetic"),
    "synth_long": (DATA / "synth_long.mkv", DATA / "synth_long.truth.srt", "100-min synthetic"),
    "tos": (
        DATA / "real" / "tears_of_steel_720p.mov",
        DATA / "real" / "TOS-en.srt",
        "Tears of Steel (EN subs)",
    ),
    "tos_de": (
        DATA / "real" / "tears_of_steel_720p.mov",
        DATA / "real" / "TOS-de.srt",
        "Tears of Steel (DE subs, EN audio)",
    ),
}
METHODS = ["offset", "drift", "piecewise", "smart"]

GEN_SECTION_HEADER = [
    "## Subtitle generation",
    "",
    "`benchmarks/generation_bench.py`: `subfix generate --no-cache` end to end (audio extraction,",
    "VAD, model load, transcription), peak RSS of the whole process tree, WER of the generated",
    "text",
    "against the reference subtitles (normalised tokens). On Tears of Steel the reference is the",
    "official subtitle text, which paraphrases and omits shouted lines, so WER there is an upper",
    "bound on the real recognition error.",
    "",
]
NOTES = [
    "## How to read these numbers",
    "",
    "* **Synthetic films** have exact ground truth (the speech span of every TTS sentence). The",
    "  ~20 ms floor is the VAD frame resolution (32 ms) plus onset latency.",
    "* **Tears of Steel**: the reference is the official, human-timed subtitle file (times",
    "  rounded to 0.5 s). Its cues sit ~0.1 s from where the audio puts the speech, so ~110 ms is",
    "  the floor an audio-only method reaches against that reference, not an error.",
    "* **DE subs on EN audio** cannot be aligned by text; `smart` detects the mismatch and falls",
    "  back to audio-only sync, so its row equals `piecewise`.",
    "* Tears of Steel repeats its opening dialogue in a later scene with the same rhythm, which",
    "  makes audio-only piecewise sync ambiguous for some cut positions (DECISIONS.md D21); its",
    "  loud climax is barely detected by VAD or Whisper, which bounds every method there.",
    "* Time/run excludes the one-off transcription (cached); see the generation table for that.",
    "",
]


def run(datasets: list[str], seeds: int) -> dict:
    setup_logging(quiet=True)
    results: dict = {
        "meta": {
            "date": time.strftime("%Y-%m-%d"),
            "python": platform.python_version(),
            "seeds": seeds,
        },
        "runs": [],
    }
    for name in datasets:
        media, truth_path, _ = DATASETS[name]
        if not media.is_file():
            print(f"skip {name}: {media} missing")
            continue
        truth = load_subtitles(truth_path).subs
        ctx = Context(media)
        t0 = time.perf_counter()
        _ = ctx.speech
        t_vad = time.perf_counter() - t0
        t0 = time.perf_counter()
        _ = ctx.transcript()
        t_asr = time.perf_counter() - t0
        print(
            f"== {name}: {len(truth)} cues, {ctx.audio.duration / 60:.1f} min "
            f"(VAD {t_vad:.1f}s, transcript {t_asr:.1f}s incl. cache)",
            flush=True,
        )
        for kind, fn in CORRUPTORS.items():
            for seed in range(seeds):
                c = fn(
                    truth,
                    random.Random(1000 * seed + {"offset": 1, "drift": 2, "piecewise": 3}[kind]),
                )
                base = timing_errors(c.subs, truth)
                results["runs"].append(
                    {
                        "dataset": name,
                        "kind": kind,
                        "seed": seed,
                        "method": "uncorrected",
                        "desc": c.description,
                        "errors": base.tolist(),
                        "seconds": 0.0,
                    }
                )
                for m in METHODS:
                    t = time.perf_counter()
                    try:
                        r = run_sync(ctx, c.subs, m)
                        errs = timing_errors(r.subs, truth)
                        info = r.describe() + (" | " + "; ".join(r.notes) if r.notes else "")
                    except Exception as exc:  # record failures instead of aborting
                        errs = np.full(len(c.subs), np.nan)
                        info = f"FAILED: {exc}"
                    dt = time.perf_counter() - t
                    s = summarize(errs)
                    print(
                        f"  {kind:9} s{seed} {m:9} med={s['median']:7.0f} "
                        f"<200={s['within200']:5.1f}%  {dt:5.2f}s  {info[:70]}",
                        flush=True,
                    )
                    results["runs"].append(
                        {
                            "dataset": name,
                            "kind": kind,
                            "seed": seed,
                            "method": m,
                            "desc": c.description,
                            "errors": errs.tolist(),
                            "seconds": dt,
                            "info": info,
                        }
                    )
    return results


def aggregate(results: dict) -> dict:
    agg: dict = {}
    for r in results["runs"]:
        key = (r["dataset"], r["kind"], r["method"])
        a = agg.setdefault(key, {"errors": [], "seconds": []})
        a["errors"].extend(r["errors"])
        a["seconds"].append(r["seconds"])
    out = {}
    for key, a in agg.items():
        e = np.array(a["errors"], dtype=float)
        s = summarize(e)
        s["failed_cues"] = float(np.isnan(e).mean() * 100)
        s["seconds"] = float(np.mean(a["seconds"]))
        out[key] = s
    return out


def model_label(g: dict) -> str:
    if g["model"] != "default":
        return g["model"]
    return "large-v3-turbo int8_float16" if g["device"] == "cuda" else "small int8"


def fmt_ms(x: float) -> str:
    if np.isnan(x):
        return "—"
    return f"{x / 1000:.1f} s" if x >= 10_000 else f"{x:.0f}"


def write_markdown(results: dict, path: Path) -> None:
    agg = aggregate(results)
    datasets = list(dict.fromkeys(r["dataset"] for r in results["runs"]))
    lines = [
        "# Benchmark results",
        "",
        f"Generated by `benchmarks/run_benchmark.py` on {results['meta']['date']} "
        f"({results['meta']['seeds']} random corruptions per mode and dataset; RTX 4050 Laptop, "
        "large-v3-turbo int8_float16). Error = |start − true start| per cue, in ms.",
        "",
        "Methods: **offset / drift / piecewise** use audio only (Silero VAD); "
        "**piecewise** is `--mode piecewise` (drift removal + Viterbi sections); "
        "**smart** aligns subtitle text to a Whisper transcript (`--mode smart`, the GPU default).",
        "",
    ]
    for kind, title in (
        ("offset", "Constant offset"),
        ("drift", "Linear drift (fps mismatch)"),
        ("piecewise", "Piecewise offsets (cuts / inserted footage)"),
    ):
        lines += [
            f"## {title}",
            "",
            "| Dataset | Method | Mean | Median | P90 | ≤200 ms | ≤500 ms | Time/run |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
        for ds in datasets:
            for m in ["uncorrected", *METHODS]:
                s = agg.get((ds, kind, m))
                if not s:
                    continue
                label = DATASETS[ds][2]
                best = m != "uncorrected" and s["within200"] >= max(
                    agg[(ds, kind, mm)]["within200"] for mm in METHODS if (ds, kind, mm) in agg
                )
                name = f"**{m}**" if best else m
                lines.append(
                    f"| {label} | {name} | {fmt_ms(s['mean'])} | {fmt_ms(s['median'])} | "
                    f"{fmt_ms(s['p90'])} | {s['within200']:.1f}% | {s['within500']:.1f}% | "
                    f"{s['seconds']:.2f} s |"
                )
        lines.append("")
    lines += GEN_SECTION_HEADER
    gen = ROOT / "generation_results.json"
    if gen.is_file():
        lines += [
            "| Dataset | Device | Model | Audio | Wall time | × real time | Peak RAM | WER |",
            "|---|---|---|---:|---:|---:|---:|---:|",
        ]
        for g in json.loads(gen.read_text()):
            label = DATASETS[g["dataset"]][2] if g["dataset"] in DATASETS else g["dataset"]
            lines.append(
                f"| {label} | {g['device']} | {model_label(g)} | {g['audio_min']:.1f} min | "
                f"{g['wall_s']:.0f} s | {g['x_realtime']:.1f}× | "
                f"{g['peak_rss_mb'] / 1024:.1f} GB | {g['wer']:.1%} |"
            )
        lines.append("")
    lines += NOTES
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--datasets", default="synth_short,synth_long,tos,tos_de")
    ap.add_argument("--out", type=Path, default=ROOT / "results.json")
    ap.add_argument(
        "--report-only", action="store_true", help="Rebuild RESULTS.md from --out JSON."
    )
    a = ap.parse_args()
    if a.report_only:
        results = json.loads(a.out.read_text(encoding="utf-8"))
    else:
        results = run(a.datasets.split(","), a.seeds)
        a.out.write_text(json.dumps(results), encoding="utf-8")
    write_markdown(results, ROOT / "RESULTS.md")
    print(f"wrote {ROOT / 'RESULTS.md'}" + ("" if a.report_only else f" and {a.out}"))


if __name__ == "__main__":
    main()
