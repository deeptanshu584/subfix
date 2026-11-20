"""Browser tests for the subfix web UI against the mock server (docs/WEB_API.md §12 hooks)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Page, expect

MockControl = Any  # conftest.MockControl (not imported: conftest modules aren't importable)
SCREENSHOTS = Path(__file__).resolve().parent / "screenshots"
TEARS = r"D:\Movies\Tears of Steel.mkv"


def choose(page: Page, mock: MockControl, path: str) -> None:
    mock.pick(path)
    page.click("#choose-media")
    name = path.rsplit("\\", 1)[-1]
    expect(page.locator("#media-card .file-name")).to_have_text(name)


def add_job(page: Page, mock: MockControl, path: str) -> str:
    """Choose a movie via the picker, add it to the queue and return the new job id."""
    choose(page, mock, path)
    with page.expect_response(
        lambda r: r.url.endswith("/api/jobs") and r.request.method == "POST"
    ) as info:
        page.click("#submit")
    job = info.value.json()
    expect(page.locator(f'.job[data-job-id="{job["id"]}"]')).to_be_visible()
    expect(page.locator("#media-card")).to_be_hidden()
    return job["id"]


def job_card(page: Page, job_id: str):
    return page.locator(f'.job[data-job-id="{job_id}"]')


def drop_files(page: Page, files: list[tuple[str, str]]) -> None:
    """Dispatch a real drop event with a DataTransfer holding (name, text content) files."""
    page.evaluate(
        """(files) => {
            const dt = new DataTransfer();
            for (const [name, text] of files) dt.items.add(new File([text], name));
            const zone = document.querySelector('#drop-zone');
            for (const type of ['dragenter', 'dragover', 'drop']) {
                zone.dispatchEvent(new DragEvent(type, {dataTransfer: dt, bubbles: true, cancelable: true}));
            }
        }""",
        files,
    )


# ------------------------------------------------------------------------------ page & config


def test_page_loads_with_token_and_config(page: Page, mock: MockControl) -> None:
    token = page.get_attribute('meta[name="subfix-token"]', "content")
    assert token == mock.token
    expect(page.locator("#device")).to_contain_text("GPU: RTX 4050 · large-v3-turbo")
    expect(page.locator("#upload-dir")).to_have_text(r"<repo>\.cache\uploads")
    formats = page.locator("#formats")
    for ext in (".mkv", ".m2ts", ".opus", ".srt", ".vtt"):
        expect(formats).to_contain_text(ext)
    first = page.locator("#language option").first
    expect(first).to_have_attribute("value", "")
    expect(first).to_have_text("Auto-detect")
    assert page.locator("#language option").count() == 101
    assert page.locator("#sync-mode option").evaluate_all("os => os.map(o => o.value)") == [
        "auto",
        "offset",
        "drift",
        "piecewise",
        "smart",
    ]
    expect(page.locator("input[name=mode][value=auto]")).to_be_checked()
    expect(page.locator("#error")).to_be_hidden()
    expect(page.locator("#jobs-empty")).to_be_visible()


def test_formatting_helpers(page: Page) -> None:
    ui = "window.subfixUI"
    assert page.evaluate(f"{ui}.formatEta(130)") == "2 min 10 s left"
    assert page.evaluate(f"{ui}.formatEta(45)") == "45 s left"
    assert page.evaluate(f"{ui}.formatEta(3900)") == "1 h 5 min left"
    assert page.evaluate(f"{ui}.formatDuration(734.1)") == "12 min 14 s"
    assert (
        page.evaluate(f"{ui}.cleanPath('  \"D:\\\\Movies\\\\A B.mkv\"  ')") == r"D:\Movies\A B.mkv"
    )
    assert page.evaluate(f"{ui}.cleanPath(\"'D:\\\\x.srt'\")") == r"D:\x.srt"


def test_page_without_token_explains(browser, mock: MockControl) -> None:
    pg = browser.new_page()
    try:
        pg.goto(mock.url + "/static/index.html")  # placeholder not replaced
        expect(pg.locator("#error")).to_contain_text("opened by subfix")
    finally:
        pg.close()


# ------------------------------------------------------------------------------ main flow


def test_choose_movie_to_done(page: Page, mock: MockControl) -> None:
    mock.reset(job_seconds=3.0)
    choose(page, mock, TEARS)
    card = page.locator("#media-card")
    expect(card).to_contain_text("12 min 14 s")
    expect(card).to_contain_text("Video")
    expect(card).to_contain_text("used in place")
    page.select_option("#language", "en")

    with page.expect_request("**/api/jobs") as req:
        page.click("#submit")
    body = json.loads(req.value.post_data or "{}")
    assert body == {
        "media": {"path": TEARS},
        "subs": None,
        "mode": "auto",
        "sync_mode": "auto",
        "translate": False,
        "language": "en",
    }
    job = page.locator(".job").first
    job_id = job.get_attribute("data-job-id")
    assert job_id and job_id.startswith("j-")

    # Progress while running: stage label, percentage, ETA, live log.
    expect(job).to_have_attribute("data-state", "running")
    expect(job.locator(".progress-bar")).not_to_have_attribute("aria-valuenow", "0")
    expect(job.locator(".eta")).to_have_text(re.compile(r"(\d+ (min|s).* left|almost done)"))
    expect(job.locator(".stage")).to_have_text(
        re.compile("Extracting audio|Detecting speech|Loading Whisper model|Transcribing")
    )
    expect(job.locator(".log")).to_contain_text("Extracting audio from Tears of Steel.mkv")
    expect(job.locator(".cancel")).to_be_visible()

    # Done: result, download link, preview.
    expect(job).to_have_attribute("data-state", "done", timeout=15_000)
    expect(job.locator(".cancel")).to_be_hidden()
    expect(job.locator(".eta")).to_be_hidden()
    expect(job.locator(".summary")).to_contain_text("Generated 76 subtitle lines")
    expect(job.locator(".output-path")).to_have_text(r"D:\Movies\Tears of Steel.generated.srt")
    download = job.locator(".download")
    expect(download).to_have_attribute("href", f"/api/jobs/{job_id}/srt?token={mock.token}")
    rows = job.locator(".preview tbody tr")
    expect(rows).to_have_count(20)
    expect(rows.first).to_contain_text("00:00:23,000")
    expect(rows.first).to_contain_text("You're a jerk, Thom.")
    # Markup in cue text is shown literally, never parsed.
    expect(job.locator(".preview")).to_contain_text("<script>alert(1)</script>")
    assert job.locator(".preview script, .preview b").count() == 0

    with page.expect_download() as dl:
        download.click()
    assert dl.value.suggested_filename == "Tears of Steel.generated.srt"

    job.locator(".open-folder").click()
    expect(job.locator(".open-folder")).to_contain_text("Opened")
    assert mock.state()["opened"][job_id] == 1

    # History row recorded live.
    row = page.locator("#history .history-row").first
    expect(row).to_contain_text("Tears of Steel.mkv")
    expect(row).to_contain_text("12 min 14 s")
    expect(row).to_contain_text(r"D:\Movies\Tears of Steel.generated.srt")
    expect(row).to_contain_text("Done")
    expect(page).to_have_title("subfix")


def test_paste_path_strips_quotes_and_shows_sidecar(page: Page, mock: MockControl) -> None:
    page.fill("#path-input", '  "D:\\Movies\\Big Buck withsubs.mp4"  ')
    with page.expect_request("**/api/inspect") as req:
        page.click("#path-add")
    assert json.loads(req.value.post_data or "{}") == {"path": r"D:\Movies\Big Buck withsubs.mp4"}
    card = page.locator("#media-card")
    expect(card).to_contain_text("Big Buck withsubs.mp4")
    expect(card).to_contain_text("Subtitle found next to the movie: Big Buck withsubs.srt")
    expect(page.locator("#path-input")).to_have_value("")

    # A pasted subtitle path attaches it as the subtitle to sync (Enter submits).
    page.fill("#path-input", "'D:\\Subs\\Other.en.srt'")
    page.press("#path-input", "Enter")
    expect(page.locator("#subs-name")).to_have_text("Other.en.srt")
    expect(page.locator("#clear-subs")).to_be_enabled()
    page.click("#clear-subs")
    expect(page.locator("#subs-name")).to_have_text("None chosen")

    # Auto mode with a sidecar ends up as a sync.
    page.click("#submit")
    job = page.locator(".job").first
    expect(job).to_have_attribute("data-state", "done", timeout=15_000)
    expect(job.locator(".summary")).to_contain_text("Synced 76 subtitle lines")


def test_unsupported_path_rejected_client_side(page: Page) -> None:
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url))
    page.fill("#path-input", r"D:\Docs\notes.txt")
    page.click("#path-add")
    error = page.locator("#error")
    expect(error).to_be_visible()
    expect(error).to_contain_text("notes.txt: unsupported file type '.txt'")
    expect(error).to_contain_text(".mkv")
    expect(error).to_contain_text(".srt")
    assert not any("/api/inspect" in u for u in requests)


def test_missing_file_shows_server_error_and_dismiss(page: Page) -> None:
    page.fill("#path-input", r"D:\Movies\missing.mkv")
    page.click("#path-add")
    error = page.locator("#error")
    expect(error).to_have_text(r"File not found: D:\Movies\missing.mkv")
    page.click("#error .banner-close")
    expect(error).to_be_hidden()


def test_choose_subtitle_and_sync_mode(page: Page, mock: MockControl) -> None:
    choose(page, mock, TEARS)
    mock.pick(r"D:\Movies\Tears of Steel.en.srt")
    page.click("#choose-subs")
    expect(page.locator("#subs-name")).to_have_text("Tears of Steel.en.srt")
    page.check("input[name=mode][value=sync]")
    page.select_option("#sync-mode", "drift")
    with page.expect_request("**/api/jobs") as req:
        page.click("#submit")
    body = json.loads(req.value.post_data or "{}")
    assert body["subs"] == {"path": r"D:\Movies\Tears of Steel.en.srt"}
    assert body["mode"] == "sync" and body["sync_mode"] == "drift"
    expect(page.locator("#subs-name")).to_have_text("None chosen")
    job = page.locator(".job").first
    expect(job).to_have_attribute("data-state", "done", timeout=15_000)
    expect(job.locator(".summary")).to_contain_text("Synced")


def test_picker_cancelled_does_nothing(page: Page, mock: MockControl) -> None:
    mock.pick(None)
    with page.expect_response("**/api/pick"):
        page.click("#choose-media")
    page.wait_for_timeout(200)
    expect(page.locator("#media-card")).to_be_hidden()
    expect(page.locator("#error")).to_be_hidden()


# ------------------------------------------------------------------------------ uploads


def test_upload_via_file_input(page: Page, mock: MockControl) -> None:
    payload = b"\0" * (3 * 1024 * 1024)
    with page.expect_request("**/api/uploads?*") as req:
        page.set_input_files(
            "#file-input", files=[{"name": "clip.mp4", "mimeType": "video/mp4", "buffer": payload}]
        )
    assert req.value.headers.get("x-subfix-token") == mock.token
    assert "filename=clip.mp4" in req.value.url and f"size={len(payload)}" in req.value.url
    card = page.locator("#media-card")
    expect(card).to_contain_text("clip.mp4")
    expect(card).to_contain_text("uploaded copy")
    expect(card).to_contain_text("3.0 MB")
    expect(page.locator("#upload")).to_be_hidden()
    assert mock.state()["uploaded_bytes"] == len(payload)

    with page.expect_request("**/api/jobs") as job_req:
        page.click("#submit")
    body = json.loads(job_req.value.post_data or "{}")
    assert set(body["media"]) == {"upload_id"}
    job = page.locator(".job").first
    expect(job).to_contain_text("uploaded copy")
    expect(job).to_have_attribute("data-state", "done", timeout=15_000)
    expect(job.locator(".output-path")).to_contain_text(r"\.cache\web\outputs")


def test_drop_movie_and_subtitle(page: Page, mock: MockControl) -> None:
    drop_files(page, [("Home video.mkv", "x" * 5000), ("Home video.en.srt", "1\n")])
    expect(page.locator("#media-card")).to_contain_text("Home video.mkv")
    expect(page.locator("#subs-name")).to_have_text("Home video.en.srt")
    assert len(mock.state()["uploads"]) == 2
    with page.expect_request("**/api/jobs") as req:
        page.click("#submit")
    body = json.loads(req.value.post_data or "{}")
    assert set(body["media"]) == {"upload_id"} and set(body["subs"]) == {"upload_id"}
    expect(page.locator(".job").first).to_have_attribute("data-state", "done", timeout=15_000)
    expect(page.locator(".job .summary").first).to_contain_text("Synced")


def test_drop_unsupported_rejected(page: Page, mock: MockControl) -> None:
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url))
    drop_files(page, [("readme.pdf", "%PDF")])
    error = page.locator("#error")
    expect(error).to_contain_text("readme.pdf: unsupported file type '.pdf'")
    expect(error).to_contain_text(".mkv")
    assert not any("/api/uploads" in u for u in requests)
    assert mock.state()["uploaded_bytes"] == 0


def test_replacing_upload_deletes_the_old_one(page: Page, mock: MockControl) -> None:
    drop_files(page, [("a.mp4", "aaaa")])
    expect(page.locator("#media-card")).to_contain_text("a.mp4")
    assert len(mock.state()["uploads"]) == 1
    with page.expect_response(lambda r: "/api/uploads/" in r.url and r.request.method == "DELETE"):
        choose(page, mock, TEARS)
    assert mock.state()["uploads"] == []


# ------------------------------------------------------------------------------ queue


def test_queue_positions_and_cancel(page: Page, mock: MockControl) -> None:
    mock.reset(job_seconds=30)
    first = add_job(page, mock, r"D:\Movies\One.mkv")
    second = add_job(page, mock, r"D:\Movies\Two.mkv")
    third = add_job(page, mock, r"D:\Movies\Three.mkv")

    expect(job_card(page, first)).to_have_attribute("data-state", "running")
    for job_id, pos, text in ((second, "1", "Next in line"), (third, "2", "Position 2")):
        card = job_card(page, job_id)
        expect(card).to_have_attribute("data-state", "queued")
        expect(card).to_have_attribute("data-position", pos)
        expect(card.locator(".queue-position")).to_contain_text(text)
        expect(card.locator(".eta")).to_be_hidden()
    # Running job first, then the queue in order.
    order = page.locator(".job").evaluate_all("els => els.map(e => e.dataset.jobId)")
    assert order == [first, second, third]
    expect(page.locator("#queue-summary")).to_have_text("1 running · 2 waiting")

    # Cancel a queued job: immediate.
    job_card(page, second).locator(".cancel").click()
    expect(job_card(page, second)).to_have_attribute("data-state", "cancelled")
    expect(job_card(page, second).locator(".job-cancelled")).to_have_text("Cancelled")
    expect(job_card(page, third)).to_have_attribute("data-position", "1")

    # Cancel the running job: the next one starts.
    job_card(page, first).locator(".cancel").click()
    expect(job_card(page, first)).to_have_attribute("data-state", "cancelled")
    expect(job_card(page, third)).to_have_attribute("data-state", "running")
    expect(page.locator("#history .history-row")).to_have_count(2)
    job_card(page, third).locator(".cancel").click()
    expect(job_card(page, third)).to_have_attribute("data-state", "cancelled")


def test_failed_job_shows_error(page: Page, mock: MockControl) -> None:
    job_id = add_job(page, mock, r"D:\Movies\will-fail.mkv")
    card = job_card(page, job_id)
    expect(card).to_have_attribute("data-state", "failed", timeout=15_000)
    expect(card.locator(".job-error")).to_contain_text("Could not read the audio of will-fail.mkv")
    expect(card.locator(".download")).to_be_hidden()
    expect(page.locator("#history .history-row").first).to_contain_text("Failed")


def test_log_tail_loaded_for_finished_jobs_after_reload(page: Page, mock: MockControl) -> None:
    job_id = add_job(page, mock, TEARS)
    expect(job_card(page, job_id)).to_have_attribute("data-state", "done", timeout=15_000)
    page.reload()
    card = job_card(page, job_id)
    expect(card).to_have_attribute("data-state", "done")
    expect(card.locator(".preview tbody tr")).to_have_count(20)
    card.locator(".log-wrap summary").click()
    expect(card.locator(".log")).to_contain_text("Wrote D:\\Movies\\Tears of Steel.generated.srt")


# ------------------------------------------------------------------------------ history


def test_history_persists_and_clears(page: Page, mock: MockControl) -> None:
    expect(page.locator("#history-empty")).to_be_visible()
    expect(page.locator("#clear-history")).to_be_disabled()
    job_id = add_job(page, mock, TEARS)
    expect(job_card(page, job_id)).to_have_attribute("data-state", "done", timeout=15_000)
    expect(page.locator("#history .history-row")).to_have_count(1)
    page.reload()
    expect(page.locator("#history .history-row")).to_have_count(1)
    expect(page.locator("#history .history-row").first).to_have_attribute("data-job-id", job_id)
    page.click("#clear-history")
    expect(page.locator("#history .history-row")).to_have_count(0)
    expect(page.locator("#history-empty")).to_be_visible()
    assert mock.api("GET", "/api/history")["items"] == []


# ------------------------------------------------------------------------------ errors


def test_api_error_banner(page: Page, mock: MockControl) -> None:
    mock.fail("POST", "/api/pick", 409, "picker_busy", "A file window is already open.")
    page.click("#choose-media")
    error = page.locator("#error")
    expect(error).to_be_visible()
    expect(error).to_have_text("A file window is already open.")
    page.click("#error .banner-close")
    expect(error).to_be_hidden()

    # Sync without any subtitle → server error no_subtitles.
    choose(page, mock, TEARS)
    page.check("input[name=mode][value=sync]")
    page.click("#submit")
    expect(error).to_contain_text("No subtitle file to sync")
    expect(page.locator(".job")).to_have_count(0)


def test_submit_without_movie_explains(page: Page) -> None:
    page.click("#submit")
    expect(page.locator("#error")).to_contain_text("Choose a movie first")


def test_error_banner_visible_when_scrolled(browser, mock: MockControl) -> None:
    context = browser.new_context(viewport={"width": 1280, "height": 600})
    pg = context.new_page()
    try:
        pg.goto(mock.url + "/")
        expect(pg.locator("#conn")).to_have_attribute("data-conn", "live")
        pg.locator("#submit").scroll_into_view_if_needed()
        assert pg.evaluate("window.scrollY") > 200
        pg.click("#submit")
        expect(pg.locator("#error")).to_be_in_viewport()
    finally:
        context.close()


# ------------------------------------------------------------------------------ live events


def test_sse_reconnect_resyncs(page: Page, mock: MockControl) -> None:
    conn = page.locator("#conn")
    # Drop the stream and refuse the browser's first retry, so the page has to recover itself.
    assert mock.drop_events(refuse=1) >= 1
    injected = mock.inject_job("Missed while offline.mkv")
    expect(conn).not_to_have_attribute("data-conn", "live")
    expect(conn).to_have_attribute("data-conn", "live", timeout=15_000)
    # Re-fetched after reconnecting even though no event was ever sent for it.
    expect(job_card(page, injected)).to_have_attribute("data-state", "cancelled")
    expect(page.locator("#history .history-row").first).to_contain_text("Missed while offline.mkv")
    # Live events still flow on the new stream.
    job_id = add_job(page, mock, TEARS)
    expect(job_card(page, job_id)).to_have_attribute("data-state", "done", timeout=15_000)


# ------------------------------------------------------------------------------ layout


@pytest.mark.parametrize("width", [900, 600])
def test_responsive_no_horizontal_scroll(browser, mock: MockControl, width: int) -> None:
    context = browser.new_context(viewport={"width": width, "height": 900})
    pg = context.new_page()
    try:
        pg.goto(mock.url + "/")
        expect(pg.locator("#conn")).to_have_attribute("data-conn", "live")
        choose(
            pg, mock, r"D:\Movies\A movie with a very very long file name indeed (2024) 1080p.mkv"
        )
        pg.click("#submit")
        expect(pg.locator(".job").first).to_have_attribute("data-state", "done", timeout=15_000)
        overflow = pg.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 0
        # Cards stay inside their panel (no min-content blow-out from the preview table).
        panel = pg.locator(".queue-panel").bounding_box()
        card = pg.locator(".job").first.bounding_box()
        assert panel and card and card["x"] + card["width"] <= panel["x"] + panel["width"]
        for sel in ("#choose-media", "#submit", "#drop-zone", ".job .download"):
            box = pg.locator(sel).first.bounding_box()
            assert box and box["x"] >= 0 and box["x"] + box["width"] <= width
    finally:
        context.close()


def test_keyboard_focus_ring(page: Page) -> None:
    page.focus("#choose-media")
    page.keyboard.press("Tab")
    page.keyboard.press("Shift+Tab")
    outline = page.eval_on_selector("#choose-media", "el => getComputedStyle(el).outlineStyle")
    assert outline == "solid"


# ------------------------------------------------------------------------------ screenshots


def _shot(pg: Page, name: str) -> None:
    pg.evaluate("window.scrollTo(0, 0)")
    pg.wait_for_timeout(300)
    pg.screenshot(path=str(SCREENSHOTS / name), full_page=True)


def test_screenshots(browser, mock: MockControl) -> None:
    """Capture the UI in typical states for a visual check (written to screenshots/)."""
    SCREENSHOTS.mkdir(exist_ok=True)
    mock.reset(job_seconds=2.0)
    context = browser.new_context(viewport={"width": 1360, "height": 1000})
    pg = context.new_page()
    try:
        pg.goto(mock.url + "/")
        expect(pg.locator("#conn")).to_have_attribute("data-conn", "live")
        _shot(pg, "01-empty.png")

        done = add_job(pg, mock, TEARS)
        expect(job_card(pg, done)).to_have_attribute("data-state", "done", timeout=15_000)
        failed = add_job(pg, mock, r"D:\Movies\will-fail.mkv")
        expect(job_card(pg, failed)).to_have_attribute("data-state", "failed", timeout=15_000)
        mock.reset(job_seconds=40)
        pg.reload()
        expect(pg.locator("#conn")).to_have_attribute("data-conn", "live")
        running = add_job(pg, mock, r"D:\Movies\Sintel.mkv")
        add_job(pg, mock, r"D:\Movies\Big Buck Bunny.mp4")
        choose(pg, mock, r"D:\Movies\Cosmos Laundromat withsubs.mkv")
        expect(job_card(pg, running).locator(".eta")).to_be_visible()
        job_card(pg, running).locator(".log-wrap summary").click()
        pg.wait_for_timeout(1500)
        _shot(pg, "02-running-queue.png")

        mock.reset(job_seconds=2.0)
        pg.reload()
        expect(pg.locator("#conn")).to_have_attribute("data-conn", "live")
        done = add_job(pg, mock, TEARS)
        expect(job_card(pg, done)).to_have_attribute("data-state", "done", timeout=15_000)
        pg.fill("#path-input", r"D:\Docs\notes.txt")
        pg.click("#path-add")
        _shot(pg, "03-done-and-error.png")

        pg.set_viewport_size({"width": 900, "height": 1000})
        _shot(pg, "04-narrow-900.png")
    finally:
        context.close()
