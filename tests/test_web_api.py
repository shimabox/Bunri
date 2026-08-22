"""FastAPI route tests: upload -> job lifecycle -> static package serving,
using TestClient. The subprocess runner is always a fake (see
tests/test_web_jobs.py's FakeRunner for the rationale; this one is kept
separate/decoupled the same way test_package.py's fake separator is kept
apart from test_separate.py's).
"""

from __future__ import annotations

import io
import hashlib
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import bunri.web.app as app_module
from bunri.web.app import create_app
from bunri.web.jobs import safe_filename


class ApiFakeRunner:
    def __init__(self, *, write_player: bool = True, returncode: int = 0) -> None:
        self.write_player = write_player
        self.returncode = returncode
        self.calls: list[dict[str, Any]] = []

    def __call__(self, upload_path: Path, out_dir: Path, title: str, target: str, log_path: Path) -> int:
        self.calls.append({"title": title, "target": target})
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("fake bunri run\n" * 3, encoding="utf-8")
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


def _upload(client, *, name="song.mp3", content=b"fake-audio-bytes", title=None, targets=None):
    data = {"title": title} if title is not None else {}
    if targets is not None:
        data["targets"] = targets
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
    assert body["jobs"] == [{"id": body["job_id"], "target": "guitar", "dedup": False}]
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
    # package_url is percent-encoded (see _serialize_job); non-ASCII bytes
    # are encoded too, same as any other character outside quote()'s
    # unreserved set.
    assert detail["package_url"] == (
        "/packages/%E6%96%9C%E9%99%BD/%E6%96%9C%E9%99%BD.guitar.player.html"
    )

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


def test_multiple_targets_are_normalized_and_returned_per_target(client):
    res = _upload(client, title="Band", targets=["piano", "guitar", "vocals"])
    assert res.status_code == 202
    body = res.json()
    assert [job["target"] for job in body["jobs"]] == ["guitar", "vocals", "piano"]
    assert body["job_id"] == body["jobs"][0]["id"]
    assert body["dedup"] is False
    assert all(job["dedup"] is False for job in body["jobs"])
    _wait_until(lambda: len(client.fake_runner.calls) == 3)
    assert [call["target"] for call in client.fake_runner.calls] == ["guitar", "vocals", "piano"]


@pytest.mark.parametrize("targets", [[""], ["guitar", "guitar"], ["violin"]])
def test_invalid_targets_are_rejected_before_upload_or_job_creation(client, targets):
    res = _upload(client, targets=targets)
    assert res.status_code == 400
    assert list((client.out_dir / "web" / "uploads").iterdir()) == []
    assert client.get("/api/jobs").json() == []


def test_partial_target_dedup_creates_only_the_missing_target(client):
    first = _upload(client, content=b"same", targets=["guitar"])
    first_id = first.json()["job_id"]
    _wait_until(lambda: _job_status(client, first_id) == "done")

    second = _upload(client, content=b"same", targets=["vocals", "guitar"])
    assert second.status_code == 202
    assert second.json()["dedup"] is False
    jobs = second.json()["jobs"]
    assert jobs[0] == {"id": first_id, "target": "guitar", "dedup": True}
    assert jobs[1]["target"] == "vocals"
    assert jobs[1]["dedup"] is False


def test_songs_group_digest_and_use_latest_jobs_in_target_order(tmp_path):
    _write_job_file(
        tmp_path, "j-old-guitar", digest="digest-a", title="Old title", target="guitar",
        created_at="2026-01-01T00:00:00+00:00",
    )
    _write_job_file(
        tmp_path, "j-new-guitar", digest="digest-a", title="New title", target="guitar",
        created_at="2026-01-02T00:00:00+00:00",
    )
    _write_job_file(
        tmp_path, "j-vocals", digest="digest-a", title="New title", target="vocals",
        created_at="2026-01-02T00:00:00+00:00",
    )
    _write_job_file(
        tmp_path, "j-legacy", digest="digest-a", title="New title", target="zither",
        created_at="2026-01-01T12:00:00+00:00",
    )
    _write_job_file(
        tmp_path, "j-other", digest="digest-b", title="Other", target="bass",
        created_at="2025-12-01T00:00:00+00:00",
    )
    app = create_app(tmp_path, runner=ApiFakeRunner())
    with TestClient(app) as c:
        songs = c.get("/api/songs").json()
        legacy = c.get("/api/jobs/j-legacy").json()

    assert [song["title"] for song in songs] == ["New title", "Other"]
    song = songs[0]
    assert song["id"] == hashlib.sha256(b"digest-a").hexdigest()
    assert song["created_at"] == "2026-01-02T00:00:00+00:00"
    assert [target["target"] for target in song["targets"]] == ["guitar", "vocals", "zither"]
    assert song["targets"][0]["id"] == "j-new-guitar"
    assert [target["target_label"] for target in song["targets"]] == ["ギター", "ボーカル", "zither"]
    assert "title" not in song["targets"][0]
    assert legacy["status"] == "done"
    assert legacy["package_url"] == "/packages/New%20title/New%20title.zither.player.html"


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
    assert "Bunri" in res.text


def test_failed_job_reports_log_tail_as_error(client):
    failing_client_runner = ApiFakeRunner(returncode=1)
    app = create_app(client.out_dir / "other", runner=failing_client_runner)
    with TestClient(app) as c:
        res = _upload(c, title="Boom")
        job_id = res.json()["job_id"]
        _wait_until(lambda: _job_status(c, job_id) == "error")
        detail = c.get(f"/api/jobs/{job_id}").json()
        assert detail["error"] is not None
        assert "fake bunri run" in detail["error"]
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


def test_uppercase_private_dir_aliases_are_blocked_via_casefold(client):
    """Regression for the case-insensitivity bypass: a literal, differently-
    cased "WEB"/".CACHE" directory must be blocked exactly like the real
    "web"/".cache" ones -- created here with their real (uppercase) names
    rather than relying on a lowercase directory being reachable through an
    uppercase URL, so the bug reproduces on a case-sensitive filesystem
    (Linux CI) too, not just on macOS's case-insensitive default."""
    secret_web_dir = client.out_dir / "WEB"
    secret_web_dir.mkdir(parents=True, exist_ok=True)
    (secret_web_dir / "secret.txt").write_text("do not leak", encoding="utf-8")

    secret_cache_dir = client.out_dir / ".CACHE"
    secret_cache_dir.mkdir(parents=True, exist_ok=True)
    (secret_cache_dir / "secret.txt").write_text("do not leak", encoding="utf-8")

    assert client.get("/packages/WEB/secret.txt").status_code == 404
    assert client.get("/packages/.CACHE/secret.txt").status_code == 404


def test_dotfile_anywhere_under_a_package_path_is_blocked(client):
    package_dir = client.out_dir / "Song"
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / ".secret").write_text("do not leak", encoding="utf-8")

    assert client.get("/packages/Song/.secret").status_code == 404


def _write_job_file(out_dir: Path, job_id: str, **overrides) -> None:
    """A persisted job record. `package` and `log` default to the values the
    store derives from the title/target/id, because those are the only ones
    the loader accepts -- a record may only claim its own package and write
    its own log."""
    import json

    title = overrides.get("title", "Song 1")
    target = overrides.get("target", "guitar")
    payload = {
        "id": job_id,
        "digest": "d-legacy",
        "title": title,
        "target": target,
        "status": "done",
        "created_at": "2026-01-01T00:00:00+00:00",
        "started_at": "2026-01-01T00:00:01+00:00",
        "finished_at": "2026-01-01T00:00:02+00:00",
        "error": None,
        "package": f"{safe_filename(title)}/{safe_filename(title)}.{target}.player.html",
        "log": f"web/logs/{job_id}.log",
        "upload": "web/uploads/song.mp3",
    }
    payload.update(overrides)
    jobs_dir = out_dir / "web" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / f"{job_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_package_url_is_percent_encoded_for_spaces(tmp_path):
    """A space survives safe_filename, so it reaches the URL and has to be
    encoded there -- otherwise the link breaks in the browser."""
    _write_job_file(tmp_path, "j-spaced", title="Song 1")
    app = create_app(tmp_path, runner=ApiFakeRunner())
    with TestClient(app) as c:
        detail = c.get("/api/jobs/j-spaced").json()
        assert detail["package_url"] == "/packages/Song%201/Song%201.guitar.player.html"


def test_package_url_is_percent_encoded_for_hash_and_space(tmp_path):
    """A job record persisted by a released version, whose sanitizer left
    "#" in the slug. It is still that user's finished package, so it loads
    and is served -- with the "#" percent-encoded, or the URL would truncate
    at a fragment.

    The record's `package` is checked for shape rather than for matching what
    today's sanitizer would produce. Requiring the latter quarantined records
    like this one, discarding real packages over a slug rule that changed
    after they were written."""
    _write_job_file(
        tmp_path, "j-legacy-hash", title="Song #1",
        package="Song #1/Song #1.guitar.player.html",
    )
    app = create_app(tmp_path, runner=ApiFakeRunner())
    with TestClient(app) as c:
        detail = c.get("/api/jobs/j-legacy-hash").json()
        assert detail["package_url"] == "/packages/Song%20%231/Song%20%231.guitar.player.html"


@pytest.mark.parametrize(
    "package",
    [
        "Song/Somebody-elses.guitar.player.html",  # file half doesn't match the dir
        "Song/Song.vocals.player.html",  # a different target's package
        "../outside/outside.guitar.player.html",
        "/etc/etc.guitar.player.html",
        "web/web.guitar.player.html",  # the server's own directory
        ".hidden/.hidden.guitar.player.html",
        "Song.guitar.player.html",  # no directory at all
    ],
)
def test_a_record_claiming_a_package_of_the_wrong_shape_is_rejected(tmp_path, package):
    """Shape is not the same as "anything goes": `package` is what the API
    tells the browser to fetch, so it must still be one package directory
    containing its own player for this record's target, and nothing else."""
    _write_job_file(tmp_path, "j-claims", title="Song", package=package)
    app = create_app(tmp_path, runner=ApiFakeRunner())
    with TestClient(app) as c:
        assert c.get("/api/jobs/j-claims").status_code == 404
        assert c.get("/api/jobs").json() == []


@pytest.mark.parametrize(
    "url",
    [
        "/packages/Alias/uploads/secret.mp3",
        "/packages/Alias/jobs/j-x.json",
        "/packages/Alias/logs/j-x.log",
        "/packages/AliasC/stem.wav",
    ],
)
def test_an_internal_alias_symlink_cannot_serve_the_private_directories(tmp_path, url):
    """The segment rules describe the URL, and a symlink inside out_dir can
    make the URL innocent while the file is not: `out/Alias -> web` has no
    "web" in the path, no leading dot, and resolves inside out_dir, so every
    one of those checks passed and the user's own uploaded audio was served.

    The check now also asks where the path really lands.
    """
    out_dir = tmp_path
    (out_dir / "web" / "uploads").mkdir(parents=True)
    (out_dir / "web" / "uploads" / "secret.mp3").write_bytes(b"the user's source audio")
    (out_dir / "web" / "jobs").mkdir(parents=True, exist_ok=True)
    (out_dir / "web" / "jobs" / "j-x.json").write_text("{}", encoding="utf-8")
    (out_dir / "web" / "logs").mkdir(parents=True, exist_ok=True)
    (out_dir / "web" / "logs" / "j-x.log").write_text("log", encoding="utf-8")
    (out_dir / ".cache").mkdir(parents=True)
    (out_dir / ".cache" / "stem.wav").write_bytes(b"an expensive intermediate")

    (out_dir / "Alias").symlink_to(out_dir / "web", target_is_directory=True)
    (out_dir / "AliasC").symlink_to(out_dir / ".cache", target_is_directory=True)

    app = create_app(out_dir, runner=ApiFakeRunner())
    with TestClient(app) as c:
        assert c.get(url).status_code == 404


@pytest.mark.parametrize(
    ("real_dir", "url"),
    [
        # The case that was getting through: the URL rule casefolded, the
        # resolved-path rule compared against a literal "web", so the two
        # disagreed about a directory named WEB. On a case-sensitive
        # filesystem that is a directory of its own; on a case-insensitive
        # one it *is* web/, which makes the leak the user's real audio.
        ("WEB", "/packages/Alias/secret.txt"),
        ("Web", "/packages/Alias/secret.txt"),
        (".CACHE", "/packages/Alias/secret.txt"),
    ],
)
def test_an_alias_to_a_differently_cased_private_directory_is_refused(tmp_path, real_dir, url):
    """Asking the same question of the URL and of the resolved path only
    works if it really is the same question. It was two questions before, and
    an alias to `WEB` answered "no" to one and "yes" to the other."""
    out_dir = tmp_path
    (out_dir / real_dir).mkdir(parents=True, exist_ok=True)
    (out_dir / real_dir / "secret.txt").write_bytes(b"private to the server")
    (out_dir / "Alias").symlink_to(out_dir / real_dir, target_is_directory=True)

    app = create_app(out_dir, runner=ApiFakeRunner())
    with TestClient(app) as c:
        res = c.get(url)
        assert res.status_code == 404, f"served {res.content!r} from {real_dir}/"
        # Direct access stays refused too -- the URL rule was never the
        # broken half.
        assert c.get(f"/packages/{real_dir}/secret.txt").status_code == 404


def test_an_alias_to_a_dot_directory_deeper_in_the_tree_is_refused(tmp_path):
    """The dot rule applies to every component of the real path, not just its
    first: an alias can point at a private directory nested inside an
    otherwise ordinary package."""
    out_dir = tmp_path
    hidden = out_dir / "Song" / ".hidden"
    hidden.mkdir(parents=True)
    (hidden / "secret.txt").write_bytes(b"private to the server")
    (out_dir / "Alias2").symlink_to(hidden, target_is_directory=True)

    app = create_app(out_dir, runner=ApiFakeRunner())
    with TestClient(app) as c:
        assert c.get("/packages/Alias2/secret.txt").status_code == 404
        assert c.get("/packages/Song/.hidden/secret.txt").status_code == 404


def test_a_real_package_is_still_served_alongside_the_alias_check(tmp_path):
    """The resolve-based check must not cost the ordinary case: a genuine
    package directory still serves."""
    out_dir = tmp_path
    package = out_dir / "Song 1"
    package.mkdir(parents=True)
    (package / "Song 1.guitar.player.html").write_text("<html>player</html>", encoding="utf-8")

    app = create_app(out_dir, runner=ApiFakeRunner())
    with TestClient(app) as c:
        res = c.get("/packages/Song%201/Song%201.guitar.player.html")
        assert res.status_code == 200
        assert "player" in res.text


# ---------------------------------------------------------------------------
# CSRF: strict same-origin enforcement on unsafe methods
# ---------------------------------------------------------------------------
def _post_job(client, *, origin: str | None):
    headers = {"origin": origin} if origin is not None else {}
    return client.post(
        "/api/jobs",
        files={"file": ("song.mp3", io.BytesIO(b"abc"), "audio/mpeg")},
        headers=headers,
    )


def test_same_origin_post_is_allowed(client):
    res = _post_job(client, origin="http://testserver")
    assert res.status_code in (200, 202)


def test_missing_origin_header_is_allowed(client):
    res = _post_job(client, origin=None)
    assert res.status_code in (200, 202)


def test_cross_port_origin_is_rejected_with_403(client):
    res = _post_job(client, origin="http://testserver:9999")
    assert res.status_code == 403


def test_hostile_origin_is_rejected_with_403(client):
    res = _post_job(client, origin="http://evil.example.com")
    assert res.status_code == 403


def test_unparseable_origin_port_is_rejected_with_403_not_500(client):
    """A non-numeric port makes urlsplit's .port raise ValueError. An Origin
    we can't even parse is certainly not our origin, so it must take the 403
    branch -- previously the ValueError escaped the middleware and turned a
    malformed header into a server error."""
    res = _post_job(client, origin="http://testserver:not-a-port")
    assert res.status_code == 403


def test_timezone_naive_job_file_does_not_break_the_job_list(tmp_path):
    """A hand-written/legacy job record with a timezone-naive started_at and
    no finished_at used to make _elapsed_seconds subtract a naive datetime
    from an aware "now" -> TypeError -> GET /api/jobs 500 for *every* job.
    Such a record is now rejected at load time (quarantined) instead."""
    _write_job_file(
        tmp_path,
        "j-naive",
        status="done",
        started_at="2026-01-01T00:00:01",  # no UTC offset
        finished_at=None,
    )
    _write_job_file(tmp_path, "j-fine")

    app = create_app(tmp_path, runner=ApiFakeRunner())
    with TestClient(app) as c:
        res = c.get("/api/jobs")
        assert res.status_code == 200
        ids = [j["id"] for j in res.json()]
        assert "j-naive" not in ids
        assert "j-fine" in ids


def test_non_local_host_header_is_rejected(client):
    """DNS rebinding pins an attacker domain to 127.0.0.1; the Host header is
    the one thing the browser still reports truthfully, so reject foreign ones."""
    resp = client.get("/api/jobs", headers={"host": "evil.example.com"})
    assert resp.status_code == 400


def test_aac_upload_is_accepted(client):
    res = _upload(client, name="song.aac", content=b"abc")
    assert res.status_code == 202


def test_mp4_video_upload_is_accepted(client):
    """Video containers are allowed: ffmpeg extracts the audio track during
    normalization (verified against a real .mp4), so the web whitelist must
    not reject them."""
    resp = _upload(client, name="live-video.mp4", content=b"fake-video-bytes")
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    _wait_until(lambda: _job_status(client, job_id) == "done")
