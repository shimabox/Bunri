"""Non-blocking cross-process lock for Pocket synchronization."""

from __future__ import annotations

from pathlib import Path
from types import TracebackType

from bunri.lock import ProcessLock, ProcessLockBusy
from bunri.safepath import verified_mkdir


class SyncLockBusy(ProcessLockBusy):
    pass


class SyncLock:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = Path(out_dir)
        self._lock: ProcessLock | None = None

    def acquire(self) -> "SyncLock":
        directory = verified_mkdir(self.out_dir, ".pocket")
        lock = ProcessLock(
            directory / "sync.lock",
            "別の Pocket 同期が実行中です。完了後に再実行してください。",
            busy_error=SyncLockBusy,
        ).acquire()
        self._lock = lock
        return self

    def release(self) -> None:
        if self._lock is None:
            return
        lock, self._lock = self._lock, None
        lock.release()

    def __enter__(self) -> "SyncLock":
        return self.acquire()

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.release()
