from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path

import pytest

from bunri.package_metadata import PackageMetadata, SourceIdentity, TargetMetadata, write_package_metadata
from bunri.pocket.config import PocketConfig, save_config
from bunri.pocket.http import JSONDocument, PocketHTTPError
from bunri.pocket.local import all_package_names
from bunri.pocket.lock import SyncLock, SyncLockBusy
from bunri.pocket.service import PocketServiceError, inspect_remote, inventory, sync_all


TOKEN = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")


def make_package(out: Path, name: str, digest: str) -> None:
    directory = out / name
    directory.mkdir(parents=True)
    metadata = PackageMetadata(
        name,
        name,
        SourceIdentity("sha1", digest, digest[:12]),
        (TargetMetadata("guitar", ("mp3",)),),
    )
    write_package_metadata(directory / ".bunri-package.json", metadata)
    for suffix in ("original.mp3", "guitar.mp3", "guitar.backing.mp3"):
        (directory / f"{name}.{suffix}").write_bytes(suffix.encode())


def test_all_package_names_is_complete_deterministic_and_excludes_internal_paths(tmp_path):
    for name in ("Zulu", "alpha", "web", ".cache", ".pocket", ".hidden"):
        (tmp_path / name).mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "alpha", target_is_directory=True)
    (tmp_path / "plain.txt").write_text("x")
    assert all_package_names(tmp_path) == ["alpha", "Zulu"]


def test_inventory_reports_all_legacy_but_rejects_duplicate_identity(tmp_path):
    make_package(tmp_path, "A", "a" * 40)
    (tmp_path / "Old1").mkdir()
    (tmp_path / "Old2").mkdir()
    found = inventory(tmp_path)
    assert found.legacy == ("Old1", "Old2")

    make_package(tmp_path, "B", "a" * 40)
    with pytest.raises(PocketServiceError, match="複数のパッケージ名"):
        inventory(tmp_path)


def test_sync_lock_is_non_blocking_and_reusable_after_release(tmp_path):
    first = SyncLock(tmp_path).acquire()
    with pytest.raises(SyncLockBusy):
        SyncLock(tmp_path).acquire()
    first.release()
    SyncLock(tmp_path).acquire().release()
    assert (tmp_path / ".pocket" / "sync.lock").read_bytes() == b""


def test_sync_lock_is_released_when_owning_process_exits(tmp_path):
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os, sys\n"
                "from pathlib import Path\n"
                "from bunri.pocket.lock import SyncLock\n"
                "SyncLock(Path(sys.argv[1])).acquire()\n"
                "os._exit(7)\n"
            ),
            str(tmp_path),
        ],
        check=False,
    )
    assert child.returncode == 7
    SyncLock(tmp_path).acquire().release()


def test_remote_status_uses_only_get_and_head(tmp_path):
    make_package(tmp_path, "Song", "a" * 40)
    package = inventory(tmp_path).packages[0]

    class Client:
        def __init__(self):
            self.calls = []

        def get_json(self, path):
            self.calls.append(("GET", path))
            return None

        def head_media(self, song_id, name):
            self.calls.append(("HEAD", name))
            return None

    client = Client()
    status = inspect_remote(package, client)
    assert status.state == "not_synced"
    assert [method for method, _ in client.calls] == ["GET", "GET"]


def test_remote_status_rejects_library_entry_without_manifest(tmp_path):
    make_package(tmp_path, "Song", "a" * 40)
    package = inventory(tmp_path).packages[0]

    class Client:
        def get_json(self, path):
            if path.startswith("manifest/"):
                return None
            return JSONDocument(
                {
                    "schema_version": "1.0",
                    "updated_at": "2026-09-05T00:00:00Z",
                    "songs": [
                        {
                            "song_id": "a" * 12,
                            "title": "Song",
                            "manifest": f"tracks/{'a' * 12}/manifest.json",
                            "has_original": True,
                            "instruments": [{"target": "guitar", "label": "ギター"}],
                            "updated_at": "2026-09-05T00:00:00Z",
                        }
                    ],
                },
                '"library"',
            )

    status = inspect_remote(package, Client())
    assert status.state == "different"
    assert status.can_sync is False
    assert status.message == "棚の状態に不整合があるためアップロードできません。"


def test_batch_validates_every_local_package_before_first_http_call(tmp_path):
    save_config(tmp_path, PocketConfig("https://example.invalid", TOKEN))
    make_package(tmp_path, "A", "a" * 40)
    make_package(tmp_path, "B", "b" * 40)
    (tmp_path / "B" / "B.guitar.mp3").unlink()

    class Client:
        def get_json(self, path):
            pytest.fail("HTTP started before local validation completed")

    with pytest.raises(PocketServiceError):
        sync_all(tmp_path, client=Client())


def test_batch_stops_after_first_remote_failure_and_marks_rest_pending(tmp_path):
    save_config(tmp_path, PocketConfig("https://example.invalid", TOKEN))
    make_package(tmp_path, "A", "a" * 40)
    make_package(tmp_path, "B", "b" * 40)

    class Client:
        def __init__(self):
            self.calls = []

        def get_json(self, path):
            self.calls.append(path)
            raise PocketHTTPError(503, "UNAVAILABLE", "https://secret.invalid?token=hidden")

    client = Client()
    result = sync_all(tmp_path, client=client)
    assert [item.status for item in result.items] == ["error", "pending"]
    assert result.completed == 0 and result.failed == 1 and result.pending == 1
    assert len(client.calls) == 1
    assert "secret.invalid" not in (result.items[0].error or "")
