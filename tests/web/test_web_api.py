"""Web API: security, info endpoints, inspect, picker, uploads (TestClient, no GPU)."""

from __future__ import annotations

import os
import threading
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from webutil import PORT, TOKEN, write_media

from subfix import formats
from subfix.web import common, picker, server, uploads
from subfix.web.history import History

# ----------------------------------------------------------------------------- security


def test_health_needs_no_token(app):
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as c:
        r = c.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "app": "subfix", "version": "0.1.0"}
    assert "access-control-allow-origin" not in r.headers


@pytest.mark.parametrize("headers", [{}, {"X-Subfix-Token": "wrong"}])
def test_api_requires_token(app, headers):
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as c:
        r = c.get("/api/config", headers=headers)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_token_in_query_string(app):
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as c:
        assert c.get(f"/api/formats?token={TOKEN}").status_code == 200
        assert c.get("/api/formats?token=nope").status_code == 403


@pytest.mark.parametrize("host", ["evil.example:8765", "127.0.0.1:9999", "localhost"])
def test_bad_host_header_rejected(client, host):
    r = client.get("/api/health", headers={"Host": host})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"
    assert client.get("/", headers={"Host": host}).status_code == 403


def test_localhost_host_allowed(client):
    assert client.get("/api/health", headers={"Host": f"localhost:{PORT}"}).status_code == 200


def test_index_injects_token(client):
    r = client.get("/")
    assert r.status_code == 200
    assert TOKEN in r.text
    assert server.TOKEN_PLACEHOLDER not in r.text
    assert r.headers["cache-control"] == "no-store"


def test_index_fallback_when_static_missing(client, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "STATIC_DIR", tmp_path / "nothing")
    r = client.get("/")
    assert r.status_code == 200
    assert f'content="{TOKEN}"' in r.text


def test_static_files_served(client):
    if not (server.STATIC_DIR / "index.html").is_file():
        pytest.skip("frontend not present")
    assert client.get("/static/index.html").status_code == 200
    assert client.get("/static/does-not-exist.js").status_code == 404


def test_error_format_for_unknown_route_and_validation(client):
    r = client.get("/api/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
    r = client.post("/api/inspect", json={"wrong": 1})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    r = client.post(
        "/api/inspect", content=b"not json", headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


# ----------------------------------------------------------------------------- info


def test_config(client, env):
    c = client.get("/api/config").json()
    assert c["version"] == "0.1.0"
    assert c["device"]["gpu"] == "Fake GPU"
    assert c["upload_dir"] == str(env["uploads"])
    assert c["outputs_dir"] == str(env["home"] / "web" / "outputs")
    assert c["modes"] == ["auto", "generate", "sync"]
    assert c["sync_modes"] == ["auto", "offset", "drift", "piecewise", "smart"]
    assert {"code": "en", "name": "English"} in c["languages"]
    assert len(c["languages"]) == len(formats.LANGUAGES)
    assert c["formats"]["subtitle"] == list(formats.SUBTITLE_EXTENSIONS)


def test_formats(client):
    f = client.get("/api/formats").json()
    assert f == {
        "video": list(formats.VIDEO_EXTENSIONS),
        "audio": list(formats.AUDIO_EXTENSIONS),
        "subtitle": list(formats.SUBTITLE_EXTENSIONS),
    }


# ----------------------------------------------------------------------------- inspect


def _wav(path: Path, seconds: float = 1.0) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))
    return path


def test_inspect_audio_with_real_ffprobe_and_sidecar(client, env, monkeypatch):
    monkeypatch.setattr(server, "duration_of", common.duration_of)
    wav = _wav(env["media"] / "clip.wav")
    (env["media"] / "clip.en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n\n")
    r = client.post("/api/inspect", json={"path": f'"{wav}"'})  # Explorer "Copy as path" quotes
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["path"] == os.path.abspath(wav)
    assert d["name"] == "clip.wav"
    assert d["kind"] == "audio"
    assert d["size_bytes"] == wav.stat().st_size
    assert d["duration_seconds"] is None or abs(d["duration_seconds"] - 1.0) < 0.05
    assert d["sidecar"]["name"] == "clip.en.srt"


def test_inspect_duration_null_on_ffprobe_failure(client, env, monkeypatch):
    monkeypatch.setattr(server, "duration_of", common.duration_of)
    p = write_media(env, "broken.mkv", b"not a video")
    d = client.post("/api/inspect", json={"path": str(p)}).json()
    assert d["kind"] == "video"
    assert d["duration_seconds"] is None
    assert d["sidecar"] is None


def test_inspect_subtitle(client, env):
    p = env["media"] / "x.srt"
    p.write_text("x")
    d = client.post("/api/inspect", json={"path": str(p)}).json()
    assert d["kind"] == "subtitle"
    assert d["duration_seconds"] is None


@pytest.mark.parametrize(
    ("make", "status", "code"),
    [
        (lambda m: str(m / "missing.mkv"), 404, "file_not_found"),
        (lambda m: str(m), 404, "file_not_found"),
        (lambda m: "relative/movie.mkv", 400, "invalid_request"),
        (lambda m: "", 400, "invalid_request"),
    ],
)
def test_inspect_errors(client, env, make, status, code):
    r = client.post("/api/inspect", json={"path": make(env["media"])})
    assert r.status_code == status
    assert r.json()["error"]["code"] == code


def test_inspect_unsupported(client, env):
    p = write_media(env, "notes.txt")
    r = client.post("/api/inspect", json={"path": str(p)})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "unsupported_format"
    assert ".mkv" in err["message"]


# ----------------------------------------------------------------------------- picker


def test_pick_mocked(client, env, monkeypatch):
    chosen = env["media"] / "film.mkv"
    seen = []

    def fake_dialog(self, kind, initial_dir):
        seen.append((kind, initial_dir))
        return str(chosen)

    monkeypatch.setattr(picker, "tk_available", lambda: True)
    monkeypatch.setattr(picker.Picker, "run_dialog", fake_dialog)
    r = client.post("/api/pick", json={"kind": "media"})
    assert r.json() == {"path": str(chosen)}
    r = client.post("/api/pick", json={"kind": "subtitle"})
    assert r.status_code == 200
    # second dialog starts in the folder of the last pick
    assert seen == [("media", ""), ("subtitle", str(env["media"]))]


def test_pick_cancelled_returns_null(client, monkeypatch):
    monkeypatch.setattr(picker, "tk_available", lambda: True)
    monkeypatch.setattr(picker.Picker, "run_dialog", lambda self, k, d: None)
    assert client.post("/api/pick", json={"kind": "media"}).json() == {"path": None}


def test_pick_bad_kind(client):
    r = client.post("/api/pick", json={"kind": "folder"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


def test_pick_busy(client, app, monkeypatch):
    release = threading.Event()
    entered = threading.Event()

    def blocking(self, kind, initial_dir):
        entered.set()
        release.wait(5)
        return None

    monkeypatch.setattr(picker, "tk_available", lambda: True)
    monkeypatch.setattr(picker.Picker, "run_dialog", blocking)
    t = threading.Thread(target=app.state.picker.pick, args=("media",))
    t.start()
    assert entered.wait(5)
    r = client.post("/api/pick", json={"kind": "media"})
    release.set()
    t.join(5)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "picker_busy"


def test_pick_unavailable(client, monkeypatch):
    monkeypatch.setattr(picker, "tk_available", lambda: False)
    r = client.post("/api/pick", json={"kind": "media"})
    assert r.status_code == 501
    assert r.json()["error"]["code"] == "picker_unavailable"


def test_picker_script_and_filters():
    compile(picker.DIALOG_SCRIPT, "<dialog>", "exec")
    media = dict(picker.filetypes("media"))
    assert "*.mkv" in media["Video and audio"] and "*.opus" in media["Video and audio"]
    assert dict(picker.filetypes("subtitle"))["Subtitles"] == "*.srt *.ass *.ssa *.vtt"


# ----------------------------------------------------------------------------- uploads


def test_upload_streams_in_chunks(client, env, monkeypatch):
    writes: list[int] = []
    orig = uploads.UploadManager._write_chunk

    def spy(self, f, data):
        writes.append(len(data))
        orig(self, f, data)

    monkeypatch.setattr(uploads.UploadManager, "_write_chunk", spy)
    body = bytes(range(256)) * (5 * 4096 + 7)  # ~5 MiB, not a multiple of the chunk size
    r = client.post(
        f"/api/uploads?filename=My%20Movie.mkv&size={len(body)}",
        content=body,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["name"] == "My Movie.mkv"
    assert d["kind"] == "video"
    assert d["size_bytes"] == len(body)
    p = Path(d["path"])
    assert p.parent.parent == env["uploads"]
    assert p.parent.name == d["upload_id"]
    assert p.read_bytes() == body
    assert max(writes) <= uploads.CHUNK_SIZE
    assert len(writes) >= 5


def test_upload_unsupported_extension_rejected(client, env):
    r = client.post("/api/uploads?filename=evil.exe&size=10", content=b"0123456789")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "unsupported_format"
    assert not env["uploads"].exists() or not any(env["uploads"].iterdir())


def test_upload_refused_on_system_drive(client, env, monkeypatch):
    monkeypatch.delenv("SUBFIX_ALLOW_SYSTEM_DRIVE_UPLOADS")
    monkeypatch.setenv("SYSTEMDRIVE", env["uploads"].drive or "C:")
    r = client.post("/api/uploads?filename=a.mkv&size=3", content=b"abc")
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "upload_dir_on_system_drive"
    monkeypatch.setenv("SYSTEMDRIVE", "Q:")
    assert client.post("/api/uploads?filename=a.mkv&size=3", content=b"abc").status_code == 201


def test_upload_too_large(client):
    r = client.post("/api/uploads?filename=a.mkv&size=999999999999999999", content=b"abc")
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "upload_too_large"


def test_upload_size_mismatch_leaves_nothing(client, env):
    r = client.post("/api/uploads?filename=a.mkv&size=10", content=b"abc")
    assert r.status_code == 400
    r = client.post("/api/uploads?filename=a.mkv&size=2", content=b"abc")
    assert r.status_code == 400
    assert list(env["uploads"].iterdir()) == []


def test_upload_missing_filename(client):
    r = client.post("/api/uploads?size=3", content=b"abc")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


def test_delete_upload(client, env):
    d = client.post("/api/uploads?filename=x.srt&size=3", content=b"abc").json()
    assert Path(d["path"]).is_file()
    assert client.delete(f"/api/uploads/{d['upload_id']}").status_code == 204
    assert not Path(d["path"]).parent.exists()
    r = client.delete(f"/api/uploads/{d['upload_id']}")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "upload_not_found"


def test_stale_and_startup_cleanup(env, tmp_path):
    up_dir = env["uploads"]
    up_dir.mkdir()
    (up_dir / "u-0badc0de").mkdir()
    (up_dir / "u-0badc0de" / "left.mkv").write_bytes(b"x")
    (up_dir / "keep-me.txt").write_text("not ours")
    m = uploads.UploadManager(up_dir, stale_seconds=0.0)
    assert m.cleanup_all() == 1
    assert (up_dir / "keep-me.txt").exists()
    assert not (up_dir / "u-0badc0de").exists()


def test_stale_uploads_removed(client, app):
    d = client.post("/api/uploads?filename=x.wav&size=3", content=b"abc").json()
    app.state.uploads.stale_seconds = 0.0
    assert app.state.uploads.cleanup_stale() == 1
    assert not Path(d["path"]).exists()


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("movie.mkv", "movie.mkv"),
        ("..\\..\\Windows\\evil.mkv", "evil.mkv"),
        ("a/b/c:d?.srt", "c_d_.srt"),
        ("CON.mkv", "_CON.mkv"),
        ("", "upload"),
        ("x" * 300 + ".mkv", "x" * 146 + ".mkv"),
    ],
)
def test_sanitise_name(raw, clean):
    assert uploads.sanitise_name(raw) == clean


# ----------------------------------------------------------------------------- history


def test_history_cap_and_order(tmp_path):
    h = History(tmp_path / "h.jsonl", cap=5)
    for i in range(12):
        h.append({"id": i})
    assert [i["id"] for i in h.items(100)] == [11, 10, 9, 8, 7]
    assert [i["id"] for i in h.items(2)] == [11, 10]
    with (tmp_path / "h.jsonl").open("a") as f:
        f.write("{torn\n")
    assert len(h.items()) == 5
    h.clear()
    assert h.items() == []


def test_history_thread_safe(tmp_path):
    h = History(tmp_path / "h.jsonl", cap=50)
    threads = [
        threading.Thread(target=lambda n=n: [h.append({"t": n, "i": i}) for i in range(20)])
        for n in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(h.items(500)) == 50
