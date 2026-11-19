"""Turn word-timestamped Whisper output into readable subtitle cues.

Rules (Netflix-style, simplified): at most 2 lines of ~42 characters, at most ~6 s on screen,
prefer to break after sentence punctuation, never merge across long pauses, and keep each cue on
screen for at least ~0.8 s (borrowing time from the following gap, never overlapping).
"""

from __future__ import annotations

import re
import unicodedata

from .config import MAX_CUE_SECONDS, MAX_LINE_CHARS, MAX_LINES, MIN_CUE_SECONDS, MIN_GAP_SECONDS
from .srt_io import Subtitle
from .transcribe import Transcript, Word

_SENTENCE_END = re.compile(r"[.!?…。！？]['\")\]]*$")
_CLAUSE_END = re.compile(r"[,;:，、；：—-]['\")\]]*$")


def _is_cjk(text: str) -> bool:
    cjk = sum(1 for ch in text if unicodedata.east_asian_width(ch) in ("W", "F"))
    return cjk > max(3, len(text) // 4)


def _join(words: list[Word]) -> str:
    return "".join(w.text for w in words).strip()


def wrap_lines(text: str, max_chars: int = MAX_LINE_CHARS) -> str:
    """Split ``text`` into two balanced lines if it is longer than ``max_chars``."""
    text = " ".join(text.split()) if " " in text else text
    if len(text) <= max_chars:
        return text
    if " " not in text:  # CJK: split by characters
        mid = len(text) // 2
        return text[:mid] + "\n" + text[mid:]
    spaces = [i for i, ch in enumerate(text) if ch == " "]
    mid = len(text) / 2

    def score(i: int) -> float:
        bonus = -6 if text[i - 1] in ",.;:!?" else 0  # prefer breaking after punctuation
        return abs(i - mid) + bonus

    best = min(spaces, key=score)
    return text[:best] + "\n" + text[best + 1 :]


def words_to_cues(
    words: list[Word],
    max_chars: int | None = None,
    max_duration: float = MAX_CUE_SECONDS,
    pause_split: float = 0.7,
) -> list[list[Word]]:
    """Group words into cue-sized runs."""
    if not words:
        return []
    if max_chars is None:
        sample = _join(words[:200])
        max_chars = (18 if _is_cjk(sample) else MAX_LINE_CHARS) * MAX_LINES
    groups: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur:
            gap = w.start - cur[-1].end
            text_len = len(_join([*cur, w]))
            too_long = text_len > max_chars or (w.end - cur[0].start) > max_duration
            if gap >= pause_split or too_long:
                if too_long and gap < pause_split:
                    # move back to the last clause boundary if it leaves a reasonable cue
                    cut = None
                    for j in range(len(cur) - 1, 0, -1):
                        if _SENTENCE_END.search(cur[j - 1].text.strip()) or _CLAUSE_END.search(
                            cur[j - 1].text.strip()
                        ):
                            if len(_join(cur[:j])) >= max_chars * 0.4:
                                cut = j
                            break
                    if cut:
                        groups.append(cur[:cut])
                        cur = cur[cut:]
                    else:
                        groups.append(cur)
                        cur = []
                else:
                    groups.append(cur)
                    cur = []
        cur.append(w)
        # end the cue at a sentence boundary once it has some substance
        if _SENTENCE_END.search(w.text.strip()) and len(_join(cur)) >= max_chars * 0.35:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def transcript_to_subtitles(tr: Transcript) -> list[Subtitle]:
    """Build final subtitle cues from a transcript (uses word timestamps when available)."""
    subs: list[Subtitle] = []
    for seg in tr.segments:
        words = seg.words or [Word(seg.start, seg.end, " " + seg.text)]
        for group in words_to_cues(words):
            text = _join(group)
            if not text:
                continue
            line_chars = 18 if _is_cjk(text) else MAX_LINE_CHARS
            subs.append(Subtitle(group[0].start, group[-1].end, wrap_lines(text, line_chars)))
    subs.sort(key=lambda s: s.start)
    # timing polish: small tail, minimum duration, no overlaps
    for i, s in enumerate(subs):
        nxt = subs[i + 1].start if i + 1 < len(subs) else float("inf")
        limit = nxt - MIN_GAP_SECONDS
        end = max(s.end + 0.15, s.start + MIN_CUE_SECONDS)
        s.end = max(s.start + 0.2, min(end, limit))
    return subs
