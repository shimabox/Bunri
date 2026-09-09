from __future__ import annotations

import base64
import json
import shutil
import threading
import time

import pytest

from bunri.package_metadata import (
    PackageMetadata,
    SourceIdentity,
    TargetMetadata,
    write_package_metadata,
)
from bunri.pocket.config import PocketConfig, connection_fingerprint, save_config
from bunri.pocket.lock import SyncLock, SyncLockBusy
from bunri.pocket.service import DeleteTargetIdentity, PocketServiceError
from bunri.pocket.sync import SyncResult
from bunri.web.jobs import Job, JobStore, SongNotFoundError


TOKEN = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")
CONNECTION_FINGERPRINT = connection_fingerprint(PocketConfig("https://example.invalid", TOKEN))


def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def write_job(out_dir, job: Job) -> None:
    jobs_dir = out_dir / "web" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / f"{job.id}.json").write_text(
        json.dumps(job.to_dict()), encoding="utf-8"
    )


def make_local_song(out_dir, *, digest: str = "a" * 40) -> Job:
    package = out_dir / "Song"
    package.mkdir(parents=True)
    write_package_metadata(
        package / ".bunri-package.json",
        PackageMetadata(
            "Song",
            "Song",
            SourceIdentity("sha1", digest, digest[:12]),
            (TargetMetadata("guitar", ("mp3",)),),
        ),
    )
    for suffix in ("original.mp3", "guitar.mp3", "guitar.backing.mp3"):
        (package / f"Song.{suffix}").write_bytes(b"audio")
    job = Job(
        id="j-local-song",
        digest=digest,
        title="Song",
        target="guitar",
        status="done",
        created_at="2026-09-07T00:00:00+00:00",
        finished_at="2026-09-07T00:00:01+00:00",
        package="Song/Song.guitar.player.html",
        log="web/logs/j-local-song.log",
        upload="web/uploads/song.mp3",
    )
    write_job(out_dir, job)
    return job


def make_recoverable_delete(out_dir, *, pocket_deleted: bool) -> Job:
    job = Job(
        id="j-pocket-delete-recovery",
        digest="",
        title="",
        target="",
        status="running",
        created_at="2026-09-07T00:00:02+00:00",
        started_at="2026-09-07T00:00:03+00:00",
        kind="pocket_delete",
        pocket_song_id="a" * 12,
        pocket_digest="a" * 40,
        pocket_safe_name="Song",
        pocket_connection_fingerprint=CONNECTION_FINGERPRINT,
        result={"pocket_deleted": pocket_deleted, "local_deleted": False},
    )
    write_job(out_dir, job)
    return job


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


def test_pocket_delete_runs_remote_before_local_and_records_complete_result(tmp_path, monkeypatch):
    import bunri.web.jobs as jobs_module

    order = []
    monkeypatch.setattr(jobs_module, "delete_track", lambda *_args, **_kwargs: order.append("remote"))
    store = JobStore(tmp_path, runner=lambda *args: 99)
    monkeypatch.setattr(
        store,
        "delete_song",
        lambda requested, **kwargs: order.append(("local", requested, kwargs["exclude_pocket_job_id"])),
    )
    try:
        job = store.create_pocket_delete_job(
            song_id="a" * 12,
            digest="a" * 40,
            safe_name="Song",
            connection_fingerprint=CONNECTION_FINGERPRINT,
            sync_lock=SyncLock(tmp_path).acquire(),
        )
        wait_for(lambda: store.get_job(job.id).status == "done")
        finished = store.get_job(job.id)
        assert order == ["remote", ("local", jobs_module.song_id("a" * 40), job.id)]
        assert finished.result == {"pocket_deleted": True, "local_deleted": True}
    finally:
        store.shutdown()


def test_pocket_delete_remote_failure_keeps_local_and_stores_safe_retry_message(tmp_path, monkeypatch):
    import bunri.web.jobs as jobs_module
    from bunri.pocket.http import PocketHTTPError

    monkeypatch.setattr(
        jobs_module,
        "delete_track",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PocketHTTPError(503, "UNAVAILABLE", "https://secret.invalid/token")
        ),
    )
    store = JobStore(tmp_path, runner=lambda *args: 99)
    monkeypatch.setattr(store, "delete_song", lambda *_args, **_kwargs: pytest.fail("local deletion started"))
    try:
        job = store.create_pocket_delete_job(
            song_id="a" * 12,
            digest="a" * 40,
            safe_name="Song",
            connection_fingerprint=CONNECTION_FINGERPRINT,
            sync_lock=SyncLock(tmp_path).acquire(),
        )
        wait_for(lambda: store.get_job(job.id).status == "error")
        failed = store.get_job(job.id)
        assert failed.result == {"pocket_deleted": False, "local_deleted": False}
        assert "ローカルデータは削除していません" in failed.error
        assert "secret" not in failed.error
    finally:
        store.shutdown()


def test_pocket_delete_local_failure_records_partial_success(tmp_path, monkeypatch):
    import bunri.web.jobs as jobs_module

    monkeypatch.setattr(jobs_module, "delete_track", lambda *_args, **_kwargs: None)
    store = JobStore(tmp_path, runner=lambda *args: 99)
    monkeypatch.setattr(
        store,
        "delete_song",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private path")),
    )
    try:
        job = store.create_pocket_delete_job(
            song_id="a" * 12,
            digest="a" * 40,
            safe_name="Song",
            connection_fingerprint=CONNECTION_FINGERPRINT,
            sync_lock=SyncLock(tmp_path).acquire(),
        )
        wait_for(lambda: store.get_job(job.id).status == "error")
        failed = store.get_job(job.id)
        assert failed.result == {"pocket_deleted": True, "local_deleted": False}
        assert "棚からは削除済み" in failed.error
        assert "private path" not in failed.error
    finally:
        store.shutdown()


def test_pocket_delete_missing_local_song_after_remote_204_is_idempotent_success(
    tmp_path, monkeypatch
):
    import bunri.web.jobs as jobs_module

    monkeypatch.setattr(jobs_module, "delete_track", lambda *_args, **_kwargs: None)
    store = JobStore(tmp_path, runner=lambda *args: 99)
    monkeypatch.setattr(
        store,
        "delete_song",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SongNotFoundError("already deleted")),
    )
    try:
        job = store.create_pocket_delete_job(
            song_id="a" * 12,
            digest="a" * 40,
            safe_name="Song",
            connection_fingerprint=CONNECTION_FINGERPRINT,
            sync_lock=SyncLock(tmp_path).acquire(),
        )
        wait_for(lambda: store.get_job(job.id).status == "done")
        finished = store.get_job(job.id)
        assert finished.result == {"pocket_deleted": True, "local_deleted": True}
        assert finished.error is None
    finally:
        store.shutdown()


def test_pocket_delete_stops_if_connection_changes_while_job_is_queued(tmp_path, monkeypatch):
    import bunri.pocket.service as service_module
    import bunri.web.jobs as jobs_module

    make_local_song(tmp_path)
    save_config(tmp_path, PocketConfig("https://example.invalid", TOKEN))
    started = threading.Event()
    proceed = threading.Event()
    remote_calls = []

    def delayed_delete(*args, **kwargs):
        started.set()
        proceed.wait(timeout=5)
        return service_module.delete_track(*args, **kwargs)

    class UnexpectedClient:
        def __init__(self, *_args, **_kwargs):
            remote_calls.append("client-created")

    monkeypatch.setattr(jobs_module, "delete_track", delayed_delete)
    monkeypatch.setattr(service_module, "PocketHTTPClient", UnexpectedClient)
    store = JobStore(tmp_path, runner=lambda *args: 99)
    try:
        job = store.create_pocket_delete_job(
            song_id="a" * 12,
            digest="a" * 40,
            safe_name="Song",
            connection_fingerprint=CONNECTION_FINGERPRINT,
            sync_lock=SyncLock(tmp_path).acquire(),
        )
        assert started.wait(timeout=5)
        saved = json.loads((tmp_path / "web" / "jobs" / f"{job.id}.json").read_text())
        assert saved["pocket_connection_fingerprint"] == CONNECTION_FINGERPRINT
        assert "example.invalid" not in json.dumps(saved)
        assert TOKEN not in json.dumps(saved)

        save_config(tmp_path, PocketConfig("https://other.invalid", TOKEN))
        proceed.set()
        wait_for(lambda: store.get_job(job.id).status == "error")

        failed = store.get_job(job.id)
        assert failed.result == {"pocket_deleted": False, "local_deleted": False}
        assert failed.error == "接続先が変更されたため削除を中止しました。対象を選び直してください"
        assert remote_calls == []
    finally:
        proceed.set()
        store.shutdown()


def test_recovered_pocket_delete_stops_if_connection_changed(tmp_path, monkeypatch):
    import bunri.pocket.service as service_module

    local_job = make_local_song(tmp_path)
    delete_job = make_recoverable_delete(tmp_path, pocket_deleted=False)
    save_config(tmp_path, PocketConfig("https://other.invalid", TOKEN))
    remote_calls = []

    class UnexpectedClient:
        def __init__(self, *_args, **_kwargs):
            remote_calls.append("client-created")

    monkeypatch.setattr(service_module, "PocketHTTPClient", UnexpectedClient)
    store = JobStore(tmp_path, runner=lambda *args: 99)
    try:
        wait_for(lambda: store.get_job(delete_job.id).status == "error")

        failed = store.get_job(delete_job.id)
        assert failed.result == {"pocket_deleted": False, "local_deleted": False}
        assert failed.error == "接続先が変更されたため削除を中止しました。対象を選び直してください"
        assert remote_calls == []
        assert store.get_job(local_job.id) is not None
        assert (tmp_path / "Song").is_dir()
    finally:
        store.shutdown()


@pytest.mark.parametrize("missing", ["package_directory", "sidecar"])
def test_recovered_pocket_delete_finishes_partial_local_deletion(
    tmp_path, monkeypatch, missing
):
    import bunri.web.jobs as jobs_module

    local_job = make_local_song(tmp_path)
    delete_job = make_recoverable_delete(tmp_path, pocket_deleted=True)
    package = tmp_path / "Song"
    if missing == "package_directory":
        shutil.rmtree(package)
    else:
        (package / ".bunri-package.json").unlink()

    remote_targets = []
    monkeypatch.setattr(
        jobs_module,
        "delete_track",
        lambda _out_dir, target, **_kwargs: remote_targets.append(target),
    )
    store = JobStore(tmp_path, runner=lambda *args: 99)
    try:
        wait_for(lambda: store.get_job(delete_job.id).status == "done")

        assert remote_targets == [
            DeleteTargetIdentity(
                song_id="a" * 12,
                digest="a" * 40,
                safe_name=None,
            )
        ]
        assert not package.exists()
        assert store.get_job(local_job.id) is None
        assert not (tmp_path / "web" / "jobs" / f"{local_job.id}.json").exists()
        assert store.get_job(delete_job.id).result == {
            "pocket_deleted": True,
            "local_deleted": True,
        }
    finally:
        store.shutdown()


def test_recovered_cli_delete_uses_saved_identity_after_sidecar_was_removed(
    tmp_path, monkeypatch
):
    import bunri.web.jobs as jobs_module

    package = tmp_path / "Song"
    package.mkdir()
    (package / "remaining.wav").write_bytes(b"partial local data")
    delete_job = make_recoverable_delete(tmp_path, pocket_deleted=True)
    remote_targets = []
    monkeypatch.setattr(
        jobs_module,
        "delete_track",
        lambda _out_dir, target, **_kwargs: remote_targets.append(target),
    )

    store = JobStore(tmp_path, runner=lambda *args: 99)
    try:
        wait_for(lambda: store.get_job(delete_job.id).status == "done")
        assert remote_targets == [
            DeleteTargetIdentity(
                song_id="a" * 12,
                digest="a" * 40,
                safe_name=None,
            )
        ]
        assert not package.exists()
        assert store.get_job(delete_job.id).result == {
            "pocket_deleted": True,
            "local_deleted": True,
        }
    finally:
        store.shutdown()


def test_recovered_pocket_delete_finishes_when_audio_is_already_missing(
    tmp_path, monkeypatch
):
    import bunri.web.jobs as jobs_module

    local_job = make_local_song(tmp_path)
    delete_job = make_recoverable_delete(tmp_path, pocket_deleted=True)
    (tmp_path / "Song" / "Song.guitar.mp3").unlink()

    remote_targets = []
    monkeypatch.setattr(
        jobs_module,
        "delete_track",
        lambda _out_dir, target, **_kwargs: remote_targets.append(target),
    )
    store = JobStore(tmp_path, runner=lambda *args: 99)
    try:
        wait_for(lambda: store.get_job(delete_job.id).status == "done")

        assert remote_targets == [
            DeleteTargetIdentity(
                song_id="a" * 12,
                digest="a" * 40,
                safe_name=None,
            )
        ]
        assert not (tmp_path / "Song").exists()
        assert store.get_job(local_job.id) is None
    finally:
        store.shutdown()


def test_recovered_confirmed_pocket_delete_stops_on_sidecar_identity_change(
    tmp_path, monkeypatch
):
    import bunri.pocket.service as service_module

    local_job = make_local_song(tmp_path)
    delete_job = make_recoverable_delete(tmp_path, pocket_deleted=True)
    (tmp_path / "Song" / "Song.guitar.mp3").unlink()
    write_package_metadata(
        tmp_path / "Song" / ".bunri-package.json",
        PackageMetadata(
            "Song",
            "Song",
            SourceIdentity("sha1", "b" * 40, "b" * 12),
            (TargetMetadata("guitar", ("mp3",)),),
        ),
    )
    token = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")
    save_config(tmp_path, PocketConfig("https://example.invalid", token))
    remote_calls = []

    class UnexpectedClient:
        def __init__(self, *_args, **_kwargs):
            remote_calls.append("client-created")

    monkeypatch.setattr(service_module, "PocketHTTPClient", UnexpectedClient)
    store = JobStore(tmp_path, runner=lambda *args: 99)
    try:
        wait_for(lambda: store.get_job(delete_job.id).status == "error")

        failed = store.get_job(delete_job.id)
        assert failed.result == {"pocket_deleted": True, "local_deleted": False}
        assert "identity" in failed.error
        assert remote_calls == []
        assert store.get_job(local_job.id) is not None
        assert (tmp_path / "Song").is_dir()
    finally:
        store.shutdown()


def test_recovered_pocket_delete_still_rejects_tampering_before_remote_204(tmp_path):
    local_job = make_local_song(tmp_path)
    delete_job = make_recoverable_delete(tmp_path, pocket_deleted=False)
    write_package_metadata(
        tmp_path / "Song" / ".bunri-package.json",
        PackageMetadata(
            "Song",
            "Song",
            SourceIdentity("sha1", "b" * 40, "b" * 12),
            (TargetMetadata("guitar", ("mp3",)),),
        ),
    )
    token = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")
    save_config(tmp_path, PocketConfig("https://example.invalid", token))

    store = JobStore(tmp_path, runner=lambda *args: 99)
    try:
        wait_for(lambda: store.get_job(delete_job.id).status == "error")

        failed = store.get_job(delete_job.id)
        assert failed.result == {"pocket_deleted": False, "local_deleted": False}
        assert "identity" in failed.error
        assert store.get_job(local_job.id) is not None
        assert (tmp_path / "Song").is_dir()
    finally:
        store.shutdown()


def test_recovered_pocket_delete_waits_for_busy_lock_then_succeeds(tmp_path, monkeypatch):
    import bunri.web.jobs as jobs_module

    jobs_dir = tmp_path / "web" / "jobs"
    jobs_dir.mkdir(parents=True)
    job_id = "j-pocket-delete-recovery"
    (jobs_dir / f"{job_id}.json").write_text(json.dumps({
        "id": job_id,
        "kind": "pocket_delete",
        "status": "running",
        "created_at": "2026-09-07T00:00:00+00:00",
        "started_at": "2026-09-07T00:00:01+00:00",
        "finished_at": None,
        "error": None,
        "pocket_song_id": "a" * 12,
        "pocket_digest": "a" * 40,
        "pocket_safe_name": "Song",
        "pocket_connection_fingerprint": CONNECTION_FINGERPRINT,
        "progress": None,
        "result": {"pocket_deleted": True, "local_deleted": False},
    }))
    held = SyncLock(tmp_path).acquire()
    remote_called = threading.Event()
    monkeypatch.setattr(
        jobs_module,
        "delete_track",
        lambda *_args, **_kwargs: remote_called.set(),
    )
    monkeypatch.setattr(JobStore, "delete_song", lambda *_args, **_kwargs: None)
    store = JobStore(tmp_path, runner=lambda *args: 99)
    try:
        wait_for(lambda: store.get_job(job_id).status == "queued")
        assert not remote_called.wait(timeout=0.3)
        assert store.get_job(job_id).error is None

        held.release()
        wait_for(lambda: store.get_job(job_id).status == "done")
        assert remote_called.is_set()
        assert store.get_job(job_id).result == {
            "pocket_deleted": True,
            "local_deleted": True,
        }
    finally:
        held.release()
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
