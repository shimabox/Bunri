"""FastAPI route tests: upload -> job lifecycle -> static package serving,
using TestClient. The subprocess runner is always a fake (see
tests/test_web_jobs.py's FakeRunner for the rationale; this one is kept
separate/decoupled the same way test_package.py's fake separator is kept
apart from test_separate.py's).
"""

from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import stemlab.web.app as app_module
from stemlab.web.app import create_app
from stemlab.web.jobs import safe_filename


class ApiFakeRunner:
    def __init__(self, *, write_player: bool = True, returncode: int = 0) -> None:
        self.write_player = write_player
        self.returncode = returncode
        self.calls: list[dict[str, Any]] = []

    def __call__(self, upload_path: Path, out_dir: Path, title: str, target: str, log_path: Path) -> int:
        self.calls.append({"title": title, "target": target})
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("fake stemlab run\n" * 3, encoding="utf-8")
        if self.write_player:
            safe = safe_filename(title)
            pkg_dir = out_dir / safe
            pkg_dir.mkdir(parents=True, exist_ok=True)
            (pkg_dir / f"{safe}.{target}.player.html").write_text(
                "<html><body>player</body></html>", encoding="utf-8"
            )
        return self.returncode


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), "timed out waiting for condition"


@pytest.fixture()
def client(tmp_path):
    runner = ApiFakeRunner()
    app = create_app(tmp_path, runner=runner)
    with TestClient(app) as c:
        c.fake_runner = runner  # type: ignore[attr-defined]
        c.out_dir = tmp_path  # type: ignore[attr-defined]
        yield c


def _upload(client, *, name="song.mp3", content=b"fake-audio-bytes", title=None):
    data = {"title": title} if title is not None else {}
    return client.post(
        "/api/jobs",
        files={"file": (name, io.BytesIO(content), "audio/mpeg")},
        data=data,
    )


def _job_status(client, job_id: str) -> str:
    return client.get(f"/api/jobs/{job_id}").json()["status"]


# ---------------------------------------------------------------------------
def test_upload_returns_202_and_job_appears_queued_or_running(client):
    res = _upload(client, title="My Song")
    assert res.status_code == 202
    body = res.json()
    assert body["dedup"] is False
    job_id = body["job_id"]

    listed = client.get("/api/jobs").json()
    assert any(j["id"] == job_id for j in listed)
    job = next(j for j in listed if j["id"] == job_id)
    assert job["title"] == "My Song"
    assert job["status"] in ("queued", "running", "done")


def test_job_reaches_done_and_package_url_is_reachable(client):
    res = _upload(client, title="斜陽")
    job_id = res.json()["job_id"]

    _wait_until(lambda: _job_status(client, job_id) == "done")
    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["package_url"] == "/packages/斜陽/斜陽.guitar.player.html"

    player_res = client.get(detail["package_url"])
    assert player_res.status_code == 200
    assert "player" in player_res.text


def test_default_title_is_filename_stem_when_not_provided(client):
    res = _upload(client, name="my-track.wav", content=b"abc")
    job_id = res.json()["job_id"]
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["title"] == "my-track"


def test_unsupported_extension_is_rejected(client):
    res = _upload(client, name="notes.txt", content=b"hello")
    assert res.status_code == 400


def test_uppercase_extension_is_accepted(client):
    res = _upload(client, name="song.MP3", content=b"abc")
    assert res.status_code == 202


def test_oversized_upload_is_rejected_with_413(client, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_UPLOAD_BYTES", 16)
    res = _upload(client, content=b"x" * 1000)
    assert res.status_code == 413


def test_reuploading_identical_content_dedups_once_done(client):
    first = _upload(client, title="Song")
    job_id = first.json()["job_id"]
    _wait_until(lambda: _job_status(client, job_id) == "done")

    second = _upload(client, title="Song (again)")
    assert second.status_code == 200
    body = second.json()
    assert body["dedup"] is True
    assert body["job_id"] == job_id
    assert len(client.fake_runner.calls) == 1


def test_unknown_job_id_is_404(client):
    res = client.get("/api/jobs/j-does-not-exist")
    assert res.status_code == 404


def test_job_list_is_newest_first(client):
    first = _upload(client, name="a.mp3", content=b"aaa", title="A").json()["job_id"]
    _wait_until(lambda: _job_status(client, first) == "done")
    second = _upload(client, name="b.mp3", content=b"bbb", title="B").json()["job_id"]

    ids = [j["id"] for j in client.get("/api/jobs").json()]
    assert ids.index(second) < ids.index(first)


def test_index_page_serves_html(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "StemLab" in res.text


def test_failed_job_reports_log_tail_as_error(client):
    failing_client_runner = ApiFakeRunner(returncode=1)
    app = create_app(client.out_dir / "other", runner=failing_client_runner)
    with TestClient(app) as c:
        res = _upload(c, title="Boom")
        job_id = res.json()["job_id"]
        _wait_until(lambda: _job_status(c, job_id) == "error")
        detail = c.get(f"/api/jobs/{job_id}").json()
        assert detail["error"] is not None
        assert "fake stemlab run" in detail["error"]
        assert detail["package_url"] is None


def test_directory_listing_is_not_exposed(client):
    res = _upload(client, title="Song")
    job_id = res.json()["job_id"]
    _wait_until(lambda: _job_status(client, job_id) == "done")

    res = client.get("/packages/Song/")
    assert res.status_code in (404, 403)


def test_private_out_dir_areas_are_not_served(client):
    """/packages must expose packages only: the uploaded source audio, job
    records and logs under web/, and .cache/ intermediates all live in the
    same out_dir but must not resolve."""
    resp = _upload(client)
    job_id = resp.json()["job_id"]
    _wait_until(lambda: _job_status(client, job_id) == "done")
    job = client.get(f"/api/jobs/{job_id}").json()
    assert client.get(job["package_url"]).status_code == 200  # packages do serve

    uploads = [p for p in (client.out_dir / "web" / "uploads").iterdir() if p.is_file()]
    assert uploads, "expected the uploaded source to be stored"
    assert client.get(f"/packages/web/uploads/{uploads[0].name}").status_code == 404
    # Double slash must not bypass the segment check.
    assert client.get(f"/packages//web/uploads/{uploads[0].name}").status_code == 404
    assert client.get(f"/packages/web/jobs/{job_id}.json").status_code == 404
    assert client.get(f"/packages/web/logs/{job_id}.log").status_code == 404
    assert client.get("/packages/.cache/anything").status_code == 404


def test_non_local_host_header_is_rejected(client):
    """DNS rebinding pins an attacker domain to 127.0.0.1; the Host header is
    the one thing the browser still reports truthfully, so reject foreign ones."""
    resp = client.get("/api/jobs", headers={"host": "evil.example.com"})
    assert resp.status_code == 400


def test_mp4_video_upload_is_accepted(client):
    """Video containers are allowed: ffmpeg extracts the audio track during
    normalization (verified against a real .mp4), so the web whitelist must
    not reject them."""
    resp = _upload(client, name="live-video.mp4", content=b"fake-video-bytes")
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    _wait_until(lambda: _job_status(client, job_id) == "done")
