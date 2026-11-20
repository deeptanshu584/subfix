"""FastAPI app and ``subfix web`` entry point (docs/WEB_API.md).

Security model: the server binds 127.0.0.1 only, rejects requests whose ``Host`` header is not
``127.0.0.1:<port>``/``localhost:<port>`` (DNS rebinding) and requires a per-run token on every
``/api/*`` route except ``GET /api/health``. No CORS headers are sent.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
from starlette.datastructures import Headers, QueryParams
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import StreamingResponse
from starlette.staticfiles import StaticFiles

from .. import __version__, formats
from ..config import subfix_home
from ..srt_io import find_sidecar
from .common import ApiError, classify, duration_of, existing_file, formats_dict, now_iso
from .history import History
from .jobs import EventBroker, FileSpec, JobManager
from .picker import Picker
from .uploads import UploadManager

STATIC_DIR = Path(__file__).parent / "static"
TOKEN_PLACEHOLDER = "__SUBFIX_TOKEN__"
MODES = ["auto", "generate", "sync"]
SYNC_MODES = ["auto", "offset", "drift", "piecewise", "smart"]

FALLBACK_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="subfix-token" content="__SUBFIX_TOKEN__"><title>subfix</title></head>
<body style="font-family:system-ui;padding:2rem">
<h1>subfix</h1><p>The web UI files are missing (subfix/web/static/index.html).
The API is running.</p></body></html>
"""


# ---------------------------------------------------------------------------- helpers


def open_in_explorer(path: Path) -> None:
    """Show ``path`` selected in the file manager (patched in tests)."""
    path = Path(path)
    if sys.platform == "win32":
        # A string command line is passed verbatim, so the quotes reach explorer intact.
        subprocess.Popen(f'explorer /select,"{path}"')
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])


_device_cache: dict[str, Any] | None = None
_device_lock = threading.Lock()


def device_summary() -> dict[str, Any]:
    """GPU info and default models (computed once; imports ctranslate2, runs nvidia-smi)."""
    global _device_cache
    with _device_lock:
        if _device_cache is None:
            from ..device import cuda_available, default_choice, gpu_info

            try:
                cuda = cuda_available()
            except Exception:
                cuda = False
            info = gpu_info() if cuda else None
            _device_cache = {
                "device": {
                    "cuda": cuda,
                    "gpu": info.name if info else None,
                    "vram_mb": info.vram_total_mb if info else None,
                },
                "defaults": {
                    "transcribe": str(default_choice("transcribe")),
                    "translate": str(default_choice("translate")),
                },
            }
        return _device_cache


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


class GuardMiddleware:
    """Host-header check on every request and token check on ``/api/*`` (except health)."""

    def __init__(self, app: Any, state: Any) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host", "").lower()
        port = self.state.port
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            resp = error_response(403, "forbidden", "Requests must be made to 127.0.0.1.")
            await resp(scope, receive, send)
            return
        path = scope.get("path", "")
        if path.startswith("/api/") and path != "/api/health":
            token = headers.get("x-subfix-token") or QueryParams(
                scope.get("query_string", b"")
            ).get("token")
            if not token or not secrets.compare_digest(token, self.state.token):
                resp = error_response(
                    403, "forbidden", "Missing or wrong token. Reload the page (Ctrl+F5)."
                )
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------- request bodies


class PickRequest(BaseModel):
    kind: Literal["media", "subtitle"] = "media"


class InspectRequest(BaseModel):
    path: str


class FileRef(BaseModel):
    path: str | None = None
    upload_id: str | None = None


class JobRequest(BaseModel):
    media: FileRef
    subs: FileRef | None = None
    mode: Literal["auto", "generate", "sync"] = "auto"
    sync_mode: Literal["auto", "offset", "drift", "piecewise", "smart"] = "auto"
    translate: bool = False
    language: str | None = None


# ---------------------------------------------------------------------------- app


def default_upload_dir(home: Path) -> Path:
    env = os.environ.get("SUBFIX_UPLOAD_DIR")
    return Path(env).expanduser() if env else home / "uploads"


def create_app(
    *,
    upload_dir: Path | None = None,
    home: Path | None = None,
    port: int = 8765,
    token: str | None = None,
    ping_interval: float = 15.0,
    idle_release_seconds: float = 120.0,
) -> FastAPI:
    """Build the app. ``home`` defaults to ``SUBFIX_HOME``; everything lives below it."""
    home = Path(home) if home is not None else subfix_home()
    web_dir = home / "web"
    upload_dir = Path(upload_dir) if upload_dir is not None else default_upload_dir(home)
    outputs_dir = web_dir / "outputs"

    broker = EventBroker()
    uploads = UploadManager(upload_dir)
    history = History(web_dir / "history.jsonl")
    jobs = JobManager(
        outputs_dir, history, uploads, broker, idle_release_seconds=idle_release_seconds
    )
    picker = Picker(web_dir / "picker.json")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        web_dir.mkdir(parents=True, exist_ok=True)
        outputs_dir.mkdir(parents=True, exist_ok=True)
        uploads.cleanup_all()
        jobs.start()
        try:
            yield
        finally:
            broker.close()
            await asyncio.to_thread(jobs.shutdown)
            uploads.cleanup_all()

    app = FastAPI(
        title="subfix",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    st = app.state
    st.port = port
    st.token = token or secrets.token_hex(16)
    st.home = home
    st.upload_dir = upload_dir
    st.outputs_dir = outputs_dir
    st.broker = broker
    st.uploads = uploads
    st.history = history
    st.jobs = jobs
    st.picker = picker
    st.ping_interval = ping_interval
    app.add_middleware(GuardMiddleware, state=st)

    # ------------------------------------------------------------------ errors
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.body(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        parts = []
        for err in exc.errors()[:3]:
            loc = ".".join(str(x) for x in err.get("loc", ()) if x not in ("body", "query"))
            parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
        return error_response(400, "invalid_request", "; ".join(parts) or "Invalid request.")

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def _internal_error(request: Request, exc: Exception) -> JSONResponse:
        return error_response(500, "internal", str(exc) or type(exc).__name__)

    # ------------------------------------------------------------------ page
    @app.get("/", include_in_schema=False)
    def index() -> HTMLResponse:
        try:
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        except OSError:
            html = FALLBACK_PAGE
        return HTMLResponse(
            html.replace(TOKEN_PLACEHOLDER, st.token), headers={"Cache-Control": "no-store"}
        )

    app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")

    # ------------------------------------------------------------------ info
    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "app": "subfix", "version": __version__}

    @app.get("/api/config")
    def config() -> dict:
        dev = device_summary()
        return {
            "version": __version__,
            "device": dev["device"],
            "defaults": dev["defaults"],
            "upload_dir": str(upload_dir),
            "outputs_dir": str(outputs_dir),
            "formats": formats_dict(),
            "languages": [{"code": c, "name": n} for c, n in formats.LANGUAGES.items()],
            "modes": MODES,
            "sync_modes": SYNC_MODES,
        }

    @app.get("/api/formats")
    def get_formats() -> dict:
        return formats_dict()

    # ------------------------------------------------------------------ files
    @app.post("/api/pick")
    def pick(body: PickRequest) -> dict:
        return {"path": st.picker.pick(body.kind)}

    @app.post("/api/inspect")
    def inspect(body: InspectRequest) -> dict:
        path = existing_file(body.path)
        kind = classify(path)
        sidecar = None
        duration = None
        if kind != "subtitle":
            duration = duration_of(path)
            try:
                found = find_sidecar(path)
            except OSError:
                found = None
            if found:
                sidecar = {"path": str(found), "name": found.name}
        return {
            "path": str(path),
            "name": path.name,
            "kind": kind,
            "size_bytes": path.stat().st_size,
            "duration_seconds": duration,
            "sidecar": sidecar,
        }

    @app.post("/api/uploads", status_code=201)
    async def upload(
        request: Request,
        filename: str = Query(..., min_length=1),
        size: int | None = Query(None, ge=0),
    ) -> dict:
        if size is None:
            length = request.headers.get("content-length")
            size = int(length) if length and length.isdigit() else None
        up = await uploads.receive(filename, size, request.stream())
        return up.to_dict()

    @app.delete("/api/uploads/{upload_id}", status_code=204)
    def delete_upload(upload_id: str) -> Response:
        uploads.delete(upload_id)
        return Response(status_code=204)

    # ------------------------------------------------------------------ jobs
    def file_spec(ref: FileRef, role: str) -> FileSpec:
        if bool(ref.path) == bool(ref.upload_id):
            raise ApiError(
                400, "invalid_request", f"{role}: give exactly one of 'path' or 'upload_id'."
            )
        if ref.path:
            path = existing_file(ref.path)
            kind = classify(path)
            return FileSpec(path, path.name, kind, "path", size_bytes=path.stat().st_size)
        up = uploads.get(ref.upload_id or "")
        return FileSpec(
            up.path, up.name, up.kind, "upload", size_bytes=up.size_bytes, upload_id=up.id
        )

    @app.post("/api/jobs", status_code=201)
    def create_job(body: JobRequest) -> dict:
        language = body.language or None
        if language is not None and language not in formats.LANGUAGES:
            raise ApiError(400, "invalid_request", f"Unknown language code '{language}'.")
        media = file_spec(body.media, "media")
        if media.kind not in ("video", "audio"):
            raise ApiError(
                400,
                "unsupported_format",
                f"{media.name} is a subtitle file; choose a video or audio file as the media.",
            )
        subs = file_spec(body.subs, "subs") if body.subs else None
        if subs is not None and subs.kind != "subtitle":
            raise ApiError(
                400,
                "unsupported_format",
                f"{subs.name} is not a subtitle file (expected "
                f"{' '.join(formats.SUBTITLE_EXTENSIONS)}).",
            )
        if body.mode == "sync" and subs is None:
            sidecar = None
            if media.source == "path":
                try:
                    sidecar = find_sidecar(media.path)
                except OSError:
                    sidecar = None
            if sidecar is None:
                raise ApiError(
                    400,
                    "no_subtitles",
                    f"Sync needs a subtitle file: choose one, or put it next to {media.name} "
                    "with the same name.",
                )
        media.duration_seconds = duration_of(media.path)
        return jobs.submit(media, subs, body.mode, body.sync_mode, body.translate, language)

    @app.get("/api/jobs")
    def list_jobs() -> dict:
        return {"jobs": jobs.list()}

    def get_job(job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise ApiError(404, "job_not_found", f"Unknown job '{job_id}'.")
        return job

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str) -> dict:
        return jobs.detail(get_job(job_id))

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict:
        job = get_job(job_id)
        snap = jobs.cancel(job)
        if snap is None:
            raise ApiError(409, "job_finished", f"Job {job_id} has already finished ({job.state}).")
        return snap

    def output_of(job_id: str) -> Path:
        job = get_job(job_id)
        result = job.result if job.state == "done" else None
        path = Path(result["output_path"]) if result else None
        if path is None or not path.is_file():
            raise ApiError(404, "output_not_found", f"Job {job_id} has no subtitle file yet.")
        return path

    @app.get("/api/jobs/{job_id}/srt")
    def download_srt(job_id: str) -> FileResponse:
        path = output_of(job_id)
        return FileResponse(path, media_type="application/x-subrip", filename=path.name)

    @app.post("/api/jobs/{job_id}/open-folder")
    def open_folder(job_id: str) -> dict:
        open_in_explorer(output_of(job_id))
        return {"ok": True}

    # ------------------------------------------------------------------ history
    @app.get("/api/history")
    def get_history(limit: int = Query(100, ge=1, le=500)) -> dict:
        return {"items": history.items(limit)}

    @app.delete("/api/history")
    def clear_history() -> dict:
        history.clear()
        return {"ok": True}

    # ------------------------------------------------------------------ events
    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        queue = broker.subscribe()

        async def stream():
            try:
                yield "retry: 2000\n\n"
                yield EventBroker.format("hello", {"version": __version__, "time": now_iso()})
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=st.ping_interval)
                    except TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield ": ping\n\n"
                        continue
                    if item is None:
                        break
                    yield item
            finally:
                broker.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


# ---------------------------------------------------------------------------- run


def session_file(home: Path | None = None) -> Path:
    return (home or subfix_home()) / "web" / "server.json"


def _port_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _health_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/health", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("app") == "subfix"
    except (OSError, ValueError):
        return False


def existing_server(port: int, home: Path | None = None) -> str | None:
    """URL of a running subfix server on ``port`` (valid server.json + health), else None."""
    try:
        info = json.loads(session_file(home).read_text(encoding="utf-8"))
        if int(info.get("port")) == port and _health_ok(info["url"]):
            return str(info["url"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    url = f"http://127.0.0.1:{port}"
    return url if _health_ok(url) else None


#: The uvicorn server of the current :func:`run` call (lets tests stop it).
_active_server: Any = None


def run(port: int = 8765, open_browser: bool = True, upload_dir: Path | None = None) -> None:
    """Serve the web UI on 127.0.0.1:``port`` until Ctrl+C."""
    import uvicorn

    from ..log import console

    url = f"http://127.0.0.1:{port}"
    if not _port_free(port):
        running = existing_server(port)
        if running:
            console.print(f"subfix web is already running at [bold]{running}/[/]")
            if open_browser:
                webbrowser.open(f"{running}/")
            return
        raise SystemExit(
            f"Port {port} on 127.0.0.1 is in use by another program. "
            f"Start with another port, e.g.: subfix web --port {port + 1}"
        )

    app = create_app(upload_dir=upload_dir, port=port)
    token = app.state.token
    session = session_file(app.state.home)

    class _Server(uvicorn.Server):
        def handle_exit(self, sig: int, frame: Any) -> None:
            app.state.broker.close()  # end SSE streams so shutdown doesn't wait for them
            super().handle_exit(sig, frame)

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=3,
        lifespan="on",
    )
    server = _Server(config)
    global _active_server
    _active_server = server

    def announce() -> None:
        deadline = time.monotonic() + 60
        while not server.started and not server.should_exit and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            return
        session.parent.mkdir(parents=True, exist_ok=True)
        session.write_text(
            json.dumps(
                {
                    "url": url,
                    "port": port,
                    "token": token,
                    "pid": os.getpid(),
                    "started_at": now_iso(),
                }
            ),
            encoding="utf-8",
        )
        console.print(
            f"[bold green]subfix web[/] running at [bold]{url}/[/] "
            f"(uploads: {app.state.upload_dir}) — press Ctrl+C to stop"
        )
        if open_browser:
            webbrowser.open(f"{url}/")

    threading.Thread(target=announce, name="subfix-announce", daemon=True).start()
    try:
        server.run()
    finally:
        try:
            if json.loads(session.read_text(encoding="utf-8")).get("token") == token:
                session.unlink()
        except (OSError, ValueError):
            pass
        _active_server = None
