"""Capture README screenshots of the real web UI (Tears of Steel only — CC BY 3.0 Blender).

Needs a *fresh* `subfix web --no-browser` on 127.0.0.1:8765 and `.cache/tmp/e2e/` media
(an uncached copy "Tears of Steel.mov" of the full film plus the 3-minute clips).
Usage: python tests/e2e/screenshots.py docs/images
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
E2E = ROOT / ".cache" / "tmp" / "e2e"
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "docs" / "images")
OUT.mkdir(parents=True, exist_ok=True)


def add(page, path: Path, mode: str) -> None:
    page.fill("#path-input", str(path))
    page.click("#path-add")
    expect(page.locator("#media-card")).to_contain_text(path.name, timeout=15_000)
    page.check(f"input[name=mode][value={mode}]")


with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
    page.goto("http://127.0.0.1:8765/")
    expect(page.locator("#conn")).to_have_attribute("data-conn", "live", timeout=15_000)

    film = E2E / "Tears of Steel.mov"
    add(page, film, "generate")
    page.screenshot(path=str(OUT / "web-main.png"))

    page.click("#submit")
    add(page, E2E / "tears_part2.mp4", "generate")
    page.click("#submit")
    add(page, E2E / "tears_part3.mp3", "generate")
    page.click("#submit")
    card = page.locator(".job").first
    job_id = card.get_attribute("data-job-id")
    token = page.get_attribute('meta[name="subfix-token"]', "content")
    # poll the API (fast) until the film is a third of the way through transcription
    deadline = time.time() + 300
    while time.time() < deadline:
        pr = page.request.get(
            f"http://127.0.0.1:8765/api/jobs/{job_id}", headers={"X-Subfix-Token": token}
        ).json()
        if pr["progress"]["stage"] == "transcribe":
            break
        time.sleep(0.05)
    card.locator("details.log-wrap > summary").click()  # open the live log
    time.sleep(0.5)
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(path=str(OUT / "web-progress.png"))

    # wait for all three, then show the finished film with its preview
    for _ in range(600):
        states = page.eval_on_selector_all(".job", "els => els.map(e => e.dataset.state)")
        if states and all(s in ("done", "failed", "cancelled") for s in states):
            break
        time.sleep(0.5)
    film_card = page.locator(".job", has_text="Tears of Steel.mov")
    if film_card.locator("details.log-wrap[open]").count():
        film_card.locator("details.log-wrap > summary").click()
    if not film_card.locator("details.preview-wrap[open]").count():
        film_card.locator("details.preview-wrap > summary").click()
    for other in page.locator(".job").all():
        if "Tears of Steel.mov" in other.inner_text():
            continue
        if other.locator("details.preview-wrap[open]").count():
            other.locator("details.preview-wrap > summary").click()
    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(0.4)
    page.screenshot(path=str(OUT / "web-result.png"))
    page.locator("#history").scroll_into_view_if_needed()
    page.screenshot(path=str(OUT / "web-history.png"))
    print("states:", states)
    browser.close()
