"""Fixtures for the web API tests: tmp SUBFIX_HOME, a fake run_job (no GPU), clients."""

from __future__ import annotations

import logging
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient
from webutil import PORT, TOKEN, FakeRunner, Recorder, free_port, make_app

from subfix import device, pipeline
from subfix.web import server


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("SUBFIX_HOME", str(home))
    monkeypatch.setenv("SUBFIX_ALLOW_SYSTEM_DRIVE_UPLOADS", "1")
    monkeypatch.delenv("SUBFIX_UPLOAD_DIR", raising=False)
    runner = FakeRunner()
    monkeypatch.setattr(pipeline, "run_job", runner)
    monkeypatch.setattr(
        server,
        "device_summary",
        lambda: {
            "device": {"cuda": True, "gpu": "Fake GPU", "vram_mb": 6000},
            "defaults": {"transcribe": "tiny on cpu (int8)", "translate": "tiny on cpu (int8)"},
        },
    )
    opened = Recorder()
    monkeypatch.setattr(server, "open_in_explorer", opened)
    released = Recorder()
    monkeypatch.setattr(device, "release_models", released)
    monkeypatch.setattr(server, "duration_of", lambda p: 12.5)
    logging.getLogger("subfix").setLevel(logging.INFO)
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    return {
        "home": home,
        "uploads": tmp_path / "uploads",
        "media": media_dir,
        "runner": runner,
        "opened": opened,
        "released": released,
    }


@pytest.fixture
def app(env):
    return make_app(env)


@pytest.fixture
def client(app):
    with TestClient(
        app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-Subfix-Token": TOKEN}
    ) as c:
        yield c


@pytest.fixture
def live(env):
    """A real uvicorn server on an ephemeral port (for SSE and streaming uploads)."""
    port = free_port()
    app = make_app(env, port=port, ping_interval=0.3)
    cfg = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
        timeout_graceful_shutdown=2,
    )
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.02)
    yield {"url": f"http://127.0.0.1:{port}", "port": port, "app": app}
    app.state.broker.close()
    srv.should_exit = True
    t.join(10)
