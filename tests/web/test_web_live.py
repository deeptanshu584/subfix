"""Against a real uvicorn server: SSE, streaming uploads, ``run()`` session handling."""

from __future__ import annotations

import json
import socket
import threading
import time
import webbrowser
from pathlib import Path

import httpx
import pytest
from webutil import TOKEN, free_port, wait_for, write_media

from subfix.web import server


def read_events(lines, until, timeout: float = 10.0) -> list[tuple[str, object]]:
    """Parse SSE ``lines`` until ``until(events)`` is true. Comments → ("comment", text)."""
    events: list[tuple[str, object]] = []
    name, data = None, []
    deadline = time.monotonic() + timeout
    for line in lines:
        assert time.monotonic() < deadline, f"timed out; got {events}"
        if line.startswith(":"):
            events.append(("comment", line[1:].strip()))
        elif line.startswith("event:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data.append(line.split(":", 1)[1].strip())
        elif line == "" and name:
            events.append((name, json.loads("\n".join(data))))
            name, data = None, []
        if until(events):
            return events
    raise AssertionError(f"stream ended early: {events}")


def test_sse_hello_job_log_history_and_ping(live, env):
    url = live["url"]
    media = write_media(env, "sse.mkv")
    with (
        httpx.Client(base_url=url, timeout=10) as c,
        c.stream("GET", f"/api/events?token={TOKEN}") as r,
    ):
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        lines = r.iter_lines()
        first = read_events(lines, lambda ev: any(n == "hello" for n, _ in ev))
        hello = next(d for n, d in first if n == "hello")
        assert hello["version"] == "1.0.0" and hello["time"]
        job = c.post(
            "/api/jobs",
            json={"media": {"path": str(media)}},
            headers={"X-Subfix-Token": TOKEN},
        ).json()

        def finished(ev):
            return any(n == "history" for n, _ in ev) and any(n == "comment" for n, _ in ev)

        ev = read_events(lines, finished)
    job_events = [d for n, d in ev if n == "job" and d["id"] == job["id"]]
    assert job_events[-1]["state"] == "done"
    assert {d["state"] for d in job_events} >= {"running", "done"}
    logs = [d for n, d in ev if n == "log"]
    assert any(line["message"] == "Processing sse.mkv now" for line in logs)
    assert all(line["job_id"] == job["id"] for line in logs)
    hist = next(d for n, d in ev if n == "history")
    assert hist["id"] == job["id"] and hist["state"] == "done"
    assert ("comment", "ping") in ev
    wait_for(lambda: live["app"].state.broker.subscriber_count == 0)


def test_sse_requires_token(live):
    r = httpx.get(f"{live['url']}/api/events", timeout=5)
    assert r.status_code == 403


def test_upload_is_streamed_to_disk_while_sending(live, env):
    """The first MiB must be on disk before the client has sent the rest of the body."""
    total = 4 * (1 << 20)
    piece = b"\xab" * 65536
    state = {"saw_partial": False}

    def partial_file_size() -> int:
        files = list(env["uploads"].glob("u-*/*.mkv"))
        return files[0].stat().st_size if files else 0

    def body():
        sent = 0
        while sent < total:
            if sent == total // 2:
                deadline = time.monotonic() + 5
                while partial_file_size() < (1 << 20) and time.monotonic() < deadline:
                    time.sleep(0.01)
                state["saw_partial"] = partial_file_size() >= (1 << 20)
            yield piece
            sent += len(piece)

    r = httpx.post(
        f"{live['url']}/api/uploads?filename=big.mkv&size={total}",
        content=body(),
        headers={"X-Subfix-Token": TOKEN, "Content-Type": "application/octet-stream"},
        timeout=30,
    )
    assert r.status_code == 201, r.text
    assert state["saw_partial"], "server buffered the body instead of streaming it"
    p = Path(r.json()["path"])
    assert p.stat().st_size == total
    assert p.read_bytes() == b"\xab" * total


def test_aborted_upload_leaves_no_partial_file(live, env):
    port = live["port"]
    with socket.create_connection(("127.0.0.1", port)) as s:
        s.sendall(
            (
                f"POST /api/uploads?filename=cut.mkv&size=5000000 HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\nX-Subfix-Token: {TOKEN}\r\n"
                "Content-Length: 5000000\r\n\r\n"
            ).encode()
            + b"x" * 300_000
        )
        wait_for(lambda: list(env["uploads"].glob("u-*")) != [])
    # the connection is closed mid-body: the partial upload must disappear
    wait_for(lambda: list(env["uploads"].glob("u-*")) == [])


def test_run_writes_session_and_serves(env, monkeypatch):
    monkeypatch.setattr(webbrowser, "open", lambda url: None)
    port = free_port()
    t = threading.Thread(target=server.run, kwargs={"port": port, "open_browser": False})
    t.start()
    session = env["home"] / "web" / "server.json"
    try:
        wait_for(session.is_file, timeout=15)
        info = json.loads(session.read_text())
        assert info["url"] == f"http://127.0.0.1:{port}" and info["port"] == port
        assert len(info["token"]) == 32 and info["pid"] > 0 and info["started_at"]
        base = info["url"]
        assert httpx.get(f"{base}/api/health").json()["app"] == "subfix"
        assert info["token"] in httpx.get(f"{base}/").text
        r = httpx.get(f"{base}/api/formats", headers={"X-Subfix-Token": info["token"]})
        assert r.status_code == 200

        # a second `subfix web` on the same port just opens the browser on the running one
        opened = []
        monkeypatch.setattr(webbrowser, "open", opened.append)
        server.run(port=port, open_browser=True)
        assert opened == [f"{base}/"]
    finally:
        server._active_server.should_exit = True
        t.join(15)
    assert not t.is_alive()
    assert not session.exists()


def test_run_port_taken_by_other_program(env):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        with pytest.raises(SystemExit, match="in use"):
            server.run(port=port, open_browser=False)
