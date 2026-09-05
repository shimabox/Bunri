from __future__ import annotations

import json
import time

from bunri.pocket.lock import SyncLock
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
