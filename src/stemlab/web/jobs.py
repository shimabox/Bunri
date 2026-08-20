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

# Job records we write are a few hundred bytes (see Job's fields -- ids,
# statuses, a couple of paths); 1 MiB leaves four orders of magnitude of
# headroom for a title full of multi-byte characters and a long error tail,
# while still capping what a hand-placed or corrupted file can make startup
# read and parse. Anything past it is not a job record.
MAX_JOB_FILE_BYTES = 1024 * 1024

# The read limit above is only half the story: this app also *writes* job
# records, and until these three caps existed it could write one it would
# then refuse to read back. A runner that emits a single multi-megabyte log
# line (a progress bar with no newlines, a dumped payload) made `_tail` hand
# that whole line to `job.error`, which `_write_job` duly serialized past
# MAX_JOB_FILE_BYTES -- so an ordinary failed job produced a record that the
# next startup quarantined, and the job silently vanished.

# How much of a log's tail `_tail` will read at all. 64 KiB holds the last
# handful of lines of any sane log many times over, and bounds the read even
# when the file has no line breaks to work with.
LOG_TAIL_READ_BYTES = 64 * 1024

# Ceiling on the string `_tail` returns, i.e. on what lands in `job.error`
# and therefore in the record on disk. 8 lines of context is what this is
# for; 10k characters is generous for that and still two orders of magnitude
# below the record limit.
MAX_ERROR_CHARS = 10_000

# Prefixed to a value whose beginning was cut away, so the truncation is
# visible in the UI rather than looking like a log that simply starts
# mid-sentence.
_TRUNCATION_NOTE = "... (truncated)\n"

# Longest requested title kept. A song title is realistically well under 100
# characters; 200 leaves room for "Artist - Title (Live at Somewhere, 1994)"
# while keeping the record's one user-supplied free-text field bounded.
# (This is about record size only -- how long a *folder* name may be remains
# the filesystem's business, unchanged by this cap.)
MAX_TITLE_CHARS = 200

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


_WARNED: set[str] = set()


def _warn(message: str) -> None:
    """stderr warning, emitted at most once per distinct message for the life
    of the process. The conditions these warn about (an unreadable sidecar, a
    process we may not signal) persist by design, and `JobStore.shutdown`
    calls the reaper in a tight retry loop -- without this, one stuck job
    would bury the shutdown output under dozens of identical lines."""
    if message in _WARNED:
        return
    _WARNED.add(message)
    print(message, file=sys.stderr)


def _process_command(pid: int) -> Optional[str]:
    """Command line of `pid`, or "" if `ps` ran fine and reported no such
    process. Returns **None when we could not find out at all**.

    That third case must not be flattened into "": "the process is gone" and
    "we have no idea whether the process is gone" lead to opposite decisions
    (drop the sidecar and re-run the job vs. hold the job back), and
    collapsing them is what let a job be re-queued while its previous run was
    demonstrably still alive. So the exit status is part of the verdict, not
    just whether the command produced output -- a `ps` that ran but failed
    prints nothing on stdout too, and reading that as "gone" is the same
    fail-open bug wearing a different hat.

    `ps` is fine here: this project only targets macOS and Linux, and the two
    agree on the statuses that matter (verified on both):

    * rc 0 -- found it; stdout is the command line.
    * rc 1 with empty stderr -- ran correctly, no process with that pid.
      This is the ordinary "already exited" answer.
    * rc 1 with something on stderr -- `ps` rejected the request rather than
      answering it (macOS says e.g. "ps: process id too large: 999999999"
      for an out-of-range pid). Nothing was learned about the process.
    * anything else (rc > 1, or any status we didn't anticipate) -- likewise
      no answer.

    The last two are None, which every caller turns into FAILED.
    """
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None  # ps couldn't be launched, or never came back
    if out.returncode == 0:
        return out.stdout.strip()
    if out.returncode == 1 and not (out.stderr or "").strip():
        return ""  # ran fine, no such process
    return None  # ps complained, or exited in a way we can't interpret


def _process_liveness(pid: int, marker: str) -> Optional[bool]:
    """True if `pid` is still running *and* still looks like the process we
    signalled (marker still in its command line); False if it is confirmed
    gone -- once it exits, `ps` finds nothing for that pid (or the pid may
    already have been recycled by an unrelated process, which fails the
    marker check too). None means the check itself could not be performed;
    see `_process_command`."""
    command = _process_command(pid)
    if command is None:
        return None
    return marker in command


class TerminationOutcome(Enum):
    """What `terminate_pid_from_sidecar` actually managed to do. Callers must
    distinguish these three: "nothing was there" and "it's confirmed gone"
    both mean the job's old subprocess can no longer touch the cache/package
    files, but "we couldn't stop it" means it may still be running and
    writing them, so the job must NOT be re-run yet."""

    #: There is no sidecar, or it points at a process that `ps` confirms is
    #: already gone / whose pid was recycled to something unrelated. Nothing
    #: needed stopping; any stale sidecar has been cleaned up.
    NOTHING_TO_STOP = "nothing_to_stop"
    #: A live, marker-matching process was signalled and confirmed gone (its
    #: process group swept along with it). The sidecar has been removed.
    STOPPED = "stopped"
    #: We could not establish that the old process is gone -- the liveness
    #: check couldn't run, the sidecar couldn't be read or made sense of, the
    #: pid isn't one we may safely signal, the signal failed with something
    #: other than "already gone", or the process was still alive after
    #: SIGKILL. Everything uncertain lands here on purpose. The sidecar is
    #: deliberately kept so the next startup's recovery gets another chance.
    FAILED = "failed"


def _leads_own_process_group(pid: int) -> Optional[bool]:
    """True if `pid` is the leader of its own process group (pgid == pid),
    which is what `default_runner`'s ``start_new_session=True`` arranges.
    None if we couldn't find out (the lookup itself failed).

    This distinction is a safety gate, not a nicety: `os.killpg(pid, ...)`
    signals *the group whose id equals that number*. If the recorded pid is
    not a group leader, that number is some other group's -- quite possibly
    our own server's -- and killpg'ing it would take down the whole server,
    or nothing at all.
    """
    try:
        return os.getpgid(pid) == pid
    except OSError:
        return None


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
    tree.

    A pid that does *not* lead its own process group -- a sidecar written by
    a server version predating that ``start_new_session=True`` -- is
    deliberately **not signalled at all**. There is no safe way to stop it:
    killpg would address our own server's group, and signalling the lone pid
    is exactly the bug above (its ffmpeg keeps running while we report
    success and the job gets re-queued into a file fight). Walking the
    process table for descendants would only re-introduce the pid-recycling
    hazard the marker check exists to avoid. So we return FAILED, keep the
    sidecar, and let the recovery path hold the job. This resolves itself
    without intervention: the old process eventually exits on its own, and
    the next startup's check then reports it gone (NOTHING_TO_STOP) and
    re-queues the job normally.

    SIGTERM first, then poll for up to `grace_seconds` for the leader to
    actually exit; if it's still alive after that, escalate to SIGKILL and
    poll again. Only a *confirmed* exit counts -- a liveness check that
    couldn't run is never read as "gone" (see `_process_command`), so an
    unusable `ps` ends in FAILED rather than in a false all-clear. Once the
    leader is confirmed gone, one final best-effort SIGKILL sweeps any group
    member that outlived it (an ffmpeg that ignored or was slow on SIGTERM);
    an empty group answers ProcessLookupError, which is the normal case and
    is ignored.

    The sidecar is only removed once the process is confirmed gone -- a job
    resuming that pid before it has actually died would race the exiting
    subprocess over the same cache/package files. If it *still* hasn't died
    even after SIGKILL (a stuck zombie/defunct, essentially never in
    practice), the sidecar is left in place and FAILED is returned so the
    next startup's recovery path gets another chance at it.

    A stale sidecar (process confirmed gone, or its pid recycled to an
    unrelated process) is removed immediately, same as before. Everything
    else uncertain -- an unreadable sidecar, garbage in it, a signal that
    failed with something other than "the process is already gone" (e.g.
    PermissionError) -- keeps the sidecar and answers FAILED, because the
    process's actual state is unknown and re-running the job is the one
    thing that must not happen while it might still be alive. See
    `TerminationOutcome` for what each return value obliges the caller to do.
    """
    sidecar = _pid_sidecar(log_path)
    try:
        raw = sidecar.read_text()
    except FileNotFoundError:
        # No sidecar: the runner never got far enough to write one, or a
        # previous reap already consumed it. Nothing to stop.
        return TerminationOutcome.NOTHING_TO_STOP
    except OSError as exc:
        _warn(
            f"[stemlab-web] cannot read pid sidecar {sidecar.name} ({exc}); "
            "assuming its process may still be running"
        )
        return TerminationOutcome.FAILED

    try:
        pid = int(raw.strip())
    except ValueError:
        pid = -1
    if pid <= 0:
        # Garbage in the sidecar: we can't tell which process (if any) this
        # job left behind. Unlike every other FAILED case this one will not
        # resolve itself on a later start -- the file will still be garbage
        # -- so say plainly that a human has to clear it.
        _warn(
            f"[stemlab-web] pid sidecar {sidecar.name} does not contain a usable pid "
            f"({raw.strip()!r}); the job it belongs to will stay held. Check for a "
            f"leftover stemlab process, then delete {sidecar} by hand to release it."
        )
        return TerminationOutcome.FAILED

    alive = _process_liveness(pid, marker)
    if alive is None:
        _warn(
            f"[stemlab-web] could not check whether pid {pid} (from {sidecar.name}) "
            "is still running; assuming it is"
        )
        return TerminationOutcome.FAILED
    if not alive:
        sidecar.unlink(missing_ok=True)
        return TerminationOutcome.NOTHING_TO_STOP

    # Decided *before* any signal: once the leader dies its pgid can no
    # longer be looked up, and we still need to know whether the final sweep
    # below is allowed to address the group.
    leads_group = _leads_own_process_group(pid)
    if leads_group is not True:
        _warn(
            f"[stemlab-web] pid {pid} (from {sidecar.name}) "
            + (
                "does not lead its own process group -- it predates this server's "
                "process-group handling, so stopping it would leave its own children "
                "(ffmpeg) running"
                if leads_group is False
                else "could not be checked for process-group leadership"
            )
            + ". Leaving it alone; its job stays held until the process exits by itself."
        )
        return TerminationOutcome.FAILED

    def _confirmed_gone() -> bool:
        # Strictly `is False`: an unknown liveness must never be mistaken for
        # a confirmed exit, or we'd drop the sidecar and re-run the job while
        # the old process is still writing the same files.
        return _process_liveness(pid, marker) is False

    def _wait_until_gone(deadline: float) -> bool:
        while time.monotonic() < deadline:
            if _confirmed_gone():
                return True
            time.sleep(poll_interval)
        return _confirmed_gone()

    def _sweep_group() -> None:
        """Best-effort SIGKILL of whatever is left in the group after its
        leader is gone. Only ever runs for a group we just verified was led
        by our marker-matching process, so it can't address a stranger's
        group -- barring the leader's pid being reaped and immediately
        recycled *as a new group leader* in the microseconds since the check
        above, which pids-are-handed-out-sequentially makes vanishingly
        unlikely and which no cheaper check can rule out."""
        try:
            os.killpg(pid, 9)  # SIGKILL
        except OSError:
            pass  # empty group (the normal case), or not ours to signal

    try:
        os.killpg(pid, 15)  # SIGTERM
    except ProcessLookupError:
        # The group emptied out between our liveness check above and this
        # signal -- genuinely gone, so the sidecar is stale now too.
        sidecar.unlink(missing_ok=True)
        return TerminationOutcome.NOTHING_TO_STOP
    except OSError as exc:
        # Some other failure (e.g. PermissionError -- we don't own the
        # process) means we don't actually know whether it's still alive.
        # Don't destroy the recovery information: leave the sidecar in
        # place so the next startup's recovery gets another chance at it,
        # same as the "still alive after SIGKILL" case below.
        _warn(f"[stemlab-web] could not signal pid {pid} (from {sidecar.name}): {exc}")
        return TerminationOutcome.FAILED

    if not _wait_until_gone(time.monotonic() + grace_seconds):
        try:
            os.killpg(pid, 9)  # SIGKILL
        except OSError:
            pass  # already gone between our last check and here
        if not _wait_until_gone(time.monotonic() + grace_seconds):
            # Still alive, un-reapable, or simply unverifiable after SIGKILL
            # -- leave the sidecar for the next startup's recovery to retry,
            # and tell the caller this job's old process is not safely gone.
            _warn(
                f"[stemlab-web] pid {pid} (from {sidecar.name}) is not confirmed gone "
                "after SIGKILL; its job stays held"
            )
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


def _serialize_job_within_limit(job: Job) -> str:
    """`job` as the JSON that goes on disk, guaranteed to fit within
    MAX_JOB_FILE_BYTES if it possibly can.

    This is the last line of defence, not the first: `_tail`'s clamp and
    MAX_TITLE_CHARS already keep both free-length fields far below the limit,
    so in normal operation this function just serializes and returns. It
    exists because writing a record the loader will refuse to read is a
    uniquely nasty failure -- the job doesn't error, it *disappears* on the
    next start -- and that must not be one upstream oversight away.

    `error` is the only field with any real slack, so it is what gets cut,
    halved repeatedly (from the front, keeping the informative end) until the
    record fits, and finally dropped altogether. `job` itself is left alone:
    the in-memory copy keeps the full text for the UI, and only the on-disk
    record is trimmed.
    """
    data = job.to_dict()

    def encode(payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def too_big(encoded: str) -> bool:
        return len(encoded.encode("utf-8")) > MAX_JOB_FILE_BYTES

    encoded = encode(data)
    if not too_big(encoded):
        return encoded

    error = data.get("error") or ""
    while error and too_big(encoded):
        # +1 so this strictly shrinks and always terminates, even at length 1.
        error = error[len(error) // 2 + 1:]
        data["error"] = _TRUNCATION_NOTE + error if error else None
        encoded = encode(data)

    if too_big(encoded):
        # Nothing left to give: something other than `error` is oversized,
        # which the caps upstream should make impossible. Write it anyway --
        # a record the next startup quarantines is still better than an
        # exception here, which would take down the worker mid-job -- but say
        # so, because it means a cap above has been outflanked.
        data["error"] = "(error message dropped: job record too large)"
        encoded = encode(data)
        if too_big(encoded):
            _warn(
                f"[stemlab-web] job {job.id}: record is {len(encoded.encode('utf-8'))} bytes "
                f"even after dropping its error, over the {MAX_JOB_FILE_BYTES}-byte limit; "
                "writing it anyway, but it will not survive a restart"
            )
    return encoded


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
        tmp.write_text(_serialize_job_within_limit(job), encoding="utf-8")
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
            # Size first, so a rogue file is never read into memory or
            # handed to the parser at all. A failed stat is treated like any
            # other unreadable file: quarantine and move on.
            try:
                size = path.stat().st_size
            except OSError as exc:
                self._quarantine_job_file(path, f"unreadable: {exc}")
                continue
            if size > MAX_JOB_FILE_BYTES:
                self._quarantine_job_file(
                    path, f"job file is {size} bytes, over the {MAX_JOB_FILE_BYTES}-byte limit"
                )
                continue

            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, RecursionError) as exc:
                # ValueError rather than json.JSONDecodeError: decoding runs
                # before parsing, and a file with bytes that aren't valid
                # UTF-8 raises UnicodeDecodeError. Both are ValueError
                # subclasses. RecursionError is neither -- it's what
                # json.loads raises on deeply nested input, since it
                # recurses once per level and a few thousand nested arrays
                # exhaust the stack in well under the size limit above.
                # All three mean the same thing here -- this file is not a
                # job record we can read -- and all three must be contained,
                # or one corrupted file takes the whole server's startup
                # down instead of just being quarantined.
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
        # Bounded before it reaches _resolve_title, so neither the stored
        # title nor the "-2"/"-3" variants derived from it can grow the job
        # record without limit (see MAX_TITLE_CHARS).
        requested_title = requested_title[:MAX_TITLE_CHARS]
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


def _clamp_error(text: str) -> str:
    """`text`, shortened from the front to at most MAX_ERROR_CHARS. The tail
    is what matters in a failure message (the error is at the end), so it is
    the beginning that gets dropped, with a note in its place."""
    if len(text) <= MAX_ERROR_CHARS:
        return text
    return _TRUNCATION_NOTE + text[-(MAX_ERROR_CHARS - len(_TRUNCATION_NOTE)):]


def _tail(log_path: Path, n: int) -> str:
    """Last `n` lines of a log, for a failed job's error message.

    Only the final LOG_TAIL_READ_BYTES are read, and the result is clamped to
    MAX_ERROR_CHARS. Both bounds matter: without them a runner emitting one
    enormous line pulled its entire log into memory here and from there into
    `job.error`, producing a job record too big for the loader to accept --
    see the constants at the top of this module.
    """
    try:
        with log_path.open("rb") as f:
            size = f.seek(0, os.SEEK_END)
            start = max(0, size - LOG_TAIL_READ_BYTES)
            f.seek(start)
            raw = f.read()
    except OSError:
        return "(log file unavailable)"

    # errors="replace" as before, and doubly needed now: starting from a byte
    # offset can land in the middle of a multi-byte character.
    text = raw.decode("utf-8", errors="replace")
    if start > 0:
        first_break = text.find("\n")
        if first_break != -1:
            # Whatever precedes it is a fragment of a line we never saw the
            # start of, so it isn't one of the last `n` lines.
            text = text[first_break + 1:]
        # else: the window holds no line break at all -- one gigantic line,
        # exactly the case the byte cap exists for. Keep the fragment; it's
        # the only content there is, and the clamp below still bounds it.

    lines = text.splitlines()
    if not lines:
        return "(empty log)"
    return _clamp_error("\n".join(lines[-n:]))
