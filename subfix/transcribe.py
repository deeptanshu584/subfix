"""Chunked faster-whisper transcription with word timestamps and a JSON transcript cache.

Long films are split into ~10-minute chunks whose boundaries are moved into the quietest nearby
frame of the VAD signal, so no word is cut in half and memory stays bounded. Each chunk is
transcribed with faster-whisper's batched pipeline (VAD-segmented, batched on the GPU).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .audio import AudioFile, file_fingerprint
from .config import CACHE_VERSION, ModelChoice, cache_dir
from .device import default_choice, load_model
from .log import emit_status, log, progress
from .vad import SpeechSignal


@dataclass
class Word:
    start: float
    end: float
    text: str
    prob: float = 1.0


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0


@dataclass
class Transcript:
    language: str
    task: str
    model: str
    segments: list[Segment]
    language_probability: float = 0.0

    @property
    def words(self) -> list[Word]:
        return [w for s in self.segments for w in s.words]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> Transcript:
        d = json.loads(text)
        segs = [
            Segment(**{**s, "words": [Word(**w) for w in s.get("words", [])]})
            for s in d.pop("segments")
        ]
        return cls(segments=segs, **d)


# --------------------------------------------------------------------------- chunk planning


def plan_chunks(
    duration: float,
    speech: SpeechSignal | None,
    target_s: float = 600.0,
    search_s: float = 30.0,
) -> list[tuple[float, float]]:
    """Split ``[0, duration]`` into ~``target_s`` chunks with cuts placed in silence."""
    if duration <= target_s * 1.25:
        return [(0.0, duration)]
    cuts = [0.0]
    t = target_s
    while t < duration - target_s * 0.25:
        cut = t
        if speech is not None and len(speech.probs):
            lo = max(0, int((t - search_s) / speech.hop))
            hi = min(len(speech.probs), int((t + search_s) / speech.hop))
            if hi - lo > 8:
                # smooth over ~0.5 s so we pick the middle of a pause, not a single quiet frame
                k = max(1, int(0.5 / speech.hop))
                win = np.convolve(speech.probs[lo:hi], np.ones(k) / k, mode="same")
                cut = (lo + int(np.argmin(win))) * speech.hop
        cuts.append(cut)
        t = cut + target_s
    cuts.append(duration)
    return list(zip(cuts[:-1], cuts[1:], strict=True))


# --------------------------------------------------------------------------- cleanup

_HALLUCINATIONS = re.compile(
    r"^\s*(thanks? (you )?for watching|thank you\.?|subtitles? (by|made by)|"
    r"(please )?subscribe|amara\.org|transcribed by|♪+|\.+)\s*[.!]?\s*$",
    re.IGNORECASE,
)


def _is_hallucination(seg: Segment) -> bool:
    if seg.no_speech_prob > 0.8 and seg.avg_logprob < -0.8:
        return True
    return bool(_HALLUCINATIONS.match(seg.text)) and seg.avg_logprob < -0.4


def clean_segments(segments: list[Segment]) -> list[Segment]:
    """Drop likely hallucinations and collapse repetition loops (same text 3+ times in a row)."""
    out: list[Segment] = []
    repeats = 0
    for seg in segments:
        if not seg.text.strip() or _is_hallucination(seg):
            continue
        if out and seg.text.strip().lower() == out[-1].text.strip().lower():
            repeats += 1
            if repeats >= 2:
                continue
        else:
            repeats = 0
        out.append(seg)
    return out


# --------------------------------------------------------------------------- main entry


def _cache_path(audio: AudioFile, choice: ModelChoice, task: str, language: str | None) -> Path:
    key = file_fingerprint(audio.path, extra=f"{choice.model}|{task}|{language}")
    return cache_dir("transcripts") / f"{Path(audio.path).stem[:40]}.{key}.v{CACHE_VERSION}.json"


def transcribe(
    audio: AudioFile,
    speech: SpeechSignal | None = None,
    choice: ModelChoice | None = None,
    task: str = "transcribe",
    language: str | None = None,
    batch_size: int = 8,
    use_cache: bool = True,
    beam_size: int = 5,
) -> Transcript:
    """Transcribe (or translate to English) the whole audio file, with word timestamps.

    Results are cached per (audio, model, task, language); a cache hit returns immediately.
    """
    choice = choice or default_choice(task)
    cache = _cache_path(audio, choice, task, language)
    if use_cache and cache.is_file():
        log.info("Using cached transcript ([dim]%s[/])", cache.name)
        return Transcript.from_json(cache.read_text(encoding="utf-8"))

    from faster_whisper import BatchedInferencePipeline

    model, used = load_model(choice, task)
    pipeline = BatchedInferencePipeline(model)
    chunks = plan_chunks(audio.duration, speech)
    log.info(
        "Transcribing %.1f min of audio in %d chunk(s) with %s (task=%s)",
        audio.duration / 60,
        len(chunks),
        used,
        task,
    )

    segments: list[Segment] = []
    lang, lang_prob = language, 0.0
    with progress() as bar:
        job = bar.add_task(
            "Transcribing" if task == "transcribe" else "Translating", total=round(audio.duration)
        )
        for ci, (a, b) in enumerate(chunks):
            samples = audio.read(a, b)
            seg_iter, info = pipeline.transcribe(
                samples,
                language=lang,
                task=task,
                batch_size=batch_size,
                beam_size=beam_size,
                word_timestamps=True,
                vad_filter=True,
                condition_on_previous_text=False,
                language_detection_segments=4,
            )
            if lang is None:
                lang, lang_prob = info.language, info.language_probability
                log.info("Detected language: [bold]%s[/] (p=%.2f)", lang, lang_prob)
            for s in seg_iter:
                words = [
                    Word(
                        round(w.start + a, 3), round(w.end + a, 3), w.word, round(w.probability, 3)
                    )
                    for w in (s.words or [])
                ]
                segments.append(
                    Segment(
                        round(s.start + a, 3),
                        round(s.end + a, 3),
                        s.text.strip(),
                        words,
                        round(s.avg_logprob, 3),
                        round(s.no_speech_prob, 3),
                    )
                )
                bar.update(job, completed=min(round(audio.duration), round(s.end + a)))
            bar.update(job, completed=round(b))
            emit_status(f"transcribed {100 * b / max(audio.duration, 1):.0f}%")
            del samples
            log.debug("chunk %d/%d done", ci + 1, len(chunks))

    segments.sort(key=lambda s: s.start)
    tr = Transcript(lang or "unknown", task, used.model, clean_segments(segments), lang_prob)
    if use_cache:
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(tr.to_json(), encoding="utf-8")
        tmp.replace(cache)
    return tr
