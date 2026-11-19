"""Smart sync: align human-written subtitle text to a Whisper transcript.

Keeps the original subtitle text but takes timings from what Whisper heard.

Pipeline
--------
1. Tokenise both sides (lower-case, punctuation/tags/sound-descriptions removed; CJK → chars).
2. **Anchors**: word trigrams that are rare on both sides are matched; the longest chain that is
   increasing on both sides (LIS, O(P log P)) keeps only order-consistent anchors. Text order is
   unaffected by timing corruption, so this works for offset, drift *and* cuts alike.
3. **Gap filling**: between consecutive anchors, a fuzzy Needleman–Wunsch DP (rapidfuzz similarity
   per token) aligns the remaining words.
4. **Per-line offset**: start of the first matched word, minus the expected time to speak the
   characters before it → ``δ_line = true_start − subtitle_start``.
5. **Robust smoothing**: a running median of δ over neighbouring matched lines removes Whisper
   word-timing noise and bad matches while keeping step changes (cuts) sharp.
6. **Unmatched lines** take the offset of the previous or next matched line (whichever overlaps
   detected speech better), or a time interpolation if both agree.

If too few lines match (e.g. subtitles in another language than the audio), the caller falls
back to audio-only sync.
"""

from __future__ import annotations

import bisect
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from rapidfuzz.distance import Levenshtein

from ..log import log
from ..srt_io import Subtitle
from ..transcribe import Transcript
from ..vad import SpeechSignal
from .common import RATE, SyncResult, alignment_score, dialogue, reference_signal

_TAG = re.compile(r"<[^>]*>|\{[^}]*\}")
_SOUND = re.compile(r"\[[^\]]*\]|\([^)]*\)|♪[^♪]*♪?")
_SPEAKER = re.compile(r"^\s*(-|[A-Z][A-Z .']{1,20}:)\s*", re.MULTILINE)
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
_HYPHEN_JOIN = re.compile(r"(?<=[^\W\d_])-(?=[^\W\d_])")
#: Spelling variants that Whisper and subtitle authors write differently (applied to both sides).
_CANON = {
    "mr": "mister",
    "mrs": "missus",
    "ms": "miss",
    "dr": "doctor",
    "st": "saint",
    "ok": "okay",
    "o.k": "okay",
    "gonna": "going",
    "wanna": "want",
    "gotta": "got",
    "'cause": "because",
    "cos": "because",
    "till": "until",
    "'til": "until",
}


def _is_cjk_char(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in ("W", "F") and ch.isalnum()


def tokenize(text: str) -> list[tuple[str, int]]:
    """Normalised tokens with their character offset in the cleaned text."""
    text = _TAG.sub("", text)
    text = _SOUND.sub(" ", text)
    text = _SPEAKER.sub("", text)
    text = unicodedata.normalize("NFKC", text).lower().replace("’", "'").replace("‘", "'")
    text = _HYPHEN_JOIN.sub("", text)
    out: list[tuple[str, int]] = []
    for m in _WORD.finditer(text):
        w = _CANON.get(m.group(0), m.group(0))
        if any(_is_cjk_char(c) for c in w):
            out.extend((c, m.start() + k) for k, c in enumerate(w) if c.strip())
        else:
            out.append((w, m.start()))
    return out


@dataclass
class SubToken:
    text: str
    line: int
    chars_before: int  # characters of the line spoken before this token


@dataclass
class AsrToken:
    text: str
    start: float
    end: float


def subtitle_tokens(subs: Sequence[Subtitle]) -> list[SubToken]:
    toks: list[SubToken] = []
    for i, s in enumerate(subs):
        for t, pos in tokenize(s.text):
            toks.append(SubToken(t, i, pos))
    return toks


def transcript_tokens(tr: Transcript) -> list[AsrToken]:
    toks: list[AsrToken] = []
    for w in tr.words:
        parts = tokenize(w.text)
        if not parts:
            continue
        if len(parts) == 1:
            toks.append(AsrToken(parts[0][0], w.start, w.end))
        else:  # e.g. "well-known" or CJK multi-char word: spread time evenly
            step = (w.end - w.start) / len(parts)
            for k, (p, _) in enumerate(parts):
                toks.append(AsrToken(p, w.start + k * step, w.start + (k + 1) * step))
    return toks


# --------------------------------------------------------------------------- alignment


def longest_increasing_chain(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Longest chain of pairs strictly increasing in both coordinates (patience sorting)."""
    pairs = sorted(set(pairs), key=lambda p: (p[0], -p[1]))
    tails: list[int] = []  # j values
    tails_idx: list[int] = []
    prev = [-1] * len(pairs)
    for k, (_, j) in enumerate(pairs):
        pos = bisect.bisect_left(tails, j)
        if pos == len(tails):
            tails.append(j)
            tails_idx.append(k)
        else:
            tails[pos] = j
            tails_idx[pos] = k
        prev[k] = tails_idx[pos - 1] if pos > 0 else -1
    chain = []
    k = tails_idx[-1] if tails_idx else -1
    while k >= 0:
        chain.append(pairs[k])
        k = prev[k]
    return chain[::-1]


def anchor_pairs(
    a: list[str], b: list[str], n: int = 3, max_count: int = 2
) -> list[tuple[int, int]]:
    """Order-consistent token matches from rare n-grams shared by ``a`` and ``b``."""
    if len(a) < n or len(b) < n:
        return []
    ga = [tuple(a[i : i + n]) for i in range(len(a) - n + 1)]
    gb = [tuple(b[j : j + n]) for j in range(len(b) - n + 1)]
    ca, cb = Counter(ga), Counter(gb)
    index: dict[tuple, list[int]] = defaultdict(list)
    for j, g in enumerate(gb):
        if cb[g] <= max_count:
            index[g].append(j)
    pairs = []
    for i, g in enumerate(ga):
        if ca[g] <= max_count and g in index:
            pairs.extend((i, j) for j in index[g])
    chain = longest_increasing_chain(pairs)
    tok_pairs: dict[int, int] = {}
    last_j = -1
    for i, j in chain:
        for t in range(n):
            if i + t not in tok_pairs and j + t > last_j:
                tok_pairs[i + t] = j + t
                last_j = j + t
    return sorted(tok_pairs.items())


def _sim(x: str, y: str) -> float:
    if x == y:
        return 1.0
    if min(len(x), len(y)) < 3:
        return 0.0
    return Levenshtein.normalized_similarity(x, y)


def fuzzy_align(a: list[str], b: list[str], max_cells: int = 60_000) -> list[tuple[int, int]]:
    """Needleman–Wunsch on tokens with fuzzy match score; returns matched index pairs."""
    m, n = len(a), len(b)
    if not m or not n or m * n > max_cells:
        return []
    gap = -0.6
    score = np.zeros((m + 1, n + 1))
    score[:, 0] = gap * np.arange(m + 1)
    score[0, :] = gap * np.arange(n + 1)
    move = np.zeros((m + 1, n + 1), dtype=np.int8)  # 0 diag, 1 up, 2 left
    move[1:, 0] = 1
    move[0, 1:] = 2
    sims = np.array([[_sim(x, y) for y in b] for x in a])
    match = np.where(sims >= 0.75, 2.0 * sims, -1.0)
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            d = score[i - 1, j - 1] + match[i - 1, j - 1]
            u = score[i - 1, j] + gap
            lft = score[i, j - 1] + gap
            if d >= u and d >= lft:
                score[i, j], move[i, j] = d, 0
            elif u >= lft:
                score[i, j], move[i, j] = u, 1
            else:
                score[i, j], move[i, j] = lft, 2
    pairs = []
    i, j = m, n
    while i > 0 and j > 0:
        mv = move[i, j]
        if mv == 0:
            if match[i - 1, j - 1] > 0:
                pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif mv == 1:
            i -= 1
        else:
            j -= 1
    return pairs[::-1]


def align_tokens(a: list[str], b: list[str]) -> list[tuple[int, int]]:
    """Full alignment: rare-trigram anchors + LIS, then fuzzy DP inside the gaps."""
    anchors = anchor_pairs(a, b)
    if len(anchors) < 5:  # short or noisy input: try bigrams
        anchors = anchor_pairs(a, b, n=2, max_count=3)
    pairs: list[tuple[int, int]] = []
    prev_i, prev_j = -1, -1
    for i, j in [*anchors, (len(a), len(b))]:
        if i - prev_i > 1 and j - prev_j > 1:
            gi, gj = prev_i + 1, prev_j + 1
            for x, y in fuzzy_align(a[gi:i], b[gj:j]):
                pairs.append((gi + x, gj + y))
        if i < len(a):
            pairs.append((i, j))
        prev_i, prev_j = i, j
    return pairs


# --------------------------------------------------------------------------- timing


def _speech_rate(asr: list[AsrToken]) -> float:
    """Characters per second in the transcript (for estimating time before the first match)."""
    chars = sum(len(t.text) + 1 for t in asr)
    dur = sum(max(0.05, t.end - t.start) for t in asr)
    return float(np.clip(chars / dur if dur else 14.0, 8.0, 25.0))


def running_median(values: np.ndarray, k: int) -> np.ndarray:
    out = np.empty_like(values)
    n = len(values)
    for i in range(n):
        out[i] = np.median(values[max(0, i - k) : min(n, i + k + 1)])
    return out


def snap_onset(start: float, end: float, speech: SpeechSignal | None, thr: float = 0.5) -> float:
    """Move a word start forward to the first VAD speech frame inside ``[start, end)``.

    Whisper's cross-attention word timings let the first word of a segment begin in the
    preceding silence (observed 100–400 ms early); VAD knows where the voice really starts.
    """
    if speech is None:
        return start
    a = max(0, int(start / speech.hop))
    b = min(len(speech.probs), max(a + 1, int(end / speech.hop)))
    hits = np.flatnonzero(speech.probs[a:b] >= thr)
    return (a + hits[0]) * speech.hop if len(hits) else start


def local_slope(t: np.ndarray, d: np.ndarray, span: int = 20, min_dt: float = 15.0) -> float:
    """Median slope of ``d`` over ``t`` between points at most ``span`` indices apart."""
    slopes = []
    for k in range(1, span + 1):
        dt = t[k:] - t[:-k]
        ok = dt >= min_dt
        slopes.append(((d[k:] - d[:-k])[ok]) / dt[ok])
    allv = np.concatenate(slopes) if slopes else np.array([])
    if len(allv) < 10:
        return 0.0
    b = float(np.median(allv))
    return b if abs(b) < 0.3 else 0.0


def line_emission(sub: Subtitle, offset: float, ref: np.ndarray) -> float:
    a = max(0, int((sub.start + offset) * RATE))
    b = min(len(ref), int((sub.end + offset) * RATE))
    return float(ref[a:b].sum()) if b > a else -1e9


def sync_smart(
    subs: Sequence[Subtitle],
    transcript: Transcript,
    speech: SpeechSignal | None = None,
    smooth_lines: int = 4,
    min_match_ratio: float = 0.25,
) -> SyncResult:
    """Re-time ``subs`` using the word timings of ``transcript``.

    Raises ``ValueError`` if fewer than ``min_match_ratio`` of the dialogue lines can be matched.
    """
    subs = sorted(subs, key=lambda s: s.start)
    stoks = subtitle_tokens(subs)
    asr = transcript_tokens(transcript)
    if not stoks or not asr:
        raise ValueError("nothing to align (empty subtitles or transcript)")
    pairs = align_tokens([t.text for t in stoks], [t.text for t in asr])
    rate = _speech_rate(asr)

    # per-line raw offsets from the first matched token of each line
    first: dict[int, tuple[int, int]] = {}
    count: Counter[int] = Counter()
    for si, aj in pairs:
        ln = stoks[si].line
        count[ln] += 1
        if ln not in first:
            first[ln] = (si, aj)
    n_lines = len(subs)
    raw = np.full(n_lines, np.nan)
    for ln, (si, aj) in first.items():
        n_tok_line = sum(1 for t in stoks if t.line == ln)
        if count[ln] < (1 if n_tok_line <= 2 else 2):
            continue  # a single matched word in a long line is too weak
        onset = snap_onset(asr[aj].start, asr[aj].end, speech)
        true_start = onset - stoks[si].chars_before / rate
        raw[ln] = true_start - subs[ln].start

    matched = np.flatnonzero(~np.isnan(raw))
    dlg_lines = sum(1 for s in subs if subtitle_tokens([s]))
    ratio = len(matched) / max(1, dlg_lines)
    log.info(
        "Smart sync: %d/%d lines matched to the transcript (%.0f%%)",
        len(matched),
        dlg_lines,
        100 * ratio,
    )
    if ratio < min_match_ratio or len(matched) < 3:
        raise ValueError(
            f"only {ratio:.0%} of subtitle lines match the transcript — wrong language or "
            "unrelated subtitles?"
        )

    # Model: global drift (slope) + piecewise-constant offsets (cuts) + Whisper noise.
    # The slope is the median of slopes between *nearby* matched lines, so the few pairs that
    # straddle a cut cannot bias it. After detrending, δ is piecewise flat and a running median
    # removes noise/outliers while keeping the steps sharp.
    t_m = np.array([subs[i].start for i in matched])
    b = local_slope(t_m, raw[matched])
    trend = b * t_m
    smooth = trend + running_median(raw[matched] - trend, smooth_lines)
    # reject lines whose own estimate disagrees strongly with their neighbourhood
    offsets = np.full(n_lines, np.nan)
    offsets[matched] = smooth

    ref = reference_signal(speech) if speech is not None else None
    mt = matched
    for ln in range(n_lines):
        if not np.isnan(offsets[ln]):
            continue
        k = np.searchsorted(mt, ln)
        prev_ln = mt[k - 1] if k > 0 else None
        next_ln = mt[k] if k < len(mt) else None
        if prev_ln is None or next_ln is None:
            offsets[ln] = offsets[prev_ln if next_ln is None else next_ln]
            continue
        op, on = offsets[prev_ln], offsets[next_ln]
        expected = b * (subs[next_ln].start - subs[prev_ln].start)  # drift between neighbours
        if abs((on - op) - expected) < 0.3:
            tp, tn = subs[prev_ln].start, subs[next_ln].start
            w = (subs[ln].start - tp) / (tn - tp) if tn > tp else 0.5
            offsets[ln] = op + w * (on - op)
        elif ref is not None:
            offsets[ln] = max((op, on), key=lambda o: line_emission(subs[ln], o, ref))
        else:
            near_prev = subs[ln].start - subs[prev_ln].end < subs[next_ln].start - subs[ln].end
            offsets[ln] = op if near_prev else on

    out = [Subtitle(s.start + o, s.end + o, s.text) for s, o in zip(subs, offsets, strict=True)]
    # remove overlaps introduced by different offsets of neighbouring lines
    for a, b in zip(out, out[1:], strict=False):
        if a.end > b.start - 0.02 and a.start < b.start:
            a.end = max(a.start + 0.3, b.start - 0.02)

    score = alignment_score(dialogue(out), ref) if ref is not None else float("nan")
    return SyncResult(
        out,
        "smart",
        score=score,
        confidence=float(ratio),
        params={
            "matched_lines": int(len(matched)),
            "total_lines": int(dlg_lines),
            "match_ratio": float(ratio),
            "offset": float(np.median(smooth)),
        },
    )
