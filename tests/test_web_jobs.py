"""JobStore + worker tests: state transitions, atomic persistence, crash
recovery on restart, dedup, and title-collision numbering.

The subprocess launcher (`runner`) is always a fake here -- see
stemlab.web.jobs's module docstring for why (this repo's FakeSeparator
pattern, applied to the web layer's job worker instead of the separator).
Real `stemlab` CLI execution is never exercised by these tests.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from stemlab.web.jobs import Job, JobStore, safe_filename


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), "timed out waiting for condition"


class FakeRunner:
    """Configurable stand-in for jobs.default_runner: never spawns a real
    subprocess. Records every call so tests can assert how many times (and
    with what arguments) the worker invoked it."""

    def __init__(
        self,
        *,
        write_player: bool = True,
        returncode: int = 0,
        log_text: str = "fake run ok\n",
        hold: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self.write_player = write_player
        self.returncode = returncode
        self.log_text = log_text
        # `hold`/`release` let a test pause the worker mid-job (to exercise
        # dedup against an in-flight job) and then let it finish on demand.
        self.hold = hold
        self.release = release
        self.calls: list[dict[str, Any]] = []

    def __call__(self, upload_path: Path, out_dir: Path, title: str, target: str, log_path: Path) -> int:
        self.calls.append(
            {"upload_path": upload_path, "out_dir": out_dir, "title": title, "target": target}
        )
        if self.hold is not None:
            self.hold.set()
        if self.release is not None:
            self.release.wait(timeout=5.0)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(self.log_text, encoding="utf-8")
        if self.write_player:
            safe = safe_filename(title)
            pkg_dir = out_dir / safe
            pkg_dir.mkdir(parents=True, exist_ok=True)
            (pkg_dir / f"{safe}.{target}.player.html").write_text("<html></html>", encoding="utf-8")
        return self.returncode


def _make_upload(out_dir: Path, name: str = "song.mp3", content: bytes = b"fake-audio") -> Path:
    uploads = out_dir / "web" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    path = uploads / name
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------------------
# basic state machine
# ---------------------------------------------------------------------------
def test_new_job_starts_queued_then_reaches_done(tmp_path):
    upload = _make_upload(tmp_path)
    store = JobStore(tmp_path, runner=FakeRunner())
    job, created = store.create_job(upload, digest="d1", requested_title="Song")

    assert created is True
    assert job.status == "queued"
    assert job.digest == "d1"
    assert job.target == "guitar"

    _wait_until(lambda: store.get_job(job.id).status == "done")
    done = store.get_job(job.id)
    assert done.package == "Song/Song.guitar.player.html"
    assert done.error is None
    assert done.started_at is not None
    assert done.finished_at is not None


def test_nonzero_exit_code_becomes_error_with_log_tail(tmp_path):
    upload = _make_upload(tmp_path)
    log_lines = "\n".join(f"line {i}" for i in range(1, 21))
    runner = FakeRunner(returncode=1, log_text=log_lines)
    store = JobStore(tmp_path, runner=runner)

    job, _ = store.create_job(upload, digest="d1", requested_title="Song")
    _wait_until(lambda: store.get_job(job.id).status == "error")

    failed = store.get_job(job.id)
    assert failed.package is None
    assert failed.error is not None
    # Only the last 8 lines are kept.
    assert failed.error.splitlines() == [f"line {i}" for i in range(13, 21)]


def test_success_exit_code_without_player_html_is_still_an_error(tmp_path):
    # exit 0 but the expected player.html never showed up -- e.g. a partial
    # crash after the CLI printed success but before the file landed. The
    # plan requires *both* conditions for "done".
    upload = _make_upload(tmp_path)
    runner = FakeRunner(write_player=False, returncode=0, log_text="looked fine but no output\n")
    store = JobStore(tmp_path, runner=runner)

    job, _ = store.create_job(upload, digest="d1", requested_title="Song")
    _wait_until(lambda: store.get_job(job.id).status == "error")
    assert store.get_job(job.id).package is None


def test_jobs_run_strictly_sequentially(tmp_path):
    # A single worker thread + FIFO queue: the second job shouldn't even
    # start until the first really finishes.
    order: list[str] = []

    class OrderTrackingRunner(FakeRunner):
        def __call__(self, upload_path, out_dir, title, target, log_path):
            order.append(f"start:{title}")
            time.sleep(0.05)
            rc = super().__call__(upload_path, out_dir, title, target, log_path)
            order.append(f"end:{title}")
            return rc

    store = JobStore(tmp_path, runner=OrderTrackingRunner())
    upload_a = _make_upload(tmp_path, "a.mp3")
    upload_b = _make_upload(tmp_path, "b.mp3")
    store.create_job(upload_a, digest="da", requested_title="A")
    store.create_job(upload_b, digest="db", requested_title="B")

    _wait_until(lambda: len(order) == 4)
    assert order == ["start:A", "end:A", "start:B", "end:B"]


# ---------------------------------------------------------------------------
# atomic persistence
# ---------------------------------------------------------------------------
def test_job_file_is_written_atomically_with_no_leftover_tmp(tmp_path):
    upload = _make_upload(tmp_path)
    store = JobStore(tmp_path, runner=FakeRunner())
    job, _ = store.create_job(upload, digest="d1", requested_title="Song")
    _wait_until(lambda: store.get_job(job.id).status == "done")

    jobs_dir = tmp_path / "web" / "jobs"
    names = [p.name for p in jobs_dir.iterdir()]
    assert f"{job.id}.json" in names
    assert not any(".tmp" in n for n in names), names

    on_disk = json.loads((jobs_dir / f"{job.id}.json").read_text(encoding="utf-8"))
    assert on_disk["status"] == "done"
    assert on_disk["id"] == job.id


# ---------------------------------------------------------------------------
# restart recovery
# ---------------------------------------------------------------------------
def _write_job_file(out_dir: Path, job: Job) -> None:
    jobs_dir = out_dir / "web" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / f"{job.id}.json").write_text(json.dumps(job.to_dict()), encoding="utf-8")


def test_running_job_from_a_previous_crash_is_requeued_on_startup(tmp_path):
    upload = _make_upload(tmp_path)
    stale = Job(
        id="j-stale-running",
        digest="d1",
        title="Song",
        target="guitar",
        status="running",
        created_at="2026-01-01T00:00:00+00:00",
        started_at="2026-01-01T00:00:01+00:00",
        log="web/logs/j-stale-running.log",
        upload=str(upload.relative_to(tmp_path)),
    )
    _write_job_file(tmp_path, stale)

    store = JobStore(tmp_path, runner=FakeRunner())
    _wait_until(lambda: store.get_job("j-stale-running").status == "done")
    recovered = store.get_job("j-stale-running")
    assert recovered.package == "Song/Song.guitar.player.html"


def test_queued_job_never_picked_up_is_requeued_on_startup(tmp_path):
    upload = _make_upload(tmp_path)
    stale = Job(
        id="j-stale-queued",
        digest="d1",
        title="Song",
        target="guitar",
        status="queued",
        created_at="2026-01-01T00:00:00+00:00",
        log="web/logs/j-stale-queued.log",
        upload=str(upload.relative_to(tmp_path)),
    )
    _write_job_file(tmp_path, stale)

    store = JobStore(tmp_path, runner=FakeRunner())
    _wait_until(lambda: store.get_job("j-stale-queued").status == "done")


def test_done_and_error_jobs_are_left_alone_on_startup(tmp_path):
    done_job = Job(
        id="j-old-done",
        digest="d1",
        title="Song",
        target="guitar",
        status="done",
        created_at="2026-01-01T00:00:00+00:00",
        started_at="2026-01-01T00:00:01+00:00",
        finished_at="2026-01-01T00:00:02+00:00",
        package="Song/Song.guitar.player.html",
        log="web/logs/j-old-done.log",
        upload="web/uploads/song.mp3",
    )
    error_job = Job(
        id="j-old-error",
        digest="d2",
        title="Other",
        target="guitar",
        status="error",
        created_at="2026-01-01T00:00:03+00:00",
        started_at="2026-01-01T00:00:04+00:00",
        finished_at="2026-01-01T00:00:05+00:00",
        error="boom",
        log="web/logs/j-old-error.log",
        upload="web/uploads/other.mp3",
    )
    _write_job_file(tmp_path, done_job)
    _write_job_file(tmp_path, error_job)

    runner = FakeRunner()
    store = JobStore(tmp_path, runner=runner)
    time.sleep(0.2)  # give a stray worker iteration a chance to misfire

    assert store.get_job("j-old-done").status == "done"
    assert store.get_job("j-old-error").status == "error"
    assert runner.calls == []  # neither terminal job should have been re-run


# ---------------------------------------------------------------------------
# dedup
# ---------------------------------------------------------------------------
def test_creating_a_job_for_an_already_done_digest_and_target_reuses_it(tmp_path):
    upload = _make_upload(tmp_path)
    runner = FakeRunner()
    store = JobStore(tmp_path, runner=runner)

    first, created_first = store.create_job(upload, digest="d1", requested_title="Song")
    _wait_until(lambda: store.get_job(first.id).status == "done")

    second, created_second = store.create_job(upload, digest="d1", requested_title="Song again")
    assert created_second is False
    assert second.id == first.id
    assert len(runner.calls) == 1  # no second run triggered


def test_creating_a_job_while_one_is_already_running_reuses_it(tmp_path):
    upload = _make_upload(tmp_path)
    hold = threading.Event()
    release = threading.Event()
    runner = FakeRunner(hold=hold, release=release)
    store = JobStore(tmp_path, runner=runner)

    first, created_first = store.create_job(upload, digest="d1", requested_title="Song")
    assert created_first is True
    hold.wait(timeout=5.0)  # the worker has picked it up and is now "running"
    _wait_until(lambda: store.get_job(first.id).status == "running")

    second, created_second = store.create_job(upload, digest="d1", requested_title="Song")
    assert created_second is False
    assert second.id == first.id

    release.set()  # let the held job finish so the test can clean up promptly
    _wait_until(lambda: store.get_job(first.id).status == "done")
    assert len(runner.calls) == 1


def test_different_target_is_not_deduped(tmp_path):
    upload = _make_upload(tmp_path)
    store = JobStore(tmp_path, runner=FakeRunner())

    guitar_job, _ = store.create_job(upload, digest="d1", requested_title="Song", target="guitar")
    _wait_until(lambda: store.get_job(guitar_job.id).status == "done")

    vocals_job, created = store.create_job(upload, digest="d1", requested_title="Song", target="vocals")
    assert created is True
    assert vocals_job.id != guitar_job.id


def test_a_failed_job_can_be_retried_rather_than_deduped(tmp_path):
    upload = _make_upload(tmp_path)
    failing_runner = FakeRunner(returncode=1)
    store = JobStore(tmp_path, runner=failing_runner)

    first, _ = store.create_job(upload, digest="d1", requested_title="Song")
    _wait_until(lambda: store.get_job(first.id).status == "error")

    second, created = store.create_job(upload, digest="d1", requested_title="Song")
    assert created is True
    assert second.id != first.id


# ---------------------------------------------------------------------------
# title collision numbering
# ---------------------------------------------------------------------------
def test_same_title_different_digest_gets_numbered_suffix(tmp_path):
    upload_a = _make_upload(tmp_path, "a.mp3")
    upload_b = _make_upload(tmp_path, "b.mp3")
    upload_c = _make_upload(tmp_path, "c.mp3")
    store = JobStore(tmp_path, runner=FakeRunner())

    job_a, _ = store.create_job(upload_a, digest="da", requested_title="Song")
    _wait_until(lambda: store.get_job(job_a.id).status == "done")
    assert job_a.title == "Song"

    job_b, _ = store.create_job(upload_b, digest="db", requested_title="Song")
    _wait_until(lambda: store.get_job(job_b.id).status == "done")
    assert job_b.title == "Song-2"

    job_c, _ = store.create_job(upload_c, digest="dc", requested_title="Song")
    _wait_until(lambda: store.get_job(job_c.id).status == "done")
    assert job_c.title == "Song-3"


def test_same_title_same_digest_is_dedup_not_a_new_suffix(tmp_path):
    upload = _make_upload(tmp_path)
    store = JobStore(tmp_path, runner=FakeRunner())

    job1, _ = store.create_job(upload, digest="d1", requested_title="Song")
    _wait_until(lambda: store.get_job(job1.id).status == "done")

    # Same digest as job1 -> dedup reuse, no new title resolution happens.
    job2, created = store.create_job(upload, digest="d1", requested_title="Song")
    assert created is False
    assert job2.id == job1.id
    assert job2.title == "Song"


def test_title_collision_resolution_uses_safe_filename_rule(tmp_path):
    # A title with path-unsafe characters collides on its *sanitized* form.
    upload_a = _make_upload(tmp_path, "a.mp3")
    upload_b = _make_upload(tmp_path, "b.mp3")
    store = JobStore(tmp_path, runner=FakeRunner())

    job_a, _ = store.create_job(upload_a, digest="da", requested_title="Rock/Roll")
    _wait_until(lambda: store.get_job(job_a.id).status == "done")
    assert safe_filename(job_a.title) == "Rock_Roll"

    job_b, _ = store.create_job(upload_b, digest="db", requested_title="Rock/Roll")
    _wait_until(lambda: store.get_job(job_b.id).status == "done")
    assert safe_filename(job_b.title) == "Rock_Roll-2"


# ---------------------------------------------------------------------------
# list / get
# ---------------------------------------------------------------------------
def test_list_jobs_is_newest_first(tmp_path):
    store = JobStore(tmp_path, runner=FakeRunner())
    upload_a = _make_upload(tmp_path, "a.mp3")
    upload_b = _make_upload(tmp_path, "b.mp3")

    job_a, _ = store.create_job(upload_a, digest="da", requested_title="A")
    time.sleep(0.02)
    job_b, _ = store.create_job(upload_b, digest="db", requested_title="B")

    ids_in_order = [j.id for j in store.list_jobs()]
    assert ids_in_order.index(job_b.id) < ids_in_order.index(job_a.id)


def test_get_job_returns_none_for_unknown_id(tmp_path):
    store = JobStore(tmp_path, runner=FakeRunner())
    assert store.get_job("j-does-not-exist") is None


def test_safe_filename_matches_package_py_rule():
    # Same regex as stemlab.package._safe_filename (duplicated deliberately,
    # see jobs.py's module docstring) -- pin the exact rule here.
    assert safe_filename('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
    assert safe_filename("  ") == "untitled"
    assert safe_filename("") == "untitled"


def test_preexisting_cli_package_folder_is_a_collision(tmp_path):
    """A package folder built by the CLI directly (no job record) must not be
    written into by a web job with the same title: build_package overwrites
    existing folders without hesitation, so the web layer has to step aside
    with a numbered suffix."""
    (tmp_path / "Song").mkdir()
    upload = _make_upload(tmp_path, "a.mp3")
    store = JobStore(tmp_path, runner=FakeRunner())

    job, _ = store.create_job(upload, digest="da", requested_title="Song")
    _wait_until(lambda: store.get_job(job.id).status == "done")
    assert job.title == "Song-2"


def _spawn_marked_sleeper():
    """A throwaway process whose command line contains "stemlab.cli" (inside
    the -c payload), so the sidecar reaper's pid-recycling guard accepts it."""
    import subprocess
    import sys

    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)  # stemlab.cli"])


def test_terminate_pid_from_sidecar_kills_only_matching_processes(tmp_path):
    from stemlab.web.jobs import terminate_pid_from_sidecar

    log = tmp_path / "j-x.log"
    proc = _spawn_marked_sleeper()
    try:
        (tmp_path / "j-x.pid").write_text(str(proc.pid))
        assert terminate_pid_from_sidecar(log) is True
        _wait_until(lambda: proc.poll() is not None)
        assert not (tmp_path / "j-x.pid").exists(), "sidecar must be consumed"
    finally:
        if proc.poll() is None:
            proc.kill()

    # Recycled/foreign pid: marker doesn't match -> must NOT be signalled.
    import subprocess
    import sys
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        (tmp_path / "j-x.pid").write_text(str(other.pid))
        assert terminate_pid_from_sidecar(log) is False
        assert other.poll() is None, "unrelated process must be left alone"
    finally:
        other.kill()


def test_recovery_reaps_surviving_subprocess_before_requeueing(tmp_path):
    """A server killed mid-job leaves its separation subprocess alive
    (children don't die with the parent). The next server's recovery must
    stop that orphan before re-queueing the job, or two subprocesses write
    the same cache/package files concurrently (observed live)."""
    upload = _make_upload(tmp_path)
    jobs_dir = tmp_path / "web" / "jobs"
    logs_dir = tmp_path / "web" / "logs"
    jobs_dir.mkdir(parents=True)
    logs_dir.mkdir(parents=True)

    orphan = _spawn_marked_sleeper()
    try:
        (logs_dir / "j-old.pid").write_text(str(orphan.pid))
        (jobs_dir / "j-old.json").write_text(json.dumps({
            "id": "j-old", "digest": "dx", "title": "Song", "target": "guitar",
            "status": "running", "created_at": "2026-07-12T00:00:00+00:00",
            "started_at": "2026-07-12T00:00:01+00:00",
            "log": "web/logs/j-old.log",
            "upload": str(upload.relative_to(tmp_path)),
        }), encoding="utf-8")

        store = JobStore(tmp_path, runner=FakeRunner())
        _wait_until(lambda: orphan.poll() is not None)  # orphan reaped
        _wait_until(lambda: store.get_job("j-old").status == "done")  # and re-run cleanly
    finally:
        if orphan.poll() is None:
            orphan.kill()
