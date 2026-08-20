"""Job store + worker for the StemLab web UI.

Design (see .claude/plans/stemlab-web-plan.md):

* Jobs are persisted as one JSON file per job under ``<out>/web/jobs/`` so
  they survive server restarts and browser closes/reopens. Writes go to a
  temp file in the same directory followed by ``os.replace`` for atomicity.
* A single daemon worker thread drains a queue and runs jobs strictly
  sequentially (MPS has nothing to gain from parallel separations, per the
  plan). The actual separation happens in a subprocess running the existing,
  already-tested `stemlab` CLI (`python -m stemlab.cli ...`) -- never
  in-process -- so this module never needs to import torch / audio_separator
  and a crash in the separator can't take the web server down with it.
* The subprocess launcher (``runner``) is constructor-injectable, mirroring
  this repo's FakeSeparator pattern (see tests/test_separate.py): tests swap
  in a fake that fabricates a player.html (or an error) instantly instead of
  actually running the CLI.
* On startup, any job left ``running`` (server crashed mid-job) or ``queued``
  (server stopped before the worker got to it) is re-queued -- the
  separation cache under out/.cache makes a re-run cheap.

This module deliberately duplicates package.py's ``_safe_filename`` regex
rather than importing it: package.py transitively pulls in separate.py,
audio.py etc., and while none of *those* import torch at module scope
either, keeping the web layer's import graph fully disjoint from the
separation stack is the simplest way to guarantee `import stemlab.web.app`
never accidentally grows slow or heavy as those modules evolve.
"""

from __future__ import annotations

import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

# 2 hours: generous upper bound for a single separation + export on CPU.
DEFAULT_TIMEOUT_SECONDS = 2 * 60 * 60

# Same rule as stemlab.package._safe_filename -- see module docstring for why
# this is duplicated rather than imported.
_UNSAFE_CHARS = re.compile(r'[/\\:*?"<>|#%]')


def safe_filename(title: str) -> str:
    # See stemlab.package._safe_filename's comments for why leading dots are
    # stripped after substitution and why a "web" result is renamed --
    # identical rule, duplicated here on purpose.
    slug = _UNSAFE_CHARS.sub("_", title).strip().lstrip(".")
    if not slug:
        return "untitled"
    if slug.casefold() == "web":
        return "web-package"
    return slug


def _now_iso() -> str:
    # Microsecond precision (not just seconds) so two jobs created in quick
    # succession still sort correctly by created_at ("新しい順" in the job
    # list) instead of tying.
    return datetime.now(timezone.utc).isoformat()


def new_job_id() -> str:
    # Sortable-ish (timestamp prefix) + random suffix to avoid collisions
    # from two uploads landing in the same second ("ulid風" per the plan,
    # without pulling in a ulid dependency for one id format).
    return f"j-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{secrets.token_hex(4)}"


# A runner takes (upload_path, out_dir, title, target, log_path) and returns
# the process's exit code (0 == success). Real jobs use `default_runner`;
# tests inject fakes (see tests/test_web_jobs.py).
Runner = Callable[[Path, Path, str, str, Path], int]


def _pid_sidecar(log_path: Path) -> Path:
    """Where the runner advertises its live child's pid. The recovery path
    (and the server's shutdown hook) use this to find and stop a separation
    subprocess that outlived -- or is about to outlive -- its parent server;
    without it, a server restart mid-job leaves an orphan running AND
    re-queues the same job, so two subprocesses end up writing the same
    cache/package files concurrently (observed in practice)."""
    return log_path.with_suffix(".pid")


def _process_command(pid: int) -> str:
    """Command line of a live process, or "" if it's gone / unreadable.
    `ps` is fine here: this project only targets macOS and Linux."""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _process_alive(pid: int, marker: str) -> bool:
    """True if `pid` is still running *and* still looks like the process we
    signalled (marker still in its command line) -- once it exits, `ps` finds
    nothing for that pid (or the pid may already have been recycled by an
    unrelated process, which fails the marker check too)."""
    return marker in _process_command(pid)


class TerminationOutcome(Enum):
    """What `terminate_pid_from_sidecar` actually managed to do. Callers must
    distinguish these three: "nothing was there" and "it's confirmed gone"
    both mean the job's old subprocess can no longer touch the cache/package
    files, but "we couldn't stop it" means it may still be running and
    writing them, so the job must NOT be re-run yet."""

    #: No sidecar, an unreadable one, or one pointing at a process that is
    #: already gone / whose pid was recycled to something unrelated. Nothing
    #: needed stopping; any stale sidecar has been cleaned up.
    NOTHING_TO_STOP = "nothing_to_stop"
    #: A live, marker-matching process was signalled and confirmed gone (its
    #: process group swept along with it). The sidecar has been removed.
    STOPPED = "stopped"
    #: We tried and can't confirm success -- the signal failed with something
    #: other than "already gone" (e.g. PermissionError), or the process was
    #: still alive after SIGKILL. The sidecar is deliberately kept so the
    #: next startup's recovery gets another chance at it.
    FAILED = "failed"


def _leads_own_process_group(pid: int) -> bool:
    """True if `pid` is the leader of its own process group (pgid == pid),
    which is what `default_runner`'s ``start_new_session=True`` arranges.

    This distinction is a safety gate, not a nicety: `os.killpg(pid, ...)`
    signals *the group whose id equals that number*. If the recorded pid is
    not a group leader (a sidecar written by an older version of this code,
    or any process launched without its own session), that number is some
    other group's -- quite possibly our own server's -- and killpg'ing it
    would take down the whole server, or nothing at all. So group-signalling
    is only ever used when we know the pid leads the group.
    """
    try:
        return os.getpgid(pid) == pid
    except OSError:
        return False


def _signal_tree(pid: int, sig: int, *, group: bool) -> None:
    """Send `sig` to `pid`'s whole process group (when `pid` leads it) or to
    `pid` alone otherwise. Raises the same OSErrors os.kill/os.killpg do --
    ProcessLookupError in particular means the target is already gone."""
    if group:
        os.killpg(pid, sig)
    else:
        os.kill(pid, sig)


def terminate_pid_from_sidecar(
    log_path: Path,
    *,
    marker: str = "stemlab.cli",
    grace_seconds: float = 5.0,
    poll_interval: float = 0.1,
) -> TerminationOutcome:
    """Stop the process a pid sidecar points at, but only if its command line
    still contains `marker` -- pids get recycled, and we must never kill an
    unrelated process that happens to have inherited the number.

    The whole *process group* is signalled, not just the recorded pid: the
    CLI shells out to ffmpeg (and audio_separator spawns its own helpers), so
    signalling only the pid we recorded leaves those grandchildren running --
    observed live, an ffmpeg outliving the CLI that was SIGTERM'd above it.
    `default_runner` starts the CLI with ``start_new_session=True`` precisely
    so its pid *is* its process group id and one killpg reaches the whole
    tree. See `_leads_own_process_group` for why a pid that doesn't lead its
    own group is signalled individually instead.

    SIGTERM first, then poll for up to `grace_seconds` for the leader to
    actually exit; if it's still alive after that, escalate to SIGKILL and
    poll again. Once the leader is confirmed gone, one final best-effort
    SIGKILL sweeps any group member that outlived it (an ffmpeg that ignored
    or was slow on SIGTERM); an empty group answers ProcessLookupError, which
    is the normal case and is ignored.

    The sidecar is only removed once the process is confirmed gone -- a job
    resuming that pid before it has actually died would race the exiting
    subprocess over the same cache/package files. If it *still* hasn't died
    even after SIGKILL (a stuck zombie/defunct, essentially never in
    practice), the sidecar is left in place and FAILED is returned so the
    next startup's recovery path gets another chance at it.

    A stale sidecar (process already gone, or its pid recycled to an
    unrelated process) is removed immediately, same as before. If the SIGTERM
    itself fails with something other than "the process is already gone"
    (e.g. PermissionError -- we don't own it), the process's actual state is
    unknown, so the sidecar is kept rather than discarded, same as the
    "still alive after SIGKILL" case. See `TerminationOutcome` for what each
    return value obliges the caller to do.
    """
    sidecar = _pid_sidecar(log_path)
    try:
        pid = int(sidecar.read_text().strip())
    except (OSError, ValueError):
        return TerminationOutcome.NOTHING_TO_STOP

    if pid <= 0 or not _process_alive(pid, marker):
        sidecar.unlink(missing_ok=True)
        return TerminationOutcome.NOTHING_TO_STOP

    # Decided *before* any signal: once the leader dies its pgid can no
    # longer be looked up, and we still need to know whether the final sweep
    # below is allowed to address the group.
    group = _leads_own_process_group(pid)

    def _wait_until_gone(deadline: float) -> bool:
        while time.monotonic() < deadline:
            if not _process_alive(pid, marker):
                return True
            time.sleep(poll_interval)
        return not _process_alive(pid, marker)

    def _sweep_group() -> None:
        """Best-effort SIGKILL of whatever is left in the group after its
        leader is gone. Only ever runs for a group we just verified was led
        by our marker-matching process, so it can't address a stranger's
        group -- barring the leader's pid being reaped and immediately
        recycled *as a new group leader* in the microseconds since the check
        above, which pids-are-handed-out-sequentially makes vanishingly
        unlikely and which no cheaper check can rule out."""
        if not group:
            return
        try:
            os.killpg(pid, 9)  # SIGKILL
        except OSError:
            pass  # empty group (the normal case), or not ours to signal

    try:
        _signal_tree(pid, 15, group=group)  # SIGTERM
    except ProcessLookupError:
        # The process exited between our liveness check above and this
        # signal -- genuinely gone, so the sidecar is stale now too.
        sidecar.unlink(missing_ok=True)
        return TerminationOutcome.NOTHING_TO_STOP
    except OSError:
        # Some other failure (e.g. PermissionError -- we don't own the
        # process) means we don't actually know whether it's still alive.
        # Don't destroy the recovery information: leave the sidecar in
        # place so the next startup's recovery gets another chance at it,
        # same as the "still alive after SIGKILL" case below.
        return TerminationOutcome.FAILED

    if not _wait_until_gone(time.monotonic() + grace_seconds):
        try:
            _signal_tree(pid, 9, group=group)  # SIGKILL
        except OSError:
            pass  # already gone between our last check and here
        if not _wait_until_gone(time.monotonic() + grace_seconds):
            # Still alive (or un-reapable) after SIGKILL -- leave the sidecar
            # for the next startup's recovery to retry, and tell the caller
            # this job's old process is not safely gone.
            return TerminationOutcome.FAILED

    _sweep_group()
    sidecar.unlink(missing_ok=True)
    return TerminationOutcome.STOPPED


def default_runner(
    upload_path: Path,
    out_dir: Path,
    title: str,
    target: str,
    log_path: Path,
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> int:
    """Run the existing `stemlab` CLI as a subprocess (never in-process --
    see module docstring). PATH-independent: uses sys.executable -m rather
    than relying on a `stemlab` script being on PATH.

    ``start_new_session=True`` puts the CLI in a brand-new session, so it
    leads its own process group and every descendant it spawns (ffmpeg,
    audio_separator's helpers) inherits that group id. Every stop path --
    `terminate_pid_from_sidecar` on shutdown/recovery, and the timeout
    escalation below -- then signals the group rather than the lone pid,
    which is what stops an ffmpeg from outliving the CLI above it and
    writing the cache concurrently with the job's later re-run."""
    cmd = [
        sys.executable,
        "-m",
        "stemlab.cli",
        str(upload_path),
        "-o",
        str(out_dir),
        "--title",
        title,
        "--target",
        target,
    ]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar = _pid_sidecar(log_path)
    with log_path.open("wb") as log_f:
        proc = subprocess.Popen(
            cmd, stdout=log_f, stderr=subprocess.STDOUT, start_new_session=True
        )
        sidecar.write_text(str(proc.pid))
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Group-wide, for the same reason the shutdown path is: killing
            # only the CLI would leave its ffmpeg running long past the
            # timeout we just declared. Fall back to the lone process if the
            # group can't be signalled (it should always be ours, given
            # start_new_session above).
            try:
                os.killpg(proc.pid, 9)  # SIGKILL
            except OSError:
                proc.kill()
            proc.wait()
            log_f.write(
                f"\n[stemlab-web] timed out after {timeout}s; process killed\n".encode()
            )
            return 124
        finally:
            sidecar.unlink(missing_ok=True)


@dataclass
class Job:
    id: str
    digest: str
    title: str
    target: str
    status: str  # queued | running | done | error
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    package: Optional[str] = None  # "<safe>/<safe>.<target>.player.html", relative to out_dir
    log: Optional[str] = None  # "web/logs/<id>.log", relative to out_dir
    # Not part of the plan's illustrative schema, but needed by the worker to
    # find the file it must feed the subprocess; relative to out_dir like the
    # other paths above.
    upload: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


_VALID_STATUSES = {"queued", "running", "done", "error"}
# Must exist and be a string; every other field is Optional[str] (may be
# absent or None).
_REQUIRED_STR_FIELDS = ("id", "digest", "title", "target", "status", "created_at")
_OPTIONAL_DATETIME_FIELDS = ("started_at", "finished_at")
_OPTIONAL_STR_FIELDS = ("error", "package", "log", "upload")


def _datetime_problem(value: str, name: str) -> Optional[str]:
    """None if `value` is an ISO-8601 timestamp this app can actually do
    arithmetic with, else why it isn't.

    Timezone-awareness is a hard requirement, not a nicety: everything this
    app writes is UTC-aware (`_now_iso`), and web/app.py's `_elapsed_seconds`
    subtracts `started_at` from an aware "now" for a still-running job.
    Python refuses to subtract an aware datetime from a naive one, so a
    single hand-written naive timestamp in one job file used to raise
    TypeError and turn the *entire* GET /api/jobs listing into a 500. Reject
    it at load time (the file gets quarantined) so one bad record can't take
    the job list down with it.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return f"unparseable ISO-8601 datetime in {name!r}: {value!r}"
    if parsed.utcoffset() is None:
        return f"timezone-naive datetime in {name!r} (a UTC offset is required): {value!r}"
    return None


def _validate_job_record(data: Any, expected_id: str) -> Optional[str]:
    """Check a decoded job JSON payload against Job's schema before it's
    trusted enough to build a Job from and drive the worker with. Returns
    None if `data` is well-formed, else a short human-readable reason it
    isn't -- a hand-edited or truncated-write job file must never crash
    startup or silently corrupt the in-memory job table for every *other*
    job too.

    `expected_id` is the job id implied by the filename (`<id>.json`): a
    mismatch means the file was renamed/copied/hand-edited into
    inconsistency with its own name, which would otherwise let it silently
    shadow (or be shadowed by) a different job's record.
    """
    if not isinstance(data, dict):
        return "not a JSON object"
    for name in _REQUIRED_STR_FIELDS:
        if not isinstance(data.get(name), str):
            return f"missing or non-string required field: {name!r}"
    if data["status"] not in _VALID_STATUSES:
        return f"unknown status: {data['status']!r}"
    if data["id"] != expected_id:
        return f"id {data['id']!r} does not match filename (expected {expected_id!r})"
    for name in _OPTIONAL_DATETIME_FIELDS:
        value = data.get(name)
        if value is None:
            continue
        if not isinstance(value, str):
            return f"non-string datetime field: {name!r}"
        problem = _datetime_problem(value, name)
        if problem is not None:
            return problem
    problem = _datetime_problem(data["created_at"], "created_at")
    if problem is not None:
        return problem
    for name in _OPTIONAL_STR_FIELDS:
        value = data.get(name)
        if value is not None and not isinstance(value, str):
            return f"non-string field: {name!r}"
    return None


class JobStore:
    """File-backed job queue + sequential worker.

    One instance is created per running server (see web/app.py's
    create_app). `out_dir` is where practice packages, uploads, job records
    and logs all live -- the same directory the `stemlab` CLI's `-o` points
    at, so packages produced by jobs land exactly where the API expects to
    find and serve them.
    """

    def __init__(self, out_dir: Path, runner: Runner = default_runner) -> None:
        self.out_dir = Path(out_dir)
        self.jobs_dir = self.out_dir / "web" / "jobs"
        self.logs_dir = self.out_dir / "web" / "logs"
        self.uploads_dir = self.out_dir / "web" / "uploads"
        for d in (self.jobs_dir, self.logs_dir, self.uploads_dir):
            d.mkdir(parents=True, exist_ok=True)

        self._runner = runner
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        # None is the shutdown sentinel (see shutdown()/`_worker_loop`): it's
        # never a real job id, which always starts with "j-".
        self._queue: "queue.Queue[str | None]" = queue.Queue()
        self._stopping = threading.Event()

        self._load_and_recover()

        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="stemlab-web-worker")
        self._worker.start()

    # -- persistence --------------------------------------------------
    def _job_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def _write_job(self, job: Job) -> None:
        path = self._job_path(job.id)
        tmp = path.with_suffix(f".json.tmp-{secrets.token_hex(4)}")
        tmp.write_text(json.dumps(job.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)  # atomic on the same filesystem

    def _quarantine_job_file(self, path: Path, reason: str) -> None:
        """Rename an unreadable/invalid job file out of the way so it stops
        being picked up on every future startup, without ever clobbering an
        earlier quarantine of a different bad file (hence the retry-on-
        collision loop, however unlikely a collision actually is with an
        8-hex-char random suffix)."""
        while True:
            dest = path.with_name(f"{path.name}.bad-{secrets.token_hex(4)}")
            if not dest.exists():
                break
        try:
            path.rename(dest)
        except OSError as exc:
            print(f"[stemlab-web] failed to quarantine {path.name}: {exc}", file=sys.stderr)
            return
        print(
            f"[stemlab-web] quarantined invalid job file {path.name} -> {dest.name}: {reason}",
            file=sys.stderr,
        )

    def _load_and_recover(self) -> None:
        for path in sorted(self.jobs_dir.glob("j-*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                self._quarantine_job_file(path, f"unreadable/invalid JSON: {exc}")
                continue
            # The id a well-formed file for this path *should* have -- ".json"
            # stripped from the filename, matching how _job_path/_write_job
            # name job files.
            expected_id = path.stem
            reason = _validate_job_record(data, expected_id)
            if reason is not None:
                self._quarantine_job_file(path, reason)
                continue
            job = Job.from_dict(data)
            self._jobs[job.id] = job

        # Anything that didn't reach a terminal state last time this process
        # ran gets re-queued: `running` means the server died mid-job,
        # `queued` means it never got picked up. A `running` job's subprocess
        # may have survived the old server (children aren't killed with their
        # parent) -- reap it via its pid sidecar first, or the re-queued run
        # and the orphan would separate the same song concurrently into the
        # same cache/package files.
        pending = [j for j in self._jobs.values() if j.status in ("running", "queued")]
        pending.sort(key=lambda j: j.created_at)
        for job in pending:
            if job.status == "running" and job.log:
                outcome = terminate_pid_from_sidecar(self.out_dir / job.log)
                if outcome is TerminationOutcome.FAILED:
                    # We could not confirm the previous run's process tree is
                    # gone (no permission to signal it, or it survived
                    # SIGKILL). Re-queueing now is exactly the bug this
                    # guard exists for: the surviving process keeps writing
                    # the same cache/package files while a fresh run writes
                    # them too. So hold the job: leave it "running" on disk,
                    # untouched, and don't enqueue it. Next startup calls
                    # this same path again and retries the reap -- and until
                    # then find_reusable() treating it as in-flight is the
                    # behaviour we want, since a duplicate upload must not
                    # start a competing run either.
                    print(
                        f"[stemlab-web] job {job.id}: previous run's process could not be "
                        f"confirmed stopped (pid sidecar kept); leaving it 'running' and "
                        f"NOT re-running it -- will retry on the next start",
                        file=sys.stderr,
                    )
                    continue
            job.status = "queued"
            job.started_at = None
            self._write_job(job)
            self._queue.put(job.id)

    def shutdown(self, *, join_timeout: float = 15.0, poll_interval: float = 0.2) -> None:
        """Graceful-shutdown hook (wired to the app's lifespan): stop the
        currently running separation subprocess, if any, so a normal server
        stop doesn't orphan it or mark its job an error just because the
        process serving it went away. A SIGKILLed server can't run this --
        that case is covered by the sidecar reaping in _load_and_recover on
        the next start.

        Order matters here:
          1. Under `self._lock`, set the stop flag *and* snapshot which
             job(s) are currently "running" in the same critical section.
             This is the TOCTOU fix: `_run_job` (see below) only ever
             transitions a job queued->running inside that same lock, after
             checking the stop flag. So whichever of "shutdown sets the flag
             and takes its snapshot" or "_run_job checks the flag and starts
             the job" gets the lock first, the other sees a fully consistent
             result of that decision -- either the job was already running
             when the flag was set (so it's in this snapshot and gets
             SIGTERM'd below), or the flag was already set when _run_job
             looked (so it refuses to start the job at all, leaving it
             "queued"). There is no window where a job starts running
             *after* this snapshot was taken, which is what used to let a
             job slip past shutdown() undetected and outlive join_timeout.
          2. Push the sentinel so a worker idle in `queue.get()` wakes up and
             exits immediately, instead of `join()` below blocking for
             `join_timeout` for no reason.
          3. Repeatedly SIGTERM (then SIGKILL if needed) the in-flight
             subprocess, if any, interleaved with short `join()` polls, until
             the worker exits or `join_timeout` is spent. Terminating the
             subprocess is what makes the worker's blocking
             `self._runner(...)` call in `_run_job` actually return; the
             *retrying* is what closes the remaining race described below.
          4. Joining the worker (in those same polls) is what keeps this
             method -- and therefore the ASGI lifespan's teardown -- from
             returning while a job's finishing touches to its record are
             still in flight.

        Why step 3 is a retry loop rather than one pass: `_run_job` releases
        `self._lock` after flipping a job to "running" and only *then* calls
        the runner, which spawns the subprocess and writes its pid sidecar.
        So a job can legitimately be in the snapshot above while its sidecar
        does not exist yet, and a single termination pass would find nothing
        to signal, leave the subprocess to be spawned a moment later, and
        then outlive `join_timeout` as an orphan -- the very failure the
        lock-based snapshot was meant to prevent. Retrying until the worker
        actually exits means the sidecar is picked up on a later iteration,
        however late it appears. (Re-snapshotting each round is safe and
        avoids poking at jobs that have already finished: no job can enter
        "running" after the stop flag is set -- see `_run_job`'s gate -- so
        the set of running jobs only ever shrinks from here.)
        """
        with self._lock:
            self._stopping.set()
            running = [j for j in self._jobs.values() if j.status == "running" and j.log]
        self._queue.put(None)

        deadline = time.monotonic() + join_timeout
        while True:
            for job in running:
                terminate_pid_from_sidecar(self.out_dir / job.log)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._worker.join(timeout=min(poll_interval, remaining))
            if not self._worker.is_alive():
                return
            with self._lock:
                running = [j for j in self._jobs.values() if j.status == "running" and j.log]

    # -- queries --------------------------------------------------------
    def list_jobs(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def find_reusable(self, digest: str, target: str) -> Optional[Job]:
        """Dedup lookup: a finished job for this digest+target wins outright
        (no re-run needed); otherwise an already queued/running one wins (so
        a second upload of the same file while the first is still working
        doesn't queue a duplicate). Returns None if neither exists."""
        with self._lock:
            candidates = sorted(
                (j for j in self._jobs.values() if j.digest == digest and j.target == target),
                key=lambda j: j.created_at,
                reverse=True,
            )
        for j in candidates:
            if j.status == "done":
                return j
        for j in candidates:
            if j.status in ("queued", "running"):
                return j
        return None

    # -- title / folder collision handling -------------------------------
    def _resolve_title(self, requested_title: str, digest: str) -> str:
        """Same folder-naming rule as package.py's _safe_filename, plus a
        -2/-3/... suffix when a *different* digest already used that safe
        folder name (see the plan's "タイトル衝突" section). A digest that
        already owns the folder (e.g. a retried job after a previous error)
        is not a collision -- it reuses its own name."""
        owners: dict[str, set[str]] = {}
        with self._lock:
            for j in self._jobs.values():
                owners.setdefault(safe_filename(j.title), set()).add(j.digest)

        candidate_title = requested_title
        n = 2
        while True:
            candidate_safe = safe_filename(candidate_title)
            existing_owners = owners.get(candidate_safe)
            if digest in (existing_owners or ()):
                return candidate_title  # this digest already owns the folder
            # A folder no job record owns (built by the CLI directly, or its
            # job JSON was lost/corrupted) must be treated as a collision too:
            # build_package writes into an existing folder without hesitation,
            # so reusing the name would overwrite someone else's package.
            folder_taken = existing_owners is not None or (self.out_dir / candidate_safe).exists()
            if not folder_taken:
                return candidate_title
            candidate_title = f"{requested_title}-{n}"
            n += 1

    # -- mutation ---------------------------------------------------------
    def create_job(
        self, upload_path: Path, digest: str, requested_title: str, target: str = "guitar"
    ) -> tuple[Job, bool]:
        """Returns (job, created). created is False when an existing
        done/queued/running job for this digest+target was reused instead of
        starting a new one."""
        with self._lock:
            reusable = self.find_reusable(digest, target)
            if reusable is not None:
                return reusable, False

            job_id = new_job_id()
            title = self._resolve_title(requested_title, digest)
            try:
                upload_rel = str(upload_path.relative_to(self.out_dir))
            except ValueError:
                upload_rel = str(upload_path)
            job = Job(
                id=job_id,
                digest=digest,
                title=title,
                target=target,
                status="queued",
                created_at=_now_iso(),
                log=str(Path("web") / "logs" / f"{job_id}.log"),
                upload=upload_rel,
            )
            self._jobs[job_id] = job
            self._write_job(job)
            self._queue.put(job_id)
            return job, True

    # -- worker -------------------------------------------------------------
    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:  # shutdown sentinel -- see shutdown()
                return
            if self._stopping.is_set():
                # Cheap fast path only (unlocked read) -- skips the log-dir
                # setup etc. below for the common case of jobs still waiting
                # in the queue ahead of the sentinel once a shutdown is
                # already well underway. Not load-bearing for correctness:
                # _run_job re-checks the flag under self._lock right before
                # it would mark the job "running", and *that* check is the
                # one that actually closes the TOCTOU against a racing
                # shutdown() (see shutdown()'s docstring). A job skipped
                # here stays "queued" on disk untouched, ready for the next
                # startup's recovery to pick up.
                continue
            job = self.get_job(job_id)
            if job is None:
                continue
            self._run_job(job)

    def _run_job(self, job: Job) -> None:
        with self._lock:
            if self._stopping.is_set():
                # A shutdown raced us here, between the worker pulling this
                # job off the queue and this lock acquisition: refuse to
                # start it. This check runs inside the same lock shutdown()
                # holds while it sets the stop flag and snapshots which
                # job(s) are "running" (see shutdown()'s docstring) -- that
                # shared lock is what guarantees no job can ever flip to
                # "running" *after* shutdown() has already taken its
                # snapshot of what to SIGTERM, closing the race where such a
                # job would start anyway and still be running past
                # join_timeout. Left "queued" (its current, unmodified,
                # on-disk state) for the next startup's recovery.
                return
            job.status = "running"
            job.started_at = _now_iso()
            self._write_job(job)

        upload_path = self.out_dir / job.upload if job.upload else None
        log_path = self.out_dir / job.log if job.log else self.logs_dir / f"{job.id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            if upload_path is None or not upload_path.exists():
                raise RuntimeError(f"upload file missing: {job.upload}")
            rc = self._runner(upload_path, self.out_dir, job.title, job.target, log_path)
        except Exception as exc:  # runner itself blew up (not the subprocess exit code)
            rc = 1
            with log_path.open("a", encoding="utf-8") as f:
                f.write(f"\n[stemlab-web] runner raised: {exc!r}\n")

        safe = safe_filename(job.title)
        expected_player = self.out_dir / safe / f"{safe}.{job.target}.player.html"
        succeeded = rc == 0 and expected_player.exists()

        with self._lock:
            if not succeeded and self._stopping.is_set():
                # A shutdown killed this job's subprocess mid-run (see
                # shutdown()): that's not a genuine failure, so don't record
                # one. Reset it to "queued" -- exactly the state
                # _load_and_recover() would put a `running` job into on the
                # next startup -- so it just re-runs then instead of showing
                # the user a permanent error for something the server itself
                # interrupted. (If it *did* succeed -- e.g. it finished right
                # as shutdown began -- fall through to the normal "done"
                # branch below instead: a completed job must stay done.)
                job.status = "queued"
                job.started_at = None
                self._write_job(job)
                return
            job.finished_at = _now_iso()
            if succeeded:
                job.status = "done"
                job.package = f"{safe}/{safe}.{job.target}.player.html"
                job.error = None
            else:
                job.status = "error"
                job.error = _tail(log_path, 8)
            self._write_job(job)


def _tail(log_path: Path, n: int) -> str:
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(log file unavailable)"
    return "\n".join(lines[-n:]) if lines else "(empty log)"
