from __future__ import annotations

import json
import threading
import time

import pytest

from bunri.pocket.lock import SyncLock, SyncLockBusy
from bunri.pocket.service import PocketServiceError
from bunri.pocket.sync import SyncResult
from bunri.web.jobs import JobStore


def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_pocket_job_uses_direct_service_and_omits_separation_fields(tmp_path, monkeypatch):
    import bunri.web.jobs as jobs_module

    calls = []
    monkeypatch.setattr(
        jobs_module,
        "sync_one",
        lambda *args, **kwargs: calls.append((args, kwargs)) or SyncResult(media_uploaded=3),
    )
    store = JobStore(tmp_path, runner=lambda *args: 99)
    try:
        job = store.create_pocket_job(
            song_id="a" * 12,
            digest="a" * 40,
            safe_name="Song",
            sync_lock=SyncLock(tmp_path).acquire(),
        )
        wait_for(lambda: store.get_job(job.id).status == "done")
        saved = json.loads((tmp_path / "web" / "jobs" / f"{job.id}.json").read_text())
        assert saved["kind"] == "pocket_single"
        assert not ({"target", "upload", "package", "log"} & saved.keys())
        assert len(calls) == 1
        assert calls[0][1]["resolution"] == "song_id"
        assert calls[0][1]["include_original"] is True
    finally:
        store.shutdown()


def test_running_pocket_job_is_requeued_without_subprocess_recovery(tmp_path, monkeypatch):
    import bunri.web.jobs as jobs_module

    jobs_dir = tmp_path / "web" / "jobs"
    jobs_dir.mkdir(parents=True)
    job_id = "j-pocket-recovery"
    (jobs_dir / f"{job_id}.json").write_text(json.dumps({
        "id": job_id,
        "kind": "pocket_single",
        "status": "running",
        "created_at": "2026-09-05T00:00:00+00:00",
        "started_at": "2026-09-05T00:00:01+00:00",
        "finished_at": None,
        "error": None,
        "pocket_song_id": "a" * 12,
        "pocket_digest": "a" * 40,
        "pocket_safe_name": "Song",
        "progress": None,
        "result": None,
    }))
    monkeypatch.setattr(jobs_module, "sync_one", lambda *args, **kwargs: SyncResult())
    store = JobStore(tmp_path, runner=lambda *args: 99)
    try:
        wait_for(lambda: store.get_job(job_id).status == "done")
        assert store.get_job(job_id).kind == "pocket_single"
    finally:
        store.shutdown()


def test_recovered_pocket_job_holds_sync_lock_while_queued(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "web" / "jobs"
    jobs_dir.mkdir(parents=True)
    job_id = "j-pocket-queued-lock"
    (jobs_dir / f"{job_id}.json").write_text(json.dumps({
        "id": job_id,
        "kind": "pocket_single",
        "status": "running",
        "created_at": "2026-09-05T00:00:00+00:00",
        "started_at": "2026-09-05T00:00:01+00:00",
        "finished_at": None,
        "error": None,
        "pocket_song_id": "a" * 12,
        "pocket_digest": "a" * 40,
        "pocket_safe_name": "Song",
        "progress": None,
        "result": None,
    }))
    worker_started = threading.Event()
    release_worker = threading.Event()

    def hold_worker(_store):
        worker_started.set()
        release_worker.wait(timeout=5)

    monkeypatch.setattr(JobStore, "_worker_loop", hold_worker)
    store = JobStore(tmp_path, runner=lambda *args: 99)
    assert worker_started.wait(timeout=5)
    try:
        assert store.get_job(job_id).status == "queued"
        with pytest.raises(SyncLockBusy):
            SyncLock(tmp_path).acquire()
    finally:
        held = store._pocket_locks.pop(job_id, None)
        if held is not None:
            held.release()
        release_worker.set()
        store.shutdown()


def test_pocket_batch_preserves_legacy_names_on_local_validation_error(
    tmp_path, monkeypatch
):
    import bunri.web.jobs as jobs_module

    def fail_sync_all(*_args, **_kwargs):
        raise PocketServiceError(
            "ローカル検証に失敗しました。",
            kind="local",
            legacy=("Old One", "Old Two"),
        )

    monkeypatch.setattr(jobs_module, "sync_all", fail_sync_all)
    store = JobStore(tmp_path, runner=lambda *args: 99)
    try:
        job = store.create_pocket_job(
            song_id=None,
            digest=None,
            safe_name=None,
            sync_lock=SyncLock(tmp_path).acquire(),
        )
        wait_for(lambda: store.get_job(job.id).status == "error")
        saved = json.loads((tmp_path / "web" / "jobs" / f"{job.id}.json").read_text())
        assert saved["progress"]["legacy"] == ["Old One", "Old Two"]
        assert saved["progress"]["legacy_count"] == 2
        assert saved["result"]["legacy"] == ["Old One", "Old Two"]
        assert saved["result"]["legacy_count"] == 2
    finally:
        store.shutdown()


def test_pocket_batch_job_bounds_names_and_keeps_complete_counts(
    tmp_path, monkeypatch
):
    import bunri.web.jobs as jobs_module
    from bunri.pocket.service import BatchItem, BatchResult
    from bunri.web.jobs import (
        MAX_JOB_FILE_BYTES,
        MAX_POCKET_JOB_NAMES_PER_STATE,
        _validate_job_record,
    )

    item_count = 5_000
    name_tail = "\x01" * 249
    statuses = ("done", "error", "pending")
    items = [
        BatchItem(f"{index:05d}-{name_tail}", statuses[index % len(statuses)])
        for index in range(item_count)
    ]
    legacy = [f"{index:05d}-{name_tail}" for index in range(item_count)]
    uncapped_names = json.dumps(
        {"done": [item.safe_name for item in items], "legacy": legacy}
    ).encode("utf-8")
    assert len(uncapped_names) > MAX_JOB_FILE_BYTES

    monkeypatch.setattr(
        jobs_module,
        "sync_all",
        lambda *_args, **_kwargs: BatchResult(
            total=item_count,
            legacy=legacy,
            items=items,
        ),
    )
    store = JobStore(tmp_path, runner=lambda *args: 99)
    try:
        job = store.create_pocket_job(
            song_id=None,
            digest=None,
            safe_name=None,
            sync_lock=SyncLock(tmp_path).acquire(),
        )
        wait_for(lambda: store.get_job(job.id).status == "error")
        record_path = tmp_path / "web" / "jobs" / f"{job.id}.json"
        saved = json.loads(record_path.read_text(encoding="utf-8"))

        assert record_path.stat().st_size > MAX_JOB_FILE_BYTES * 0.7
        assert record_path.stat().st_size <= MAX_JOB_FILE_BYTES
        assert _validate_job_record(saved, job.id) is None
        assert saved["progress"]["completed"] == len(
            [item for item in items if item.status == "done"]
        )
        assert saved["progress"]["failed_count"] == len(
            [item for item in items if item.status == "error"]
        )
        assert saved["progress"]["pending_count"] == len(
            [item for item in items if item.status == "pending"]
        )
        assert saved["progress"]["legacy_count"] == item_count
        assert len(saved["progress"]["done"]) == MAX_POCKET_JOB_NAMES_PER_STATE
        assert len(saved["progress"]["failed"]) == MAX_POCKET_JOB_NAMES_PER_STATE
        assert len(saved["progress"]["pending"]) == MAX_POCKET_JOB_NAMES_PER_STATE
        assert len(saved["progress"]["legacy"]) == MAX_POCKET_JOB_NAMES_PER_STATE
        assert saved["result"]["completed"] == saved["progress"]["completed"]
        assert saved["result"]["failed"] == saved["progress"]["failed_count"]
        assert saved["result"]["pending"] == saved["progress"]["pending_count"]
        assert saved["result"]["legacy_count"] == item_count
        assert len(saved["result"]["legacy"]) == MAX_POCKET_JOB_NAMES_PER_STATE
    finally:
        store.shutdown()
