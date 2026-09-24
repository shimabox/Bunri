from __future__ import annotations

import base64
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

from bunri.package_metadata import PackageMetadata, SourceIdentity, TargetMetadata, write_package_metadata
from bunri.pocket.config import PocketConfig, connection_fingerprint, save_config
from bunri.pocket.http import JSONDocument, PocketHTTPError
from bunri.pocket.local import all_package_names
from bunri.pocket.lock import SyncLock, SyncLockBusy
from bunri.pocket.service import (
    DeleteTargetIdentity,
    PocketServiceError,
    delete_track,
    inspect_packages,
    inspect_remote,
    inventory,
    list_library_tracks,
    resolve_package,
    resolve_delete_target,
    safe_delete_error,
    safe_error,
    sync_all,
    sync_one,
)


TOKEN = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")


def make_package(
    out: Path, name: str, digest: str, *, metadata_name: str | None = None
) -> None:
    directory = out / name
    directory.mkdir(parents=True)
    metadata = PackageMetadata(
        name,
        metadata_name or name,
        SourceIdentity("sha1", digest, digest[:12]),
        (TargetMetadata("guitar", ("mp3",)),),
    )
    write_package_metadata(directory / ".bunri-package.json", metadata)
    for suffix in ("original.mp3", "guitar.mp3", "guitar.backing.mp3"):
        (directory / f"{name}.{suffix}").write_bytes(suffix.encode())


class EmptyRemoteClient:
    def get_json(self, path):
        return None

    def head_media(self, song_id, name):
        return None


@pytest.mark.parametrize("operation", ["one", "all"])
def test_sync_stops_before_upload_when_connection_fingerprint_changes(
    tmp_path, monkeypatch, operation
):
    import bunri.pocket.service as service_module

    make_package(tmp_path, "Song", "a" * 40)
    displayed = PocketConfig("https://shelf-a.invalid", TOKEN)
    save_config(tmp_path, displayed)
    expected = connection_fingerprint(displayed)
    save_config(tmp_path, PocketConfig("https://shelf-b.invalid", TOKEN))
    monkeypatch.setattr(
        service_module,
        "synchronize",
        lambda *_args, **_kwargs: pytest.fail("upload must not start"),
    )

    with pytest.raises(PocketServiceError) as caught:
        if operation == "one":
            sync_one(
                tmp_path,
                "Song",
                resolution="safe_name",
                expected_connection_fingerprint=expected,
            )
        else:
            sync_all(tmp_path, expected_connection_fingerprint=expected)

    assert caught.value.kind == "connection_changed"
    assert safe_error(caught.value) == (
        "接続先が変更されたため同期を中止しました。"
        "状態を再読込して確認し直してください"
    )


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
    with pytest.raises(PocketServiceError, match="複数のパッケージ名") as caught:
        inventory(tmp_path)
    assert caught.value.kind == "local"
    assert caught.value.legacy == ("Old1", "Old2")


def test_nfd_package_is_inspected_inventoried_and_resolved_by_either_form(tmp_path):
    nfc_name = "ガンバのバラード"
    nfd_name = unicodedata.normalize("NFD", nfc_name)
    make_package(tmp_path, nfd_name, "a" * 40, metadata_name=nfc_name)

    found = inventory(tmp_path)
    statuses = inspect_packages(tmp_path, EmptyRemoteClient())
    by_nfc = resolve_package(tmp_path, nfc_name, resolution="safe_name")
    by_nfd = resolve_package(tmp_path, nfd_name, resolution="safe_name")

    assert [package.directory.name for package in found.packages] == [nfd_name]
    assert len(statuses) == 1
    assert statuses[0].safe_name == nfd_name
    assert statuses[0].remote.state == "not_synced"
    assert by_nfc.directory == by_nfd.directory == tmp_path / nfd_name

    save_config(tmp_path, PocketConfig("https://example.invalid", TOKEN))

    class FailingRemoteClient:
        def __init__(self):
            self.calls = 0

        def get_json(self, path):
            self.calls += 1
            raise PocketHTTPError(503, "UNAVAILABLE", "https://example.invalid")

    client = FailingRemoteClient()
    result = sync_all(tmp_path, client=client)
    assert client.calls == 1
    assert [item.status for item in result.items] == ["error"]


def test_canonically_equivalent_directories_are_rejected_as_name_conflict(tmp_path):
    nfc_name = "ざらめのゆき"
    nfd_name = unicodedata.normalize("NFD", nfc_name)
    make_package(tmp_path, nfc_name, "a" * 40)
    try:
        make_package(tmp_path, nfd_name, "b" * 40)
    except FileExistsError:
        pytest.skip("filesystem does not distinguish NFC and NFD filenames")

    with pytest.raises(PocketServiceError, match="NFC正規化後に同じ名前"):
        inventory(tmp_path)
    for selector in (nfc_name, nfd_name):
        with pytest.raises(PocketServiceError) as caught:
            resolve_package(tmp_path, selector, resolution="safe_name")
        assert caught.value.kind == "conflict"
    for song_id in ("a" * 12, "b" * 12):
        with pytest.raises(PocketServiceError) as caught:
            resolve_package(tmp_path, song_id, resolution="song_id")
        assert caught.value.kind == "conflict"

    statuses = inspect_packages(tmp_path, RefusingClient())
    assert {status.safe_name for status in statuses} == {nfc_name, nfd_name}
    assert all(status.remote.conflict for status in statuses)
    assert all(not status.remote.can_sync for status in statuses)


class RefusingClient:
    """Any call means a conflicting package reached the network."""

    def get_json(self, path):
        pytest.fail("a conflicting identity must be settled before any request")

    def head_media(self, song_id, name):
        pytest.fail("a conflicting identity must be settled before any request")


def test_duplicate_song_id_is_detected_even_when_one_copy_fails_preflight(tmp_path):
    make_package(tmp_path, "Good", "a" * 40)
    make_package(tmp_path, "Broken", "a" * 12 + "b" * 28)
    (tmp_path / "Broken" / "Broken.guitar.mp3").unlink()

    with pytest.raises(PocketServiceError, match="identity が競合"):
        resolve_package(tmp_path, "a" * 12, resolution="song_id")

    statuses = {item.safe_name: item for item in inspect_packages(tmp_path, RefusingClient())}
    assert statuses["Good"].remote.conflict is True
    assert statuses["Good"].remote.can_sync is False
    assert statuses["Broken"].remote.conflict is True


def test_duplicate_full_digest_is_detected_even_when_one_copy_fails_preflight(tmp_path):
    make_package(tmp_path, "Good", "a" * 40)
    make_package(tmp_path, "Broken", "a" * 40)
    (tmp_path / "Broken" / "Broken.original.mp3").write_bytes(b"")

    with pytest.raises(PocketServiceError, match="identity が競合"):
        resolve_package(tmp_path, "Good", resolution="safe_name")

    with pytest.raises(PocketServiceError, match="複数のパッケージ名"):
        inventory(tmp_path)

    statuses = {item.safe_name: item for item in inspect_packages(tmp_path, RefusingClient())}
    assert statuses["Good"].remote.conflict is True


def test_duplicate_full_digest_is_detected_when_package_is_copied_under_another_name(tmp_path):
    make_package(tmp_path, "Good", "a" * 40)
    shutil.copytree(tmp_path / "Good", tmp_path / "Renamed")

    with pytest.raises(PocketServiceError, match="identity が競合"):
        resolve_package(tmp_path, "Good", resolution="safe_name")

    with pytest.raises(PocketServiceError, match="複数のパッケージ名"):
        inventory(tmp_path)

    statuses = {item.safe_name: item for item in inspect_packages(tmp_path, RefusingClient())}
    assert statuses["Good"].remote.conflict is True
    assert statuses["Renamed"].remote.conflict is True


def test_a_sidecarless_directory_never_joins_the_identity_check(tmp_path):
    make_package(tmp_path, "Good", "a" * 40)
    (tmp_path / "Legacy").mkdir()

    package = resolve_package(tmp_path, "a" * 12, resolution="song_id")
    assert package.directory.name == "Good"
    assert inventory(tmp_path).legacy == ("Legacy",)


def test_safe_name_and_song_id_resolution_do_not_share_a_namespace(tmp_path):
    make_package(tmp_path, "aaaaaaaaaaaa", "b" * 40)
    make_package(tmp_path, "Actual Song", "a" * 40)

    by_name = resolve_package(tmp_path, "a" * 12, resolution="safe_name")
    by_song_id = resolve_package(tmp_path, "a" * 12, resolution="song_id")

    assert by_name.directory.name == "aaaaaaaaaaaa"
    assert by_song_id.directory.name == "Actual Song"


def test_safe_name_resolution_never_falls_back_to_a_song_id(tmp_path):
    make_package(tmp_path, "Actual Song", "a" * 40)

    with pytest.raises(PocketServiceError) as caught:
        resolve_package(tmp_path, "a" * 12, resolution="safe_name")

    assert caught.value.kind == "not_found"


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
    assert status.message == "音源ポケットの状態に不整合があるためアップロードできません。"


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


def _library_document(*songs):
    return JSONDocument(
        {
            "schema_version": "1.0",
            "updated_at": "2026-09-07T00:00:00Z",
            "songs": [
                {
                    "song_id": song_id,
                    "title": title,
                    "manifest": f"tracks/{song_id}/manifest.json",
                    "has_original": True,
                    "instruments": [],
                    "updated_at": "2026-09-07T00:00:00Z",
                }
                for song_id, title in songs
            ],
        },
        '"library"',
    )


def test_library_selection_preserves_validated_remote_order(tmp_path):
    save_config(tmp_path, PocketConfig("https://example.invalid", TOKEN))

    class Client:
        def get_json(self, path):
            assert path == "library"
            return _library_document(("b" * 12, "Same"), ("a" * 12, "Same"))

    tracks = list_library_tracks(tmp_path, client=Client())
    assert [(item.song_id, item.title) for item in tracks] == [
        ("b" * 12, "Same"),
        ("a" * 12, "Same"),
    ]


def test_direct_remote_only_song_id_deletes_without_local_package_and_releases_lock(tmp_path):
    save_config(tmp_path, PocketConfig("https://example.invalid", TOKEN))
    calls = []

    class Client:
        def delete_track(self, song_id):
            calls.append(song_id)

    result = delete_track(
        tmp_path,
        DeleteTargetIdentity("abcdef123456"),
        expected_connection_fingerprint=connection_fingerprint(PocketConfig("https://example.invalid", TOKEN)),
        client=Client(),
    )
    assert result.song_id == "abcdef123456"
    assert calls == ["abcdef123456"]
    SyncLock(tmp_path).acquire().release()


def test_safe_name_delete_revalidates_full_identity_inside_lock(tmp_path):
    save_config(tmp_path, PocketConfig("https://example.invalid", TOKEN))
    make_package(tmp_path, "aaaaaaaaaaaa", "b" * 40)
    make_package(tmp_path, "Actual", "a" * 40)
    target = resolve_delete_target(tmp_path, "aaaaaaaaaaaa")
    assert (target.song_id, target.digest) == ("b" * 12, "b" * 40)

    class Client:
        def delete_track(self, song_id):
            assert song_id == "b" * 12

    delete_track(
        tmp_path,
        target,
        expected_connection_fingerprint=connection_fingerprint(PocketConfig("https://example.invalid", TOKEN)),
        client=Client(),
    )


def test_delete_503_is_safe_retryable_and_lock_is_released(tmp_path):
    save_config(tmp_path, PocketConfig("https://example.invalid", TOKEN))

    class Client:
        def delete_track(self, song_id):
            raise PocketHTTPError(503, "UNAVAILABLE", "https://secret.invalid/token")

    with pytest.raises(PocketHTTPError) as caught:
        delete_track(
            tmp_path,
            DeleteTargetIdentity("abcdef123456"),
            expected_connection_fingerprint=connection_fingerprint(PocketConfig("https://example.invalid", TOKEN)),
            client=Client(),
        )
    message = safe_delete_error(caught.value)
    assert "同じ song ID" in message
    assert "secret" not in message
    SyncLock(tmp_path).acquire().release()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (503, "Pocket を利用できません。後で再実行してください。"),
        (422, "Pocket との通信に失敗しました。後で再実行してください。"),
    ],
)
def test_sync_safe_error_keeps_existing_http_wording(status, expected):
    error = PocketHTTPError(status, "PRIVATE", "https://secret.invalid/token")

    assert safe_error(error) == expected
