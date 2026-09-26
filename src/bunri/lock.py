"""Cross-process file lock (fcntl.flock), optionally waiting for a holder.

flock locks belong to an open file description, so two ``ProcessLock``
objects exclude each other even inside one process (two threads, or two
acquisitions in a test), not just across processes. The kernel drops the lock
when the owning process exits, so a crashed holder never leaves it stuck.
"""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path
from types import TracebackType

_RETRY_INTERVAL = 0.05


class ProcessLockBusy(RuntimeError):
    pass


class ProcessLock:
    """Exclusive lock on ``path``.

    With ``timeout=None`` acquisition never waits: a held lock raises
    ``busy_error(busy_message)`` at once. With a number it retries every
    50 ms and raises the same error once that many seconds have passed.
    """

    def __init__(
        self,
        path: Path,
        busy_message: str,
        *,
        timeout: float | None = None,
        busy_error: type[RuntimeError] = ProcessLockBusy,
    ) -> None:
        self.path = Path(path)
        self.busy_message = busy_message
        self.timeout = timeout
        self.busy_error = busy_error
        self._fd: int | None = None

    def acquire(self) -> "ProcessLock":
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        # O_NOFOLLOW makes a symlink planted at the lock path fail the open
        # (ELOOP) instead of creating or locking whatever it points at.
        fd = os.open(self.path, flags, 0o600)
        try:
            if not self.path.is_file() or self.path.is_symlink():
                raise OSError(f"lock file is not a regular file: {self.path}")
            deadline = None if self.timeout is None else time.monotonic() + self.timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if deadline is None or time.monotonic() >= deadline:
                        raise self.busy_error(self.busy_message) from exc
                time.sleep(_RETRY_INTERVAL)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "ProcessLock":
        return self.acquire()

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.release()
