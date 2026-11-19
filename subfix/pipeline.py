"""High-level operations used by the CLI: generate, sync, auto."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .audio import AudioFile, load_audio
from .config import ModelChoice
from .cues import transcript_to_subtitles
from .device import default_choice
from .log import emit_status, log
from .srt_io import Subtitle, write_srt
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
            emit_status("transcribing" if task == "transcribe" else "translating")
            self._transcripts[key] = transcribe(
                self.audio,
                self.speech,
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
