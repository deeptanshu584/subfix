"""End-to-end test of the real web UI on the real GPU (run by hand, not collected by pytest).

Prerequisites: `subfix web --no-browser` running on 127.0.0.1:8765, test media prepared in
`.cache/tmp/e2e/` (Tears of Steel clips — CC BY 3.0 Blender Foundation — a German LibriVox
clip, a copy of the 100-min synthetic film for the cancel test and a bogus .docx).

Usage:  python tests/e2e/web_e2e.py [--screenshots docs/images] [--only name,name]

Drives the page with Playwright + the installed Chrome (headless) and checks results on disk
and through the API. Prints one PASS/FAIL line per scenario and writes e2e_results.json.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import threading
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[2]
E2E = Path(os.environ.get("SUBFIX_E2E_DIR", ROOT / ".cache" / "tmp" / "e2e"))
HOME = ROOT / ".cache"
URL = "http://127.0.0.1:8765/"
RESULTS: list[dict] = []


def session() -> dict:
    return json.loads((HOME / "web" / "server.json").read_text())


def api(path: str, method: str = "GET", body: dict | None = None):
    s = session()
    req = urllib.request.Request(
        s["url"].rstrip("/") + "/" + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Subfix-Token": s["token"], "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"null")


def record(name: str, ok: bool, detail: str, t0: float) -> None:
    dt = time.perf_counter() - t0
    RESULTS.append({"scenario": name, "ok": ok, "detail": detail, "seconds": round(dt, 1)})
    print(f"{'PASS' if ok else 'FAIL'}  {name:28} {dt:6.1f}s  {detail}", flush=True)


# ------------------------------------------------------------------ page helpers


def job_ids(page: Page) -> list[str]:
    return page.eval_on_selector_all(".job[data-job-id]", "els => els.map(e => e.dataset.jobId)")


def add_path(page: Page, path: Path) -> None:
    page.fill("#path-input", f'"{path}"')  # quoted, like Explorer's "Copy as path"
    page.click("#path-add")
    expect(page.locator("#media-card")).to_be_visible(timeout=15_000)


def set_options(
    page: Page,
    mode: str = "auto",
    translate: bool = False,
    language: str = "",
    sync_mode: str = "auto",
) -> None:
    page.check(f"input[name=mode][value={mode}]")
    page.set_checked("#translate", translate)
    page.select_option("#language", language)
    page.select_option("#sync-mode", sync_mode)


def submit(page: Page) -> str:
    before = set(job_ids(page))
    page.click("#submit")
    deadline = time.time() + 15
    while time.time() < deadline:
        new = [j for j in job_ids(page) if j not in before]
        if new:
            return new[0]
        time.sleep(0.1)
    raise AssertionError("no job card appeared after submit")


def wait_state(page: Page, job: str, states: tuple[str, ...], timeout: float = 600) -> str:
    loc = page.locator(f'.job[data-job-id="{job}"]')
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = loc.get_attribute("data-state")
        if st in states:
            return st
        time.sleep(0.2)
    raise AssertionError(f"job {job} not in {states} after {timeout}s")


# ------------------------------------------------------------------ native picker driver
user32 = ctypes.WinDLL("user32", use_last_error=True)
ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
WM_SETTEXT, WM_COMMAND, IDOK = 0x000C, 0x0111, 1


def _text(h) -> str:
    n = user32.GetWindowTextLengthW(h)
    b = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(h, b, n + 1)
    return b.value


def _cls(h) -> str:
    b = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(h, b, 256)
    return b.value


def answer_open_dialog(title: str, path: Path, timeout: float = 20) -> bool:
    """Fill the file-name box of the dialog titled ``title`` and press Open — via messages sent
    to that window only (no global keystrokes that could land in another window)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        found: list = []
        edits: list = []

        def top(h, _, found=found):
            if _text(h) == title and user32.IsWindowVisible(h):
                found.append(h)
            return True

        def child(h, _, edits=edits):
            if _cls(h) == "Edit" and _cls(user32.GetParent(h)) == "ComboBox":
                edits.append(h)
            return True

        user32.EnumWindows(ENUMPROC(top), 0)
        if found:
            dlg = found[0]
            user32.EnumChildWindows(dlg, ENUMPROC(child), 0)
            if edits:
                time.sleep(0.5)
                user32.SendMessageW(edits[0], WM_SETTEXT, 0, ctypes.c_wchar_p(str(path)))
                user32.PostMessageW(dlg, WM_COMMAND, IDOK, 0)
                return True
        time.sleep(0.2)
    return False


# ------------------------------------------------------------------ scenarios


def scenario_generate_mkv(page: Page, shots: Path | None) -> None:
    t0 = time.perf_counter()
    media = E2E / "tears_clip.mkv"
    out = media.with_name("tears_clip.generated.srt")
    out.unlink(missing_ok=True)
    add_path(page, media)
    set_options(page, mode="generate")
    if shots:
        page.screenshot(path=str(shots / "web-main.png"))
    job = submit(page)
    wait_state(page, job, ("done", "failed", "cancelled"))
    card = page.locator(f'.job[data-job-id="{job}"]')
    rows = card.locator(".preview tr").count()
    ok = out.is_file() and card.get_attribute("data-state") == "done" and rows >= 5
    if shots:
        card.scroll_into_view_if_needed()
        page.screenshot(path=str(shots / "web-result.png"))
    record("generate .mkv (path)", ok, f"{out.name} exists={out.is_file()} preview_rows={rows}", t0)


def scenario_picker(page: Page) -> None:
    t0 = time.perf_counter()
    media = E2E / "tears_clip.mkv"
    th = threading.Thread(
        target=lambda: RESULTS.append(
            {"_picker_answered": answer_open_dialog("Choose a video or audio file", media)}
        )
    )
    th.start()
    page.click("#choose-media")
    th.join(30)
    try:
        expect(page.locator("#media-card")).to_contain_text("tears_clip.mkv", timeout=15_000)
        ok, detail = True, "native dialog answered; original path used in place"
    except AssertionError as exc:
        ok, detail = False, f"media card not filled: {exc}"
    record("native file picker", ok, detail, t0)


def scenario_queue(page: Page, shots: Path | None) -> None:
    """mp4 generate + mp3 (audio-only) generate + mkv sync with a dropped .srt, queued at once."""
    t0 = time.perf_counter()
    outs = [
        E2E / "tears_part2.generated.srt",
        E2E / "tears_part3.generated.srt",
        E2E / "tears_clip.synced.srt",
    ]
    for o in outs:
        o.unlink(missing_ok=True)
    jobs = []
    add_path(page, E2E / "tears_part2.mp4")
    set_options(page, mode="generate")
    jobs.append(submit(page))
    add_path(page, E2E / "tears_part3.mp3")
    set_options(page, mode="generate")
    jobs.append(submit(page))
    add_path(page, E2E / "tears_clip.mkv")
    set_options(page, mode="sync", sync_mode="auto")
    page.set_input_files("#file-input", str(E2E / "tears_clip_offset.srt"))  # drop a .srt
    expect(page.locator("#subs-name")).to_contain_text("tears_clip_offset.srt", timeout=15_000)
    jobs.append(submit(page))
    # queue positions visible while job 1 runs
    positions = []
    for _ in range(100):
        states = [
            page.locator(f'.job[data-job-id="{j}"]').get_attribute("data-state") for j in jobs
        ]
        if states[0] == "running" and states[1] == states[2] == "queued":
            positions = [
                page.locator(f'.job[data-job-id="{j}"] .queue-position').inner_text()
                for j in jobs[1:]
            ]
            if shots:
                time.sleep(1.5)
                page.screenshot(path=str(shots / "web-progress.png"))
            break
        time.sleep(0.1)
    finals = [wait_state(page, j, ("done", "failed", "cancelled")) for j in jobs]
    info = [api(f"api/jobs/{j}") for j in jobs]
    spans = sorted((i["started_at"], i["finished_at"]) for i in info)
    sequential = all(a[1] <= b[0] for a, b in zip(spans, spans[1:], strict=False))
    # sync accuracy against the official timing
    sys.path.insert(0, str(ROOT / "benchmarks"))
    from corrupt import summarize, timing_errors

    from subfix.srt_io import load_subtitles

    acc = "n/a"
    if outs[2].is_file():
        s = summarize(
            timing_errors(
                load_subtitles(outs[2]).subs, load_subtitles(E2E / "tears_clip_truth.srt").subs
            )
        )
        acc = f"sync median {s['median']:.0f} ms, {s['within200']:.0f}% ≤200 ms"
    ok = finals == ["done"] * 3 and sequential and all(o.is_file() for o in outs)
    record(
        "queue of 3 (mp4, mp3, sync)",
        ok,
        f"states={finals} sequential={sequential} positions={positions} {acc}",
        t0,
    )
    record("generate .mp4", outs[0].is_file(), outs[0].name, t0)
    record("audio-only .mp3", outs[1].is_file(), outs[1].name, t0)
    record("sync with dropped .srt", outs[2].is_file(), acc, t0)


def scenario_translate(page: Page) -> None:
    t0 = time.perf_counter()
    media = E2E / "grimm_de.mp3"
    out = media.with_name("grimm_de.generated.srt")
    out.unlink(missing_ok=True)
    add_path(page, media)
    set_options(page, mode="generate", translate=True)
    job = submit(page)
    st = wait_state(page, job, ("done", "failed", "cancelled"))
    text = out.read_text(encoding="utf-8") if out.is_file() else ""
    ok = st == "done" and "grandfather" in text.lower()
    record(
        "translate German → English",
        ok,
        f"state={st}; contains 'grandfather': {'grandfather' in text.lower()}",
        t0,
    )


def scenario_cancel(page: Page) -> None:
    t0 = time.perf_counter()
    media = E2E / "cancel_long.mkv"  # uncached copy of the 100-min synthetic film
    out = media.with_name("cancel_long.generated.srt")
    out.unlink(missing_ok=True)
    add_path(page, media)
    set_options(page, mode="generate")
    job = submit(page)
    card = page.locator(f'.job[data-job-id="{job}"]')
    deadline = time.time() + 300
    while time.time() < deadline:
        if api(f"api/jobs/{job}")["progress"]["stage"] == "transcribe":
            break
        time.sleep(0.3)
    stage = api(f"api/jobs/{job}")["progress"]
    t_cancel = time.perf_counter()
    card.locator(".cancel").click()
    st = wait_state(page, job, ("done", "failed", "cancelled"), timeout=120)
    latency = time.perf_counter() - t_cancel
    ok = st == "cancelled" and not out.exists()
    record(
        "cancel mid-job",
        ok,
        f"cancelled during '{stage['stage']}' at "
        f"{stage['overall']:.0%}; stopped after {latency:.1f}s; no output={not out.exists()}",
        t0,
    )


def scenario_wrong_type(page: Page) -> None:
    t0 = time.perf_counter()
    page.fill("#path-input", str(E2E / "report.docx"))
    page.click("#path-add")
    banner = page.locator("#error")
    expect(banner).to_be_visible(timeout=10_000)
    msg = banner.inner_text()
    # server-side check too (the page rejects client-side before calling the API)
    try:
        api("api/inspect", "POST", {"path": str(E2E / "report.docx")})
        server = "accepted?!"
    except urllib.error.HTTPError as exc:
        server = json.loads(exc.read())["error"]["code"]
    ok = "unsupported" in msg.lower() or ".docx" in msg
    record(
        "wrong file type",
        ok and server == "unsupported_format",
        f"banner='{msg[:70]}…' server={server}",
        t0,
    )
    page.locator("#error button, #error .close").first.click() if page.locator(
        "#error button, #error .close"
    ).count() else None


def scenario_upload(page: Page) -> None:
    t0 = time.perf_counter()
    up_dir = HOME / "uploads"
    seen: list[tuple[str, int]] = []
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            for f in up_dir.rglob("*"):
                if f.is_file():
                    seen.append((str(f), f.stat().st_size))
            time.sleep(0.05)

    w = threading.Thread(target=watch, daemon=True)
    w.start()
    src = E2E / "tears_part2.mp4"
    page.set_input_files("#file-input", str(src))
    expect(page.locator("#media-card")).to_contain_text("tears_part2.mp4", timeout=60_000)
    set_options(page, mode="generate")
    job = submit(page)
    st = wait_state(page, job, ("done", "failed", "cancelled"))
    time.sleep(1.0)
    stop.set()
    info = api(f"api/jobs/{job}")
    leftovers = [str(f) for f in up_dir.rglob("*") if f.is_file()]
    paths = sorted({p for p, _ in seen})
    on_d = all(p.upper().startswith("D:") for p in paths)
    max_size = max((s for _, s in seen), default=0)
    out = Path(info["result"]["output_path"]) if info.get("result") else None
    ok = (
        st == "done"
        and paths
        and on_d
        and not leftovers
        and out is not None
        and out.is_file()
        and info["media"]["source"] == "upload"
    )
    record(
        "drag-and-drop upload",
        ok,
        f"temp={paths[:1]} (on D:={on_d}, peak {max_size / 2**20:.1f} MiB of "
        f"{src.stat().st_size / 2**20:.1f}) leftovers={leftovers} output={out}",
        t0,
    )


SCENARIOS = {
    "picker": scenario_picker,
    "generate": scenario_generate_mkv,
    "queue": scenario_queue,
    "translate": scenario_translate,
    "cancel": scenario_cancel,
    "wrong": scenario_wrong_type,
    "upload": scenario_upload,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--screenshots", type=Path, default=None)
    ap.add_argument("--only", default=",".join(SCENARIOS))
    a = ap.parse_args()
    if a.screenshots:
        a.screenshots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 960})
        page.goto(URL)
        expect(page.locator("#conn")).to_have_attribute("data-conn", "live", timeout=15_000)
        for name in a.only.split(","):
            fn = SCENARIOS[name]
            try:
                if name in ("generate", "queue"):
                    fn(page, a.screenshots)
                else:
                    fn(page)
            except Exception as exc:  # keep going; report
                record(name, False, f"exception: {exc!r}"[:300], time.perf_counter())
            page.reload()
            expect(page.locator("#conn")).to_have_attribute("data-conn", "live", timeout=15_000)
        if a.screenshots:
            page.screenshot(path=str(a.screenshots / "web-history.png"), full_page=True)
        browser.close()
    (E2E / "e2e_results.json").write_text(json.dumps(RESULTS, indent=1))
    fails = [r for r in RESULTS if "ok" in r and not r["ok"]]
    print(f"\n{len([r for r in RESULTS if 'ok' in r]) - len(fails)} passed, {len(fails)} failed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
