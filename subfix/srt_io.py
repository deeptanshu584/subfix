"""Reading and writing subtitle files.

* SRT and WebVTT are parsed by a tolerant hand-written parser (missing indices, missing blank
  lines, ``.`` instead of ``,``, 1–3 digit milliseconds, blank lines inside cues).
* ASS/SSA are read through pysubs2 (override tags stripped, ``\\N`` → newline).
* Encoding detection: BOM → strict UTF-8 → charset-normalizer for non-Western scripts →
  cp1252 → latin-1 (never fails).
* Output is always UTF-8 SRT with CRLF line endings; originals are never modified.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

SUBTITLE_EXTENSIONS = (".srt", ".vtt", ".ass", ".ssa")


class SubtitleError(ValueError):
    """Raised for unreadable or empty subtitle files."""


@dataclass(slots=True)
class Subtitle:
    """One subtitle cue. Times are in seconds."""

    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def mid(self) -> float:
        return 0.5 * (self.start + self.end)


@dataclass
class SubtitleDoc:
    """A parsed subtitle file plus how it was read."""

    subs: list[Subtitle]
    encoding: str = "utf-8"
    fmt: str = "srt"
    source: Path | None = None
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- encoding


_WESTERN = {
    "cp1252",
    "windows-1252",
    "latin_1",
    "latin-1",
    "iso8859_1",
    "iso-8859-1",
    "iso8859_15",
    "ascii",
    "cp1250",
    "iso8859_2",
    "mac_roman",
    "cp850",
}


def decode_bytes(data: bytes) -> tuple[str, str]:
    """Decode subtitle bytes, returning ``(text, encoding_name)``."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace"), "utf-16"
    # UTF-16 without BOM: NUL bytes in most odd (LE) or even (BE) positions of Latin text.
    odd, even = data[1:200:2], data[:200:2]
    if len(data) >= 16 and odd.count(0) > 0.4 * len(odd):
        return data.decode("utf-16-le", errors="replace"), "utf-16-le"
    if len(data) >= 16 and even.count(0) > 0.4 * len(even):
        return data.decode("utf-16-be", errors="replace"), "utf-16-be"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(data).best()
        if best is not None and best.encoding and best.encoding.lower() not in _WESTERN:
            return str(best), best.encoding
    except Exception:  # pragma: no cover - library robustness
        pass
    try:
        return data.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1"


# --------------------------------------------------------------------------- timestamps

_TS = r"(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:[,.:](\d{1,3}))?"
_TS_LINE = re.compile(rf"^\s*{_TS}\s*-->\s*{_TS}")


def _ts_to_seconds(h: str | None, m: str, s: str, frac: str | None) -> float:
    ms = int((frac or "0").ljust(3, "0")[:3])
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + ms / 1000.0


def format_timestamp(seconds: float, sep: str = ",") -> str:
    """Format seconds as ``HH:MM:SS,mmm`` (negative values clamp to zero)."""
    total_ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


# --------------------------------------------------------------------------- parsers

_VTT_TAG = re.compile(r"<(?!/?(?:i|b|u)>)[^>]*>")


def _parse_cues(text: str, vtt: bool) -> list[Subtitle]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ts_idx = [i for i, ln in enumerate(lines) if _TS_LINE.match(ln)]
    subs: list[Subtitle] = []
    for k, i in enumerate(ts_idx):
        g = _TS_LINE.match(lines[i]).groups()  # type: ignore[union-attr]
        start = _ts_to_seconds(*g[:4])
        end = _ts_to_seconds(*g[4:])
        stop = ts_idx[k + 1] if k + 1 < len(ts_idx) else len(lines)
        body = lines[i + 1 : stop]
        if k + 1 < len(ts_idx) and body and body[-1].strip():
            # The line right before the next timestamp is that cue's identifier/index.
            last = body[-1].strip()
            before_blank = len(body) >= 2 and not body[-2].strip()
            if last.isdigit() or (vtt and before_blank):
                body = body[:-1]
        while body and not body[-1].strip():
            body.pop()
        if vtt:
            # drop NOTE blocks that were swallowed into this cue
            cut = next(
                (
                    j
                    for j in range(1, len(body))
                    if not body[j - 1].strip() and body[j].startswith(("NOTE", "STYLE", "REGION"))
                ),
                None,
            )
            if cut is not None:
                body = body[: cut - 1]
            body = [html.unescape(_VTT_TAG.sub("", ln)) for ln in body]
        cue_text = "\n".join(ln.rstrip() for ln in body).strip("\n")
        if not cue_text.strip():
            continue
        subs.append(Subtitle(start, end, cue_text))
    return subs


def parse_srt(text: str) -> list[Subtitle]:
    """Parse SRT text (tolerant)."""
    return _parse_cues(text, vtt=False)


def parse_vtt(text: str) -> list[Subtitle]:
    """Parse WebVTT text."""
    return _parse_cues(text, vtt=True)


def parse_ass(text: str) -> list[Subtitle]:
    """Parse ASS/SSA text via pysubs2."""
    import pysubs2

    # Pass the format explicitly: pysubs2's auto-detection needs a styles section, which
    # minimal real-world files omit.
    fmt = "ssa" if "[V4 Styles]" in text else "ass"
    try:
        doc = pysubs2.SSAFile.from_string(text, format_=fmt)
    except Exception as exc:
        raise SubtitleError(f"Could not parse ASS/SSA subtitles: {exc}") from exc
    out: list[Subtitle] = []
    for ev in doc.events:
        if ev.is_comment:
            continue
        plain = ev.plaintext.strip()
        if plain:
            out.append(Subtitle(ev.start / 1000.0, ev.end / 1000.0, plain))
    return out


def _sniff_format(text: str, suffix: str) -> str:
    head = text.lstrip("﻿ \t\r\n")[:2000]
    if head.startswith("WEBVTT"):
        return "vtt"
    if "[Script Info]" in head or "[Events]" in text[:20000]:
        return "ass"
    if suffix in (".vtt",):
        return "vtt"
    if suffix in (".ass", ".ssa"):
        return "ass"
    return "srt"


def parse_subtitles(text: str, suffix: str = ".srt") -> tuple[list[Subtitle], str]:
    """Parse text of any supported format. Returns ``(subs, format_name)``."""
    fmt = _sniff_format(text, suffix.lower())
    parser = {"srt": parse_srt, "vtt": parse_vtt, "ass": parse_ass}[fmt]
    return parser(text), fmt


def load_subtitles(path: Path | str) -> SubtitleDoc:
    """Load a subtitle file, detecting encoding and format. Cues are sorted by start time."""
    path = Path(path)
    if not path.is_file():
        raise SubtitleError(f"Subtitle file not found: {path}")
    text, enc = decode_bytes(path.read_bytes())
    subs, fmt = parse_subtitles(text, path.suffix)
    if not subs:
        raise SubtitleError(f"No subtitle cues found in {path} (format guessed: {fmt})")
    doc = SubtitleDoc(subs=subs, encoding=enc, fmt=fmt, source=path)
    if any(b.start < a.start for a, b in zip(subs, subs[1:], strict=False)):
        doc.warnings.append("cues were not in chronological order; sorted")
        subs.sort(key=lambda s: s.start)
    bad = sum(1 for s in subs if s.end <= s.start)
    if bad:
        doc.warnings.append(f"{bad} cue(s) had end <= start")
    return doc


# --------------------------------------------------------------------------- writing


def sanitize(subs: Iterable[Subtitle], min_duration: float = 0.3) -> list[Subtitle]:
    """Sort, clamp to t >= 0, ensure ``end > start`` and drop cues pushed entirely below 0."""
    out: list[Subtitle] = []
    for s in sorted(subs, key=lambda x: x.start):
        if s.end <= 0:
            continue
        start = max(0.0, s.start)
        end = max(s.end, start + min_duration) if s.end <= start else s.end
        out.append(Subtitle(start, end, s.text))
    return out


def to_srt(subs: Sequence[Subtitle]) -> str:
    """Render cues as SRT text (LF newlines)."""
    blocks = []
    for i, s in enumerate(sanitize(subs), 1):
        blocks.append(
            f"{i}\n{format_timestamp(s.start)} --> {format_timestamp(s.end)}\n{s.text.strip()}\n"
        )
    return "\n".join(blocks)


def write_srt(subs: Sequence[Subtitle], path: Path | str) -> Path:
    """Write UTF-8 SRT with CRLF line endings (atomically). Returns the path."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(to_srt(subs).replace("\n", "\r\n").encode("utf-8"))
    tmp.replace(path)
    return path


def transform(subs: Sequence[Subtitle], fn: Callable[[float], float]) -> list[Subtitle]:
    """Apply a time mapping ``fn`` to start and end of every cue."""
    return [replace(s, start=fn(s.start), end=fn(s.end)) for s in subs]


def shift(subs: Sequence[Subtitle], offset: float, scale: float = 1.0) -> list[Subtitle]:
    """Return cues with ``t' = scale * t + offset``."""
    return transform(subs, lambda t: scale * t + offset)


# --------------------------------------------------------------------------- discovery


def find_sidecar(video: Path) -> Path | None:
    """Find a subtitle next to ``video``: exact stem first, then ``stem.<lang>.srt`` variants.

    Files produced by subfix itself (``.synced`` / ``.generated``) are ignored.
    """
    video = Path(video)
    folder = video.parent
    exact = [folder / f"{video.stem}{ext}" for ext in SUBTITLE_EXTENSIONS]
    for p in exact:
        if p.is_file():
            return p
    cands = []
    for p in folder.iterdir():
        name = p.name.lower()
        if (
            p.suffix.lower() in SUBTITLE_EXTENSIONS
            and p.name.startswith(video.stem + ".")
            and ".synced." not in name
            and ".generated." not in name
            and not name.endswith((".synced.srt", ".generated.srt"))
        ):
            cands.append(p)
    if not cands:
        return None

    # prefer English, then SRT, then shortest name
    def rank(p: Path) -> tuple[int, int, int]:
        n = p.name.lower()
        eng = 0 if any(t in n for t in (".en.", ".eng.", ".english.")) else 1
        return (eng, 0 if p.suffix.lower() == ".srt" else 1, len(n))

    return sorted(cands, key=rank)[0]
