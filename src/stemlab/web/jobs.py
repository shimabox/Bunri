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
from pathlib import Path
from typing import Callable, Optional

# 2 hours: generous upper bound for a single separation + export on CPU.
DEFAULT_TIMEOUT_SECONDS = 2 * 60 * 60

# Same rule as stemlab.package._safe_filename -- see module docstring for why
# this is duplicated rather than imported.
_UNSAFE_CHARS = re.compile(r'[/\\:*?"<>|]')


def safe_filename(title: str) -> str:
    return _UNSAFE_CHARS.sub("_", title).strip() or "untitled"


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


def terminate_pid_from_sidecar(log_path: Path, *, marker: str = "stemlab.cli") -> bool:
    """Kill the process a pid sidecar points at, but only if its command line
    still contains `marker` -- pids get recycled, and we must never kill an
    unrelated process that happens to have inherited the number. Returns True
    if a process was signalled. Removes the sidecar either way."""
    sidecar = _pid_sidecar(log_path)
    try:
        pid = int(sidecar.read_text().strip())
    except (OSError, ValueError):
        return False
    finally:
        sidecar.unlink(missing_ok=True)
    if pid <= 0 or marker not in _process_command(pid):
        return False
    try:
        os.kill(pid, 15)  # SIGTERM
    except OSError:
        return False
    return True


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
    than relying on a `stemlab` script being on PATH."""
    cmd = [
        sys.executable,
        "-m",
        "stemlab.cli",
        str(upload_path),
        "-o",
        str(out_dir),
        "--title",
        title,
    ]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar = _pid_sidecar(log_path)
    with log_path.open("wb") as log_f:
        proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT)
        sidecar.write_text(str(proc.pid))
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
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
        self._queue: "queue.Queue[str]" = queue.Queue()

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

    def _load_and_recover(self) -> None:
        for path in sorted(self.jobs_dir.glob("j-*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
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
                terminate_pid_from_sidecar(self.out_dir / job.log)
            job.status = "queued"
            job.started_at = None
            self._write_job(job)
            self._queue.put(job.id)

    def shutdown(self) -> None:
        """Graceful-shutdown hook (wired to the app's lifespan): stop the
        currently running separation subprocess, if any, so a normal server
        stop doesn't orphan it. A SIGKILLed server can't run this -- that
        case is covered by the sidecar reaping in _load_and_recover on the
        next start."""
        with self._lock:
            running = [j for j in self._jobs.values() if j.status == "running" and j.log]
        for job in running:
            terminate_pid_from_sidecar(self.out_dir / job.log)

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
            job = self.get_job(job_id)
            if job is None:
                continue
            self._run_job(job)

    def _run_job(self, job: Job) -> None:
        with self._lock:
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

        with self._lock:
            job.finished_at = _now_iso()
            if rc == 0 and expected_player.exists():
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
