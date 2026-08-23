"""JobStore + worker tests: state transitions, atomic persistence, crash
recovery on restart, dedup, and title-collision numbering.

The subprocess launcher (`runner`) is always a fake here -- see
bunri.web.jobs's module docstring for why (this repo's FakeSeparator
pattern, applied to the web layer's job worker instead of the separator).
Real `bunri` CLI execution is never exercised by these tests.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from bunri.web.jobs import Job, JobStore, safe_filename


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


def test_create_jobs_shares_latest_digest_title_and_registry_order(tmp_path):
    upload = _make_upload(tmp_path)
    runner = FakeRunner()
    store = JobStore(tmp_path, runner=runner)
    original, _ = store.create_job(upload, "same", "Original", target="guitar")
    _wait_until(lambda: store.get_job(original.id).status == "done")

    results = store.create_jobs(upload, "same", "Ignored rename", ["piano", "vocals", "bass"])
    assert [job.target for job, _ in results] == ["bass", "vocals", "piano"]
    assert {job.title for job, _ in results} == {"Original"}
    _wait_until(lambda: len(runner.calls) == 4)
    assert [call["target"] for call in runner.calls] == ["guitar", "bass", "vocals", "piano"]


def test_create_jobs_dedups_per_target_and_retries_error(tmp_path):
    upload = _make_upload(tmp_path)
    store = JobStore(tmp_path, runner=FakeRunner(returncode=1, write_player=False))
    failed, _ = store.create_job(upload, "same", "Song", target="vocals")
    _wait_until(lambda: store.get_job(failed.id).status == "error")

    retried = store.create_jobs(upload, "same", "Song", ["vocals"])
    assert retried[0][1] is True
    assert retried[0][0].id != failed.id


@pytest.mark.parametrize("targets", [[], ["guitar", "guitar"], ["violin"]])
def test_create_jobs_rejects_invalid_targets(tmp_path, targets):
    upload = _make_upload(tmp_path)
    store = JobStore(tmp_path, runner=FakeRunner())
    with pytest.raises(ValueError):
        store.create_jobs(upload, "digest", "Song", targets)
    assert store.list_jobs() == []


def test_create_jobs_run_strictly_sequentially(tmp_path):
    upload = _make_upload(tmp_path)
    intervals: list[tuple[str, float, float]] = []

    def timed_runner(upload_path, out_dir, title, target, log_path):
        started = time.monotonic()
        time.sleep(0.04)
        safe = safe_filename(title)
        package_dir = out_dir / safe
        package_dir.mkdir(parents=True, exist_ok=True)
        (package_dir / f"{safe}.{target}.player.html").write_text("player", encoding="utf-8")
        log_path.write_text("ok", encoding="utf-8")
        intervals.append((target, started, time.monotonic()))
        return 0

    store = JobStore(tmp_path, runner=timed_runner)
    results = store.create_jobs(upload, "digest", "Song", ["drums", "guitar", "bass"])
    _wait_until(lambda: all(store.get_job(job.id).status == "done" for job, _ in results))
    assert [target for target, _, _ in intervals] == ["guitar", "bass", "drums"]
    assert all(current[2] <= following[1] for current, following in zip(intervals, intervals[1:]))


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


def test_a_huge_single_line_log_still_produces_a_loadable_job_record(tmp_path):
    """Regression for the app writing a record it would then refuse to read:
    _tail read the whole log and stored the last 8 lines verbatim, so a
    runner emitting one multi-megabyte line (a progress bar with no newlines,
    a dumped payload) put that whole line in job.error and pushed the job
    JSON past MAX_JOB_FILE_BYTES. The failure looked normal at the time --
    and then the job vanished on the next start, quarantined by its own
    size check."""
    from bunri.web.jobs import MAX_ERROR_CHARS, MAX_JOB_FILE_BYTES

    upload = _make_upload(tmp_path)
    monster = "boom " * 250_000  # ~1.25 MB, not a single newline in it
    assert len(monster) > MAX_JOB_FILE_BYTES
    runner = FakeRunner(returncode=1, write_player=False, log_text=monster)
    store = JobStore(tmp_path, runner=runner)

    job, _ = store.create_job(upload, digest="d1", requested_title="Song")
    _wait_until(lambda: store.get_job(job.id).status == "error")

    failed = store.get_job(job.id)
    assert failed.error, "the failure must still be reported, just bounded"
    assert len(failed.error) <= MAX_ERROR_CHARS
    assert failed.error.endswith("boom "), "the informative end of the log is what's kept"

    record = tmp_path / "web" / "jobs" / f"{job.id}.json"
    written = record.stat().st_size
    assert written <= MAX_JOB_FILE_BYTES, f"wrote a record of {written} bytes"

    # The real point: a restart must still find the job, not quarantine it.
    reloaded = JobStore(tmp_path, runner=FakeRunner())
    survivor = reloaded.get_job(job.id)
    assert survivor is not None, "the job must survive a restart, not vanish"
    assert survivor.status == "error"
    assert _quarantined_names(tmp_path, job.id) == []


def test_an_over_long_title_is_truncated(tmp_path):
    from bunri.web.jobs import MAX_TITLE_CHARS

    upload = _make_upload(tmp_path)
    store = JobStore(tmp_path, runner=FakeRunner())

    job, _ = store.create_job(upload, digest="d1", requested_title="T" * (MAX_TITLE_CHARS * 3))

    assert len(job.title) == MAX_TITLE_CHARS
    assert job.title == "T" * MAX_TITLE_CHARS


def test_serializing_a_job_never_exceeds_the_record_limit(tmp_path):
    """The belt behind _tail's braces: even handed an error field no code
    path should ever produce, the writer must emit something the loader will
    accept -- and must not mutate the caller's job to get there."""
    from bunri.web.jobs import MAX_JOB_FILE_BYTES, _serialize_job_within_limit

    oversized = "E" * (MAX_JOB_FILE_BYTES * 2)
    job = Job(
        id="j-fat", digest="d1", title="Song", target="guitar", status="error",
        created_at="2026-01-01T00:00:00+00:00", error=oversized,
    )

    encoded = _serialize_job_within_limit(job)

    assert len(encoded.encode("utf-8")) <= MAX_JOB_FILE_BYTES
    assert job.error == oversized, "the in-memory job must be left untouched"
    restored = json.loads(encoded)
    assert restored["id"] == "j-fat"
    assert restored["status"] == "error"


def test_tail_of_a_normal_multi_line_log_is_unchanged_by_the_byte_cap(tmp_path):
    """The bounded read must not disturb ordinary logs: a small file is read
    whole, and its last n lines come back exactly as before."""
    from bunri.web.jobs import _tail

    log = tmp_path / "small.log"
    log.write_text("\n".join(f"line {i}" for i in range(1, 21)) + "\n", encoding="utf-8")
    assert _tail(log, 8, expected_logs_dir=tmp_path.resolve()).splitlines() == [f"line {i}" for i in range(13, 21)]

    empty = tmp_path / "empty.log"
    empty.write_text("", encoding="utf-8")
    assert _tail(empty, 8, expected_logs_dir=tmp_path.resolve()) == "(empty log)"

    assert (
        _tail(tmp_path / "does-not-exist.log", 8, expected_logs_dir=tmp_path.resolve())
        == "(log file unavailable)"
    )


def test_tail_drops_the_partial_first_line_when_it_reads_from_an_offset(tmp_path):
    """Reading only the tail starts mid-file, so the first thing in the
    window is the back half of a line we never saw the start of. Returning
    it as if it were a whole line would be a lie about the log's content."""
    from bunri.web.jobs import LOG_TAIL_READ_BYTES, _tail

    log = tmp_path / "big.log"
    filler = "F" * (LOG_TAIL_READ_BYTES * 2)  # pushes the window past the start
    log.write_text(f"{filler}\ncomplete line A\ncomplete line B\n", encoding="utf-8")

    result = _tail(log, 8, expected_logs_dir=tmp_path.resolve())

    assert result.splitlines() == ["complete line A", "complete line B"]
    assert "F" not in result, "the fragment of the truncated line must be dropped"


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
    # Same regex as bunri.package._safe_filename (duplicated deliberately,
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


def _spawn_marked_sleeper(*, own_group: bool = True):
    """A throwaway process whose command line contains "bunri.cli" (inside
    the -c payload), so the sidecar reaper's pid-recycling guard accepts it.

    `own_group` mirrors default_runner's start_new_session=True, which makes
    the CLI lead its own process group -- the only shape the reaper is
    willing to signal. Pass own_group=False to reproduce a sidecar written by
    an older server version, which the reaper must refuse to touch."""
    import subprocess
    import sys

    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)  # bunri.cli"],
        start_new_session=own_group,
    )


def test_terminate_pid_from_sidecar_kills_only_matching_processes(tmp_path):
    from bunri.web.jobs import TerminationOutcome, terminate_pid_from_sidecar

    log = tmp_path / "j-x.log"
    proc = _spawn_marked_sleeper()
    try:
        (tmp_path / "j-x.pid").write_text(str(proc.pid))
        assert terminate_pid_from_sidecar(log, expected_logs_dir=tmp_path.resolve()) is TerminationOutcome.STOPPED
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
        assert terminate_pid_from_sidecar(log, expected_logs_dir=tmp_path.resolve()) is TerminationOutcome.NOTHING_TO_STOP
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
        "time.sleep(60)  # bunri.cli\n"
    )
    return subprocess.Popen([sys.executable, "-c", code], start_new_session=True)


def test_terminate_pid_from_sidecar_escalates_to_sigkill_when_sigterm_is_ignored(tmp_path):
    from bunri.web.jobs import TerminationOutcome, terminate_pid_from_sidecar

    log = tmp_path / "j-y.log"
    proc = _spawn_sigterm_ignoring_sleeper()
    try:
        (tmp_path / "j-y.pid").write_text(str(proc.pid))
        # Short grace period so the SIGTERM poll window elapses quickly and
        # SIGKILL kicks in without slowing the suite down.
        assert (
            terminate_pid_from_sidecar(
                log, expected_logs_dir=tmp_path.resolve(), grace_seconds=0.5, poll_interval=0.05
            )
            is TerminationOutcome.STOPPED
        )
        _wait_until(lambda: proc.poll() is not None)
        assert not (tmp_path / "j-y.pid").exists(), (
            "sidecar must only be removed once the process is confirmed gone"
        )
    finally:
        if proc.poll() is None:
            proc.kill()


def test_terminate_pid_from_sidecar_keeps_sidecar_when_sigterm_raises_non_lookup_oserror(
    tmp_path, monkeypatch
):
    """A PermissionError (or any OSError other than ProcessLookupError) from
    the SIGTERM call means the process's actual fate is unknown -- unlike a
    confirmed-gone process, the sidecar must be *kept* (not discarded) so the
    next startup's recovery gets a chance to deal with whatever's actually
    there. Regression for a bug where the sidecar was unconditionally removed
    on any OSError from the SIGTERM call."""
    from bunri.web import jobs as jobs_module
    from bunri.web.jobs import TerminationOutcome, terminate_pid_from_sidecar

    log = tmp_path / "j-perm.log"
    proc = _spawn_marked_sleeper()
    try:
        (tmp_path / "j-perm.pid").write_text(str(proc.pid))

        real_killpg = jobs_module.os.killpg

        def fake_killpg(pgid, sig):
            if sig == 15:  # SIGTERM
                raise PermissionError("simulated: not permitted to signal this group")
            return real_killpg(pgid, sig)

        monkeypatch.setattr(jobs_module.os, "killpg", fake_killpg)

        result = terminate_pid_from_sidecar(log, expected_logs_dir=tmp_path.resolve())

        assert result is TerminationOutcome.FAILED
        assert (tmp_path / "j-perm.pid").exists(), (
            "sidecar must survive a SIGTERM failure whose outcome is unknown"
        )
    finally:
        if proc.poll() is None:
            proc.kill()


def test_terminate_pid_from_sidecar_removes_stale_sidecar_when_sigterm_finds_process_gone(
    tmp_path, monkeypatch
):
    """ProcessLookupError from the SIGTERM call (the process exited in the
    narrow window between our liveness check and the signal) is the one
    OSError case where the outcome *is* known -- the process is genuinely
    gone, so the sidecar is safe (and correct) to discard immediately,
    unlike the PermissionError case above."""
    from bunri.web import jobs as jobs_module
    from bunri.web.jobs import TerminationOutcome, terminate_pid_from_sidecar

    log = tmp_path / "j-gone.log"
    proc = _spawn_marked_sleeper()
    try:
        (tmp_path / "j-gone.pid").write_text(str(proc.pid))

        real_killpg = jobs_module.os.killpg
        calls: list[tuple[int, int]] = []
        simulated = {"done": False}

        def fake_killpg(pgid, sig):
            # Only the very first SIGTERM aimed at our target group is faked
            # as "process already gone" -- everything else (including the
            # test's own cleanup in `finally` below) must go through to the
            # real os.killpg, or this monkeypatch would interfere with
            # unrelated signalling for the rest of the test.
            if not simulated["done"] and pgid == proc.pid and sig == 15:
                simulated["done"] = True
                calls.append((pgid, sig))
                raise ProcessLookupError("simulated: process already exited")
            calls.append((pgid, sig))
            return real_killpg(pgid, sig)

        monkeypatch.setattr(jobs_module.os, "killpg", fake_killpg)

        result = terminate_pid_from_sidecar(log, expected_logs_dir=tmp_path.resolve())

        assert result is TerminationOutcome.NOTHING_TO_STOP
        assert not (tmp_path / "j-gone.pid").exists(), (
            "a confirmed-gone process's sidecar must still be removed"
        )
        assert calls == [(proc.pid, 15)], (
            "SIGKILL must never be attempted once SIGTERM already reports the process gone"
        )
    finally:
        if proc.poll() is None:
            proc.kill()


def _pid_running(pid: int) -> bool:
    """Liveness by signal 0 -- no `ps` parsing, and it works for a process
    this test never spawned directly (a grandchild reparented to init)."""
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, just not ours
    return True


def _spawn_marked_parent_with_child(grandchild_pid_file: Path, *, own_group: bool = True):
    """A marker-matching process that spawns a child of its own -- standing
    in for the CLI and its ffmpeg. It reports that child's pid to
    `grandchild_pid_file`, then sleeps.

    `own_group=True` mirrors default_runner's start_new_session=True (the
    whole tree shares one signallable process group); `own_group=False`
    reproduces the shape a pre-fix server left behind, where no group-wide
    signal is possible."""
    import subprocess
    import sys

    code = (
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
        "time.sleep(60)  # bunri.cli\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", code, str(grandchild_pid_file)], start_new_session=own_group
    )


def test_terminate_pid_from_sidecar_kills_the_whole_process_tree(tmp_path):
    """Regression for orphaned grandchildren: the CLI shells out to ffmpeg,
    so signalling only the pid in the sidecar leaves that ffmpeg running
    (observed live -- the parent took SIGTERM and died, the child kept
    going). It then keeps writing the same cache/package files that the
    job's re-run writes. default_runner puts the CLI in its own process
    group so the whole tree can be signalled at once; this checks a real
    two-level process tree is actually gone afterwards.
    """
    from bunri.web.jobs import TerminationOutcome, terminate_pid_from_sidecar

    log = tmp_path / "j-tree.log"
    grandchild_pid_file = tmp_path / "grandchild.pid"
    leader = _spawn_marked_parent_with_child(grandchild_pid_file)
    grandchild_pid = None
    try:
        _wait_until(
            lambda: grandchild_pid_file.exists() and grandchild_pid_file.read_text().strip() != ""
        )
        grandchild_pid = int(grandchild_pid_file.read_text().strip())
        assert _pid_running(grandchild_pid), "grandchild should be alive before we stop anything"

        (tmp_path / "j-tree.pid").write_text(str(leader.pid))
        outcome = terminate_pid_from_sidecar(
            log, expected_logs_dir=tmp_path.resolve(), grace_seconds=2.0, poll_interval=0.05
        )

        assert outcome is TerminationOutcome.STOPPED
        _wait_until(lambda: leader.poll() is not None)
        # The whole point: the leader dying is not enough -- its own child
        # (an ffmpeg in production) must have been signalled too.
        _wait_until(lambda: not _pid_running(grandchild_pid), timeout=10.0)
    finally:
        if leader.poll() is None:
            leader.kill()
        if grandchild_pid is not None and _pid_running(grandchild_pid):
            import os as _os

            _os.kill(grandchild_pid, 9)


def _write_running_job_file(out_dir: Path, job_id: str, upload: Path) -> None:
    """A job record left "running" by a previous server, with a pid sidecar
    at web/logs/<id>.pid -- the exact shape _load_and_recover has to reason
    about on startup."""
    jobs_dir = out_dir / "web" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "web" / "logs").mkdir(parents=True, exist_ok=True)
    (jobs_dir / f"{job_id}.json").write_text(json.dumps({
        "id": job_id, "digest": "dx", "title": "Song", "target": "guitar",
        "status": "running", "created_at": "2026-07-12T00:00:00+00:00",
        "started_at": "2026-07-12T00:00:01+00:00",
        "log": f"web/logs/{job_id}.log",
        "upload": str(upload.relative_to(out_dir)),
    }), encoding="utf-8")


def test_legacy_non_group_leader_sidecar_is_held_without_signalling_anything(tmp_path):
    """Regression for sidecars written before the CLI was given its own
    process group: that pid isn't a group leader, so there is no safe way to
    reach its children. killpg on it would address our *own* server's group,
    and signalling the lone pid is the orphaned-ffmpeg bug itself (the user
    saw outcome=stopped with the child still alive, and the job re-queued
    into a file fight with it).

    The reaper must therefore leave such a process completely alone, report
    FAILED, keep the sidecar, and let the recovery path hold the job -- which
    self-resolves once the old process exits on its own."""
    from bunri.web.jobs import TerminationOutcome, terminate_pid_from_sidecar

    upload = _make_upload(tmp_path)
    _write_running_job_file(tmp_path, "j-legacy", upload)
    logs_dir = tmp_path / "web" / "logs"
    child_pid_file = tmp_path / "legacy-child.pid"

    # Exactly what the current released server leaves behind: no new session.
    parent = _spawn_marked_parent_with_child(child_pid_file, own_group=False)
    child_pid = None
    try:
        _wait_until(lambda: child_pid_file.exists() and child_pid_file.read_text().strip() != "")
        child_pid = int(child_pid_file.read_text().strip())
        (logs_dir / "j-legacy.pid").write_text(str(parent.pid))

        outcome = terminate_pid_from_sidecar(
            logs_dir / "j-legacy.log", expected_logs_dir=logs_dir.resolve(),
            grace_seconds=1.0, poll_interval=0.05,
        )

        assert outcome is TerminationOutcome.FAILED
        assert parent.poll() is None, "a pid we cannot safely signal must be left running"
        assert _pid_running(child_pid), (
            "signalling the parent alone is the very bug this guards against -- "
            "its child would be orphaned rather than stopped"
        )
        assert (logs_dir / "j-legacy.pid").exists(), "the sidecar must be kept for a later retry"

        # ...and the recovery path must act on that verdict: no re-run.
        runner = FakeRunner()
        store = JobStore(tmp_path, runner=runner)
        time.sleep(0.3)
        assert store.get_job("j-legacy").status == "running"
        assert runner.calls == [], "the job must not be re-run beside the surviving process"
        assert (logs_dir / "j-legacy.pid").exists()
    finally:
        if parent.poll() is None:
            parent.kill()
        if child_pid is not None and _pid_running(child_pid):
            import os as _os

            _os.kill(child_pid, 9)


def _unhelpful_ps(kind: str):
    """A stand-in for `subprocess.run` that reproduces one way `ps` can fail
    to answer the "is this pid alive?" question. None of these mean "the
    process is gone", and all of them produce empty stdout -- which is
    exactly why the verdict cannot be based on stdout alone."""
    import subprocess

    def fake_run(*args, **kwargs):
        if kind == "ps-unavailable":
            raise OSError("simulated: cannot exec ps")
        if kind == "ps-timeout":
            raise subprocess.TimeoutExpired(cmd="ps", timeout=10)
        if kind == "ps-rejected-the-request":
            # macOS, verbatim shape: ps exits 1 like it does for a genuinely
            # absent pid, but complains on stderr instead of answering.
            return subprocess.CompletedProcess(
                args=["ps"], returncode=1, stdout="",
                stderr="ps: process id too large: 999999999\n",
            )
        if kind == "ps-unexpected-status":
            return subprocess.CompletedProcess(
                args=["ps"], returncode=2, stdout="", stderr="",
            )
        raise AssertionError(f"unknown ps failure kind: {kind}")

    return fake_run


@pytest.mark.parametrize(
    "failure_kind",
    ["ps-unavailable", "ps-timeout", "ps-rejected-the-request", "ps-unexpected-status"],
)
def test_unverifiable_liveness_holds_the_job_instead_of_assuming_the_process_is_gone(
    tmp_path, monkeypatch, failure_kind
):
    """Regression for collapsing "ps couldn't tell us" into "the process is
    gone": that made the reaper drop the sidecar and report NOTHING_TO_STOP,
    so the recovery path cheerfully re-ran a job whose previous process was
    demonstrably still alive (the user saw rerun_started=True with
    old_process_alive=True and the sidecar deleted). An unverifiable check
    must fail closed -- FAILED, sidecar kept, job held.

    The two non-exception cases matter just as much as the two exceptional
    ones: a `ps` that launches and then exits unsuccessfully prints nothing
    on stdout either, so judging by output alone reads it as "no such
    process" -- the same fail-open, just further along."""
    from bunri.web import jobs as jobs_module
    from bunri.web.jobs import TerminationOutcome, terminate_pid_from_sidecar

    upload = _make_upload(tmp_path)
    _write_running_job_file(tmp_path, "j-unknown", upload)
    logs_dir = tmp_path / "web" / "logs"

    orphan = _spawn_marked_sleeper()
    try:
        (logs_dir / "j-unknown.pid").write_text(str(orphan.pid))

        monkeypatch.setattr(jobs_module.subprocess, "run", _unhelpful_ps(failure_kind))

        outcome = terminate_pid_from_sidecar(
        logs_dir / "j-unknown.log", expected_logs_dir=logs_dir.resolve()
    )

        assert outcome is TerminationOutcome.FAILED
        assert (logs_dir / "j-unknown.pid").exists(), (
            "an unverifiable process must keep its sidecar, not have it deleted"
        )
        assert orphan.poll() is None

        runner = FakeRunner()
        store = JobStore(tmp_path, runner=runner)
        time.sleep(0.3)
        assert store.get_job("j-unknown").status == "running"
        assert runner.calls == [], "the job must not be re-run while liveness is unknown"
    finally:
        if orphan.poll() is None:
            orphan.kill()


def test_unreadable_sidecar_holds_the_job_but_a_missing_one_does_not(tmp_path, monkeypatch):
    """The three sidecar-read outcomes must not be lumped together: absent
    means "nothing to stop" (the common, healthy case), while an I/O error
    means we have no idea what the previous run left behind and must hold."""
    from bunri.web import jobs as jobs_module
    from bunri.web.jobs import TerminationOutcome, terminate_pid_from_sidecar

    logs_dir = tmp_path / "web" / "logs"
    logs_dir.mkdir(parents=True)

    # No sidecar at all -> nothing to stop.
    assert (
        terminate_pid_from_sidecar(
        logs_dir / "j-absent.log", expected_logs_dir=logs_dir.resolve()
    ) is TerminationOutcome.NOTHING_TO_STOP
    )

    sidecar = logs_dir / "j-unreadable.pid"
    sidecar.write_text("12345")
    real_open = jobs_module.os.open

    def denying_open(path, *args, **kwargs):
        if str(path).endswith("j-unreadable.pid"):
            raise PermissionError("simulated: sidecar not readable")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(jobs_module.os, "open", denying_open)

    assert (
        terminate_pid_from_sidecar(
        logs_dir / "j-unreadable.log", expected_logs_dir=logs_dir.resolve()
    ) is TerminationOutcome.FAILED
    )
    assert sidecar.exists(), "an unreadable sidecar must be kept, not silently dropped"


def test_garbage_in_the_sidecar_holds_the_job_and_says_it_needs_a_human(tmp_path, capsys):
    """A sidecar we can read but can't parse is the one held state that will
    never clear itself, so it must both hold the job and say plainly that
    someone has to remove the file."""
    from bunri.web.jobs import TerminationOutcome, terminate_pid_from_sidecar

    logs_dir = tmp_path / "web" / "logs"
    logs_dir.mkdir(parents=True)
    sidecar = logs_dir / "j-garbage.pid"
    sidecar.write_text("not-a-pid")

    assert terminate_pid_from_sidecar(
        logs_dir / "j-garbage.log", expected_logs_dir=logs_dir.resolve()
    ) is TerminationOutcome.FAILED
    assert sidecar.exists()
    err = capsys.readouterr().err
    assert "j-garbage.pid" in err
    assert "by hand" in err, "the operator must be told this one needs manual clearing"


def test_a_confirmed_dead_process_still_yields_nothing_to_stop(tmp_path):
    """The fail-closed handling above must not make the healthy path
    paranoid: when `ps` runs fine and reports no such process, that really is
    a stale sidecar -- drop it and let the job be re-queued."""
    from bunri.web.jobs import TerminationOutcome, terminate_pid_from_sidecar

    logs_dir = tmp_path / "web" / "logs"
    logs_dir.mkdir(parents=True)

    dead = _spawn_marked_sleeper()
    dead.kill()
    dead.wait()

    sidecar = logs_dir / "j-dead.pid"
    sidecar.write_text(str(dead.pid))

    assert terminate_pid_from_sidecar(
        logs_dir / "j-dead.log", expected_logs_dir=logs_dir.resolve()
    ) is TerminationOutcome.NOTHING_TO_STOP
    assert not sidecar.exists(), "a confirmed-stale sidecar is still cleaned up"


def test_ps_exit_status_decides_alive_vs_gone_vs_unknown(tmp_path, monkeypatch):
    """Pins the exact `ps` contract the reaper reads, since every branch
    above hangs off it and only one of the three produces non-empty stdout.
    rc 1 with a silent stderr is the real "no such process" answer on both
    macOS and Linux; anything noisier or stranger is no answer at all."""
    import subprocess

    from bunri.web import jobs as jobs_module

    def _ps_returning(returncode, stdout, stderr):
        return lambda *a, **kw: subprocess.CompletedProcess(
            args=["ps"], returncode=returncode, stdout=stdout, stderr=stderr
        )

    monkeypatch.setattr(
        jobs_module.subprocess, "run", _ps_returning(0, "python -m bunri.cli x\n", "")
    )
    assert jobs_module._process_command(4242) == "python -m bunri.cli x"

    monkeypatch.setattr(jobs_module.subprocess, "run", _ps_returning(1, "", ""))
    assert jobs_module._process_command(4242) == "", "rc 1 with a quiet stderr means gone"

    monkeypatch.setattr(
        jobs_module.subprocess, "run", _ps_returning(1, "", "ps: process id too large: 1\n")
    )
    assert jobs_module._process_command(4242) is None, "a complaining ps answered nothing"

    monkeypatch.setattr(jobs_module.subprocess, "run", _ps_returning(2, "", ""))
    assert jobs_module._process_command(4242) is None, "an unexpected status answered nothing"


def test_recovery_holds_a_job_whose_previous_process_could_not_be_stopped(
    tmp_path, monkeypatch, capsys
):
    """Regression for a recovery path that ignored the reaper's verdict: if
    the old run's process can't be confirmed stopped (no permission to
    signal it, or it survived SIGKILL), re-queueing the job starts a second
    process writing the very same cache/package files the survivor is still
    writing. Such a job must be held back -- left "running", not enqueued --
    and retried on the next start instead."""
    from bunri.web import jobs as jobs_module

    upload = _make_upload(tmp_path)
    jobs_dir = tmp_path / "web" / "jobs"
    logs_dir = tmp_path / "web" / "logs"
    jobs_dir.mkdir(parents=True)
    logs_dir.mkdir(parents=True)

    orphan = _spawn_marked_sleeper()
    blocking = {"on": True}
    try:
        (logs_dir / "j-stuck.pid").write_text(str(orphan.pid))
        (jobs_dir / "j-stuck.json").write_text(json.dumps({
            "id": "j-stuck", "digest": "dx", "title": "Song", "target": "guitar",
            "status": "running", "created_at": "2026-07-12T00:00:00+00:00",
            "started_at": "2026-07-12T00:00:01+00:00",
            "log": "web/logs/j-stuck.log",
            "upload": str(upload.relative_to(tmp_path)),
        }), encoding="utf-8")

        real_kill = jobs_module.os.kill
        real_killpg = jobs_module.os.killpg

        def deny(pid, sig):
            # Only the reaper's attempts on *our* orphan are denied; the
            # test's own cleanup below (and anything else in the process)
            # must still be able to signal normally.
            if blocking["on"] and pid == orphan.pid and sig in (9, 15):
                raise PermissionError("simulated: not permitted to signal this process")
            return real_kill(pid, sig)

        def deny_pg(pgid, sig):
            if blocking["on"] and pgid == orphan.pid and sig in (9, 15):
                raise PermissionError("simulated: not permitted to signal this group")
            return real_killpg(pgid, sig)

        monkeypatch.setattr(jobs_module.os, "kill", deny)
        monkeypatch.setattr(jobs_module.os, "killpg", deny_pg)

        runner = FakeRunner()
        store = JobStore(tmp_path, runner=runner)
        # Long enough for the worker to have picked the job up, had it been
        # (wrongly) enqueued.
        time.sleep(0.5)

        held = store.get_job("j-stuck")
        assert held.status == "running", "an unstoppable old run must not be re-queued"
        assert runner.calls == [], "the job must not be re-run while its old process may live"
        assert (logs_dir / "j-stuck.pid").exists(), (
            "the sidecar must survive so the next startup can retry the reap"
        )
        assert orphan.poll() is None, "the (undeniably un-signalled) orphan is still there"
        assert "j-stuck" in capsys.readouterr().err, "the held job must be reported on stderr"
    finally:
        blocking["on"] = False
        if orphan.poll() is None:
            orphan.kill()


def test_timezone_naive_datetime_is_quarantined(tmp_path):
    """A naive timestamp can't be subtracted from the UTC-aware "now" that
    web/app.py's _elapsed_seconds uses for a job with no finished_at, which
    turned the whole GET /api/jobs listing into a 500. Reject it at load."""
    _write_raw_job_file(
        tmp_path,
        "j-naive-start",
        _valid_job_payload(id="j-naive-start", started_at="2026-01-01T00:00:01"),
    )
    _write_raw_job_file(
        tmp_path,
        "j-naive-created",
        _valid_job_payload(id="j-naive-created", created_at="2026-01-01T00:00:00"),
    )

    store = JobStore(tmp_path, runner=FakeRunner())

    assert store.get_job("j-naive-start") is None
    assert _quarantined_names(tmp_path, "j-naive-start")
    assert store.get_job("j-naive-created") is None
    assert _quarantined_names(tmp_path, "j-naive-created")


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


def test_shutdown_retries_termination_until_a_late_pid_sidecar_appears(tmp_path):
    """Regression for the last shutdown race: `_run_job` marks a job
    "running" under the lock but releases it *before* calling the runner, so
    there is a window where the job is in shutdown()'s snapshot yet its pid
    sidecar doesn't exist yet. A shutdown that only tried to terminate once
    would find nothing to signal, then block in join() while the subprocess
    it missed was spawned right afterward -- leaving an orphan alive past
    join_timeout. shutdown() must keep retrying until the worker exits, so
    the sidecar is picked up as soon as it shows up.

    The fake runner here reproduces exactly that ordering: it reports the job
    as started, waits before publishing its (real, marker-matching) child's
    pid, and then blocks until that child is actually killed.
    """
    from bunri.web.jobs import _pid_sidecar

    upload = _make_upload(tmp_path)
    started = threading.Event()
    spawned: dict[str, Any] = {}

    def late_sidecar_runner(upload_path, out_dir, title, target, log_path):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("late-sidecar fake runner\n", encoding="utf-8")
        started.set()
        # The gap between "job is running" and "pid sidecar exists" -- long
        # enough that shutdown()'s first termination pass reliably lands
        # inside it.
        time.sleep(0.5)
        proc = _spawn_marked_sleeper()
        spawned["proc"] = proc
        _pid_sidecar(log_path).write_text(str(proc.pid))
        try:
            # Mirrors default_runner: block on the child, and only clean up
            # the sidecar once it's gone.
            proc.wait(timeout=30)
        finally:
            _pid_sidecar(log_path).unlink(missing_ok=True)
        return 1  # killed mid-run -> nonzero, no player written

    store = JobStore(tmp_path, runner=late_sidecar_runner)
    job, _ = store.create_job(upload, digest="d1", requested_title="Song")
    assert started.wait(timeout=5.0), "runner never started"
    _wait_until(lambda: store.get_job(job.id).status == "running")

    begin = time.monotonic()
    store.shutdown(join_timeout=15.0)
    elapsed = time.monotonic() - begin

    proc = spawned.get("proc")
    try:
        assert proc is not None, "runner never got to spawn its child"
        assert proc.poll() is not None, (
            "the subprocess whose sidecar appeared after shutdown()'s first "
            "termination pass must still have been terminated by a retry"
        )
        assert elapsed < 10.0, (
            f"shutdown() took {elapsed:.2f}s -- it waited out join_timeout "
            "instead of retrying the termination once the sidecar appeared"
        )
        # Interrupted by shutdown, not a genuine failure.
        assert store.get_job(job.id).status == "queued"
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()


def test_run_job_success_stays_done_even_if_the_stop_flag_becomes_set_mid_run(tmp_path):
    """If a job's subprocess finishes successfully right as shutdown begins
    -- the stop flag flips to set *while the job is already running*, not
    before it started -- it must land as done, not get force-reverted to
    queued merely because the flag ended up set by the time it finished.

    (A flag already set *before* the job is ever handed to _run_job is a
    different, earlier case: _run_job's own start-of-function gate refuses
    to start such a job at all -- see
    test_run_job_refuses_to_start_when_stopping_flag_already_set. This test
    has to flip the flag *from inside the runner call* to actually exercise
    the gate-passed-then-flag-set-mid-flight path, not the gate itself.)
    """
    upload = _make_upload(tmp_path)
    store = JobStore(tmp_path, runner=FakeRunner())
    succeeding_runner = FakeRunner(write_player=True, returncode=0)

    def sets_flag_mid_run_then_succeeds(upload_path, out_dir, title, target, log_path):
        store._stopping.set()  # simulates shutdown() racing in during the run
        return succeeding_runner(upload_path, out_dir, title, target, log_path)

    store._runner = sets_flag_mid_run_then_succeeds
    job = Job(
        id="j-manual", digest="d1", title="Song", target="guitar", status="queued",
        created_at="2026-01-01T00:00:00+00:00",
        log="web/logs/j-manual.log", upload=str(upload.relative_to(tmp_path)),
    )
    store._jobs[job.id] = job

    store._run_job(job)  # flag is unset when this call starts -- passes the gate

    assert store.get_job(job.id).status == "done"


def test_run_job_refuses_to_start_when_stopping_flag_already_set(tmp_path):
    """The authoritative, TOCTOU-safe check: if the stop flag is already set
    by the time _run_job acquires self._lock, it must not mark the job
    "running" (or invoke the runner) at all -- it must stay "queued",
    untouched. This is the check that closes the shutdown() race where a
    job could otherwise flip to running just after shutdown() already took
    its snapshot of what to SIGTERM."""
    upload = _make_upload(tmp_path)
    runner = FakeRunner()
    store = JobStore(tmp_path, runner=runner)
    job = Job(
        id="j-race", digest="d1", title="Song", target="guitar", status="queued",
        created_at="2026-01-01T00:00:00+00:00",
        log="web/logs/j-race.log", upload=str(upload.relative_to(tmp_path)),
    )
    store._jobs[job.id] = job
    store._write_job(job)
    store._stopping.set()

    store._run_job(job)

    after = store.get_job(job.id)
    assert after.status == "queued"
    assert after.started_at is None
    assert runner.calls == []


def test_shutdown_race_never_lets_a_job_start_after_the_running_snapshot(tmp_path, monkeypatch):
    """Regression for the TOCTOU race between _worker_loop pulling a job off
    the queue and _run_job actually marking it "running": if shutdown()'s
    flag-set + running-snapshot lands in that window, the job must never be
    allowed to start afterward (it would then run un-terminated and outlive
    join_timeout as an orphan for a real, long-running subprocess). Forced
    here by hooking _run_job to pause right before it would acquire the
    lock, firing shutdown() into that exact window from another thread, and
    only then letting the worker proceed."""
    upload = _make_upload(tmp_path)
    runner = FakeRunner()
    store = JobStore(tmp_path, runner=runner)

    about_to_call_run_job = threading.Event()
    let_worker_proceed = threading.Event()
    real_run_job = store._run_job

    def hooked_run_job(job):
        about_to_call_run_job.set()
        let_worker_proceed.wait(timeout=5.0)
        return real_run_job(job)

    monkeypatch.setattr(store, "_run_job", hooked_run_job)

    job, _ = store.create_job(upload, digest="d1", requested_title="Song")
    assert about_to_call_run_job.wait(timeout=5.0), "worker never reached _run_job"
    # At this point the job is still "queued" -- the worker is parked in the
    # hook, deliberately before _run_job's own lock/flag check.
    assert store.get_job(job.id).status == "queued"

    shutdown_thread = threading.Thread(target=store.shutdown)
    shutdown_thread.start()
    # Give shutdown() a real chance to set the flag and take its (empty,
    # since the job isn't "running" yet) snapshot before releasing the
    # worker into the race window.
    time.sleep(0.1)
    let_worker_proceed.set()
    shutdown_thread.join(timeout=10.0)

    final = store.get_job(job.id)
    # With the fix, shutdown() having set the flag first means _run_job's
    # own locked check refuses to start the job -- it never runs at all.
    # Under the old (buggy) code, _run_job unconditionally marked it
    # "running" and invoked the runner regardless of the flag, which is
    # exactly the orphaned-subprocess bug this test guards against.
    assert final.status == "queued"
    assert final.started_at is None
    assert runner.calls == []


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
        "upload": "web/uploads/song.mp3",
    }
    payload.update(overrides)
    # `log` and `package` are derived values the loader requires to match the
    # record's own id/title/target -- a job may only write its own log and
    # only claim its own package. So the fixture derives them too, unless a
    # test is deliberately overriding one to check that very rule.
    payload.setdefault("log", f"web/logs/{payload['id']}.log")
    safe = safe_filename(payload["title"])
    payload.setdefault("package", f"{safe}/{safe}.{payload['target']}.player.html")
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


def test_invalid_utf8_bytes_are_quarantined_and_startup_continues(tmp_path):
    """A job file whose *bytes* aren't valid UTF-8 fails during decoding,
    before JSON parsing ever gets a look -- a UnicodeDecodeError, not a
    JSONDecodeError. Catching only the latter let one corrupted file abort
    JobStore construction outright, taking the whole server's startup with
    it instead of quarantining the file and carrying on."""
    jobs_dir = tmp_path / "web" / "jobs"
    jobs_dir.mkdir(parents=True)
    (jobs_dir / "j-bad-bytes.json").write_bytes(b"\xff\xfe{\"id\": \"j-bad-bytes\"}")
    _write_raw_job_file(tmp_path, "j-readable", _valid_job_payload(id="j-readable"))

    store = JobStore(tmp_path, runner=FakeRunner())

    names = [p.name for p in jobs_dir.iterdir()]
    assert "j-bad-bytes.json" not in names, "the undecodable file must be moved aside"
    quarantined = _quarantined_names(tmp_path, "j-bad-bytes")
    assert len(quarantined) == 1, names
    # Quarantine keeps the original bytes intact for anyone who wants to
    # inspect them, under a name that can't collide with another bad file.
    assert (jobs_dir / quarantined[0]).read_bytes().startswith(b"\xff\xfe")
    assert store.get_job("j-readable") is not None, "other jobs must still load"


def test_deeply_nested_json_is_quarantined_and_startup_continues(tmp_path):
    """json.loads recurses once per nesting level, so a few thousand nested
    arrays exhaust the stack and raise RecursionError -- which is a
    RuntimeError, not a ValueError, and so slipped straight past the
    decode/parse handler and aborted JobStore construction. ~20KB of "[[[["
    is enough to take the whole server's startup down."""
    jobs_dir = tmp_path / "web" / "jobs"
    jobs_dir.mkdir(parents=True)
    depth = 10_000
    (jobs_dir / "j-deep.json").write_text("[" * depth + "]" * depth, encoding="utf-8")
    _write_raw_job_file(tmp_path, "j-shallow", _valid_job_payload(id="j-shallow"))

    store = JobStore(tmp_path, runner=FakeRunner())

    names = [p.name for p in jobs_dir.iterdir()]
    assert "j-deep.json" not in names
    assert len(_quarantined_names(tmp_path, "j-deep")) == 1, names
    assert store.get_job("j-shallow") is not None, "other jobs must still load"


def test_oversized_job_file_is_quarantined_without_being_parsed(tmp_path):
    """Belt to the RecursionError braces: a job record is a few hundred
    bytes, so anything past the size limit is rejected on its stat alone --
    never read into memory, never handed to the parser."""
    from bunri.web.jobs import MAX_JOB_FILE_BYTES

    jobs_dir = tmp_path / "web" / "jobs"
    jobs_dir.mkdir(parents=True)
    # Syntactically valid JSON, just absurdly large: a job-shaped object
    # padded with thousands of fields Job has never heard of.
    bloat = _valid_job_payload(id="j-huge")
    bloat.update({f"junk-{i}": "x" * 256 for i in range(6000)})
    payload = json.dumps(bloat)
    assert len(payload) > MAX_JOB_FILE_BYTES, "the fixture must actually exceed the limit"
    (jobs_dir / "j-huge.json").write_text(payload, encoding="utf-8")
    _write_raw_job_file(tmp_path, "j-small", _valid_job_payload(id="j-small"))

    store = JobStore(tmp_path, runner=FakeRunner())

    assert store.get_job("j-huge") is None
    assert len(_quarantined_names(tmp_path, "j-huge")) == 1
    assert store.get_job("j-small") is not None


def test_a_job_file_at_exactly_the_size_limit_still_loads(tmp_path):
    """Pins the boundary itself: the cap rejects files *over* the limit, so
    a record of exactly MAX_JOB_FILE_BYTES must still load. Sized to the byte
    (ASCII padding only, so len(json) == st_size) -- a comfortably smaller
    fixture would pass even if the check regressed to `>=`, which is exactly
    the mistake this guards against.
    """
    from bunri.web.jobs import MAX_JOB_FILE_BYTES

    jobs_dir = tmp_path / "web" / "jobs"
    jobs_dir.mkdir(parents=True)
    path = jobs_dir / "j-chatty.json"

    # Grow the (ASCII) error tail until the encoded record lands exactly on
    # the limit. json.dumps adds no surprises here: every padding byte costs
    # exactly one byte in the output.
    payload = _valid_job_payload(id="j-chatty", status="error", error="")
    padding = MAX_JOB_FILE_BYTES - len(json.dumps(payload))
    assert padding > 0, "the limit must leave room for a real record"
    payload["error"] = "x" * padding
    encoded = json.dumps(payload)
    assert len(encoded) == MAX_JOB_FILE_BYTES

    path.write_text(encoded, encoding="utf-8")
    assert path.stat().st_size == MAX_JOB_FILE_BYTES, "fixture must sit exactly on the boundary"

    store = JobStore(tmp_path, runner=FakeRunner())

    assert store.get_job("j-chatty") is not None, "a file *at* the limit is not over it"
    assert _quarantined_names(tmp_path, "j-chatty") == []


def test_a_job_file_one_byte_over_the_size_limit_is_quarantined(tmp_path):
    """The other half of the boundary: one byte past the limit is rejected.
    Together with the test above this pins the comparison exactly, so
    neither loosening nor tightening it by one can slip through."""
    from bunri.web.jobs import MAX_JOB_FILE_BYTES

    jobs_dir = tmp_path / "web" / "jobs"
    jobs_dir.mkdir(parents=True)
    path = jobs_dir / "j-onebyte.json"

    payload = _valid_job_payload(id="j-onebyte", status="error", error="")
    payload["error"] = "x" * (MAX_JOB_FILE_BYTES - len(json.dumps(payload)) + 1)
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert path.stat().st_size == MAX_JOB_FILE_BYTES + 1

    store = JobStore(tmp_path, runner=FakeRunner())

    assert store.get_job("j-onebyte") is None
    assert len(_quarantined_names(tmp_path, "j-onebyte")) == 1


def test_lone_surrogate_in_a_field_is_quarantined_and_startup_continues(tmp_path):
    """A JSON `"\\ud800"` escape decodes to a lone surrogate: a perfectly
    ordinary Python str that passes every schema check, and that Python
    then refuses to encode back to UTF-8. A queued record holding one used
    to be accepted, get re-saved by the recovery path, and raise
    UnicodeEncodeError out of JobStore construction -- never quarantined, so
    every subsequent start failed the same way.

    Reading a record has to imply being able to write it back, so this is
    now a validation failure like any other."""
    payload = _valid_job_payload(
        id="j-surrogate", status="queued", title="Bad\ud800Title",
        started_at=None, finished_at=None, package=None,
    )
    _write_raw_job_file(tmp_path, "j-surrogate", payload)
    _write_raw_job_file(tmp_path, "j-clean", _valid_job_payload(id="j-clean"))

    store = JobStore(tmp_path, runner=FakeRunner())

    assert store.get_job("j-surrogate") is None
    assert len(_quarantined_names(tmp_path, "j-surrogate")) == 1
    assert store.get_job("j-clean") is not None, "other jobs must still load"


def test_a_record_that_cannot_be_saved_back_is_quarantined_on_load(tmp_path):
    """The read limit and the write limit have to agree. A compact record of
    exactly MAX_JOB_FILE_BYTES passes the size check on the way in, but the
    canonical form is indented, so writing it back overflows -- and with the
    bulk in a field the serializer cannot trim (`error` is the only one it
    can), there is no way to save it at all. Accepting it meant the recovery
    path re-saved it oversized and the *next* start quarantined it: the job
    vanishing one restart later, with nothing reported at the time it was
    actually lost.

    `digest` carries the bulk here rather than `title`, which has a cap of
    its own -- this is specifically the size check, not that one."""
    from bunri.web.jobs import MAX_JOB_FILE_BYTES, _serialize_job_within_limit

    jobs_dir = tmp_path / "web" / "jobs"
    jobs_dir.mkdir(parents=True)

    payload = _valid_job_payload(
        id="j-unsavable", status="queued", digest="",
        started_at=None, finished_at=None, package=None, error=None,
    )
    payload["digest"] = "d" * (MAX_JOB_FILE_BYTES - len(json.dumps(payload)))
    compact = json.dumps(payload)
    assert len(compact) == MAX_JOB_FILE_BYTES, "fixture must be accepted by the read-side check"
    (jobs_dir / "j-unsavable.json").write_text(compact, encoding="utf-8")

    # ...and it really is unsavable: the canonical form overflows, and no
    # amount of error-trimming helps, because the bulk is in the title.
    fat = Job.from_dict(payload)
    assert len(_serialize_job_within_limit(fat).encode("utf-8")) > MAX_JOB_FILE_BYTES

    _write_raw_job_file(tmp_path, "j-slim", _valid_job_payload(id="j-slim"))

    store = JobStore(tmp_path, runner=FakeRunner())

    assert store.get_job("j-unsavable") is None, "an unsavable record must never be admitted"
    assert len(_quarantined_names(tmp_path, "j-unsavable")) == 1
    assert store.get_job("j-slim") is not None


def test_a_record_cannot_point_its_log_at_the_users_audio(tmp_path):
    """Regression for arbitrary file overwrite. `default_runner` opens the log
    with "wb", so honouring a record's stored `log` meant a hand-edited (or
    corrupted) record could name any file under out_dir and have the server
    truncate it -- observed live: pointing a queued job's log at the user's
    own source audio replaced the audio with log text on the next restart,
    no request needed. The log location is derived from the job id now, and a
    record claiming anything else is refused."""
    upload = _make_upload(tmp_path)
    victim = tmp_path / "Song" / "Song.original.mp3"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"the user's irreplaceable source audio")
    original = victim.read_bytes()

    jobs_dir = tmp_path / "web" / "jobs"
    jobs_dir.mkdir(parents=True)
    (jobs_dir / "j-evil.json").write_text(json.dumps({
        "id": "j-evil", "digest": "dx", "title": "Song", "target": "guitar",
        "status": "queued", "created_at": "2026-07-12T00:00:00+00:00",
        "log": "Song/Song.original.mp3",
        "upload": str(upload.relative_to(tmp_path)),
    }), encoding="utf-8")

    store = JobStore(tmp_path, runner=FakeRunner(returncode=1, write_player=False))
    try:
        time.sleep(0.4)  # let the worker do whatever it is going to do
        assert victim.read_bytes() == original, "the user's audio was overwritten"
        assert store.get_job("j-evil") is None, "a record claiming another file's path"
        assert len(_quarantined_names(tmp_path, "j-evil")) == 1
    finally:
        store.shutdown(join_timeout=5.0)


def test_a_symlinked_log_directory_cannot_be_used_to_write_outside_out_dir(tmp_path):
    """The other half of the same bug, and the half no string check can see:
    with `out/web/logs/escape` symlinked to a directory elsewhere, a record
    whose log pointed through it wrote outside out_dir entirely. Containment
    is therefore checked after resolve(), on the real path."""
    out_dir = tmp_path / "out"
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_bytes(b"nothing in out_dir may touch this")

    upload = _make_upload(out_dir)
    logs_dir = out_dir / "web" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "escape").symlink_to(outside, target_is_directory=True)

    jobs_dir = out_dir / "web" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / "j-escape.json").write_text(json.dumps({
        "id": "j-escape", "digest": "dx", "title": "Song", "target": "guitar",
        "status": "queued", "created_at": "2026-07-12T00:00:00+00:00",
        "log": "web/logs/escape/secret.txt",
        "upload": str(upload.relative_to(out_dir)),
    }), encoding="utf-8")

    store = JobStore(out_dir, runner=FakeRunner(returncode=1, write_player=False))
    try:
        time.sleep(0.4)
        assert secret.read_bytes() == b"nothing in out_dir may touch this"
        assert store.get_job("j-escape") is None
        assert len(_quarantined_names(out_dir, "j-escape")) == 1
    finally:
        store.shutdown(join_timeout=5.0)


def _fake_popen_factory(monkeypatch, pid: int = 4242):
    """Make default_runner's spawn a no-op, so its *file* operations -- the
    truncating log open and the pid sidecar write, which are the dangerous
    ones -- run for real without launching the separation CLI (and torch)."""
    from bunri.web import jobs as jobs_module

    class _InstantProc:
        def __init__(self):
            self.pid = pid

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(jobs_module.subprocess, "Popen", lambda *a, **kw: _InstantProc())


@pytest.mark.parametrize("planted", ["log", "pid"])
@pytest.mark.parametrize("target", ["inside", "outside"])
def test_a_symlink_standing_at_a_canonical_log_path_is_refused(
    tmp_path, monkeypatch, planted, target
):
    """Deriving the log path is not enough on its own: the derived names are
    entirely predictable, so a symlink planted *at* `web/logs/<id>.log` (or
    at its `.pid`) is followed by an ordinary open, and `default_runner`
    opens the log with O_TRUNC. "Does it resolve inside out_dir?" says yes to
    a link pointing at the user's own audio two directories over, so the
    final component is opened with O_NOFOLLOW instead.

    The `.pid` half matters just as much and had no check at all: the sidecar
    is written with the process id, so a link there overwrites its target
    with a number.
    """
    from bunri.web.jobs import default_runner

    out_dir = tmp_path / "out"
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)
    logs_dir = out_dir / "web" / "logs"
    logs_dir.mkdir(parents=True)

    if target == "inside":
        victim = out_dir / "Song" / "Song.original.mp3"
        victim.parent.mkdir(parents=True)
    else:
        victim = outside / "secret.txt"
    victim.write_bytes(b"a file this server was never asked to touch")
    original = victim.read_bytes()

    log_path = logs_dir / "j-plant.log"
    (logs_dir / f"j-plant.{'log' if planted == 'log' else 'pid'}").symlink_to(victim)

    upload = _make_upload(out_dir)
    _fake_popen_factory(monkeypatch)

    refused = None
    try:
        default_runner(upload, out_dir, "Song", "guitar", log_path)
    except OSError as exc:
        refused = exc

    # The damage first: that is what actually matters, and it makes the
    # failure message say what was destroyed rather than what wasn't raised.
    assert victim.read_bytes() == original, (
        f"the {planted} symlink was followed and {victim} overwritten"
    )
    assert refused is not None, "the runner must refuse, not silently skip the write"


@pytest.mark.parametrize("relocated", ["web/logs", "web"])
def test_a_relocated_logs_directory_cannot_be_used_to_write_outside_out_dir(
    tmp_path, monkeypatch, relocated
):
    """A symlink at `web/logs` (or at `web`) is the case a "does the parent
    resolve to the logs directory?" check cannot see, because it is the logs
    directory that moved: both sides of that comparison resolve to the link's
    target and it passes by tautology. O_NOFOLLOW does not help either -- it
    only ever guards the last component, and the intermediate ones are
    followed as normal.

    With ordinary files waiting at the far end, a perfectly valid job then
    truncates them: the log with O_TRUNC, the sidecar with its pid. So the
    expected directory is built from out_dir's own resolved location plus the
    literal components, which a relocated `web` or `logs` can never equal.
    """
    from bunri.web.jobs import default_runner

    out_dir = tmp_path / "out"
    outside = tmp_path / "outside"
    out_dir.mkdir()

    # Whatever is relocated, the victims sit where the job's own files would
    # land once the link is followed.
    victim_dir = outside if relocated == "web/logs" else outside / "logs"
    victim_dir.mkdir(parents=True)
    victims = {}
    for name in ("j-x.log", "j-x.pid"):
        victims[name] = victim_dir / name
        victims[name].write_bytes(f"{name}: a real file, nothing to do with this server".encode())
    originals = {name: p.read_bytes() for name, p in victims.items()}

    link = out_dir / relocated
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)

    upload = out_dir / "web" / "uploads" / "song.mp3"
    upload.parent.mkdir(parents=True, exist_ok=True)
    upload.write_bytes(b"fake-audio")
    _fake_popen_factory(monkeypatch)

    refused = None
    try:
        default_runner(upload, out_dir, "Song", "guitar", out_dir / "web" / "logs" / "j-x.log")
    except OSError as exc:
        refused = exc

    for name, path in victims.items():
        assert path.read_bytes() == originals[name], (
            f"{relocated} was followed and {path} overwritten"
        )
    assert refused is not None, "the runner must refuse to write through a relocated logs directory"


def test_a_relocated_logs_directory_fails_the_job_but_not_the_server(tmp_path):
    """The same attack seen from the outside: the job fails, the log outside
    the tree is untouched, and the server keeps running."""
    from bunri.web.jobs import default_runner

    out_dir = tmp_path / "out"
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)
    victim = outside / "j-reloc.log"
    victim.write_bytes(b"a file outside the output tree")

    (out_dir / "web").mkdir(parents=True)
    (out_dir / "web" / "logs").symlink_to(outside, target_is_directory=True)
    upload = _make_upload(out_dir)

    jobs_dir = out_dir / "web" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / "j-reloc.json").write_text(json.dumps({
        "id": "j-reloc", "digest": "dx", "title": "Song", "target": "guitar",
        "status": "queued", "created_at": "2026-07-12T00:00:00+00:00",
        "log": "web/logs/j-reloc.log",
        "upload": str(upload.relative_to(out_dir)),
    }), encoding="utf-8")

    store = JobStore(out_dir, runner=default_runner)
    try:
        _wait_until(lambda: store.get_job("j-reloc").status == "error", timeout=10.0)
        assert victim.read_bytes() == b"a file outside the output tree"
        # The server is still serving: another job still goes through the
        # normal path (and fails on its own merits, not on a dead worker).
        assert store.get_job("j-reloc").error
    finally:
        store.shutdown(join_timeout=5.0)


def test_a_relocated_jobs_directory_is_neither_read_nor_written(tmp_path):
    """`web/jobs` has the same fixed, predictable name as `web/logs`, and the
    same consequence when it is replaced by a symlink -- except that here the
    damage needs no job to run at all. Starting the server was enough: the
    loader renamed a stranger's unparseable JSON to `.bad-*` (quarantine),
    and the recovery path rewrote a stranger's valid-looking one.

    So a jobs directory that is not really `<out_dir>/web/jobs` is not read,
    not renamed and not written. The job list comes up empty, which is
    recoverable by fixing the directory; rewriting someone else's files is
    not.
    """
    out_dir = tmp_path / "out"
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)

    victim_bad = outside / "j-victim.json"
    victim_bad.write_text("{ definitely not json", encoding="utf-8")
    victim_good = outside / "j-good.json"
    victim_good.write_text(json.dumps({
        "id": "j-good", "digest": "dx", "title": "Song", "target": "guitar",
        "status": "queued", "created_at": "2026-07-12T00:00:00+00:00",
        "log": "web/logs/j-good.log", "upload": "web/uploads/song.mp3",
    }), encoding="utf-8")
    originals = {p: p.read_bytes() for p in (victim_bad, victim_good)}
    before = sorted(p.name for p in outside.iterdir())

    (out_dir / "web").mkdir(parents=True)
    (out_dir / "web" / "jobs").symlink_to(outside, target_is_directory=True)

    store = JobStore(out_dir, runner=FakeRunner())  # must not raise
    try:
        time.sleep(0.4)
        for path, content in originals.items():
            assert path.read_bytes() == content, f"{path} was rewritten"
        assert sorted(p.name for p in outside.iterdir()) == before, (
            "a file outside the output tree was renamed (quarantined)"
        )
        assert store.list_jobs() == [], "nothing may be loaded from a relocated jobs directory"
    finally:
        store.shutdown(join_timeout=5.0)


def test_a_failed_pid_write_stops_the_subprocess_instead_of_orphaning_it(tmp_path, monkeypatch):
    """Opening the sidecar before spawning covers the usual case -- we refuse
    to start a process whose pid we cannot record. It does not cover the
    write itself failing, which happens *after* the spawn: at that point
    there is a live subprocess whose pid is about to be lost, and nothing
    would ever reap it. Both shutdown and the next startup's recovery find
    processes only through the sidecar, so the job's re-run would separate
    the same song alongside the survivor, into the same cache.

    So the write failing stops the process there and then. The job fails,
    which is fine; an orphan is not.
    """
    from bunri.web import jobs as jobs_module
    from bunri.web.jobs import default_runner

    upload = _make_upload(tmp_path)
    log_path = tmp_path / "web" / "logs" / "j-orphan.log"

    spawned = {}
    real_popen = jobs_module.subprocess.Popen

    def recording_popen(cmd, **kwargs):
        # A harmless, long-lived stand-in for the separation CLI, spawned
        # through the real code path (its own session and all).
        proc = real_popen(
            [sys.executable, "-c", "import time; time.sleep(60)  # bunri.cli"], **kwargs
        )
        spawned["proc"] = proc
        return proc

    monkeypatch.setattr(jobs_module.subprocess, "Popen", recording_popen)

    # The sidecar opens fine and only the *write* fails -- which is the whole
    # point: an open failure is already handled by refusing to spawn, and the
    # gap is the window after the process exists.
    real_opener = jobs_module._open_in_logs_dir

    def failing_pid_write(path, *, expected_logs_dir, mode):
        opened = real_opener(path, expected_logs_dir=expected_logs_dir, mode=mode)
        if not str(path).endswith("j-orphan.pid"):
            return opened

        class _WriteRefused:
            def __enter__(self):
                opened.__enter__()
                return self

            def __exit__(self, *exc):
                return opened.__exit__(*exc)

            def fileno(self):
                return opened.fileno()

            def write(self, data):
                raise OSError("simulated: cannot write the pid")

            def flush(self):
                pass

        return _WriteRefused()

    monkeypatch.setattr(jobs_module, "_open_in_logs_dir", failing_pid_write)

    proc = None
    try:
        with pytest.raises(OSError):
            default_runner(upload, tmp_path, "Song", "guitar", log_path)

        proc = spawned.get("proc")
        assert proc is not None, "the spawn must have happened for this to test anything"
        _wait_until(lambda: proc.poll() is not None, timeout=5.0)
        assert not _pid_running(proc.pid), (
            "a subprocess whose pid could not be recorded must not be left running -- "
            "nothing would ever find it again"
        )
        assert not (tmp_path / "web" / "logs" / "j-orphan.pid").exists()
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()


def test_a_relocated_web_directory_gets_nothing_created_inside_it(tmp_path):
    """Refusing to *write* outside out_dir is not enough if the refusal comes
    after the directories are made. `mkdir(parents=True)` follows a symlink
    and builds the rest of the tree on the far side, so with `web -> outside`
    and nothing there yet, starting the server and posting an upload left an
    `uploads/` and a `logs/` behind before anything objected.

    Each component is created and checked one at a time now, so an empty
    directory stays empty.
    """
    import io

    from fastapi.testclient import TestClient

    from bunri.web.app import create_app

    out_dir = tmp_path / "out"
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)  # deliberately empty
    out_dir.mkdir(parents=True)
    (out_dir / "web").symlink_to(outside, target_is_directory=True)

    app = create_app(out_dir, runner=FakeRunner())
    with TestClient(app) as client:
        res = client.post(
            "/api/jobs", files={"file": ("song.mp3", io.BytesIO(b"abc"), "audio/mpeg")}
        )
        assert res.status_code == 500
        assert client.get("/api/jobs").status_code == 200  # the server is still up

    assert list(outside.iterdir()) == [], (
        f"created {[p.name for p in outside.iterdir()]} outside the output tree"
    )


def test_a_relocated_uploads_directory_refuses_the_upload(tmp_path):
    """The mirror of `_resolved_upload`, on the write side: uploads are saved
    into a fixed name, so if that name has been pointed elsewhere the bytes
    must not be written at all."""
    import io

    from fastapi.testclient import TestClient

    from bunri.web.app import create_app

    out_dir = tmp_path / "out"
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)
    (out_dir / "web").mkdir(parents=True)
    (out_dir / "web" / "uploads").symlink_to(outside, target_is_directory=True)

    app = create_app(out_dir, runner=FakeRunner())
    with TestClient(app) as client:
        res = client.post(
            "/api/jobs", files={"file": ("song.mp3", io.BytesIO(b"abc"), "audio/mpeg")}
        )

    assert res.status_code == 500
    assert list(outside.iterdir()) == [], "not one byte may be written outside the tree"


def test_a_running_record_whose_log_path_names_no_file_is_quarantined_not_fatal(tmp_path):
    """`log: "/"` passes every type, status and encoding check, and then the
    recovery path derives a pid sidecar from it -- `Path("/").with_suffix()`,
    which raises ValueError. That happens outside any per-file handler, so
    startup died outright and the record was never quarantined: every restart
    died the same way, and the server could not be brought up at all."""
    upload = _make_upload(tmp_path)
    jobs_dir = tmp_path / "web" / "jobs"
    jobs_dir.mkdir(parents=True)
    (tmp_path / "web" / "logs").mkdir(parents=True)
    (jobs_dir / "j-rootlog.json").write_text(json.dumps({
        "id": "j-rootlog", "digest": "dx", "title": "Song", "target": "guitar",
        "status": "running", "created_at": "2026-07-12T00:00:00+00:00",
        "started_at": "2026-07-12T00:00:01+00:00",
        "log": "/",
        "upload": str(upload.relative_to(tmp_path)),
    }), encoding="utf-8")
    _write_raw_job_file(tmp_path, "j-ok", _valid_job_payload(id="j-ok"))

    store = JobStore(tmp_path, runner=FakeRunner())  # must not raise

    assert store.get_job("j-rootlog") is None
    assert len(_quarantined_names(tmp_path, "j-rootlog")) == 1
    assert store.get_job("j-ok") is not None, "other jobs must still load"


@pytest.mark.parametrize("bad_path", ["/", ".", "..", "/etc/passwd", "../../escape"])
def test_path_fields_must_be_relative_and_name_a_file(tmp_path, bad_path):
    """Everything in a record is resolved against out_dir, so a path field
    that is absolute, climbs out with "..", or names no file at all is not a
    record this server can work with -- and two of those would let it read or
    serve files from outside the output directory."""
    _write_raw_job_file(
        tmp_path, "j-badpath", _valid_job_payload(id="j-badpath", log=bad_path)
    )

    store = JobStore(tmp_path, runner=FakeRunner())

    assert store.get_job("j-badpath") is None
    assert len(_quarantined_names(tmp_path, "j-badpath")) == 1


def test_a_multibyte_package_path_is_reserved_in_bytes_not_characters(tmp_path):
    """The admission check reserves room for fields a transition will
    overwrite, and that reservation has to be measured in bytes.

    Here the stored `package` has *more characters* than the one the job will
    derive from its title on success, but far fewer bytes -- the derived one
    is multi-byte. A character-count comparison therefore reserves nothing,
    the record is admitted, and succeeding pushes it over the limit, so the
    job is quarantined on the next start.

    Either outcome is acceptable; what must not happen is accepting it and
    then writing it oversized."""
    from bunri.web.jobs import MAX_JOB_FILE_BYTES

    upload = _make_upload(tmp_path)
    jobs_dir = tmp_path / "web" / "jobs"
    jobs_dir.mkdir(parents=True)
    (tmp_path / "web" / "logs").mkdir(parents=True)

    title = "曲" * 60  # derives a ~380-byte package path from ~140 characters
    payload = {
        "id": "j-mb", "digest": "", "title": title, "target": "guitar",
        "status": "queued", "created_at": "2026-07-12T00:00:00+00:00",
        "started_at": None, "finished_at": None, "error": None,
        "package": "x" * 150,  # more characters, a quarter of the bytes
        "log": "web/logs/j-mb.log",
        "upload": str(upload.relative_to(tmp_path)),
    }
    pad = MAX_JOB_FILE_BYTES - 200 - len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    payload["digest"] = "d" * pad
    (jobs_dir / "j-mb.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )

    store = JobStore(tmp_path, runner=FakeRunner())
    try:
        job = store.get_job("j-mb")
        if job is None:
            assert len(_quarantined_names(tmp_path, "j-mb")) == 1
            return  # refused up front: also a correct answer
        _wait_until(lambda: store.get_job("j-mb").status in ("done", "error"), timeout=10.0)
        written = (jobs_dir / "j-mb.json").stat().st_size
        assert written <= MAX_JOB_FILE_BYTES, (
            f"accepted the record, then grew it to {written} bytes by running -- "
            "it will be quarantined on the next start"
        )
    finally:
        store.shutdown(join_timeout=5.0)


def test_an_over_long_title_is_rejected_on_load(tmp_path):
    """`package` is derived from the title at roughly twice its length, so a
    record with an unbounded title grows *after* it is admitted, the moment
    the job succeeds -- and `error` being the only trimmable field means the
    writer can't recover from it. The cap create_job applies is therefore
    enforced on the way in too. Nothing this app writes can trip it."""
    from bunri.web.jobs import MAX_TITLE_CHARS

    _write_raw_job_file(
        tmp_path, "j-longtitle",
        _valid_job_payload(id="j-longtitle", title="T" * (MAX_TITLE_CHARS + 1)),
    )
    _write_raw_job_file(
        tmp_path, "j-attheborder",
        _valid_job_payload(id="j-attheborder", title="T" * MAX_TITLE_CHARS),
    )

    store = JobStore(tmp_path, runner=FakeRunner())

    assert store.get_job("j-longtitle") is None
    assert len(_quarantined_names(tmp_path, "j-longtitle")) == 1
    assert store.get_job("j-attheborder") is not None, "the cap itself is not over it"


def test_an_accepted_record_can_always_be_written_back(tmp_path):
    """The property both tests above exist to establish, checked directly on
    the healthy path: whatever the loader admits, _write_job can save --
    within the limit and without an encoding error."""
    from bunri.web.jobs import MAX_JOB_FILE_BYTES, _job_record_bytes

    _write_raw_job_file(tmp_path, "j-normal", _valid_job_payload(id="j-normal"))
    _write_raw_job_file(
        tmp_path, "j-utf8",
        _valid_job_payload(id="j-utf8", title="曲名 — ライブ音源 🎸", status="queued",
                           started_at=None, finished_at=None, package=None),
    )

    store = JobStore(tmp_path, runner=FakeRunner())

    for job_id in ("j-normal", "j-utf8"):
        job = store.get_job(job_id)
        assert job is not None, f"{job_id} should have been accepted"
        assert _job_record_bytes(job) <= MAX_JOB_FILE_BYTES
        store._write_job(job)  # must not raise
        written = (tmp_path / "web" / "jobs" / f"{job_id}.json").read_text(encoding="utf-8")
        assert json.loads(written)["id"] == job_id

    assert store.get_job("j-utf8").title == "曲名 — ライブ音源 🎸"


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
    from bunri.web import jobs as jobs_module

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
    from bunri.web import jobs as jobs_module

    captured: dict[str, Any] = {}

    class _FakeProc:
        pid = 12345

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
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
    # The CLI must lead its own process group, or the shutdown/recovery
    # reaper can only signal the CLI itself and its ffmpeg children survive
    # (see test_terminate_pid_from_sidecar_kills_the_whole_process_tree).
    assert captured["kwargs"].get("start_new_session") is True
