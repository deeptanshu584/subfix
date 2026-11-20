"""Job model, FIFO queue with exactly one worker thread, progress/ETA and SSE fan-out."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from .. import device, formats, pipeline, progress
from .. import log as subfix_log
from ..audio import AudioError
from ..srt_io import SubtitleError, find_sidecar
from .common import now_iso
from .history import History
from .uploads import UploadManager

log = logging.getLogger("subfix")

#: Share of the overall progress bar per stage (docs/WEB_API.md §5).
STAGE_WEIGHTS = {
    "extract": 0.05,
    "vad": 0.07,
    "load_model": 0.03,
    "transcribe": 0.80,
    "sync": 0.03,
    "write": 0.02,
}
_CUM_BEFORE: dict[str, float] = {}
_acc = 0.0
for _s in progress.STAGES:
    _CUM_BEFORE[_s] = _acc
    _acc += STAGE_WEIGHTS.get(_s, 0.0)

FINAL_STATES = ("done", "failed", "cancelled")
LOG_TAIL = 200
MAX_FINISHED_IN_MEMORY = 300
THROTTLE_SECONDS = 0.25  # <= 4 job events per second per job


def _strip_markup(text: str) -> str:
    try:
        from rich.text import Text

        return Text.from_markup(text).plain
    except Exception:
        return text


# ---------------------------------------------------------------------------- SSE broker


class EventBroker:
    """Thread-safe fan-out of server-sent events to asyncio subscribers."""

    QUEUE_LIMIT = 2000

    def __init__(self) -> None:
        self._subs: dict[asyncio.Queue, asyncio.AbstractEventLoop] = {}
        self._lock = threading.Lock()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subs[q] = asyncio.get_running_loop()
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs.pop(q, None)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    @staticmethod
    def format(event: str, data: Any) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def _deliver(self, q: asyncio.Queue, item: str | None) -> None:
        if item is not None and q.qsize() >= self.QUEUE_LIMIT:
            # A stalled client: end its stream; it reconnects and re-fetches the state.
            while not q.empty():
                q.get_nowait()
            item = None
        q.put_nowait(item)

    def _send(self, item: str | None) -> None:
        with self._lock:
            subs = list(self._subs.items())
        for q, loop in subs:
            try:
                loop.call_soon_threadsafe(self._deliver, q, item)
            except RuntimeError:  # loop closed
                self.unsubscribe(q)

    def publish(self, event: str, data: Any) -> None:
        self._send(self.format(event, data))

    def close(self) -> None:
        """End every open stream (server shutdown)."""
        self._send(None)


# ---------------------------------------------------------------------------- job model


@dataclass
class FileSpec:
    path: Path
    name: str
    kind: str
    source: str  # "path" | "upload"
    size_bytes: int | None = None
    duration_seconds: float | None = None
    upload_id: str | None = None


@dataclass
class Job:
    id: str
    media: FileSpec
    subs: FileSpec | None
    mode: str
    sync_mode: str
    translate: bool
    language: str | None
    created_at: str = field(default_factory=now_iso)
    state: str = "queued"
    started_at: str | None = None
    finished_at: str | None = None
    stage: str | None = None
    stage_label: str = "Waiting in queue"
    stage_progress: float | None = None
    overall: float = 0.0
    eta_seconds: float | None = None
    message: str = ""
    result: dict[str, Any] | None = None
    error: dict[str, str] | None = None
    log: deque = field(default_factory=lambda: deque(maxlen=LOG_TAIL))
    cancel_event: threading.Event = field(default_factory=threading.Event)
    t_started: float | None = None
    t_finished: float | None = None
    last_emit: float = 0.0
    timer: threading.Timer | None = None

    def to_dict(self, queue_position: int | None = None) -> dict[str, Any]:
        media = {
            "path": str(self.media.path),
            "name": self.media.name,
            "kind": self.media.kind,
            "size_bytes": self.media.size_bytes,
            "duration_seconds": self.media.duration_seconds,
            "source": self.media.source,
        }
        subs = (
            {"path": str(self.subs.path), "name": self.subs.name, "source": self.subs.source}
            if self.subs
            else None
        )
        return {
            "id": self.id,
            "state": self.state,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "queue_position": queue_position if self.state == "queued" else None,
            "media": media,
            "subs": subs,
            "options": {
                "mode": self.mode,
                "sync_mode": self.sync_mode,
                "translate": self.translate,
                "language": self.language,
            },
            "progress": {
                "stage": self.stage,
                "stage_label": self.stage_label,
                "stage_progress": self.stage_progress,
                "overall": round(self.overall, 4),
                "eta_seconds": self.eta_seconds,
                "message": self.message,
            },
            "result": self.result,
            "error": self.error,
        }

    def history_item(self) -> dict[str, Any]:
        r = self.result or {}
        elapsed = None
        if self.t_started is not None and self.t_finished is not None:
            elapsed = round(self.t_finished - self.t_started, 2)
        return {
            "id": self.id,
            "name": self.media.name,
            "media_path": str(self.media.path),
            "mode": self.mode,
            "sync_mode": self.sync_mode,
            "translate": self.translate,
            "language": r.get("language") or self.language,
            "state": self.state,
            "media_duration_seconds": self.media.duration_seconds,
            "elapsed_seconds": elapsed,
            "output_path": r.get("output_path"),
            "summary": r.get("summary"),
            "finished_at": self.finished_at,
            "error": self.error,
        }


def map_error(exc: BaseException) -> tuple[str, str]:
    """Exception from run_job → (job error code, message) per docs/WEB_API.md §8."""
    msg = str(exc) or type(exc).__name__
    if isinstance(exc, formats.UnsupportedFormat):
        return "unsupported_format", msg
    if isinstance(exc, SubtitleError):
        return "subtitle_error", msg
    if isinstance(exc, AudioError):
        return "audio_error", msg
    if isinstance(exc, pipeline.NoSubtitlesError):
        return "no_subtitles", msg
    if isinstance(exc, ValueError):
        return "internal", msg
    if isinstance(exc, RuntimeError):
        return "model_error", msg
    return "internal", msg


def _folder_writable(folder: Path) -> bool:
    try:
        with tempfile.TemporaryFile(dir=folder, prefix=".subfix-write-test-"):
            pass
        return True
    except OSError:
        return False


class _JobLogHandler(logging.Handler):
    """Forwards ``subfix`` log records emitted by the worker thread to the running job."""

    def __init__(self, manager: JobManager, job: Job, thread_id: int) -> None:
        super().__init__(logging.NOTSET)
        self.manager = manager
        self.job = job
        self.thread_id = thread_id

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self.thread_id:
            return
        try:
            message = _strip_markup(record.getMessage())
        except Exception:
            return
        self.manager._add_log(self.job, record.levelname, message)


# ---------------------------------------------------------------------------- manager


class JobManager:
    def __init__(
        self,
        outputs_dir: Path,
        history: History,
        uploads: UploadManager,
        broker: EventBroker,
        idle_release_seconds: float = 120.0,
        throttle_seconds: float = THROTTLE_SECONDS,
    ) -> None:
        self.outputs_dir = Path(outputs_dir)
        self.history = history
        self.uploads = uploads
        self.broker = broker
        self.idle_release_seconds = idle_release_seconds
        self.throttle_seconds = throttle_seconds
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._jobs: dict[str, Job] = {}
        self._queue: deque[Job] = deque()
        self._current: Job | None = None
        self._finished: deque[str] = deque()
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._models_dirty = False
        self._idle_since = time.monotonic()
        self._saved_flags: tuple[bool, bool] | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._saved_flags = (device.keep_models_loaded, not subfix_log.progress_bars_enabled())
        device.keep_models_loaded = True
        subfix_log.set_progress_bars(False)
        self._stopping = False
        self._thread = threading.Thread(target=self._worker, name="subfix-worker", daemon=True)
        self._thread.start()

    def shutdown(self, timeout: float = 10.0) -> None:
        with self._cond:
            self._stopping = True
            dropped = list(self._queue)
            self._queue.clear()
            if self._current is not None:
                self._current.cancel_event.set()
            self._cond.notify_all()
        for job in dropped:
            self._finalize(job, "cancelled")
        if self._thread is not None:
            self._thread.join(timeout)
        if self._models_dirty:
            with contextlib.suppress(Exception):
                device.release_models()
            self._models_dirty = False
        if self._saved_flags is not None:
            device.keep_models_loaded = self._saved_flags[0]
            subfix_log.set_progress_bars(not self._saved_flags[1])
            self._saved_flags = None

    # ------------------------------------------------------------------ queries
    def _position(self, job: Job) -> int | None:
        if job.state != "queued":
            return None
        for i, j in enumerate(self._queue):
            if j is job:
                return i + 1
        return None

    def snapshot(self, job: Job) -> dict[str, Any]:
        with self._lock:
            return job.to_dict(self._position(job))

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            out = []
            if self._current is not None and self._current.state == "running":
                out.append(self._current.to_dict())
            out += [j.to_dict(i + 1) for i, j in enumerate(self._queue)]
            finished = [self._jobs[i] for i in reversed(self._finished) if i in self._jobs]
            out += [j.to_dict() for j in finished]
            return out

    def detail(self, job: Job) -> dict[str, Any]:
        with self._lock:
            d = job.to_dict(self._position(job))
            d["log_tail"] = list(job.log)
            return d

    # ------------------------------------------------------------------ events
    def _emit(self, job: Job, force: bool = False) -> None:
        """Publish a ``job`` event; progress updates are throttled, state changes are not."""
        with self._lock:
            now = time.monotonic()
            if not force and now - job.last_emit < self.throttle_seconds:
                if job.timer is None:
                    delay = self.throttle_seconds - (now - job.last_emit)
                    job.timer = threading.Timer(delay, self._flush, args=(job,))
                    job.timer.daemon = True
                    job.timer.start()
                return
            if job.timer is not None:
                job.timer.cancel()
                job.timer = None
            job.last_emit = now
            # Published under the lock so events of one job can never be reordered.
            self.broker.publish("job", job.to_dict(self._position(job)))

    def _flush(self, job: Job) -> None:
        with self._lock:
            job.timer = None
            self._emit(job, force=True)

    def _emit_queue_positions(self) -> None:
        with self._lock:
            for j in self._queue:
                self._emit(j, force=True)

    def _add_log(self, job: Job, level: str, message: str) -> None:
        line = {"job_id": job.id, "time": now_iso(), "level": level, "message": message}
        with self._lock:
            job.log.append(line)
            self.broker.publish("log", line)

    # ------------------------------------------------------------------ submit / cancel
    def submit(
        self,
        media: FileSpec,
        subs: FileSpec | None,
        mode: str,
        sync_mode: str,
        translate: bool,
        language: str | None,
    ) -> dict[str, Any]:
        with self._cond:
            job_id = f"j-{secrets.token_hex(4)}"
            while job_id in self._jobs:
                job_id = f"j-{secrets.token_hex(4)}"
            job = Job(job_id, media, subs, mode, sync_mode, translate, language)
            for spec in (media, subs):
                if spec is not None and spec.upload_id:
                    self.uploads.attach(spec.upload_id, job_id)
            self._jobs[job_id] = job
            self._queue.append(job)
            self._emit(job, force=True)
            self._cond.notify_all()
            return job.to_dict(self._position(job))

    def cancel(self, job: Job) -> dict[str, Any] | None:
        """Cancel ``job``; returns None if it is already in a final state."""
        with self._cond:
            if job.state in FINAL_STATES:
                return None
            if job.state == "queued":
                self._queue.remove(job)
                queued = True
            else:
                queued = False
                job.cancel_event.set()
                job.message = "Cancelling…"
                self._emit(job, force=True)
        if queued:
            self._finalize(job, "cancelled")
            self._emit_queue_positions()
        return self.snapshot(job)

    # ------------------------------------------------------------------ worker
    def _worker(self) -> None:
        tick = max(0.05, min(5.0, self.idle_release_seconds / 4))
        while True:
            job = None
            with self._cond:
                if self._stopping:
                    return
                if self._queue:
                    job = self._queue.popleft()
                    job.state = "running"
                    job.started_at = now_iso()
                    job.t_started = time.monotonic()
                    job.stage_label = "Starting"
                    self._current = job
                    self._emit(job, force=True)
                    self._emit_queue_positions()
                else:
                    self._cond.wait(timeout=tick)
            if job is None:
                self._maintenance()
                continue
            try:
                self._run(job)
            finally:
                with self._lock:
                    self._current = None
                    self._idle_since = time.monotonic()

    def _maintenance(self) -> None:
        with self._lock:
            release = (
                self._models_dirty
                and not self._queue
                and time.monotonic() - self._idle_since >= self.idle_release_seconds
            )
            if release:
                self._models_dirty = False
        if release:
            log.debug("Queue idle: releasing Whisper models")
            with contextlib.suppress(Exception):
                device.release_models()
        with contextlib.suppress(Exception):
            self.uploads.cleanup_stale()

    def _plan_output(self, job: Job) -> tuple[Path | None, list[str]]:
        """``out`` for run_job and notes (docs/WEB_API.md §6 "Output location")."""
        media = job.media
        if job.mode == "generate":
            kind = "generated"
        elif job.mode == "sync" or job.subs is not None:
            kind = "synced"
        else:
            try:
                kind = "synced" if find_sidecar(media.path) else "generated"
            except OSError:
                kind = "generated"
        stem = Path(media.name).stem
        fallback = self.outputs_dir / job.id / f"{stem}.{kind}.srt"
        if media.source == "upload":
            fallback.parent.mkdir(parents=True, exist_ok=True)
            return fallback, []
        if _folder_writable(media.path.parent):
            return None, []
        fallback.parent.mkdir(parents=True, exist_ok=True)
        return fallback, [
            f"{media.path.parent} is not writable; the subtitles were saved to {fallback.parent}"
        ]

    def _on_progress(self, job: Job, stage: str, fraction: float | None, message: str) -> None:
        with self._lock:
            job.stage = stage
            job.stage_label = progress.STAGE_LABELS.get(stage, stage)
            job.stage_progress = fraction
            if not job.cancel_event.is_set():
                job.message = message or ""
            if fraction is not None and stage in STAGE_WEIGHTS:
                value = _CUM_BEFORE[stage] + STAGE_WEIGHTS[stage] * fraction
                job.overall = max(job.overall, min(value, 0.999))
            if job.t_started is not None and job.overall > 0:
                elapsed = time.monotonic() - job.t_started
                if elapsed >= 1.0:
                    job.eta_seconds = round(elapsed * (1 - job.overall) / job.overall, 1)
        self._emit(job)

    def _run(self, job: Job) -> None:
        handler = _JobLogHandler(self, job, threading.get_ident())
        log.addHandler(handler)
        try:
            out, notes = self._plan_output(job)
            if job.cancel_event.is_set():
                raise progress.Cancelled("cancelled by user")
            ctrl = progress.JobControl(
                on_progress=partial(self._on_progress, job), cancel_event=job.cancel_event
            )
            with progress.use(ctrl):
                outcome = pipeline.run_job(
                    media=job.media.path,
                    mode=job.mode,
                    subs_path=job.subs.path if job.subs else None,
                    sync_mode=job.sync_mode,
                    translate=job.translate,
                    language=job.language,
                    out=out,
                )
        except progress.Cancelled:
            self._finalize(job, "cancelled")
        except Exception as exc:
            code, message = map_error(exc)
            if code == "internal":
                log.exception("Job failed: %s", message)
            else:
                log.error("Job failed: %s", message)
            self._finalize(job, "failed", error={"code": code, "message": message})
        else:
            elapsed = round(time.monotonic() - (job.t_started or time.monotonic()), 2)
            path = Path(outcome.output_path)
            result = {
                "kind": outcome.kind,
                "output_path": str(path),
                "output_name": path.name,
                "cues": outcome.cues,
                "language": outcome.language,
                "summary": outcome.summary,
                "method": outcome.method,
                "notes": list(outcome.notes) + notes,
                "elapsed_seconds": elapsed,
                "preview": list(outcome.preview),
            }
            self._finalize(job, "done", result=result)
        finally:
            log.removeHandler(handler)
            with self._lock:
                self._models_dirty = True

    def _finalize(
        self,
        job: Job,
        state: str,
        result: dict[str, Any] | None = None,
        error: dict[str, str] | None = None,
    ) -> None:
        with self._lock:
            job.state = state
            job.finished_at = now_iso()
            job.t_finished = time.monotonic()
            job.result = result
            job.error = error
            job.eta_seconds = None
            job.stage_progress = None if state != "done" else 1.0
            if state == "done":
                job.overall = 1.0
                job.stage_label = "Done"
                job.message = (result or {}).get("summary") or ""
            elif state == "failed":
                job.stage_label = "Failed"
                job.message = (error or {}).get("message", "")
            else:
                job.stage_label = "Cancelled"
                job.message = ""
            self._finished.append(job.id)
            while len(self._finished) > MAX_FINISHED_IN_MEMORY:
                self._jobs.pop(self._finished.popleft(), None)
            self._emit(job, force=True)
            item = job.history_item()
        try:
            self.history.append(item)
        except OSError as exc:  # history is a convenience; never fail the job over it
            log.warning("Could not write job history: %s", exc)
        self.broker.publish("history", item)
        for spec in (job.media, job.subs):
            if spec is not None and spec.upload_id:
                self.uploads.release(spec.upload_id, job.id)
