"""Prepare the media used by web_e2e.py and screenshots.py in .cache/tmp/e2e/ (CPU only).

Needs benchmarks/data/real/tears_of_steel_720p.mov + TOS-en.srt (see benchmarks/RESULTS.md),
benchmarks/data/real/grimm_grossvater_de.mp3 and benchmarks/data/synth_long.mkv.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from subfix.audio import find_tool  # noqa: E402
from subfix.srt_io import load_subtitles, shift, write_srt  # noqa: E402

E2E = ROOT / ".cache" / "tmp" / "e2e"
REAL = ROOT / "benchmarks" / "data" / "real"
TOS = REAL / "tears_of_steel_720p.mov"


def ff(*args: str) -> None:
    subprocess.run([find_tool("ffmpeg"), "-v", "error", "-y", *args], check=True)


def main() -> None:
    E2E.mkdir(parents=True, exist_ok=True)
    ff("-ss", "0", "-t", "180", "-i", str(TOS), "-c", "copy", str(E2E / "tears_clip.mkv"))
    ff(
        "-ss",
        "180",
        "-t",
        "180",
        "-i",
        str(TOS),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(E2E / "tears_part2.mp4"),
    )
    ff(
        "-ss",
        "360",
        "-t",
        "180",
        "-i",
        str(TOS),
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        str(E2E / "tears_part3.mp3"),
    )
    shutil.copy(TOS, E2E / "Tears of Steel.mov")
    shutil.copy(REAL / "grimm_grossvater_de.mp3", E2E / "grimm_de.mp3")
    shutil.copy(ROOT / "benchmarks" / "data" / "synth_long.mkv", E2E / "cancel_long.mkv")
    (E2E / "report.docx").write_text("not a movie")
    subs = [s for s in load_subtitles(REAL / "TOS-en.srt").subs if s.end < 180]
    write_srt(shift(subs, 3.5), E2E / "tears_clip_offset.srt")
    write_srt(subs, E2E / "tears_clip_truth.srt")
    print(f"prepared {E2E}")


if __name__ == "__main__":
    main()
