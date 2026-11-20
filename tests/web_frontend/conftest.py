"""Fixtures for the web UI browser tests: the mock server (in a thread) and headless Chrome.

The tests are skipped when Playwright or a locally installed Google Chrome is missing
(``playwright install`` is never run).
"""

from __future__ import annotations

import importlib.util
import json
import secrets
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
SCREENSHOTS = HERE / "screenshots"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "browser: web UI tests that drive Google Chrome")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if Path(str(item.fspath)).resolve().parent == HERE:
            item.add_marker(pytest.mark.browser)


def _load_mock_module():
    # Loaded by file path under a unique name so it never clashes with other test helpers.
    spec = importlib.util.spec_from_file_location(
        "subfix_web_frontend_mock", HERE / "mock_server.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockControl:
    """Talks to the mock server's ``/__mock/*`` control routes."""

    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token

    def _call(self, method: str, path: str, data: Any = None, token: bool = False) -> Any:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Subfix-Token"] = self.token
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(self.url + path, data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=10) as res:
            raw = res.read()
        return json.loads(raw) if raw else None

    def reset(self, job_seconds: float = 2.5) -> None:
        self._call("POST", "/__mock/reset", {"job_seconds": job_seconds})

    def pick(self, *results: Any) -> None:
        self._call("POST", "/__mock/pick", {"results": list(results)})

    def fail(self, method: str, path: str, status: int, code: str, message: str) -> None:
        self._call(
            "POST",
            "/__mock/fail",
            {"method": method, "path": path, "status": status, "code": code, "message": message},
        )

    def drop_events(self, refuse: int = 0) -> int:
        return self._call("POST", "/__mock/drop-events", {"refuse": refuse})["dropped"]

    def inject_job(self, name: str) -> str:
        return self._call("POST", "/__mock/inject-job", {"name": name})["id"]

    def state(self) -> dict[str, Any]:
        return self._call("GET", "/__mock/state")

    def api(self, method: str, path: str, data: Any = None) -> Any:
        return self._call(method, path, data, token=True)


@pytest.fixture(scope="session")
def mock_server():
    uvicorn = pytest.importorskip("uvicorn")
    module = _load_mock_module()
    token = secrets.token_hex(16)
    port = _free_port()
    app = module.create_app(job_seconds=2.5, token=token)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", timeout_graceful_shutdown=1
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            pytest.fail("mock server did not start")
        time.sleep(0.05)
    yield MockControl(f"http://127.0.0.1:{port}", token)
    server.should_exit = True
    thread.join(10)


@pytest.fixture(scope="session")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            chrome = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:  # Chrome not installed / not launchable
            pytest.skip(f"Google Chrome is not available for Playwright: {exc}")
        yield chrome
        chrome.close()


@pytest.fixture
def mock(mock_server: MockControl) -> MockControl:
    mock_server.reset()
    return mock_server


@pytest.fixture
def page(browser, mock: MockControl):
    from playwright.sync_api import expect

    context = browser.new_context(viewport={"width": 1360, "height": 900}, accept_downloads=True)
    context.set_default_timeout(10_000)
    pg = context.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda exc: errors.append(str(exc)))
    pg.goto(mock.url + "/")
    expect(pg.locator("#conn")).to_have_attribute("data-conn", "live")
    yield pg
    context.close()
    assert not errors, f"JavaScript errors on the page: {errors}"
