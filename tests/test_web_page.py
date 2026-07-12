"""Real-browser test for the index page: dropzone wiring (via the file input,
per the plan -- true OS drag-and-drop isn't simulated), the title-confirm
step, job-list polling, and the "open player" link appearing once a job
completes.

Runs a real uvicorn server (in a background thread, ephemeral 127.0.0.1
port) with a fake job runner injected -- never the real `stemlab` CLI/
separation stack -- and drives it over http with Playwright, the same
_chromium_available skip pattern tests/test_player_html.py uses.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from stemlab.web.app import create_app
from stemlab.web.jobs import safe_filename


class PageFakeRunner:
    def __init__(self, *, write_player: bool = True, returncode: int = 0, delay: float = 0.1) -> None:
        self.write_player = write_player
        self.returncode = returncode
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    def __call__(self, upload_path: Path, out_dir: Path, title: str, target: str, log_path: Path) -> int:
        self.calls.append({"title": title, "target": target})
        time.sleep(self.delay)  # keep the job visibly "running" for a moment
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("fake run\n", encoding="utf-8")
        if self.write_player:
            safe = safe_filename(title)
            pkg_dir = out_dir / safe
            pkg_dir.mkdir(parents=True, exist_ok=True)
            (pkg_dir / f"{safe}.{target}.player.html").write_text(
                "<html><body>player ok</body></html>", encoding="utf-8"
            )
        return self.returncode


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _running_server(app):
    import uvicorn

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn server failed to start in time"

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


_HAVE_BROWSER = _chromium_available()
_needs_browser = pytest.mark.skipif(not _HAVE_BROWSER, reason="playwright chromium not available")


@contextlib.contextmanager
def _open_page(base_url: str):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(base_url + "/")
            page.wait_for_function(
                "window.__stemlabWeb && typeof window.__stemlabWeb.getJobs === 'function'",
                timeout=10_000,
            )
            yield page
        finally:
            browser.close()


@_needs_browser
def test_upload_via_input_flows_through_to_a_player_link(tmp_path):
    runner = PageFakeRunner()
    app = create_app(tmp_path / "out", runner=runner)
    with _running_server(app) as base_url, _open_page(base_url) as page:
        # No jobs yet -> polling is idle.
        assert page.evaluate("window.__stemlabWeb.getJobs().length") == 0
        assert page.evaluate("window.__stemlabWeb.isPolling()") is False

        audio_dir = tmp_path / "src"
        audio_dir.mkdir()
        audio_path = audio_dir / "テスト曲.mp3"
        audio_path.write_bytes(b"fake-audio-bytes")

        # Drag-and-drop is wired to the same file, but Playwright drives the
        # hidden <input type=file> directly -- the plan explicitly allows
        # this ("D&D 配線(input 経由で可)").
        page.set_input_files("#sw-file-input", str(audio_path))

        # The confirm panel appears with the filename stem as the default title.
        page.wait_for_selector("#sw-confirm:not([hidden])")
        assert page.input_value("#sw-title-input") == "テスト曲"

        page.click("#sw-upload-btn")

        # Confirm panel closes; the job shows up in the list and polling
        # kicks in while it's queued/running.
        page.wait_for_function("document.getElementById('sw-confirm').hidden === true")
        page.wait_for_function("window.__stemlabWeb.getJobs().length === 1")
        page.wait_for_function("window.__stemlabWeb.isPolling() === true", timeout=5_000)

        badge = page.locator(".sw-badge").first
        assert badge.text_content() in ("待機中", ) or "処理中" in badge.text_content()

        # Once the fake runner finishes, a "プレイヤーを開く" link appears
        # and polling stops (no more active jobs).
        page.wait_for_selector("a.sw-open-link", timeout=10_000)
        link = page.get_attribute("a.sw-open-link", "href")
        assert link == "/packages/テスト曲/テスト曲.guitar.player.html"
        assert page.get_attribute("a.sw-open-link", "target") == "_blank"

        page.wait_for_function("window.__stemlabWeb.isPolling() === false", timeout=5_000)

        final_badge = page.locator(".sw-badge").first
        assert final_badge.text_content() == "完了"


@_needs_browser
def test_dragover_highlights_dropzone(tmp_path):
    app = create_app(tmp_path / "out", runner=PageFakeRunner())
    with _running_server(app) as base_url, _open_page(base_url) as page:
        page.evaluate(
            "document.getElementById('sw-dropzone')"
            ".dispatchEvent(new Event('dragover', {bubbles: true, cancelable: true}))"
        )
        assert "is-dragover" in page.get_attribute("#sw-dropzone", "class")

        page.evaluate(
            "document.getElementById('sw-dropzone')"
            ".dispatchEvent(new Event('dragleave', {bubbles: true, cancelable: true}))"
        )
        assert "is-dragover" not in page.get_attribute("#sw-dropzone", "class")


@_needs_browser
def test_failed_job_shows_collapsible_log_tail(tmp_path):
    runner = PageFakeRunner(write_player=False, returncode=1)
    app = create_app(tmp_path / "out", runner=runner)
    with _running_server(app) as base_url, _open_page(base_url) as page:
        audio_dir = tmp_path / "src"
        audio_dir.mkdir()
        audio_path = audio_dir / "fail.mp3"
        audio_path.write_bytes(b"whatever")

        page.set_input_files("#sw-file-input", str(audio_path))
        page.wait_for_selector("#sw-confirm:not([hidden])")
        page.click("#sw-upload-btn")

        page.wait_for_selector(".sw-badge-error", timeout=10_000)
        assert "失敗" in page.locator(".sw-badge-error").first.text_content()

        details = page.locator("details.sw-error-details")
        assert details.count() == 1
        assert details.get_attribute("open") is None  # collapsed by default
        details.locator("summary").click()
        assert "fake run" in details.locator("pre").text_content()


@_needs_browser
def test_polling_recovers_after_a_failed_fetch(tmp_path):
    """One failed /api/jobs poll (e.g. the server restarting mid-job -- the
    exact recovery scenario the job store is built for) must not kill the
    poll chain: the page has to keep retrying and eventually show the done
    state."""
    runner = PageFakeRunner(delay=1.5)
    app = create_app(tmp_path / "out", runner=runner)
    with _running_server(app) as base_url, _open_page(base_url) as page:
        audio_path = tmp_path / "song.mp3"
        audio_path.write_bytes(b"fake-audio-bytes")
        page.set_input_files("#sw-file-input", str(audio_path))
        page.wait_for_selector("#sw-confirm:not([hidden])")
        page.click("#sw-upload-btn")
        page.wait_for_function("window.__stemlabWeb.getJobs().length === 1")

        # Fail exactly one poll while the job is still running, then let the
        # rest through.
        state = {"failed": False}

        def route_handler(route):
            if not state["failed"]:
                state["failed"] = True
                route.abort()
            else:
                route.continue_()

        page.route("**/api/jobs", route_handler)
        page.wait_for_function(
            "window.__stemlabWeb.getJobs().some(function (j) { return j.status === 'done'; })",
            timeout=15_000,
        )
        assert state["failed"], "the test never actually exercised a failed fetch"
