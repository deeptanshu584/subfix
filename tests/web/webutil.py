"""Helpers shared by the web API tests (fake run_job, waiting, app factory)."""

from __future__ import annotations

import logging
import socket
import threading
import time
from pathlib import Path

from subfix import pipeline, progress
from subfix.audio import AudioError
from subfix.srt_io import SubtitleError
from subfix.web import server

TOKEN = "0123456789abcdef0123456789abcdef"
PORT = 8765


class FakeRunner:
    """Stand-in for pipeline.run_job; behaviour is chosen by the media file's stem."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.active = 0
        self.max_active = 0
        self.order: list[str] = []
        self.gate = threading.Event()
        self.started = threading.Event()
        self._lock = threading.Lock()

    def __call__(
        self,
        media,
        mode="auto",
        subs_path=None,
        sync_mode="auto",
        translate=False,
        language=None,
        out=None,
        **kw,
    ):
        media = Path(media)
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.order.append(media.stem)
            self.calls.append(
                dict(
                    media=media,
                    mode=mode,
                    subs_path=subs_path,
                    sync_mode=sync_mode,
                    translate=translate,
                    language=language,
                    out=out,
                )
            )
        self.started.set()
        try:
            return self._run(media, mode, subs_path, out, language)
        finally:
            with self._lock:
                self.active -= 1

    def _run(self, media, mode, subs_path, out, language):
        stem = media.stem
        log = logging.getLogger("subfix")
        log.warning("Processing [bold]%s[/] now", media.name)
        if stem.startswith("fail_audio"):
            raise AudioError("ffmpeg could not decode the file")
        if stem.startswith("fail_subs"):
            raise SubtitleError("empty subtitle file")
        if stem.startswith("fail_model"):
            raise RuntimeError("no model could be loaded")
        if stem.startswith("fail_nosubs"):
            raise pipeline.NoSubtitlesError("No subtitle file to sync was given or found next to x")
        if stem.startswith("fail_internal"):
            raise KeyError("boom")
        if stem.startswith("loop"):
            while True:
                progress.report("transcribe", 0.5, "looping")
                progress.check_cancel()
                time.sleep(0.01)
        if stem.startswith("gate"):
            while not self.gate.wait(0.01):
                progress.check_cancel()
        if stem.startswith("slow"):
            time.sleep(0.15)
        if stem.startswith("many"):
            for i in range(200):
                progress.report("transcribe", i / 200, f"seg {i}")
                time.sleep(0.002)
        for f in (0.0, 0.5, 1.0):
            progress.report("extract", f)
            progress.check_cancel()
        progress.report("vad", 1.0, "cached")
        synced = subs_path is not None or mode == "sync"
        if synced:
            progress.report("sync", None, "sync mode: auto")
        else:
            progress.report("load_model", None, "Loading model")
        progress.report("transcribe", 0.3)
        progress.report("transcribe", 1.0)
        if synced:
            progress.report("sync", 1.0, "done")
        kind = "synced" if synced else "generated"
        path = pipeline.output_path(media, kind, out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello there.\n\n", encoding="utf-8")
        progress.report("write", 1.0, str(path))
        return pipeline.Outcome(
            kind,
            path,
            1,
            language or "en",
            f"{kind} 1 cues (en)",
            "smart" if synced else "generate",
            [{"index": 1, "start": 1.0, "end": 2.0, "text": "Hello there."}],
            ["a note"] if synced else [],
        )


class Recorder:
    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, *args) -> None:
        self.calls.append(args)


def make_app(env, port=PORT, **kw):
    kw.setdefault("idle_release_seconds", 0.3)
    return server.create_app(
        home=env["home"], upload_dir=env["uploads"], port=port, token=TOKEN, **kw
    )


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_media(env, name: str, data: bytes = b"\x00" * 64) -> Path:
    p = env["media"] / name
    p.write_bytes(data)
    return p


def wait_final(client, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] in ("done", "failed", "cancelled"):
            return job
        assert time.monotonic() < deadline, f"job stuck in {job['state']}"
        time.sleep(0.02)


def wait_for(cond, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)
