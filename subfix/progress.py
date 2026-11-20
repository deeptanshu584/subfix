"""Progress reporting and cooperative cancellation for embedding subfix (e.g. the web UI).

The CLI never sets a controller, so every call here is a cheap no-op for it. An embedding
application runs a job inside :func:`use`::

    ctrl = JobControl(on_progress=callback, cancel_event=threading.Event())
    with progress.use(ctrl):
        pipeline.run_job(...)          # raises progress.Cancelled once cancel_event is set

Core code calls :func:`report` at natural checkpoints and :func:`check_cancel` inside its loops
(ffmpeg output lines, VAD chunks, Whisper segments). Cancellation is cooperative: a running
ffmpeg process is killed immediately; Whisper stops after the batch it is decoding.
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

#: Stages in the order a job goes through them (a job may skip some).
STAGES = ("extract", "vad", "load_model", "transcribe", "sync", "write")

#: Human-readable stage labels.
STAGE_LABELS = {
    "extract": "Extracting audio",
    "vad": "Detecting speech",
    "load_model": "Loading Whisper model",
    "transcribe": "Transcribing",
    "sync": "Synchronising",
    "write": "Writing subtitles",
}

#: Callback signature: ``(stage, fraction, message)``. ``fraction`` is 0..1 for the stage, or
#: ``None`` when the stage has no measurable progress (e.g. loading a model).
ProgressFn = Callable[[str, "float | None", str], None]


class Cancelled(Exception):
    """Raised inside a job when its cancel event has been set."""


@dataclass
class JobControl:
    """Callbacks and flags for one job."""

    on_progress: ProgressFn | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)


_current: contextvars.ContextVar[JobControl | None] = contextvars.ContextVar(
    "subfix_job_control", default=None
)


@contextmanager
def use(control: JobControl) -> Iterator[JobControl]:
    """Activate ``control`` for the current thread/context."""
    token = _current.set(control)
    try:
        yield control
    finally:
        _current.reset(token)


def current() -> JobControl | None:
    return _current.get()


def report(stage: str, fraction: float | None = None, message: str = "") -> None:
    """Report progress of ``stage``; never raises (a broken callback must not kill a job)."""
    ctrl = _current.get()
    if ctrl is None or ctrl.on_progress is None:
        return
    if fraction is not None:
        fraction = min(1.0, max(0.0, float(fraction)))
    try:
        ctrl.on_progress(stage, fraction, message)
    except Exception:  # pragma: no cover - defensive
        pass


def cancelled() -> bool:
    ctrl = _current.get()
    return bool(ctrl and ctrl.cancel_event.is_set())


def check_cancel() -> None:
    """Raise :class:`Cancelled` if the current job was cancelled."""
    if cancelled():
        raise Cancelled("cancelled by user")
