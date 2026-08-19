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


# ---------------------------------------------------------------------------
# sanitizer hardening: #/% stripped, leading-dot escape closed, "web" renamed
# ---------------------------------------------------------------------------
def test_safe_filename_strips_hash_and_percent():
    assert safe_filename("Song #1 100%") == "Song _1 100_"


def test_safe_filename_dot_only_title_falls_back_to_untitled():
    # ".." (or any run of dots) must never survive into a directory name --
    # substitution alone doesn't touch dots, so this is the leading-dot strip.
    assert safe_filename("..") == "untitled"
    assert safe_filename("...") == "untitled"
    assert safe_filename(".") == "untitled"


def test_safe_filename_strips_only_leading_dots():
    assert safe_filename("..hidden") == "hidden"
    assert safe_filename("a..b") == "a..b"  # dots not at the start are untouched


def test_safe_filename_web_is_renamed_to_web_package():
    # "web" is the private subdirectory _block_private_package_paths blocks
    # (see web/app.py); a same-named package folder must not collide with it.
    assert safe_filename("web") == "web-package"
    assert safe_filename("WEB") == "web-package"
    assert safe_filename("WeB") == "web-package"


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


def _spawn_sigterm_ignoring_sleeper():
    """Like _spawn_marked_sleeper, but installs a SIGTERM handler that
    ignores it -- only SIGKILL (which cannot be blocked) can end this
    process, exercising terminate_pid_from_sidecar's SIGKILL escalation."""
    import subprocess
    import sys

    code = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(60)  # stemlab.cli\n"
    )
    return subprocess.Popen([sys.executable, "-c", code])


def test_terminate_pid_from_sidecar_escalates_to_sigkill_when_sigterm_is_ignored(tmp_path):
    from stemlab.web.jobs import terminate_pid_from_sidecar

    log = tmp_path / "j-y.log"
    proc = _spawn_sigterm_ignoring_sleeper()
    try:
        (tmp_path / "j-y.pid").write_text(str(proc.pid))
        # Short grace period so the SIGTERM poll window elapses quickly and
        # SIGKILL kicks in without slowing the suite down.
        assert terminate_pid_from_sidecar(log, grace_seconds=0.5, poll_interval=0.05) is True
        _wait_until(lambda: proc.poll() is not None)
        assert not (tmp_path / "j-y.pid").exists(), (
            "sidecar must only be removed once the process is confirmed gone"
        )
    finally:
        if proc.poll() is None:
            proc.kill()


# ---------------------------------------------------------------------------
# graceful shutdown
# ---------------------------------------------------------------------------
def test_shutdown_from_idle_returns_promptly_without_hitting_join_timeout(tmp_path):
    store = JobStore(tmp_path, runner=FakeRunner())
    start = time.monotonic()
    store.shutdown(join_timeout=15.0)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, (
        f"shutdown() took {elapsed:.2f}s from idle -- the sentinel likely "
        "didn't wake the worker out of its blocking queue.get()"
    )


def test_shutdown_leaves_a_completed_job_done(tmp_path):
    upload = _make_upload(tmp_path)
    store = JobStore(tmp_path, runner=FakeRunner())
    job, _ = store.create_job(upload, digest="d1", requested_title="Song")
    _wait_until(lambda: store.get_job(job.id).status == "done")

    store.shutdown()

    assert store.get_job(job.id).status == "done"


def test_shutdown_reverts_a_running_job_to_queued_without_marking_it_error(tmp_path):
    upload = _make_upload(tmp_path)
    hold = threading.Event()
    release = threading.Event()
    # Simulates the runner's subprocess having been killed by shutdown()'s
    # SIGTERM: exits nonzero, never gets to write the player.
    runner = FakeRunner(hold=hold, release=release, returncode=1, write_player=False)
    store = JobStore(tmp_path, runner=runner)

    job, _ = store.create_job(upload, digest="d1", requested_title="Song")
    hold.wait(timeout=5.0)
    _wait_until(lambda: store.get_job(job.id).status == "running")

    shutdown_thread = threading.Thread(target=store.shutdown)
    shutdown_thread.start()
    # Give shutdown() a moment to set the stop flag before letting the fake
    # "subprocess" return, mirroring real SIGTERM-then-exit ordering.
    time.sleep(0.1)
    release.set()
    shutdown_thread.join(timeout=10.0)

    reverted = store.get_job(job.id)
    assert reverted.status == "queued"
    assert reverted.started_at is None
    assert reverted.error is None


def test_shutdown_does_not_start_jobs_still_queued_behind_the_running_one(tmp_path):
    upload_a = _make_upload(tmp_path, "a.mp3")
    upload_b = _make_upload(tmp_path, "b.mp3")
    hold = threading.Event()
    release = threading.Event()
    runner = FakeRunner(hold=hold, release=release, returncode=1, write_player=False)
    store = JobStore(tmp_path, runner=runner)

    job_a, _ = store.create_job(upload_a, digest="da", requested_title="A")
    hold.wait(timeout=5.0)
    _wait_until(lambda: store.get_job(job_a.id).status == "running")

    job_b, _ = store.create_job(upload_b, digest="db", requested_title="B")
    assert store.get_job(job_b.id).status == "queued"

    shutdown_thread = threading.Thread(target=store.shutdown)
    shutdown_thread.start()
    time.sleep(0.1)
    release.set()
    shutdown_thread.join(timeout=10.0)

    assert store.get_job(job_a.id).status == "queued"
    assert store.get_job(job_b.id).status == "queued"
    assert len(runner.calls) == 1  # B's runner was never invoked


def test_run_job_success_stays_done_even_if_the_stop_flag_is_already_set(tmp_path):
    """If a job's subprocess finishes successfully right as shutdown begins,
    it must land as done -- not get force-reverted to queued merely because
    the stop flag happened to already be set by the time it finished."""
    upload = _make_upload(tmp_path)
    store = JobStore(tmp_path, runner=FakeRunner(write_player=True, returncode=0))
    job = Job(
        id="j-manual", digest="d1", title="Song", target="guitar", status="queued",
        created_at="2026-01-01T00:00:00+00:00",
        log="web/logs/j-manual.log", upload=str(upload.relative_to(tmp_path)),
    )
    store._jobs[job.id] = job
    store._stopping.set()

    store._run_job(job)

    assert store.get_job(job.id).status == "done"


# ---------------------------------------------------------------------------
# job-file schema validation / quarantine
# ---------------------------------------------------------------------------
def _write_raw_job_file(out_dir: Path, job_id: str, payload) -> Path:
    jobs_dir = out_dir / "web" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    path = jobs_dir / f"{job_id}.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def _valid_job_payload(**overrides) -> dict:
    payload = {
        "id": "j-valid-0001",
        "digest": "d1",
        "title": "Song",
        "target": "guitar",
        "status": "done",
        "created_at": "2026-01-01T00:00:00+00:00",
        "started_at": "2026-01-01T00:00:01+00:00",
        "finished_at": "2026-01-01T00:00:02+00:00",
        "error": None,
        "package": "Song/Song.guitar.player.html",
        "log": "web/logs/j-valid-0001.log",
        "upload": "web/uploads/song.mp3",
    }
    payload.update(overrides)
    return payload


def _quarantined_names(out_dir: Path, stem: str) -> list[str]:
    jobs_dir = out_dir / "web" / "jobs"
    return [p.name for p in jobs_dir.iterdir() if p.name.startswith(f"{stem}.json.bad-")]


def test_malformed_json_is_quarantined_and_startup_continues(tmp_path):
    _write_raw_job_file(tmp_path, "j-bad-json", "{not json")
    _write_raw_job_file(tmp_path, "j-good", _valid_job_payload(id="j-good"))

    store = JobStore(tmp_path, runner=FakeRunner())

    names = [p.name for p in (tmp_path / "web" / "jobs").iterdir()]
    assert "j-bad-json.json" not in names
    assert _quarantined_names(tmp_path, "j-bad-json"), names
    assert store.get_job("j-good") is not None


def test_wrong_type_field_is_quarantined(tmp_path):
    _write_raw_job_file(tmp_path, "j-bad-type", _valid_job_payload(id="j-bad-type", digest=123))
    store = JobStore(tmp_path, runner=FakeRunner())
    assert store.get_job("j-bad-type") is None
    assert _quarantined_names(tmp_path, "j-bad-type")


def test_unknown_status_is_quarantined(tmp_path):
    _write_raw_job_file(
        tmp_path, "j-bad-status", _valid_job_payload(id="j-bad-status", status="frobnicating")
    )
    store = JobStore(tmp_path, runner=FakeRunner())
    assert store.get_job("j-bad-status") is None
    assert _quarantined_names(tmp_path, "j-bad-status")


def test_id_filename_mismatch_is_quarantined(tmp_path):
    # File is named j-mismatch.json but its own "id" field says something else.
    _write_raw_job_file(tmp_path, "j-mismatch", _valid_job_payload(id="j-someone-else"))
    store = JobStore(tmp_path, runner=FakeRunner())
    assert store.get_job("j-someone-else") is None
    assert store.get_job("j-mismatch") is None
    assert _quarantined_names(tmp_path, "j-mismatch")


def test_missing_required_field_is_quarantined(tmp_path):
    payload = _valid_job_payload(id="j-missing-field")
    del payload["digest"]
    _write_raw_job_file(tmp_path, "j-missing-field", payload)
    store = JobStore(tmp_path, runner=FakeRunner())
    assert store.get_job("j-missing-field") is None
    assert _quarantined_names(tmp_path, "j-missing-field")


def test_unparseable_datetime_is_quarantined(tmp_path):
    _write_raw_job_file(
        tmp_path, "j-bad-date", _valid_job_payload(id="j-bad-date", created_at="not-a-date")
    )
    store = JobStore(tmp_path, runner=FakeRunner())
    assert store.get_job("j-bad-date") is None
    assert _quarantined_names(tmp_path, "j-bad-date")


def test_quarantine_never_overwrites_an_existing_quarantine_file(tmp_path, monkeypatch):
    from stemlab.web import jobs as jobs_module

    jobs_dir = tmp_path / "web" / "jobs"
    jobs_dir.mkdir(parents=True)
    jobs_dir.joinpath("j-dup.json").write_text("{not json", encoding="utf-8")
    # Pre-create the exact quarantine name a forced-to-collide token would
    # produce, so the retry loop is forced to pick a different one.
    pre_existing = jobs_dir / "j-dup.json.bad-aaaaaaaa"
    pre_existing.write_text("previous quarantine, must survive untouched", encoding="utf-8")

    tokens = iter(["aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(jobs_module.secrets, "token_hex", lambda n: next(tokens))

    JobStore(tmp_path, runner=FakeRunner())

    assert pre_existing.read_text(encoding="utf-8") == "previous quarantine, must survive untouched"
    assert (jobs_dir / "j-dup.json.bad-bbbbbbbb").exists()


# ---------------------------------------------------------------------------
# default_runner --target passthrough
# ---------------------------------------------------------------------------
def test_default_runner_passes_target_to_the_cli(tmp_path, monkeypatch):
    from stemlab.web import jobs as jobs_module

    captured: dict[str, Any] = {}

    class _FakeProc:
        pid = 12345

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(jobs_module.subprocess, "Popen", fake_popen)

    upload = tmp_path / "song.mp3"
    upload.write_bytes(b"x")
    log_path = tmp_path / "web" / "logs" / "j-x.log"

    rc = jobs_module.default_runner(upload, tmp_path, "My Song", "vocals", log_path)

    assert rc == 0
    cmd = captured["cmd"]
    assert "--target" in cmd
    assert cmd[cmd.index("--target") + 1] == "vocals"
