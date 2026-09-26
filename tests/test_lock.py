"""bunri.lock.ProcessLock: immediate and waiting acquisition."""

from __future__ import annotations

import threading
import time

import pytest

from bunri.lock import ProcessLock, ProcessLockBusy


def test_held_lock_without_timeout_is_busy_at_once(tmp_path):
    path = tmp_path / "x.lock"
    held = ProcessLock(path, "held").acquire()
    try:
        start = time.monotonic()
        with pytest.raises(ProcessLockBusy, match="busy now"):
            ProcessLock(path, "busy now").acquire()
        assert time.monotonic() - start < 0.2
    finally:
        held.release()
    ProcessLock(path, "free").acquire().release()
    assert path.is_file()


def test_held_lock_with_timeout_waits_before_busy(tmp_path):
    path = tmp_path / "x.lock"
    held = ProcessLock(path, "held").acquire()
    try:
        start = time.monotonic()
        with pytest.raises(ProcessLockBusy, match="gave up"):
            ProcessLock(path, "gave up", timeout=0.2).acquire()
        assert time.monotonic() - start >= 0.2
    finally:
        held.release()


def test_lock_released_during_the_wait_is_acquired(tmp_path):
    path = tmp_path / "x.lock"
    held = ProcessLock(path, "held").acquire()
    releaser = threading.Timer(0.1, held.release)
    releaser.start()
    try:
        with ProcessLock(path, "waited", timeout=5.0):
            pass
    finally:
        releaser.join()


def test_busy_error_class_is_configurable(tmp_path):
    class Custom(ProcessLockBusy):
        pass

    path = tmp_path / "x.lock"
    with ProcessLock(path, "held"):
        with pytest.raises(Custom):
            ProcessLock(path, "custom", busy_error=Custom).acquire()


def test_symlinked_lock_file_is_refused(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    link = tmp_path / "x.lock"
    link.symlink_to(victim)
    with pytest.raises(OSError):
        ProcessLock(link, "held").acquire()
    assert victim.read_text() == "keep"


def test_sync_lock_keeps_its_location_and_error(tmp_path):
    from bunri.pocket.lock import SyncLock, SyncLockBusy

    with SyncLock(tmp_path):
        assert (tmp_path / ".pocket" / "sync.lock").is_file()
        with pytest.raises(SyncLockBusy, match="別の Pocket 同期が実行中です"):
            SyncLock(tmp_path).acquire()
