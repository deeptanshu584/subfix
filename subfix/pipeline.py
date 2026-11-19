"""High-level operations used by the CLI: generate, sync, auto."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .audio import AudioFile, load_audio
from .config import ModelChoice
from .cues import transcript_to_subtitles
from .device import cuda_available, default_choice
from .log import console, emit_status, log
from .srt_io import Subtitle, SubtitleDoc, find_sidecar, load_subtitles, write_srt
from .sync.common import SyncResult, alignment_score, dialogue, reference_signal
from .transcribe import Transcript, transcribe
from .vad import SpeechSignal, speech_signal


@dataclass
class Context:
    """Lazily computed per-video artefacts shared between steps (audio, VAD, transcript)."""

    media: Path
    audio_track: int = 0
    use_cache: bool = True
    _audio: AudioFile | None = None
    _speech: SpeechSignal | None = None
    _transcripts: dict[tuple, Transcript] = field(default_factory=dict)

    @property
    def audio(self) -> AudioFile:
        if self._audio is None:
            emit_status("extracting audio")
            self._audio = load_audio(self.media, self.audio_track)
        return self._audio

    @property
    def speech(self) -> SpeechSignal:
        if self._speech is None:
            emit_status("detecting speech")
            self._speech = speech_signal(self.audio, use_cache=self.use_cache)
        return self._speech

    def transcript(
        self,
        task: str = "transcribe",
        language: str | None = None,
        choice: ModelChoice | None = None,
        batch_size: int = 8,
    ) -> Transcript:
        choice = choice or default_choice(task)
        key = (task, language, choice)
        if key not in self._transcripts:
            audio, speech = self.audio, self.speech  # extract + VAD first (ordered status)
            emit_status("transcribing" if task == "transcribe" else "translating")
            self._transcripts[key] = transcribe(
                audio,
                speech,
                choice=choice,
                task=task,
                language=language,
                batch_size=batch_size,
                use_cache=self.use_cache,
            )
        return self._transcripts[key]


def output_path(media: Path, kind: str, out: Path | None = None) -> Path:
    """``movie.mkv`` → ``movie.<kind>.srt`` next to the video (never the original file)."""
    if out is not None:
        return Path(out)
    return Path(media).with_name(f"{Path(media).stem}.{kind}.srt")


def generate(
    ctx: Context,
    task: str = "transcribe",
    language: str | None = None,
    choice: ModelChoice | None = None,
    out: Path | None = None,
    batch_size: int = 8,
) -> tuple[Path, list[Subtitle], Transcript]:
    """Generate subtitles for ``ctx.media`` and write ``<stem>.generated.srt``."""
    t0 = time.perf_counter()
    tr = ctx.transcript(task, language, choice, batch_size)
    subs = transcript_to_subtitles(tr)
    if not subs:
        raise RuntimeError("No speech was recognised in this file; nothing to write.")
    path = write_srt(subs, output_path(ctx.media, "generated", out))
    log.info(
        "Wrote [bold green]%s[/] (%d cues, language=%s, %.1fs)",
        path,
        len(subs),
        tr.language,
        time.perf_counter() - t0,
    )
    return path, subs, tr


# --------------------------------------------------------------------------- sync

_EN_STOPWORDS = {
    "the",
    "and",
    "you",
    "to",
    "a",
    "of",
    "i",
    "it",
    "is",
    "that",
    "in",
    "what",
    "this",
    "me",
    "we",
    "he",
    "she",
    "for",
    "on",
    "with",
    "my",
    "your",
    "be",
    "have",
    "not",
    "are",
    "was",
    "do",
    "don't",
    "no",
    "so",
    "just",
    "but",
    "all",
    "know",
    "they",
    "there",
    "here",
    "can",
}


def looks_english(subs: list[Subtitle]) -> bool:
    """Crude check: share of very common English words among subtitle tokens."""
    words = " ".join(s.text for s in subs[:400]).lower().split()
    if not words:
        return False
    hits = sum(1 for w in words if w.strip(".,!?\"'-") in _EN_STOPWORDS)
    return hits / len(words) > 0.18


def sync_audio(subs: list[Subtitle], speech: SpeechSignal) -> SyncResult:
    """Audio-only sync: piecewise offsets, optionally on top of a global drift correction.

    Steps in the subtitle timing can fool the drift fit into a small compromise scale, so
    piecewise is run both with and without the drift scale and the better fit wins (ties go to
    the simpler no-drift model).
    """
    from .sync.drift import sync_drift
    from .sync.piecewise import sync_piecewise

    d = sync_drift(subs, speech)
    scale = d.params["scale"]
    best = sync_piecewise(subs, speech)
    best.params["scale"] = 1.0
    if abs(scale - 1.0) > 2e-4:
        scaled = [Subtitle(s.start * scale, s.end * scale, s.text) for s in subs]
        p = sync_piecewise(scaled, speech)
        p.params["scale"] = scale
        if p.score > best.score + 0.01:
            p.notes.append(f"frame-rate drift corrected (scale {scale:.6f})")
            best = p
    if len(best.params["segments"]) == 1 and d.score >= best.score - 0.005:
        return d  # a single straight line: report it as the drift/offset it is
    return best


def run_sync(
    ctx: Context,
    subs: list[Subtitle],
    mode: str = "auto",
    choice: ModelChoice | None = None,
    language: str | None = None,
) -> SyncResult:
    """Run the requested sync ``mode`` (auto|offset|drift|piecewise|smart)."""
    from .sync.drift import sync_drift
    from .sync.offset import sync_offset

    if mode == "offset":
        return sync_offset(subs, ctx.speech)
    if mode == "drift":
        return sync_drift(subs, ctx.speech)
    if mode == "piecewise":
        return sync_audio(subs, ctx.speech)
    if mode == "auto" and not cuda_available():
        log.info("No GPU: using fast audio-only sync (use --mode smart to transcribe on CPU).")
        return sync_audio(subs, ctx.speech)

    # smart (explicit, or auto with a GPU)
    from .sync.smart import sync_smart

    emit_status("smart sync: transcribing")
    tr = ctx.transcript("transcribe", language, choice)
    try:
        return sync_smart(subs, tr, ctx.speech)
    except ValueError as exc:
        log.warning("Smart sync: %s", exc)
    if tr.language != "en" and looks_english(subs):
        log.info(
            "Subtitles look English but audio is '%s': aligning against an English "
            "translation instead.",
            tr.language,
        )
        tr_en = ctx.transcript("translate", tr.language, None)
        try:
            r = sync_smart(subs, tr_en, ctx.speech, min_match_ratio=0.15)
            r.notes.append("aligned against Whisper's English translation")
            return r
        except ValueError as exc:
            log.warning("Smart sync on translation: %s", exc)
    log.info("Falling back to audio-only sync.")
    r = sync_audio(subs, ctx.speech)
    r.notes.append("smart sync not possible; used audio-only sync")
    return r


def sync(
    ctx: Context,
    subs_path: Path,
    mode: str = "auto",
    choice: ModelChoice | None = None,
    language: str | None = None,
    out: Path | None = None,
) -> tuple[Path, SyncResult, SubtitleDoc]:
    """Synchronise ``subs_path`` to ``ctx.media``; writes ``<video>.synced.srt``."""
    t0 = time.perf_counter()
    doc = load_subtitles(subs_path)
    for w in doc.warnings:
        log.warning("%s: %s", Path(subs_path).name, w)
    log.info(
        "Loaded [bold]%s[/] (%d cues, %s, %s)",
        Path(subs_path).name,
        len(doc.subs),
        doc.fmt.upper(),
        doc.encoding,
    )
    ref = reference_signal(ctx.speech)
    before = alignment_score(dialogue(doc.subs), ref)
    result = run_sync(ctx, doc.subs, mode, choice, language)
    after = alignment_score(dialogue(result.subs), ref)
    result.params["score_before"], result.params["score_after"] = before, after
    path = output_path(ctx.media, "synced", out)
    if Path(path).resolve() == Path(subs_path).resolve():
        raise RuntimeError("Refusing to overwrite the original subtitle file.")
    write_srt(result.subs, path)
    console.print(
        f"[bold green]✓[/] {result.method}: {result.describe()}  "
        f"[dim](speech overlap {before:+.2f} → {after:+.2f}, {time.perf_counter() - t0:.1f}s)[/]"
    )
    for note in result.notes:
        log.info(note)
    if after < before - 0.02:
        log.warning(
            "The result overlaps speech *less* than the input — the subtitles may "
            "already have been in sync, or belong to a different cut/version."
        )
    log.info("Wrote [bold green]%s[/]", path)
    return path, result, doc


def auto(
    ctx: Context,
    subs_path: Path | None = None,
    mode: str = "auto",
    choice: ModelChoice | None = None,
    language: str | None = None,
    translate: bool = False,
    out: Path | None = None,
) -> Path:
    """Sync a sidecar subtitle if one exists next to the video, otherwise generate one."""
    subs_path = subs_path or find_sidecar(ctx.media)
    if subs_path is not None:
        log.info("Found subtitles: [bold]%s[/] → syncing", Path(subs_path).name)
        return sync(ctx, subs_path, mode, choice, language, out)[0]
    log.info("No subtitles next to the video → generating")
    task = "translate" if translate else "transcribe"
    return generate(ctx, task, language, choice, out)[0]
