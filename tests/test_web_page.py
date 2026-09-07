"""Real-browser test for the index page: dropzone wiring (via the file input,
per the plan -- true OS drag-and-drop isn't simulated), the title-confirm
step, job-list polling, and the "open player" link appearing once a job
completes.

Runs a real uvicorn server (in a background thread, ephemeral 127.0.0.1
port) with a fake job runner injected -- never the real `bunri` CLI/
separation stack -- and drives it over http with Playwright, the same
_chromium_available skip pattern tests/test_player_html.py uses.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from bunri.web.app import create_app
from bunri.web.jobs import safe_filename


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
            for suffix in ("", ".backing"):
                for audio_format in ("mp3", "wav"):
                    (pkg_dir / f"{safe}.{target}{suffix}.{audio_format}").write_bytes(
                        b"fake-audio"
                    )
        return self.returncode


def _bound_socket() -> socket.socket:
    """Bind an ephemeral loopback port and hand the socket to uvicorn.

    Picking a free port number and letting uvicorn bind it later leaves a
    window where another test worker (pytest-xdist) can grab the same port.
    Binding here and passing the socket in closes that window entirely.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    return sock


@contextlib.contextmanager
def _running_server(app):
    import uvicorn

    sock = _bound_socket()
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()

    deadline = time.time() + 10
    while not server.started and thread.is_alive() and time.time() < deadline:
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
def _open_page(base_url: str, *, before_goto: Callable[[Any], None] | None = None):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            if before_goto is not None:
                before_goto(page)
            page.goto(base_url + "/")
            page.wait_for_function(
                "window.__bunriWeb && typeof window.__bunriWeb.getJobs === 'function'",
                timeout=10_000,
            )
            yield page
        finally:
            browser.close()


def _upload_from_page(page, audio_path: Path, *, targets: tuple[str, ...] = ("guitar",)) -> None:
    audio_path.write_bytes(b"fake-audio-bytes")
    page.set_input_files("#sw-file-input", str(audio_path))
    page.wait_for_selector("#sw-confirm:not([hidden])")
    for value in ("guitar", "bass", "drums", "vocals", "piano"):
        checkbox = page.locator(f'input[name="targets"][value="{value}"]')
        if value in targets:
            checkbox.check()
        else:
            checkbox.uncheck()
    page.click("#sw-upload-btn")


def _write_pocket_all_record(
    out_dir: Path,
    *,
    job_id: str,
    status: str,
    error: str | None,
    progress: dict[str, Any],
) -> None:
    jobs_dir = out_dir / "web" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    finished_at = None if status in ("queued", "running") else "2026-09-05T00:00:02+00:00"
    (jobs_dir / f"{job_id}.json").write_text(json.dumps({
        "id": job_id,
        "kind": "pocket_all",
        "status": status,
        "created_at": "2026-09-05T00:00:00+00:00",
        "started_at": "2026-09-05T00:00:01+00:00" if status != "queued" else None,
        "finished_at": finished_at,
        "error": error,
        "progress": progress,
        "result": None,
    }), encoding="utf-8")


@_needs_browser
def test_done_job_with_empty_downloads_does_not_render_download_controls(tmp_path):
    out_dir = tmp_path / "out"
    jobs_dir = out_dir / "web" / "jobs"
    jobs_dir.mkdir(parents=True)
    job_id = "j-done-without-package"
    record = {
        "id": job_id,
        "digest": "done-without-package",
        "title": "No Package",
        "target": "guitar",
        "status": "done",
        "created_at": "2026-08-24T00:00:00+00:00",
        "started_at": "2026-08-24T00:00:01+00:00",
        "finished_at": "2026-08-24T00:00:02+00:00",
        "error": None,
        "package": None,
        "log": None,
        "upload": None,
    }
    (jobs_dir / f"{job_id}.json").write_text(json.dumps(record), encoding="utf-8")

    app = create_app(out_dir, runner=PageFakeRunner())
    with _running_server(app) as base_url, _open_page(base_url) as page:
        page.wait_for_function(
            "window.__bunriWeb.getJobs().length === 1 && "
            "window.__bunriWeb.getJobs()[0].status === 'done'"
        )
        job = page.evaluate("window.__bunriWeb.getJobs()[0]")
        assert job["downloads"] == []
        target = page.locator('.sw-target-block[data-target="guitar"]')
        assert target.count() == 1
        assert target.locator("button.sw-download-toggle").count() == 0
        assert target.locator(".sw-downloads").count() == 0
        assert target.locator(".sw-download-group").count() == 0
        assert target.locator("a.sw-download-link").count() == 0


@_needs_browser
def test_pocket_error_job_renders_failure_badge_and_safe_message(tmp_path, monkeypatch):
    import base64

    import bunri.web.app as app_module
    from bunri.package_metadata import (
        PackageMetadata,
        SourceIdentity,
        TargetMetadata,
        write_package_metadata,
    )
    from bunri.pocket.config import PocketConfig, save_config

    out_dir = tmp_path / "out"
    jobs_dir = out_dir / "web" / "jobs"
    jobs_dir.mkdir(parents=True)
    digest = "a" * 40
    package = out_dir / "Song"
    package.mkdir()
    write_package_metadata(
        package / ".bunri-package.json",
        PackageMetadata(
            "Song",
            "Song",
            SourceIdentity("sha1", digest, digest[:12]),
            (TargetMetadata("guitar", ("mp3",)),),
        ),
    )
    for suffix in ("original.mp3", "guitar.mp3", "guitar.backing.mp3"):
        (package / f"Song.{suffix}").write_bytes(b"audio")
    (jobs_dir / "j-separate.json").write_text(json.dumps({
        "id": "j-separate",
        "digest": digest,
        "title": "Song",
        "target": "guitar",
        "status": "done",
        "created_at": "2026-09-05T00:00:00+00:00",
        "started_at": "2026-09-05T00:00:01+00:00",
        "finished_at": "2026-09-05T00:00:02+00:00",
        "error": None,
        "package": "Song/Song.guitar.player.html",
        "log": "web/logs/j-separate.log",
        "upload": "web/uploads/song.mp3",
    }), encoding="utf-8")
    safe_message = "Pocket の同期に失敗しました。後で再実行してください。"
    (jobs_dir / "j-pocket.json").write_text(json.dumps({
        "id": "j-pocket",
        "kind": "pocket_single",
        "status": "error",
        "created_at": "2026-09-05T00:00:03+00:00",
        "started_at": "2026-09-05T00:00:04+00:00",
        "finished_at": "2026-09-05T00:00:05+00:00",
        "error": safe_message,
        "pocket_song_id": digest[:12],
        "pocket_digest": digest,
        "pocket_safe_name": "Song",
        "progress": None,
        "result": None,
    }), encoding="utf-8")
    token = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")
    save_config(out_dir, PocketConfig("https://example.invalid", token))

    class OfflineShelf:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_json(self, _path):
            return None

    monkeypatch.setattr(app_module, "PocketHTTPClient", OfflineShelf)
    app = create_app(out_dir, runner=PageFakeRunner())
    with _running_server(app) as base_url, _open_page(base_url) as page:
        page.wait_for_selector(".sw-pocket-row", state="attached")
        page.locator("button.sw-job-toggle").click()
        badge = page.locator(".sw-pocket-row .sw-badge")
        page.wait_for_function(
            "document.querySelector('.sw-pocket-row .sw-badge').textContent === '失敗'"
        )
        assert badge.get_attribute("class").endswith("sw-badge-error")
        assert page.locator(".sw-pocket-message").text_content() == safe_message


@_needs_browser
def test_pocket_connection_and_checking_render_before_status_responds(tmp_path):
    import base64

    from bunri.pocket.config import PocketConfig, save_config

    out_dir = tmp_path / "out"
    jobs_dir = out_dir / "web" / "jobs"
    jobs_dir.mkdir(parents=True)
    (jobs_dir / "j-separate.json").write_text(json.dumps({
        "id": "j-separate",
        "digest": "a" * 40,
        "title": "Song",
        "target": "guitar",
        "status": "done",
        "created_at": "2026-09-05T00:00:00+00:00",
        "started_at": "2026-09-05T00:00:01+00:00",
        "finished_at": "2026-09-05T00:00:02+00:00",
        "error": None,
        "package": "Song/Song.guitar.player.html",
        "log": "web/logs/j-separate.log",
        "upload": "web/uploads/song.mp3",
    }), encoding="utf-8")
    token = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")
    save_config(out_dir, PocketConfig("https://example.invalid", token))

    def hold_status(page):
        page.route("**/api/pocket/status", lambda _route: None)

    app = create_app(out_dir, runner=PageFakeRunner())
    with _running_server(app) as base_url, _open_page(
        base_url, before_goto=hold_status
    ) as page:
        page.wait_for_selector("#sw-pocket-banner:not([hidden])")
        page.wait_for_selector(".sw-pocket-row", state="attached")
        page.locator("button.sw-job-toggle").click()
        badge = page.locator(".sw-pocket-row .sw-badge")
        assert badge.is_visible()
        assert badge.text_content() == "確認中"


@_needs_browser
def test_separation_completion_refreshes_pocket_status_for_new_song(tmp_path):
    import base64

    from bunri.pocket.config import PocketConfig, save_config

    out_dir = tmp_path / "out"
    token = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")
    save_config(out_dir, PocketConfig("https://example.invalid", token))
    status_state = {"calls": 0, "web_song_id": None}

    def serve_pocket_status(page):
        def handler(route):
            status_state["calls"] += 1
            web_song_id = status_state["web_song_id"]
            songs = [] if web_song_id is None else [{
                "web_song_id": web_song_id,
                "song_id": "a" * 12,
                "title": "New Song",
                "safe_name": "New Song",
                "state": "not_synced",
                "can_sync": True,
                "message": None,
                "conflict": False,
            }]
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "connected": True,
                    "target_count": len(songs),
                    "package_count": len(songs),
                    "songs": songs,
                }),
            )

        page.route("**/api/pocket/status", handler)

    app = create_app(out_dir, runner=PageFakeRunner(delay=1.0))
    with _running_server(app) as base_url, _open_page(
        base_url, before_goto=serve_pocket_status
    ) as page:
        page.wait_for_function("document.getElementById('sw-pocket-controls').hidden === false")
        assert status_state["calls"] == 1

        _upload_from_page(page, tmp_path / "new-song.mp3")
        page.wait_for_function(
            "window.__bunriWeb.getJobs()[0] && "
            "['queued', 'running'].includes(window.__bunriWeb.getJobs()[0].status)",
            timeout=5_000,
        )
        status_state["web_song_id"] = page.evaluate("window.__bunriWeb.getSongs()[0].id")

        page.wait_for_function(
            "window.__bunriWeb.getJobs()[0].status === 'done' && "
            "document.querySelector('.sw-pocket-row .sw-badge').textContent === '未同期'",
            timeout=10_000,
        )
        assert status_state["calls"] == 2
        assert page.locator(".sw-pocket-row button").is_enabled()
        page.wait_for_function("window.__bunriWeb.isPolling() === false", timeout=5_000)


@_needs_browser
def test_single_pocket_upload_button_recovers_after_409(tmp_path):
    import base64

    from bunri.pocket.config import PocketConfig, save_config
    from bunri.web.jobs import song_id

    out_dir = tmp_path / "out"
    jobs_dir = out_dir / "web" / "jobs"
    jobs_dir.mkdir(parents=True)
    digest = "b" * 40
    (jobs_dir / "j-separate.json").write_text(json.dumps({
        "id": "j-separate",
        "digest": digest,
        "title": "Retry Song",
        "target": "guitar",
        "status": "done",
        "created_at": "2026-09-05T00:00:00+00:00",
        "started_at": "2026-09-05T00:00:01+00:00",
        "finished_at": "2026-09-05T00:00:02+00:00",
        "error": None,
        "package": "Retry Song/Retry Song.guitar.player.html",
        "log": "web/logs/j-separate.log",
        "upload": "web/uploads/retry.mp3",
    }), encoding="utf-8")
    token = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")
    save_config(out_dir, PocketConfig("https://example.invalid", token))
    request_state = {"posts": 0}

    def mock_pocket(page):
        page.route("**/api/pocket/status", lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "connected": True,
                "target_count": 1,
                "package_count": 1,
                "songs": [{
                    "web_song_id": song_id(digest),
                    "song_id": digest[:12],
                    "title": "Retry Song",
                    "safe_name": "Retry Song",
                    "state": "not_synced",
                    "can_sync": True,
                    "message": None,
                    "conflict": False,
                }],
            }),
        ))

        def reject_sync(route):
            request_state["posts"] += 1
            route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps({"detail": "別の Pocket 同期が実行中です。"}),
            )

        page.route("**/api/pocket/sync/*", reject_sync)
        page.on("dialog", lambda dialog: dialog.dismiss())

    app = create_app(out_dir, runner=PageFakeRunner())
    with _running_server(app) as base_url, _open_page(
        base_url, before_goto=mock_pocket
    ) as page:
        page.wait_for_function(
            "document.querySelector('.sw-pocket-row button') && "
            "!document.querySelector('.sw-pocket-row button').disabled"
        )
        page.locator("button.sw-job-toggle").click()
        button = page.locator(".sw-pocket-row button")

        with page.expect_request("**/api/pocket/sync/*"):
            button.click()
        page.wait_for_function(
            "document.querySelector('.sw-pocket-row button') && "
            "!document.querySelector('.sw-pocket-row button').disabled"
        )
        assert request_state["posts"] == 1

        with page.expect_request("**/api/pocket/sync/*"):
            button.click()
        assert request_state["posts"] == 2


@_needs_browser
def test_pocket_all_failure_summary_renders_without_song_cards(tmp_path, monkeypatch):
    import base64

    import bunri.web.jobs as jobs_module
    from bunri.pocket.config import PocketConfig, save_config
    from bunri.pocket.service import BatchItem, BatchResult

    out_dir = tmp_path / "out"
    token = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")
    save_config(out_dir, PocketConfig("https://example.invalid", token))
    safe_message = "Pocket の同期に失敗しました。後で再実行してください。"

    def fail_sync_all(*_args, progress=None, **_kwargs):
        batch = BatchResult(
            total=3,
            legacy=[f"Legacy Song {index}" for index in range(101)],
            items=[
                BatchItem("Done Song", "done"),
                BatchItem("Failed Song", "error", error=safe_message),
                BatchItem("Pending Song", "pending"),
            ],
        )
        if progress is not None:
            progress(batch, None)
        return batch

    monkeypatch.setattr(jobs_module, "sync_all", fail_sync_all)
    app = create_app(out_dir, runner=PageFakeRunner())
    with _running_server(app) as base_url, _open_page(base_url) as page:
        page.wait_for_selector("#sw-pocket-controls:not([hidden])")
        assert page.locator("li.sw-job").count() == 0
        page.locator("#sw-pocket-all").click()
        page.wait_for_function(
            "document.getElementById('sw-pocket-summary').textContent.startsWith("
            "'全曲アップロード失敗:')",
            timeout=10_000,
        )

        summary = page.locator("#sw-pocket-summary")
        assert summary.is_visible()
        assert summary.text_content() == (
            "全曲アップロード失敗: "
            f"{safe_message}（完了 1件 / 失敗 1件 / 未実行 1件）（再生成が必要 101件）"
        )
        assert summary.get_attribute("class").endswith("is-error")


@_needs_browser
def test_reloaded_page_resumes_running_batch_and_renders_its_result(tmp_path, monkeypatch):
    import base64

    import bunri.web.jobs as jobs_module
    from bunri.pocket.config import PocketConfig, save_config
    from bunri.pocket.service import BatchItem, BatchResult

    out_dir = tmp_path / "out"
    token = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")
    save_config(out_dir, PocketConfig("https://example.invalid", token))
    job_id = "j-pocket-reload"
    _write_pocket_all_record(
        out_dir,
        job_id=job_id,
        status="running",
        error=None,
        progress={
            "total": 2,
            "completed": 0,
            "current": "First Song",
            "legacy": [],
            "done": [],
            "failed": [],
            "pending": ["First Song", "Second Song"],
        },
    )
    started = threading.Event()
    release = threading.Event()

    def finish_sync_all(*_args, progress=None, **_kwargs):
        started.set()
        release.wait(timeout=10)
        batch = BatchResult(
            total=2,
            items=[BatchItem("First Song", "done"), BatchItem("Second Song", "done")],
        )
        if progress is not None:
            progress(batch, None)
        return batch

    monkeypatch.setattr(jobs_module, "sync_all", finish_sync_all)
    app = create_app(out_dir, runner=PageFakeRunner())
    assert started.wait(timeout=5)
    try:
        with _running_server(app) as base_url, _open_page(base_url) as page:
            page.reload()
            page.wait_for_function(
                "window.__bunriWeb && window.__bunriWeb.isPolling() === true",
                timeout=10_000,
            )
            release.set()
            page.wait_for_function(
                "document.getElementById('sw-pocket-summary').textContent === "
                "'全曲アップロード完了: 2件'",
                timeout=10_000,
            )
            assert page.locator("#sw-pocket-summary").is_visible()
            page.wait_for_function("window.__bunriWeb.isPolling() === false", timeout=10_000)
    finally:
        release.set()


@_needs_browser
def test_reloaded_page_resumes_polling_before_the_remote_check_answers(tmp_path, monkeypatch):
    """An unreachable shelf costs one timeout per song. The reloaded page must
    pick a running batch back up without waiting for that."""
    import base64

    import bunri.web.app as app_module
    import bunri.web.jobs as jobs_module
    from bunri.pocket.config import PocketConfig, save_config
    from bunri.pocket.service import BatchItem, BatchResult

    out_dir = tmp_path / "out"
    token = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")
    save_config(out_dir, PocketConfig("https://example.invalid", token))
    _write_pocket_all_record(
        out_dir,
        job_id="j-pocket-slow-remote",
        status="running",
        error=None,
        progress={
            "total": 2,
            "completed": 0,
            "current": "First Song",
            "legacy": [],
            "done": [],
            "failed": [],
            "pending": ["First Song", "Second Song"],
        },
    )
    started = threading.Event()
    release = threading.Event()

    def blocking_sync_all(*_args, progress=None, **_kwargs):
        started.set()
        release.wait(timeout=30)
        batch = BatchResult(
            total=2,
            items=[BatchItem("First Song", "done"), BatchItem("Second Song", "done")],
        )
        if progress is not None:
            progress(batch, None)
        return batch

    def unreachable_inspect(*_args, **_kwargs):
        release.wait(timeout=30)
        return ()

    monkeypatch.setattr(jobs_module, "sync_all", blocking_sync_all)
    monkeypatch.setattr(app_module, "inspect_packages", unreachable_inspect)
    app = create_app(out_dir, runner=PageFakeRunner())
    assert started.wait(timeout=5)
    try:
        with _running_server(app) as base_url, _open_page(base_url) as page:
            page.wait_for_function(
                "window.__bunriWeb.isPolling() === true", timeout=5_000
            )
            assert page.evaluate(
                "document.getElementById('sw-pocket-count').textContent"
            ) == "全曲アップロード: 0/2（First Song）"
    finally:
        release.set()


@_needs_browser
def test_reloaded_preflight_failure_summary_omits_empty_counts(tmp_path):
    import base64

    from bunri.pocket.config import PocketConfig, save_config

    out_dir = tmp_path / "out"
    token = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")
    save_config(out_dir, PocketConfig("https://example.invalid", token))
    safe_message = "ローカルパッケージを安全に同期できません。"
    _write_pocket_all_record(
        out_dir,
        job_id="j-pocket-preflight",
        status="error",
        error=safe_message,
        progress={
            "total": 0,
            "completed": 0,
            "current": None,
            "legacy": [],
            "done": [],
            "failed": [],
            "pending": [],
        },
    )

    app = create_app(out_dir, runner=PageFakeRunner())
    with _running_server(app) as base_url, _open_page(base_url) as page:
        page.wait_for_function(
            "document.getElementById('sw-pocket-summary').textContent === "
            "'全曲アップロード失敗: ローカルパッケージを安全に同期できません。'",
            timeout=10_000,
        )
        summary = page.locator("#sw-pocket-summary")
        assert summary.is_visible()
        assert "0件" not in summary.text_content()


@_needs_browser
def test_upload_via_input_flows_through_to_a_player_link(tmp_path):
    runner = PageFakeRunner(delay=1.0)
    app = create_app(tmp_path / "out", runner=runner)
    with _running_server(app) as base_url, _open_page(base_url) as page:
        # No jobs yet -> polling is idle.
        assert page.evaluate("window.__bunriWeb.getJobs().length") == 0
        assert page.evaluate("window.__bunriWeb.isPolling()") is False

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
        assert page.locator('input[name="targets"]:checked').count() == 1
        assert page.locator('input[name="targets"]:checked').get_attribute("value") == "guitar"

        page.click("#sw-upload-btn")

        # Confirm panel closes; the job shows up in the list and polling
        # kicks in while it's queued/running.
        page.wait_for_function("document.getElementById('sw-confirm').hidden === true")
        page.wait_for_function("window.__bunriWeb.getJobs().length === 1")
        page.wait_for_function("window.__bunriWeb.isPolling() === true", timeout=5_000)

        # Active songs start expanded.
        toggle = page.locator("button.sw-job-toggle")
        assert toggle.get_attribute("aria-expanded") == "true"
        assert page.locator(".sw-job-content").is_visible()

        badge = page.locator(".sw-target-row .sw-badge").first
        assert badge.text_content() in ("待機中", ) or "処理中" in badge.text_content()
        assert page.locator("a.sw-download-link").count() == 0

        # Once the fake runner finishes, a "プレイヤーを開く" link appears
        # and polling stops (no more active jobs).
        page.wait_for_selector("a.sw-open-link", timeout=10_000)
        link = page.get_attribute("a.sw-open-link", "href")
        # package_url is percent-encoded (see web/app.py's _serialize_job).
        assert link == (
            "/packages/%E3%83%86%E3%82%B9%E3%83%88%E6%9B%B2/"
            "%E3%83%86%E3%82%B9%E3%83%88%E6%9B%B2.guitar.player.html"
        )
        assert page.get_attribute("a.sw-open-link", "target") == "_blank"
        assert page.get_attribute("a.sw-open-link", "rel") == "noopener"
        assert page.locator("a.sw-open-link").inner_text() == "プレイヤーを開く"

        download_toggle = page.locator("button.sw-download-toggle")
        assert download_toggle.count() == 1
        assert download_toggle.get_attribute("aria-expanded") == "false"
        assert download_toggle.text_content() == "ダウンロード▼"
        assert download_toggle.locator("svg.sw-download-icon use").get_attribute("href") == (
            "#sw-download-icon"
        )
        downloads = page.locator(".sw-downloads")
        assert download_toggle.get_attribute("aria-controls") == downloads.get_attribute("id")
        assert downloads.get_attribute("hidden") == ""
        assert downloads.is_hidden()
        download_links = page.locator("a.sw-download-link")
        assert download_links.count() == 4
        assert download_links.evaluate_all("links => links.every(link => !link.checkVisibility())")

        download_toggle.click()
        assert download_toggle.get_attribute("aria-expanded") == "true"
        assert downloads.get_attribute("hidden") is None
        assert downloads.is_visible()
        assert download_links.all_text_contents() == ["mp3", "wav", "mp3", "wav"]
        download_groups = page.locator(".sw-download-group")
        assert download_groups.count() == 2
        assert download_groups.locator(".sw-download-label").all_text_contents() == [
            "ギターのみ", "ギターなし",
        ]
        assert download_groups.locator("svg.sw-download-icon").count() == 0
        assert download_links.evaluate_all(
            "links => links.map(link => link.getAttribute('href'))"
        ) == [
            (
                "/packages/%E3%83%86%E3%82%B9%E3%83%88%E6%9B%B2/"
                "%E3%83%86%E3%82%B9%E3%83%88%E6%9B%B2.guitar.mp3"
            ),
            (
                "/packages/%E3%83%86%E3%82%B9%E3%83%88%E6%9B%B2/"
                "%E3%83%86%E3%82%B9%E3%83%88%E6%9B%B2.guitar.wav"
            ),
            (
                "/packages/%E3%83%86%E3%82%B9%E3%83%88%E6%9B%B2/"
                "%E3%83%86%E3%82%B9%E3%83%88%E6%9B%B2.guitar.backing.mp3"
            ),
            (
                "/packages/%E3%83%86%E3%82%B9%E3%83%88%E6%9B%B2/"
                "%E3%83%86%E3%82%B9%E3%83%88%E6%9B%B2.guitar.backing.wav"
            ),
        ]
        assert download_links.evaluate_all(
            "links => links.map(link => link.getAttribute('download'))"
        ) == [
            "テスト曲_ギターのみ.mp3",
            "テスト曲_ギターのみ.wav",
            "テスト曲_ギターなし.mp3",
            "テスト曲_ギターなし.wav",
        ]
        assert download_links.evaluate_all(
            "links => links.map(link => link.getAttribute('aria-label'))"
        ) == [
            "ギターのみをmp3でダウンロード",
            "ギターのみをwavでダウンロード",
            "ギターなしをmp3でダウンロード",
            "ギターなしをwavでダウンロード",
        ]
        assert "original.mp3" not in page.locator("li.sw-job").inner_html()

        page.evaluate(
            "window.__downloadToggleBeforeRefresh = "
            "document.querySelector('button.sw-download-toggle')"
        )
        page.evaluate("window.__bunriWeb.refresh()")
        page.wait_for_function(
            "window.__downloadToggleBeforeRefresh && "
            "!window.__downloadToggleBeforeRefresh.isConnected"
        )
        download_toggle = page.locator("button.sw-download-toggle")
        downloads = page.locator(".sw-downloads")
        assert download_toggle.get_attribute("aria-expanded") == "true"
        assert downloads.is_visible()

        page.wait_for_function("window.__bunriWeb.isPolling() === false", timeout=5_000)

        final_badge = page.locator(".sw-target-row .sw-badge").first
        assert final_badge.text_content() == "完了"

        # Finishing does not collapse a song that was already open. A page
        # reload forgets in-memory state and applies the initial-state rule,
        # so this completed-only song then starts collapsed.
        assert toggle.get_attribute("aria-expanded") == "true"
        page.reload()
        page.wait_for_function("window.__bunriWeb.getJobs().length === 1")
        toggle = page.locator("button.sw-job-toggle")
        assert toggle.get_attribute("aria-expanded") == "false"
        assert page.locator(".sw-job-content").is_hidden()
        summary_badge = page.locator(".sw-job-summary .sw-summary-badge")
        assert summary_badge.is_visible()
        assert summary_badge.text_content() == "ギター"
        assert summary_badge.get_attribute("aria-label") == "ギター 完了"


@_needs_browser
def test_song_toggle_survives_polling_redraw(tmp_path):
    runner = PageFakeRunner(delay=4.0)
    app = create_app(tmp_path / "out", runner=runner)
    with _running_server(app) as base_url, _open_page(base_url) as page:
        audio_path = tmp_path / "long-song.mp3"
        audio_path.write_bytes(b"fake-audio-bytes")
        page.set_input_files("#sw-file-input", str(audio_path))
        page.wait_for_selector("#sw-confirm:not([hidden])")
        page.click("#sw-upload-btn")
        page.wait_for_function("window.__bunriWeb.getJobs().length === 1")
        page.wait_for_function(
            "window.__bunriWeb.getJobs()[0].status === 'running'",
            timeout=5_000,
        )

        toggle = page.locator("button.sw-job-toggle")
        assert toggle.get_attribute("aria-expanded") == "true"
        toggle.click()
        assert toggle.get_attribute("aria-expanded") == "false"
        assert page.locator(".sw-job-content").is_hidden()
        summary_badge = page.locator(".sw-job-summary .sw-summary-badge")
        assert summary_badge.text_content().startswith("ギター 処理中 (")
        assert summary_badge.text_content().endswith(")")

        page.evaluate(
            "window.__toggleBeforePoll = document.querySelector('button.sw-job-toggle')"
        )
        page.wait_for_function(
            "window.__toggleBeforePoll && !window.__toggleBeforePoll.isConnected",
            timeout=6_000,
        )
        assert toggle.get_attribute("aria-expanded") == "false"

        # Clicking the rebuilt heading opens and closes it in both directions.
        toggle.click()
        assert toggle.get_attribute("aria-expanded") == "true"
        toggle.click()
        assert toggle.get_attribute("aria-expanded") == "false"

        # A native button is keyboard-operable; Enter opens it again.
        toggle.press("Enter")
        assert toggle.get_attribute("aria-expanded") == "true"


@_needs_browser
def test_song_toggle_focus_survives_polling_redraw(tmp_path):
    runner = PageFakeRunner(delay=4.0)
    app = create_app(tmp_path / "out", runner=runner)
    with _running_server(app) as base_url, _open_page(base_url) as page:
        audio_path = tmp_path / "focus-song.mp3"
        audio_path.write_bytes(b"fake-audio-bytes")
        page.set_input_files("#sw-file-input", str(audio_path))
        page.wait_for_selector("#sw-confirm:not([hidden])")
        page.click("#sw-upload-btn")
        page.wait_for_function("window.__bunriWeb.getJobs().length === 1")
        page.wait_for_function(
            "window.__bunriWeb.getJobs()[0].status === 'running'",
            timeout=5_000,
        )

        toggle = page.locator("button.sw-job-toggle")
        song_id = page.locator("li.sw-job").get_attribute("data-song-id")
        toggle.focus()
        assert toggle.evaluate("button => document.activeElement === button")

        page.evaluate(
            "window.__focusedToggleBeforePoll = "
            "document.querySelector('button.sw-job-toggle')"
        )
        page.wait_for_function(
            "window.__focusedToggleBeforePoll && "
            "!window.__focusedToggleBeforePoll.isConnected",
            timeout=6_000,
        )
        assert page.evaluate(
            "songId => {"
            "  const active = document.activeElement;"
            "  const card = active && active.closest('li.sw-job');"
            "  return active && active.matches('button.sw-job-toggle') && "
            "    card && card.dataset.songId === songId;"
            "}",
            song_id,
        )

        before = toggle.get_attribute("aria-expanded")
        page.keyboard.press("Enter")
        assert toggle.get_attribute("aria-expanded") != before


@_needs_browser
def test_download_toggle_focus_survives_partial_completion_polling_redraw(tmp_path):
    class TargetDelayRunner(PageFakeRunner):
        def __call__(
            self,
            upload_path: Path,
            out_dir: Path,
            title: str,
            target: str,
            log_path: Path,
        ) -> int:
            self.delay = 0.1 if target == "guitar" else 5.0
            return super().__call__(upload_path, out_dir, title, target, log_path)

    app = create_app(tmp_path / "out", runner=TargetDelayRunner())
    with _running_server(app) as base_url, _open_page(base_url) as page:
        _upload_from_page(
            page,
            tmp_path / "partial-focus-song.mp3",
            targets=("guitar", "vocals"),
        )
        page.wait_for_function(
            "window.__bunriWeb.getJobs().some(function (j) { "
            "  return j.target === 'guitar' && j.status === 'done'; "
            "}) && window.__bunriWeb.getJobs().some(function (j) { "
            "  return j.target === 'vocals' && j.status === 'running'; "
            "})",
            timeout=5_000,
        )

        download_toggle = page.locator(
            '.sw-target-block[data-target="guitar"] button.sw-download-toggle'
        )
        download_toggle.click()
        download_toggle.focus()
        job_id = download_toggle.get_attribute("data-job-id")
        assert job_id
        assert download_toggle.get_attribute("aria-expanded") == "true"
        assert download_toggle.evaluate("button => document.activeElement === button")

        page.evaluate(
            "window.__focusedDownloadToggleBeforePoll = "
            "document.querySelector("
            "  '.sw-target-block[data-target=\"guitar\"] button.sw-download-toggle'"
            ")"
        )
        page.wait_for_function(
            "window.__focusedDownloadToggleBeforePoll && "
            "!window.__focusedDownloadToggleBeforePoll.isConnected",
            timeout=4_000,
        )
        assert page.evaluate(
            "jobId => {"
            "  const active = document.activeElement;"
            "  return active && active.matches('button.sw-download-toggle') && "
            "    active.dataset.jobId === jobId && "
            "    active.getAttribute('aria-expanded') === 'true';"
            "}",
            job_id,
        )


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

        page.wait_for_selector(".sw-target-row .sw-badge-error", timeout=10_000)
        assert "失敗" in page.locator(".sw-target-row .sw-badge-error").text_content()

        details = page.locator("details.sw-error-details")
        assert details.count() == 1
        assert details.get_attribute("open") is None  # collapsed by default
        details.locator("summary").click()
        assert "fake run" in details.locator("pre").text_content()


@_needs_browser
def test_target_selection_is_required_and_multiple_targets_render_in_one_song(tmp_path):
    runner = PageFakeRunner()
    app = create_app(tmp_path / "out", runner=runner)
    with _running_server(app) as base_url, _open_page(base_url) as page:
        audio_path = tmp_path / "band.mp3"
        audio_path.write_bytes(b"band-audio")
        page.set_input_files("#sw-file-input", str(audio_path))
        page.wait_for_selector("#sw-confirm:not([hidden])")
        assert page.locator('input[name="targets"]').evaluate_all(
            "inputs => inputs.map(input => input.value)"
        ) == ["guitar", "bass", "drums", "vocals", "piano"]

        page.uncheck('input[name="targets"][value="guitar"]')
        assert page.locator("#sw-upload-btn").is_disabled()
        page.check('input[name="targets"][value="vocals"]')
        page.check('input[name="targets"][value="drums"]')
        assert page.locator("#sw-upload-btn").is_enabled()
        page.click("#sw-upload-btn")

        page.wait_for_function("window.__bunriWeb.getSongs().length === 1")
        page.wait_for_selector('.sw-target-row[data-target="vocals"] a.sw-open-link', timeout=10_000)
        page.wait_for_selector('.sw-target-row[data-target="drums"] a.sw-open-link', timeout=10_000)
        assert page.locator("li.sw-job").count() == 1
        assert page.locator(".sw-target-row").count() == 2
        assert page.locator('.sw-target-row[data-target="vocals"] .sw-target-label').text_content() == "ボーカル"
        vocals = page.locator('.sw-target-block[data-target="vocals"]')
        drums = page.locator('.sw-target-block[data-target="drums"]')
        vocals_toggle = vocals.locator("button.sw-download-toggle")
        drums_toggle = drums.locator("button.sw-download-toggle")
        assert vocals_toggle.get_attribute("aria-expanded") == "false"
        assert drums_toggle.get_attribute("aria-expanded") == "false"
        assert vocals.locator("a.sw-download-link").count() == 4
        assert drums.locator("a.sw-download-link").count() == 4
        assert vocals.locator(".sw-download-group").count() == 2
        assert drums.locator(".sw-download-group").count() == 2
        assert vocals.locator(".sw-downloads").is_hidden()
        assert drums.locator(".sw-downloads").is_hidden()
        vocals_toggle.click()
        assert vocals_toggle.get_attribute("aria-expanded") == "true"
        assert drums_toggle.get_attribute("aria-expanded") == "false"
        assert vocals.locator(".sw-downloads").is_visible()
        assert drums.locator(".sw-downloads").is_hidden()
        assert vocals.locator(".sw-download-label").all_text_contents() == [
            "ボーカルのみ", "ボーカルなし",
        ]
        assert drums.locator(".sw-download-label").all_text_contents() == [
            "ドラムのみ", "ドラムなし",
        ]
        assert vocals.locator("a.sw-download-link").evaluate_all(
            "links => links.map(link => link.getAttribute('href'))"
        ) == [
            "/packages/band/band.vocals.mp3",
            "/packages/band/band.vocals.wav",
            "/packages/band/band.vocals.backing.mp3",
            "/packages/band/band.vocals.backing.wav",
        ]
        assert drums.locator("a.sw-download-link").evaluate_all(
            "links => links.map(link => link.getAttribute('href'))"
        ) == [
            "/packages/band/band.drums.mp3",
            "/packages/band/band.drums.wav",
            "/packages/band/band.drums.backing.mp3",
            "/packages/band/band.drums.backing.wav",
        ]
        assert [call["target"] for call in runner.calls] == ["drums", "vocals"]


@_needs_browser
def test_polling_recovers_after_a_failed_fetch(tmp_path):
    """One failed /api/songs poll (e.g. the server restarting mid-job -- the
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
        page.wait_for_function("window.__bunriWeb.getJobs().length === 1")

        # Fail exactly one poll while the job is still running, then let the
        # rest through.
        state = {"failed": False}

        def route_handler(route):
            if not state["failed"]:
                state["failed"] = True
                route.abort()
            else:
                route.continue_()

        page.route("**/api/songs", route_handler)
        page.wait_for_function(
            "window.__bunriWeb.getJobs().some(function (j) { return j.status === 'done'; })",
            timeout=15_000,
        )
        assert state["failed"], "the test never actually exercised a failed fetch"


@_needs_browser
def test_active_song_delete_button_is_disabled(tmp_path):
    runner = PageFakeRunner(delay=4.0)
    app = create_app(tmp_path / "out", runner=runner)
    with _running_server(app) as base_url, _open_page(base_url) as page:
        _upload_from_page(page, tmp_path / "active.mp3")
        page.wait_for_function(
            "window.__bunriWeb.getJobs().some(function (j) { return j.status === 'running'; })",
            timeout=5_000,
        )
        button = page.locator("button.sw-delete-song-btn")
        assert button.is_visible()
        assert button.is_disabled()


@_needs_browser
def test_delete_dialog_content_and_cancel_restore_focus_after_redraw(tmp_path):
    app = create_app(tmp_path / "out", runner=PageFakeRunner())
    with _running_server(app) as base_url, _open_page(base_url) as page:
        _upload_from_page(page, tmp_path / "Band Practice.mp3", targets=("guitar", "vocals"))
        page.wait_for_function(
            "window.__bunriWeb.getJobs().length === 2 && "
            "window.__bunriWeb.getJobs().every(function (j) { return j.status === 'done'; })",
            timeout=10_000,
        )
        delete_button = page.locator("button.sw-delete-song-btn")
        assert delete_button.evaluate(
            "button => button.closest('.sw-job-content').lastElementChild.contains(button)"
        )
        delete_button.click()
        dialog = page.locator("#sw-delete-dialog")
        assert dialog.evaluate("dialog => dialog.open")
        assert page.locator("#sw-delete-song-name").text_content() == "Band Practice"
        assert page.locator("#sw-delete-targets").text_content() == "2楽器: ギター、ボーカル"
        summary = page.locator(".sw-delete-summary").text_content()
        assert "練習パッケージ" in summary
        assert "全ジョブ履歴とログ" in summary
        assert "共有していないアップロード元とキャッシュ" in summary
        assert "web/uploads" not in dialog.text_content()

        # Rebuild the card while the modal is open; cancel must find the new
        # button for the same song instead of retaining a stale DOM node.
        page.evaluate("window.__bunriWeb.refresh()")
        page.wait_for_timeout(100)
        page.click("#sw-delete-cancel")
        assert not dialog.evaluate("dialog => dialog.open")
        assert page.locator("li.sw-job").count() == 1
        assert page.locator("button.sw-delete-song-btn").evaluate(
            "button => document.activeElement === button"
        )


@_needs_browser
def test_delete_dialog_prevents_duplicate_submit_and_recovers_from_api_error(tmp_path):
    app = create_app(tmp_path / "out", runner=PageFakeRunner())
    with _running_server(app) as base_url, _open_page(base_url) as page:
        _upload_from_page(page, tmp_path / "retry.mp3")
        page.wait_for_function(
            "window.__bunriWeb.getJobs()[0] && window.__bunriWeb.getJobs()[0].status === 'done'",
            timeout=10_000,
        )
        page.click("button.sw-delete-song-btn")
        page.evaluate(
            "window.__realFetch = window.fetch; window.__deleteFetchCount = 0; "
            "window.fetch = function (url, options) { "
            "  if (options && options.method === 'DELETE') { "
            "    window.__deleteFetchCount += 1; "
            "    return new Promise(function (resolve) { window.__resolveDelete = resolve; }); "
            "  } "
            "  return window.__realFetch(url, options); "
            "};"
        )
        page.evaluate(
            "document.getElementById('sw-delete-confirm').click(); "
            "document.getElementById('sw-delete-confirm').click();"
        )
        assert page.evaluate("window.__deleteFetchCount") == 1
        assert page.locator("#sw-delete-confirm").text_content() == "削除中…"
        assert page.locator("#sw-delete-confirm").is_disabled()
        assert page.locator("#sw-delete-cancel").is_disabled()
        page.keyboard.press("Escape")
        assert page.locator("#sw-delete-dialog").evaluate("dialog => dialog.open")

        page.evaluate(
            "window.__resolveDelete({ok: false, status: 500, "
            "json: function () { return Promise.resolve({detail: '削除できませんでした'}); }});"
        )
        page.wait_for_function(
            "document.getElementById('sw-delete-error').textContent === '削除できませんでした'"
        )
        assert page.locator("#sw-delete-confirm").is_enabled()
        assert page.locator("#sw-delete-cancel").is_enabled()
        assert page.locator("#sw-delete-confirm").text_content() == "削除する"
        assert page.locator("li.sw-job").count() == 1


@_needs_browser
def test_successful_delete_closes_dialog_updates_empty_state_and_focus(tmp_path):
    app = create_app(tmp_path / "out", runner=PageFakeRunner())
    with _running_server(app) as base_url, _open_page(base_url) as page:
        _upload_from_page(page, tmp_path / "gone.mp3")
        page.wait_for_function(
            "window.__bunriWeb.getJobs()[0] && window.__bunriWeb.getJobs()[0].status === 'done'",
            timeout=10_000,
        )
        page.click("button.sw-delete-song-btn")
        page.click("#sw-delete-confirm")
        page.wait_for_function("window.__bunriWeb.getSongs().length === 0", timeout=10_000)
        assert not page.locator("#sw-delete-dialog").evaluate("dialog => dialog.open")
        assert page.locator("li.sw-job").count() == 0
        assert page.locator("#sw-empty").is_visible()
        assert page.locator("#sw-songs-heading").evaluate(
            "heading => document.activeElement === heading"
        )
