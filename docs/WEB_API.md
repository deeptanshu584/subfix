# subfix web UI — API and integration contract

Status: **v1 (frozen for implementation)**. Changes go through the lead; subagents report
mismatches instead of editing someone else's files.

The web UI is a local single-user app: `subfix web` starts a FastAPI server on
**127.0.0.1 only**, opens the browser, and runs subtitle jobs one at a time on the GPU by
calling the subfix core in-process.

---

## 1. File layout and ownership

| Path | Owner | Notes |
|---|---|---|
| `subfix/progress.py`, `subfix/formats.py` | lead (core) | progress/cancel hooks, format lists, language names |
| `subfix/pipeline.py` → `run_job()`, `Outcome` | lead (core) | the only entry point the web backend uses for work |
| `subfix/device.py` → `keep_models_loaded`, `release_models()` | lead (core) | model cache for long-running servers |
| `subfix/log.py` → `set_progress_bars()` | lead (core) | disable rich bars in the server |
| `subfix/cli.py` → `subfix web` | lead (core) | calls `subfix.web.server.run(...)` |
| `subfix/web/__init__.py`, `server.py`, `jobs.py`, `uploads.py`, `picker.py`, `history.py` (any `subfix/web/*.py`) | **backend** | FastAPI app, queue, SSE, uploads, picker, history |
| `tests/web/` | **backend** | pytest API tests with a mocked `run_job` (no GPU) |
| `subfix/web/static/` (`index.html`, `app.js`, `style.css`, icons) | **frontend** | the page; no build step, no CDN |
| `tests/web_frontend/` | **frontend** | mock server implementing this contract + browser tests |
| `scripts/windows/` | **windows** | launcher `.bat`, shortcut script, HKCU context menu, toast |
| `README.md` (web UI section) | **docs** | screenshots as placeholders `docs/images/web-*.png` |
| `docs/images/` | lead | real screenshots (Tears of Steel only) |

## 2. Core API used by the backend (already implemented and tested by the lead)

```python
from subfix import pipeline, progress, device, formats
from subfix.log import set_progress_bars

device.keep_models_loaded = True          # reuse the loaded Whisper model between jobs
set_progress_bars(False)                  # no rich bars in a server process

ctrl = progress.JobControl(on_progress=callback)   # callback(stage, fraction|None, message)
with progress.use(ctrl):                  # must be entered in the worker thread
    outcome = pipeline.run_job(
        media=Path(...), mode="auto"|"generate"|"sync", subs_path=Path|None,
        sync_mode="auto"|"offset"|"drift"|"piecewise"|"smart", translate=bool,
        language=None|"en"|..., out=Path|None,
    )                                     # raises progress.Cancelled after ctrl.cancel_event.set()
# outcome: kind ("generated"|"synced"), output_path, cues, language, summary, method,
#          preview [{index,start,end,text}] (first 20), notes [str]
device.release_models()                   # free VRAM (call when the queue has been idle ~120 s)
```

* Stages (`progress.STAGES`, labels in `progress.STAGE_LABELS`):
  `extract` → `vad` → `load_model` → `transcribe` → `sync` → `write`. A job may skip stages
  (audio-only sync has no `load_model`/`transcribe`; cached steps report fraction 1.0 at once).
  `fraction` is 0..1 or `None` (indeterminate, e.g. `load_model`).
* Cancellation is cooperative: ffmpeg is killed within ~1 s; Whisper stops after the current
  batch (measured: immediate at segment granularity, a few seconds worst case).
* Exceptions from `run_job`: `progress.Cancelled`; `formats.UnsupportedFormat` (a `ValueError`);
  `subfix.audio.AudioError`; `subfix.srt_io.SubtitleError`; `ValueError` (bad options, no
  subtitles to sync); `RuntimeError` (e.g. no model could be loaded, nothing recognised).
* Log lines: the core logs to the `logging.getLogger("subfix")` logger with rich markup
  (`[bold]…[/]`). The backend attaches a handler while a job runs and strips markup
  (`rich.text.Text.from_markup(msg).plain`).
* `formats.classify(path)` → `"video" | "audio" | "subtitle"` or raises `UnsupportedFormat`
  with a user-facing message. `formats.LANGUAGES` = {code: name} for all 100 Whisper codes.

## 3. Server, security and session

* `subfix.web.server.run(port: int = 8765, open_browser: bool = True, upload_dir: Path | None = None) -> None`
  * binds **127.0.0.1** only (no option to change it);
  * if the port is taken by another running subfix server (its `server.json` is valid and
    `GET /api/health` answers), just open the browser on it and return; otherwise fail with a
    clear message;
  * writes the session file `<SUBFIX_HOME>/web/server.json`
    `{"url": "http://127.0.0.1:8765", "port": 8765, "token": "<hex>", "pid": 1234, "started_at": "<ISO>"}`
    and deletes it on shutdown (used by the right-click menu script);
  * opens `http://127.0.0.1:<port>/` in the default browser after the server is listening.
* **Token**: a random 32-hex-char token per server run. `GET /` serves `static/index.html` with
  the literal `__SUBFIX_TOKEN__` replaced by the token (the page reads
  `<meta name="subfix-token" content="__SUBFIX_TOKEN__">`). Every `/api/*` route **except
  `GET /api/health`** requires header `X-Subfix-Token: <token>` or query `?token=<token>`
  (EventSource and download links use the query form). Missing/wrong → `403 forbidden`.
* **Host check** (DNS-rebinding defence): requests whose `Host` header is not
  `127.0.0.1:<port>` or `localhost:<port>` get `403 forbidden`. No CORS headers are sent.
* Static files: `GET /static/<file>` from `subfix/web/static/`. The page must work offline
  (no CDN, no external fonts).

## 4. Supported formats

| Kind | Extensions |
|---|---|
| video | `.mkv .mp4 .avi .mov .webm .m4v .wmv .flv .ts .m2ts` |
| audio | `.mp3 .wav .m4a .flac .aac .ogg .opus` |
| subtitle (sync input) | `.srt .ass .ssa .vtt` |

Source of truth: `subfix.formats` (each container verified with ffmpeg 9.0.2 on this machine).
Matching is by extension, case-insensitive. Anything else → `400 unsupported_format` with the
message from `formats.classify`.

## 5. Data shapes

All times are ISO-8601 local time with offset (`"2026-11-20T04:12:05+05:30"`), durations in
seconds (float), sizes in bytes. Paths are absolute Windows paths as JSON strings.

### Job

```json
{
  "id": "j-7f3a9c2e",
  "state": "queued",
  "created_at": "2026-11-20T04:12:05+05:30",
  "started_at": null,
  "finished_at": null,
  "queue_position": 1,
  "media": {
    "path": "D:\\Movies\\Tears.mov", "name": "Tears.mov", "kind": "video",
    "size_bytes": 372178639, "duration_seconds": 734.1, "source": "path"
  },
  "subs": null,
  "options": {"mode": "auto", "sync_mode": "auto", "translate": false, "language": null},
  "progress": {
    "stage": null, "stage_label": "Waiting in queue", "stage_progress": null,
    "overall": 0.0, "eta_seconds": null, "message": ""
  },
  "result": null,
  "error": null
}
```

* `state`: `queued` → `running` → `done` | `failed` | `cancelled` (a queued job can go straight
  to `cancelled`). `queue_position`: 1 = next to run; `null` unless `queued`.
* `media.source`: `"path"` (original file used in place) or `"upload"` (streamed temp copy).
  `media.duration_seconds` may be `null` if ffprobe failed. `subs` = `null` or
  `{"path", "name", "source"}`.
* `progress.stage` ∈ `progress.STAGES` or `null`; `stage_label` is human readable;
  `stage_progress` 0..1 or `null` (indeterminate); `overall` 0..1, monotonic non-decreasing
  while running, 1.0 when done; `eta_seconds` = backend estimate for the whole job or `null`.
  Overall weights used by the backend: extract 5 %, vad 7 %, load_model 3 %, transcribe 80 %,
  sync 3 %, write 2 % (skipped stages are jumped over).
* `result` (only when `done`):

```json
{
  "kind": "generated", "output_path": "D:\\Movies\\Tears.generated.srt",
  "output_name": "Tears.generated.srt", "cues": 76, "language": "en",
  "summary": "generated 76 cues (en)", "method": "generate", "notes": [],
  "elapsed_seconds": 11.7,
  "preview": [{"index": 1, "start": 23.0, "end": 24.5, "text": "You're a jerk, Thom."}]
}
```

* `error` (only when `failed`): `{"code": "...", "message": "..."}` (codes in §8).
* `GET /api/jobs/{id}` additionally returns `"log_tail": [LogLine, …]` (last 200 lines).

### LogLine

```json
{"job_id": "j-7f3a9c2e", "time": "2026-11-20T04:12:07+05:30", "level": "INFO",
 "message": "Extracting audio from Tears.mov ..."}
```

### HistoryItem

```json
{
  "id": "j-7f3a9c2e", "name": "Tears.mov", "media_path": "D:\\Movies\\Tears.mov",
  "mode": "auto", "sync_mode": "auto", "translate": false, "language": "en",
  "state": "done", "media_duration_seconds": 734.1, "elapsed_seconds": 11.7,
  "output_path": "D:\\Movies\\Tears.generated.srt", "summary": "generated 76 cues (en)",
  "finished_at": "2026-11-20T04:12:17+05:30", "error": null
}
```

Stored in `<SUBFIX_HOME>/web/history.jsonl` (one JSON object per line, newest appended, capped
at 500 entries). Every job that reaches a final state is recorded.

## 6. Endpoints

All responses are JSON unless noted. Token rules in §3.

| Method & path | Request | Response |
|---|---|---|
| `GET /api/health` (no token) | – | `{"status": "ok", "app": "subfix", "version": "1.0.0"}` |
| `GET /api/config` | – | see below |
| `GET /api/formats` | – | `{"video": [...], "audio": [...], "subtitle": [...]}` |
| `POST /api/pick` | `{"kind": "media" \| "subtitle"}` | `{"path": "D:\\...\\x.mkv"}` or `{"path": null}` if the dialog was cancelled |
| `POST /api/inspect` | `{"path": "..."}` | `{"path", "name", "kind", "size_bytes", "duration_seconds", "sidecar": null \| {"path", "name"}}` |
| `POST /api/uploads?filename=<name>&size=<bytes>` | raw file bytes (`application/octet-stream`) | `201 {"upload_id", "name", "kind", "size_bytes", "path"}` |
| `DELETE /api/uploads/{upload_id}` | – | `204` |
| `POST /api/jobs` | see below | `201` Job |
| `GET /api/jobs` | – | `{"jobs": [Job, …]}` — running first, then queued by position, then finished newest first (this server run only) |
| `GET /api/jobs/{id}` | – | Job + `log_tail` |
| `POST /api/jobs/{id}/cancel` | – | Job (queued → `cancelled` now; running → stays `running` until the worker stops, then `cancelled`) |
| `GET /api/jobs/{id}/srt` | – | the `.srt` as an attachment (`Content-Disposition: attachment; filename="…"`) |
| `POST /api/jobs/{id}/open-folder` | – | `{"ok": true}`; opens Explorer with the output file selected |
| `GET /api/history?limit=100` | – | `{"items": [HistoryItem, …]}` newest first |
| `DELETE /api/history` | – | `{"ok": true}` (clears the list; never deletes `.srt` files) |
| `GET /api/events` | – | Server-sent events, §7 |

`GET /api/config`:

```json
{
  "version": "1.0.0",
  "device": {"cuda": true, "gpu": "NVIDIA GeForce RTX 4050 Laptop GPU", "vram_mb": 6141},
  "defaults": {"transcribe": "large-v3-turbo on cuda (int8_float16)",
               "translate": "large-v3 on cuda (int8_float16)"},
  "upload_dir": "D:\\subfix-data\\uploads",
  "outputs_dir": "D:\\subfix-data\\web\\outputs",
  "formats": {"video": [...], "audio": [...], "subtitle": [...]},
  "languages": [{"code": "en", "name": "English"}, ...],
  "modes": ["auto", "generate", "sync"],
  "sync_modes": ["auto", "offset", "drift", "piecewise", "smart"]
}
```

`POST /api/jobs` body:

```json
{
  "media": {"path": "D:\\Movies\\Tears.mov"},
  "subs": null,
  "mode": "auto",
  "sync_mode": "auto",
  "translate": false,
  "language": null
}
```

* `media`: exactly one of `{"path": "..."}` or `{"upload_id": "..."}`; must classify as
  video or audio. `subs`: `null`, `{"path"}` or `{"upload_id"}`; must classify as subtitle.
* `mode` = `"sync"` requires `subs` or a sidecar next to a path-source media, else
  `400 no_subtitles`. `translate` applies when subtitles are generated (ignored when syncing).
  `language` = Whisper code or `null` (auto-detect); unknown code → `400 invalid_request`.
* **Output location**: path-source media → next to the original, same names as the CLI
  (`<stem>.generated.srt` / `<stem>.synced.srt`); upload-source media →
  `<outputs_dir>/<job id>/<original stem>.<kind>.srt`. If the original's folder isn't
  writable, the backend falls back to `outputs_dir` and adds a note to `result.notes`.

## 7. Server-sent events (`GET /api/events?token=…`)

`text/event-stream`, one stream for everything. Events:

| `event:` | `data:` (JSON) | When |
|---|---|---|
| `hello` | `{"version": "1.0.0", "time": "<ISO>"}` | on connect |
| `job` | Job (without `log_tail`) | on creation, every state change, and progress updates (throttled to ≤ 4 per second per job; the final state is always sent) |
| `log` | LogLine | every log line of a running job |
| `history` | HistoryItem | when a job is recorded |

A comment line `: ping` is sent every 15 s. On (re)connect the client re-fetches
`GET /api/jobs` and `GET /api/history`, then applies events.

## 8. Errors

Every non-2xx response: `{"error": {"code": "<code>", "message": "<human readable>"}}`
(optionally `"details": {...}`).

| HTTP | `code` | Meaning |
|---|---|---|
| 400 | `invalid_request` | malformed body / unknown option value |
| 400 | `unsupported_format` | extension not in §4 (message lists the supported ones) |
| 400 | `no_subtitles` | sync requested but no subtitle file given or found |
| 403 | `forbidden` | missing/wrong token or bad Host header |
| 404 | `file_not_found` | path does not exist / is not a file |
| 404 | `job_not_found`, `upload_not_found`, `output_not_found` | unknown id / no .srt yet |
| 409 | `job_finished` | cancel on a job already in a final state |
| 409 | `picker_busy` | a file dialog is already open |
| 413 | `upload_too_large` | `size` larger than free space on the upload drive minus 1 GB |
| 500 | `upload_dir_on_system_drive` | upload dir is on the Windows system drive (C:) — refused unless env `SUBFIX_ALLOW_SYSTEM_DRIVE_UPLOADS=1` |
| 501 | `picker_unavailable` | no native dialog possible (tkinter missing) |
| 500 | `internal` | anything unexpected (message = exception text) |

Job failures (in `job.error.code`): `cancelled` is a state, not an error; failures use
`audio_error`, `subtitle_error`, `unsupported_format`, `no_subtitles`, `model_error`
(RuntimeError while loading/transcribing), `internal`.

## 9. Uploads (drag and drop)

* Browsers never reveal a dropped file's real path, so dropped files are **streamed** (no
  buffering in memory) to `<upload_dir>/<upload_id>/<sanitised name>`, 1 MiB chunks.
* `upload_dir`: `--upload-dir` / env `SUBFIX_UPLOAD_DIR` / default `<SUBFIX_HOME>/uploads`
  (for example `D:\subfix-data\uploads`). Refused if on the system drive (see §8).
* The extension is validated **before** the body is read; free space is checked against
  `size`. A failed/aborted upload deletes its partial file.
* Deletion: when the job that uses the upload reaches a final state (done/failed/cancelled),
  via `DELETE /api/uploads/{id}`, uploads not attached to a job within 1 hour, and every
  leftover in `upload_dir` at server start.
* The frontend sends uploads with `XMLHttpRequest` (`xhr.send(file)`) so it can show upload
  progress; the browser streams the file from disk.

## 10. Native file picker

`POST /api/pick` opens a Windows "Open" dialog **in a separate Python process** (tkinter;
topmost; filters from §4; starts in the folder of the last picked file) so Tk never runs inside
the server's threads. Returns the original path — no copy is ever made.

## 11. Worker and queue

* Exactly one worker thread; jobs run strictly one at a time in FIFO order.
* The worker sets `device.keep_models_loaded = True`, runs `pipeline.run_job` inside
  `progress.use(ctrl)`, forwards progress as `job` events, attaches a log handler for `log`
  events, and calls `device.release_models()` after the queue has been idle for 120 s.
* Cancel: queued → removed from the queue, state `cancelled`; running → `ctrl.cancel_event.set()`.
* On server shutdown: running job cancelled, queue dropped, uploads deleted, `server.json` removed.

## 12. Page DOM hooks (for end-to-end tests)

The frontend must expose these stable selectors (styling is free):

| Selector | Element |
|---|---|
| `#choose-media` | button → `POST /api/pick {"kind":"media"}` |
| `#path-input`, `#path-add` | text box for a pasted path + its add button |
| `#drop-zone` | drag-and-drop area (also accepts `.srt/.ass/.ssa/.vtt`) |
| `input[name=mode]` (values `auto`/`generate`/`sync`) | mode radio buttons |
| `#translate` | checkbox |
| `#language` | `<select>`, first option value `""` = auto-detect |
| `#sync-mode` | `<select>` with the five sync modes |
| `#choose-subs`, `#subs-name`, `#clear-subs` | optional subtitle file for sync |
| `#submit` | "Add to queue" |
| `#error` | visible error banner (text = `error.message`) |
| `.job[data-job-id="<id>"]` with `[data-state="<state>"]` | one card per job |
| `.job .cancel`, `.job .progress-bar`, `.job .eta`, `.job .log` | per-job controls/info |
| `.job .download`, `.job .open-folder`, `.job .preview` | result actions and preview table |
| `#history` (rows `.history-row`) | history list |
| `#formats` | list of supported formats |

## 13. Right-click menu and launcher (windows subagent)

* Launcher `scripts/windows/subfix-web.bat`: runs `<repo>\.venv\Scripts\subfix.exe web`
  (path resolved relative to the script); window title "subfix web UI".
* Context menu: `HKEY_CURRENT_USER\Software\Classes\SystemFileAssociations\<ext>\shell\subfix`
  for every video/audio extension in §4 ("Generate subtitles with subfix"). Nothing outside
  HKCU is written. On Windows 11 it appears under "Show more options".
* The menu command runs a PowerShell script that: if `<SUBFIX_HOME>/web/server.json` exists
  and `GET {url}/api/health` answers, submits `POST /api/jobs {"media":{"path":…},"mode":"auto"}`
  with the token and polls `GET /api/jobs/{id}` until final (one GPU queue for everything);
  otherwise runs `subfix.exe auto "<file>" --quiet --status`. Either way it shows a Windows
  toast notification with the result (output name or error).
