"""Mock subfix web server implementing docs/WEB_API.md with fake data (no GPU, no subfix.web).

Serves the real frontend from ``subfix/web/static`` (with the token injected) and simulates jobs
that move through the pipeline stages in a few seconds, emitting progress/log/history events.

Run it by hand to look at the UI::

    .venv/Scripts/python.exe tests/web_frontend/mock_server.py --port 8799 --job-seconds 8

Test-only control routes live under ``/__mock/`` (no token needed): reset, queued picker
results, one-shot API failures, dropping SSE streams, injecting jobs silently.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from subfix import formats

STATIC_DIR = Path(__file__).resolve().parents[2] / "subfix" / "web" / "static"
VERSION = "0.1.0"
STAGE_LABELS = {
    "extract": "Extracting audio",
    "vad": "Detecting speech",
    "load_model": "Loading Whisper model",
    "transcribe": "Transcribing",
    "sync": "Synchronising",
    "write": "Writing subtitles",
}
WEIGHTS = {"extract": 5, "vad": 7, "load_model": 3, "transcribe": 80, "sync": 3, "write": 2}
UPLOAD_DIR = r"D:\subfix-data\uploads"
OUTPUTS_DIR = r"D:\subfix-data\web\outputs"
FINAL = {"done", "failed", "cancelled"}
LINES = [
    "You're a jerk, Thom.",
    "Celia, wait!",
    "I didn't mean it like that.",
    '<b>Bold</b> & "quoted" <script>alert(1)</script>',
    "We have to go back to the tower.",
    "Where are the others?",
    "Hold on — something's coming.",
    "It's been forty years.",
    "Get the robot, now!",
    "I'm sorry.",
]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def api_error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


class MockError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def clean(job: dict[str, Any]) -> dict[str, Any]:
    """Drop the mock's private ``_fields`` from a job before sending it."""
    out = {k: v for k, v in job.items() if not k.startswith("_")}
    out["media"] = {k: v for k, v in job["media"].items() if not k.startswith("_")}
    if job["subs"]:
        out["subs"] = {k: v for k, v in job["subs"].items() if not k.startswith("_")}
    return out


def fake_size(path: str) -> int:
    digest = hashlib.sha1(path.encode()).digest()
    return 300_000_000 + int.from_bytes(digest[:4], "big") % 2_000_000_000


class Mock:
    def __init__(self, job_seconds: float, token: str) -> None:
        self.token = token
        self.job_seconds = job_seconds
        self.reset(job_seconds)
        self.subscribers: set[asyncio.Queue] = set()
        self.wake = asyncio.Event()
        self.refuse_events = 0  # number of upcoming /api/events requests to answer with 503

    def reset(self, job_seconds: float | None = None) -> None:
        # Jobs of the previous test keep running briefly; their events are dropped (stale gen).
        self.gen = getattr(self, "gen", 0) + 1
        for rec in getattr(self, "jobs", {}).values():
            rec["cancel"] = True
        if job_seconds is not None:
            self.job_seconds = job_seconds
        self.jobs: dict[str, dict[str, Any]] = {}
        self.queue: list[str] = []
        self.history: list[dict[str, Any]] = []
        self.uploads: dict[str, dict[str, Any]] = {}
        self.pick_results: list[Any] = []
        self.failures: dict[str, dict[str, Any]] = {}
        self.uploaded_bytes = 0
        self.counter = 0
        self.picks: list[str] = []

    # -- events -----------------------------------------------------------------------------

    def broadcast(self, event: str, data: dict[str, Any]) -> None:
        payload = json.dumps(data)
        for q in list(self.subscribers):
            q.put_nowait((event, payload))

    def public(self, rec: dict[str, Any]) -> dict[str, Any]:
        job = dict(rec["job"])
        job["queue_position"] = (
            self.queue.index(job["id"]) + 1 if job["state"] == "queued" else None
        )
        return job

    def emit_job(self, rec: dict[str, Any]) -> None:
        if rec["gen"] != self.gen:
            return
        self.broadcast("job", clean(self.public(rec)))

    def log(self, rec: dict[str, Any], message: str, level: str = "INFO") -> None:
        if rec["gen"] != self.gen:
            return
        line = {"job_id": rec["job"]["id"], "time": now_iso(), "level": level, "message": message}
        rec["log"].append(line)
        del rec["log"][:-200]
        self.broadcast("log", line)

    # -- jobs -------------------------------------------------------------------------------

    def new_job(self, media: dict[str, Any], subs: dict[str, Any] | None, opts: dict[str, Any]):
        self.counter += 1
        job_id = f"j-{secrets.token_hex(4)}"
        job = {
            "id": job_id,
            "state": "queued",
            "created_at": now_iso(),
            "started_at": None,
            "finished_at": None,
            "queue_position": None,
            "media": media,
            "subs": subs,
            "options": opts,
            "progress": {
                "stage": None,
                "stage_label": "Waiting in queue",
                "stage_progress": None,
                "overall": 0.0,
                "eta_seconds": None,
                "message": "",
            },
            "result": None,
            "error": None,
            "_seq": self.counter,
        }
        rec = {"job": job, "log": [], "cancel": False, "gen": self.gen}
        self.jobs[job_id] = rec
        return rec

    def ordered(self) -> list[dict[str, Any]]:
        running = [r for r in self.jobs.values() if r["job"]["state"] == "running"]
        queued = [self.jobs[i] for i in self.queue]
        done = [r for r in self.jobs.values() if r["job"]["state"] in FINAL]
        done.sort(key=lambda r: (r["job"]["finished_at"] or "", r["job"]["_seq"]), reverse=True)
        return running + queued + done

    def finish(self, rec: dict[str, Any], state: str, error: dict[str, str] | None = None) -> None:
        job = rec["job"]
        job["state"] = state
        job["finished_at"] = now_iso()
        if rec["gen"] != self.gen:
            return
        job["error"] = error
        if job["id"] in self.queue:
            self.queue.remove(job["id"])
        for part in (job["media"], job["subs"]):
            if part and part.get("source") == "upload":
                self.uploads.pop(part.get("_upload_id", ""), None)
        self.emit_job(rec)
        opts = job["options"]
        result = job["result"] or {}
        item = {
            "id": job["id"],
            "name": job["media"]["name"],
            "media_path": job["media"]["path"],
            "mode": opts["mode"],
            "sync_mode": opts["sync_mode"],
            "translate": opts["translate"],
            "language": result.get("language") or opts["language"],
            "state": state,
            "media_duration_seconds": job["media"]["duration_seconds"],
            "elapsed_seconds": result.get("elapsed_seconds", rec.get("elapsed", 0.0)),
            "output_path": result.get("output_path"),
            "summary": result.get("summary"),
            "finished_at": job["finished_at"],
            "error": error,
        }
        self.history.insert(0, item)
        del self.history[500:]
        self.broadcast("history", item)

    def will_sync(self, job: dict[str, Any]) -> bool:
        mode = job["options"]["mode"]
        return mode == "sync" or (mode == "auto" and (job["subs"] or job["media"].get("_sidecar")))

    async def run(self, rec: dict[str, Any]) -> None:
        job = rec["job"]
        loop = asyncio.get_running_loop()
        start = loop.time()
        job["state"] = "running"
        job["started_at"] = now_iso()
        syncing = self.will_sync(job)
        plan = (
            ["extract", "vad", "sync", "write"]
            if syncing
            else ["extract", "vad", "load_model", "transcribe", "write"]
        )
        total_w = sum(WEIGHTS[s] for s in plan)
        fail = "fail" in job["media"]["name"].lower()
        done_w = 0.0
        last_emit = 0.0
        self.log(rec, f"Extracting audio from {job['media']['name']} ...")
        for stage in plan:
            stage_time = self.job_seconds * WEIGHTS[stage] / total_w
            stage_time = max(stage_time, 0.15)
            steps = max(1, int(stage_time / 0.05))
            if stage == "transcribe":
                self.log(rec, "Transcribing with large-v3-turbo on cuda (int8_float16)")
            elif stage == "sync":
                self.log(rec, f"Syncing with method {job['options']['sync_mode']}")
            elif stage == "vad":
                self.log(rec, "Detecting speech (Silero VAD)")
            elif stage == "load_model":
                self.log(rec, "Loading Whisper model large-v3-turbo")
            for i in range(steps + 1):
                if rec["cancel"]:
                    self.log(rec, "Cancelled by user", "WARNING")
                    rec["elapsed"] = loop.time() - start
                    self.finish(rec, "cancelled")
                    return
                frac = i / steps
                overall = (done_w + WEIGHTS[stage] * frac) / total_w
                elapsed = loop.time() - start
                eta = elapsed / overall * (1 - overall) if overall > 0.04 else None
                job["progress"] = {
                    "stage": stage,
                    "stage_label": STAGE_LABELS[stage],
                    "stage_progress": None if stage == "load_model" else round(frac, 3),
                    "overall": round(overall, 4),
                    "eta_seconds": round(eta, 1) if eta is not None else None,
                    "message": f"{int(frac * 100)}% of {STAGE_LABELS[stage].lower()}",
                }
                if stage == "transcribe" and i and i % max(1, steps // 4) == 0:
                    self.log(rec, f"[{int(frac * 100)}%] {LINES[i % len(LINES)]}")
                t = loop.time()
                if t - last_emit >= 0.25 or i == steps:
                    last_emit = t
                    self.emit_job(rec)
                if fail and stage == "vad" and frac >= 0.5:
                    self.log(rec, "ffmpeg: Invalid data found when processing input", "ERROR")
                    rec["elapsed"] = loop.time() - start
                    self.finish(
                        rec,
                        "failed",
                        {
                            "code": "audio_error",
                            "message": f"Could not read the audio of {job['media']['name']}: "
                            "ffmpeg reported invalid data.",
                        },
                    )
                    return
                await asyncio.sleep(stage_time / steps)
            done_w += WEIGHTS[stage]
        elapsed = round(loop.time() - start, 1)
        kind = "synced" if syncing else "generated"
        media = job["media"]
        stem = PureWindowsPath(media["name"]).stem
        out_name = f"{stem}.{kind}.srt"
        if media["source"] == "path":
            out_path = str(PureWindowsPath(media["path"]).with_name(out_name))
        else:
            out_path = str(PureWindowsPath(OUTPUTS_DIR) / job["id"] / out_name)
        preview = [
            {
                "index": i + 1,
                "start": 23.0 + i * 3.25,
                "end": 25.5 + i * 3.25,
                "text": LINES[i % len(LINES)] + ("\nSecond line." if i % 7 == 6 else ""),
            }
            for i in range(20)
        ]
        lang = job["options"]["language"] or "en"
        method = job["options"]["sync_mode"] if syncing else "generate"
        job["result"] = {
            "kind": kind,
            "output_path": out_path,
            "output_name": out_name,
            "cues": 76,
            "language": lang,
            "summary": f"{method}: offset +1.250 s" if syncing else f"generated 76 cues ({lang})",
            "method": "smart" if syncing and method == "auto" else method,
            "notes": []
            if "readonly" not in media["name"].lower()
            else ["The movie's folder is not writable; saved to the outputs folder instead."],
            "elapsed_seconds": elapsed,
            "preview": preview,
        }
        job["progress"] = {
            "stage": "write",
            "stage_label": "Done",
            "stage_progress": 1.0,
            "overall": 1.0,
            "eta_seconds": 0,
            "message": "",
        }
        self.log(rec, f"Wrote {out_path}")
        self.finish(rec, "done")

    async def worker(self) -> None:
        while True:
            if not self.queue:
                self.wake.clear()
                await self.wake.wait()
                continue
            job_id = self.queue.pop(0)
            rec = self.jobs.get(job_id)
            if rec is None or rec["job"]["state"] != "queued":
                continue
            try:
                await self.run(rec)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - mock safety net
                self.finish(rec, "failed", {"code": "internal", "message": str(exc)})

    # -- request helpers --------------------------------------------------------------------

    def describe_path(self, path: str) -> dict[str, Any]:
        if not path:
            raise MockError(400, "invalid_request", "path is required")
        try:
            kind = formats.classify(path)
        except formats.UnsupportedFormat as exc:
            raise MockError(400, "unsupported_format", str(exc)) from None
        win = PureWindowsPath(path)
        upload = next((u for u in self.uploads.values() if u["path"] == path), None)
        if "missing" in path.lower() and upload is None:
            raise MockError(404, "file_not_found", f"File not found: {path}")
        sidecar = None
        if kind != "subtitle" and "withsubs" in win.name.lower():
            side = win.with_suffix(".srt")
            sidecar = {"path": str(side), "name": side.name}
        return {
            "path": str(win),
            "name": win.name,
            "kind": kind,
            "size_bytes": upload["size_bytes"] if upload else fake_size(path),
            "duration_seconds": None if kind == "subtitle" else 734.1,
            "sidecar": sidecar,
        }


def create_app(job_seconds: float = 4.0, token: str | None = None) -> FastAPI:
    mock_holder: dict[str, Mock] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        mock = Mock(job_seconds, token or secrets.token_hex(16))
        mock_holder["m"] = mock
        app.state.mock = mock
        task = asyncio.create_task(mock.worker())
        yield
        for q in list(mock.subscribers):
            q.put_nowait(None)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    app = FastAPI(lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def m() -> Mock:
        return mock_holder["m"]

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return api_error(403, "forbidden", "Bad Host header.")
        if path.startswith("/api/") and path != "/api/health":
            supplied = request.headers.get("x-subfix-token") or request.query_params.get("token")
            if supplied != m().token:
                return api_error(403, "forbidden", "Missing or wrong token.")
            fail = m().failures.get(f"{request.method} {path}")
            if fail and fail["times"] > 0:
                fail["times"] -= 1
                return api_error(fail["status"], fail["code"], fail["message"])
        return await call_next(request)

    @app.exception_handler(MockError)
    async def mock_error_handler(_request: Request, exc: MockError) -> JSONResponse:
        return api_error(exc.status, exc.code, exc.message)

    async def body_json(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except Exception:
            raise MockError(400, "invalid_request", "Body must be JSON.") from None
        if not isinstance(data, dict):
            raise MockError(400, "invalid_request", "Body must be a JSON object.")
        return data

    # -- page -------------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(
            html.replace("__SUBFIX_TOKEN__", m().token), headers={"Cache-Control": "no-store"}
        )

    # -- API --------------------------------------------------------------------------------

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "app": "subfix", "version": VERSION}

    def formats_dict() -> dict[str, list[str]]:
        return {
            "video": list(formats.VIDEO_EXTENSIONS),
            "audio": list(formats.AUDIO_EXTENSIONS),
            "subtitle": list(formats.SUBTITLE_EXTENSIONS),
        }

    @app.get("/api/config")
    async def config():
        return {
            "version": VERSION,
            "device": {"cuda": True, "gpu": "NVIDIA GeForce RTX 4050 Laptop GPU", "vram_mb": 6141},
            "defaults": {
                "transcribe": "large-v3-turbo on cuda (int8_float16)",
                "translate": "large-v3 on cuda (int8_float16)",
            },
            "upload_dir": UPLOAD_DIR,
            "outputs_dir": OUTPUTS_DIR,
            "formats": formats_dict(),
            "languages": [{"code": c, "name": n} for c, n in formats.LANGUAGES.items()],
            "modes": ["auto", "generate", "sync"],
            "sync_modes": ["auto", "offset", "drift", "piecewise", "smart"],
        }

    @app.get("/api/formats")
    async def get_formats():
        return formats_dict()

    @app.post("/api/pick")
    async def pick(request: Request):
        data = await body_json(request)
        kind = data.get("kind")
        if kind not in ("media", "subtitle"):
            raise MockError(400, "invalid_request", "kind must be 'media' or 'subtitle'")
        m().picks.append(kind)
        await asyncio.sleep(0.05)
        if m().pick_results:
            res = m().pick_results.pop(0)
            if isinstance(res, dict) and "error" in res:
                e = res["error"]
                raise MockError(e.get("status", 500), e["code"], e["message"])
            return {"path": res}
        default = (
            r"D:\Movies\Tears of Steel.mkv"
            if kind == "media"
            else r"D:\Movies\Tears of Steel.en.srt"
        )
        return {"path": default}

    @app.post("/api/inspect")
    async def inspect(request: Request):
        data = await body_json(request)
        return m().describe_path(str(data.get("path") or ""))

    @app.post("/api/uploads", status_code=201)
    async def upload(request: Request, filename: str = "", size: int = 0):
        if not filename:
            raise MockError(400, "invalid_request", "filename is required")
        try:
            kind = formats.classify(filename)
        except formats.UnsupportedFormat as exc:
            raise MockError(400, "unsupported_format", str(exc)) from None
        if size > 900 * 1024**3:
            raise MockError(413, "upload_too_large", "Not enough free space for this upload.")
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
        m().uploaded_bytes += received
        upload_id = f"u-{secrets.token_hex(4)}"
        name = PureWindowsPath(filename).name
        path = str(PureWindowsPath(UPLOAD_DIR) / upload_id / name)
        rec = {
            "upload_id": upload_id,
            "name": name,
            "kind": kind,
            "size_bytes": received,
            "path": path,
        }
        m().uploads[upload_id] = rec
        return JSONResponse(rec, status_code=201)

    @app.delete("/api/uploads/{upload_id}")
    async def delete_upload(upload_id: str):
        if m().uploads.pop(upload_id, None) is None:
            raise MockError(404, "upload_not_found", f"Unknown upload {upload_id}")
        return Response(status_code=204)

    def resolve_part(part: Any, want: set[str], label: str) -> dict[str, Any]:
        if not isinstance(part, dict) or (("path" in part) == ("upload_id" in part)):
            raise MockError(400, "invalid_request", f"{label}: give exactly one of path/upload_id")
        if "upload_id" in part:
            up = m().uploads.get(part["upload_id"])
            if up is None:
                raise MockError(404, "upload_not_found", f"Unknown upload {part['upload_id']}")
            info = {
                "path": up["path"],
                "name": up["name"],
                "kind": up["kind"],
                "size_bytes": up["size_bytes"],
                "duration_seconds": 734.1,
                "source": "upload",
                "_upload_id": up["upload_id"],
                "_sidecar": None,
            }
        else:
            d = m().describe_path(str(part["path"]))
            info = {
                "path": d["path"],
                "name": d["name"],
                "kind": d["kind"],
                "size_bytes": d["size_bytes"],
                "duration_seconds": d["duration_seconds"],
                "source": "path",
                "_sidecar": d["sidecar"],
            }
        if info["kind"] not in want:
            raise MockError(
                400,
                "unsupported_format",
                f"{info['name']} is not a {' or '.join(sorted(want))} file",
            )
        return info

    @app.post("/api/jobs", status_code=201)
    async def create_job(request: Request):
        data = await body_json(request)
        media = resolve_part(data.get("media"), {"video", "audio"}, "media")
        subs = None
        if data.get("subs") is not None:
            s = resolve_part(data["subs"], {"subtitle"}, "subs")
            subs = {"path": s["path"], "name": s["name"], "source": s["source"]}
            if "_upload_id" in s:
                subs["_upload_id"] = s["_upload_id"]
        mode = data.get("mode", "auto")
        sync_mode = data.get("sync_mode", "auto")
        language = data.get("language")
        if mode not in ("auto", "generate", "sync"):
            raise MockError(400, "invalid_request", f"unknown mode {mode!r}")
        if sync_mode not in ("auto", "offset", "drift", "piecewise", "smart"):
            raise MockError(400, "invalid_request", f"unknown sync_mode {sync_mode!r}")
        if language is not None and language not in formats.LANGUAGES:
            raise MockError(400, "invalid_request", f"unknown language {language!r}")
        if mode == "sync" and subs is None and not media["_sidecar"]:
            raise MockError(
                400,
                "no_subtitles",
                f"No subtitle file to sync was given or found next to {media['name']}",
            )
        opts = {
            "mode": mode,
            "sync_mode": sync_mode,
            "translate": bool(data.get("translate")),
            "language": language,
        }
        rec = m().new_job(media, subs, opts)
        m().queue.append(rec["job"]["id"])
        m().emit_job(rec)
        m().wake.set()
        return JSONResponse(clean(m().public(rec)), status_code=201)

    def get_rec(job_id: str) -> dict[str, Any]:
        rec = m().jobs.get(job_id)
        if rec is None:
            raise MockError(404, "job_not_found", f"Unknown job {job_id}")
        return rec

    @app.get("/api/jobs")
    async def list_jobs():
        return {"jobs": [clean(m().public(r)) for r in m().ordered()]}

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str):
        rec = get_rec(job_id)
        return {**clean(m().public(rec)), "log_tail": rec["log"][-200:]}

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str):
        rec = get_rec(job_id)
        st = rec["job"]["state"]
        if st in FINAL:
            raise MockError(409, "job_finished", "This job has already finished.")
        if st == "queued":
            m().finish(rec, "cancelled")
        else:
            rec["cancel"] = True
        return clean(m().public(rec))

    @app.get("/api/jobs/{job_id}/srt")
    async def get_srt(job_id: str):
        rec = get_rec(job_id)
        result = rec["job"]["result"]
        if rec["job"]["state"] != "done" or not result:
            raise MockError(404, "output_not_found", "No subtitles for this job yet.")
        blocks = [
            f"{c['index']}\n{srt_time(c['start'])} --> {srt_time(c['end'])}\n{c['text']}\n"
            for c in result["preview"]
        ]
        return Response(
            "\n".join(blocks),
            media_type="application/x-subrip",
            headers={"Content-Disposition": f'attachment; filename="{result["output_name"]}"'},
        )

    @app.post("/api/jobs/{job_id}/open-folder")
    async def open_folder(job_id: str):
        rec = get_rec(job_id)
        if rec["job"]["state"] != "done":
            raise MockError(404, "output_not_found", "No subtitles for this job yet.")
        rec.setdefault("opened", 0)
        rec["opened"] += 1
        return {"ok": True}

    @app.get("/api/history")
    async def history(limit: int = 100):
        return {"items": m().history[: max(0, limit)]}

    @app.delete("/api/history")
    async def clear_history():
        m().history.clear()
        return {"ok": True}

    @app.get("/api/events")
    async def events(request: Request):
        mock = m()
        if mock.refuse_events > 0:
            mock.refuse_events -= 1
            return api_error(503, "internal", "events temporarily unavailable")
        q: asyncio.Queue = asyncio.Queue()
        mock.subscribers.add(q)

        async def stream() -> AsyncIterator[str]:
            try:
                yield f"event: hello\ndata: {json.dumps({'version': VERSION, 'time': now_iso()})}\n\n"
                while True:
                    try:
                        item = await asyncio.wait_for(q.get(), timeout=15)
                    except TimeoutError:
                        yield ": ping\n\n"
                        continue
                    if item is None:
                        return
                    event, payload = item
                    yield f"event: {event}\ndata: {payload}\n\n"
            finally:
                mock.subscribers.discard(q)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
        )

    # -- test control -----------------------------------------------------------------------

    @app.post("/__mock/reset")
    async def mock_reset(request: Request):
        data = await body_json(request)
        m().reset(data.get("job_seconds"))
        m().refuse_events = 0
        return {"ok": True}

    @app.post("/__mock/pick")
    async def mock_pick(request: Request):
        data = await body_json(request)
        m().pick_results.extend(data.get("results", []))
        return {"ok": True}

    @app.post("/__mock/fail")
    async def mock_fail(request: Request):
        data = await body_json(request)
        m().failures[f"{data.get('method', 'GET')} {data['path']}"] = {
            "status": data.get("status", 500),
            "code": data.get("code", "internal"),
            "message": data.get("message", "Simulated failure"),
            "times": data.get("times", 1),
        }
        return {"ok": True}

    @app.post("/__mock/drop-events")
    async def mock_drop(request: Request):
        data = await body_json(request)
        m().refuse_events = int(data.get("refuse", 0))
        n = len(m().subscribers)
        for q in list(m().subscribers):
            q.put_nowait(None)
        return {"dropped": n}

    @app.post("/__mock/inject-job")
    async def mock_inject(request: Request):
        """Create a finished job and history item without sending any events."""
        data = await body_json(request)
        name = data.get("name", "Injected.mkv")
        media = {
            "path": rf"D:\Movies\{name}",
            "name": name,
            "kind": "video",
            "size_bytes": 1234567,
            "duration_seconds": 60.0,
            "source": "path",
        }
        rec = m().new_job(
            media,
            None,
            {"mode": "generate", "sync_mode": "auto", "translate": False, "language": None},
        )
        job = rec["job"]
        job.update(state="cancelled", started_at=now_iso(), finished_at=now_iso())
        m().history.insert(
            0,
            {
                "id": job["id"],
                "name": name,
                "media_path": media["path"],
                "mode": "generate",
                "sync_mode": "auto",
                "translate": False,
                "language": None,
                "state": "cancelled",
                "media_duration_seconds": 60.0,
                "elapsed_seconds": 1.0,
                "output_path": None,
                "summary": None,
                "finished_at": job["finished_at"],
                "error": None,
            },
        )
        return {"id": job["id"]}

    @app.get("/__mock/state")
    async def mock_state():
        mock = m()
        return {
            "uploaded_bytes": mock.uploaded_bytes,
            "uploads": list(mock.uploads),
            "subscribers": len(mock.subscribers),
            "picks": mock.picks,
            "opened": {k: r.get("opened", 0) for k, r in mock.jobs.items()},
            "token": mock.token,
        }

    return app


def srt_time(t: float) -> str:
    ms = round(t * 1000)
    h, ms = divmod(ms, 3_600_000)
    mi, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{mi:02}:{s:02},{ms:03}"


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--job-seconds", type=float, default=8.0)
    parser.add_argument("--token", default=None)
    args = parser.parse_args()
    app = create_app(args.job_seconds, args.token)
    print(f"mock subfix web UI on http://127.0.0.1:{args.port}/")
    uvicorn.run(
        app, host="127.0.0.1", port=args.port, log_level="warning", timeout_graceful_shutdown=1
    )


if __name__ == "__main__":
    main()
