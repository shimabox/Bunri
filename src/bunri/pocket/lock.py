"""Non-blocking cross-process lock for Pocket synchronization."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType

from bunri.safepath import verified_mkdir


class SyncLockBusy(RuntimeError):
    pass


class SyncLock:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = Path(out_dir)
        self._fd: int | None = None

    def acquire(self) -> "SyncLock":
        directory = verified_mkdir(self.out_dir, ".pocket")
        path = directory / "sync.lock"
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            if not path.is_file() or path.is_symlink():
                raise OSError("Pocket sync lock is not a regular file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SyncLockBusy("別の Pocket 同期が実行中です。完了後に再実行してください。") from exc
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

    def __enter__(self) -> "SyncLock":
        return self.acquire()

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.release()
