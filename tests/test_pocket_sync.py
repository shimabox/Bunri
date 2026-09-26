from __future__ import annotations

from pathlib import Path

import pytest

from bunri.package_metadata import PackageMetadata, SourceIdentity, TargetMetadata
from bunri.pocket.http import JSONDocument, PocketHTTPError
from bunri.pocket.local import LocalAsset, LocalPackage
from bunri.pocket.protocol import AssetInfo
from bunri.pocket.sync import RemoteFeatures, SyncError, synchronize

SUPPORTED = {"api": {"major": 1}, "features": {"pan_split": True}}
UNSUPPORTED = {"api": {"major": 1}}
CLOCK = lambda: "2026-08-30T00:00:00Z"  # noqa: E731


class FakeClient:
    def __init__(self, capabilities=SUPPORTED): self.docs = {}; self.media = {}; self.calls = []; self.fail_library_412 = 0; self.fail_manifest_412 = 0; self.manifest_collision = None; self.capabilities_value = capabilities
    def capabilities(self):
        self.calls.append(("CAPABILITIES",))
        if isinstance(self.capabilities_value, Exception): raise self.capabilities_value
        return self.capabilities_value
    def get_json(self, path): self.calls.append(("GET", path)); return self.docs.get(path)
    def head_media(self, song, name): self.calls.append(("HEAD", name)); return self.media.get(name)
    def put_media(self, song, name, path, size, checksum): self.calls.append(("PUT_MEDIA", name)); self.media[name]=(checksum,size); return checksum
    def put_json(self, path, data, condition):
        import json
        self.calls.append(("PUT_JSON", path, condition))
        if path.startswith("manifest/") and self.fail_manifest_412:
            self.fail_manifest_412 -= 1
            if not self.fail_manifest_412 and self.manifest_collision is not None:
                self.docs[path] = JSONDocument(self.manifest_collision, '"other"')
            raise PocketHTTPError(412, "PRECONDITION_FAILED", "https://safe.invalid")
        if path == "library" and self.fail_library_412:
            self.fail_library_412 -= 1; raise PocketHTTPError(412, "PRECONDITION_FAILED", "https://safe.invalid")
        self.docs[path]=JSONDocument(json.loads(data), '"next"'); return '"next"'


def package(tmp_path: Path, pan_split: str | None = None) -> LocalPackage:
    metadata=PackageMetadata("Song","Song",SourceIdentity("sha1","a"*40,"a"*12),(TargetMetadata("guitar",("mp3","wav"),pan_split),))
    assets=[]
    entries=[("original.mp3",None,None),("guitar.mp3","guitar","target"),("guitar.backing.mp3","guitar","backing")]
    if pan_split == "left_right": entries += [("guitar.left.mp3","guitar","left"),("guitar.right.mp3","guitar","right")]
    for name,target,role in entries:
        path=tmp_path/name; path.write_bytes(name.encode()); info=AssetInfo(name,path.stat().st_size,(name.encode().hex()+"0"*64)[:64],target,role); assets.append(LocalAsset(info,path))
    return LocalPackage(tmp_path,metadata,tuple(assets))


def _uploads(client):
    return [call[1] for call in client.calls if call[0] == "PUT_MEDIA"]


def _manifest_doc(client):
    return client.docs["manifest/" + "a" * 12].value


def test_unsupported_pocket_gets_the_three_files_without_pan_split(tmp_path):
    client = FakeClient(UNSUPPORTED)
    result = synchronize(package(tmp_path, "left_right"), client, clock=CLOCK)
    assert sorted(_uploads(client)) == ["guitar.backing.mp3", "guitar.mp3", "original.mp3"]
    manifest = _manifest_doc(client)
    assert manifest["schema_version"] == "1.0"
    assert "pan_split" not in str(manifest)
    assert [stem["role"] for stem in manifest["instruments"][0]["stems"]] == ["target", "backing"]
    assert result.pan_split_supported is False


def test_unsupported_pocket_documents_match_a_package_without_split(tmp_path):
    documents = []
    for index, pan_split in enumerate((None, "left_right", "single")):
        directory = tmp_path / str(index); directory.mkdir()
        client = FakeClient(UNSUPPORTED)
        synchronize(package(directory, pan_split), client, clock=CLOCK)
        documents.append({path: doc.value for path, doc in client.docs.items()})
    assert documents[0] == documents[1] == documents[2]


def test_supported_pocket_gets_left_and_right(tmp_path):
    client = FakeClient()
    result = synchronize(package(tmp_path, "left_right"), client, clock=CLOCK)
    assert _uploads(client) == ["original.mp3", "guitar.mp3", "guitar.backing.mp3", "guitar.left.mp3", "guitar.right.mp3"]
    manifest = _manifest_doc(client)
    assert manifest["schema_version"] == "1.1"
    instrument = manifest["instruments"][0]
    assert instrument["pan_split"] == "left_right"
    assert [(stem["role"], stem["path"]) for stem in instrument["stems"]] == [
        ("target", "guitar.mp3"), ("backing", "guitar.backing.mp3"),
        ("left", "guitar.left.mp3"), ("right", "guitar.right.mp3"),
    ]
    assert result.library_updated == 1 and "library" in client.docs
    assert result.pan_split_supported is True
    # audio → manifest → library
    puts = [call[0] if call[0] == "PUT_MEDIA" else call[1] for call in client.calls if call[0].startswith("PUT")]
    assert puts == ["PUT_MEDIA"] * 5 + ["manifest/" + "a" * 12, "library"]


def test_supported_pocket_records_single_without_extra_files(tmp_path):
    client = FakeClient()
    result = synchronize(package(tmp_path, "single"), client, clock=CLOCK)
    assert len(_uploads(client)) == 3
    manifest = _manifest_doc(client)
    assert manifest["schema_version"] == "1.1"
    assert manifest["instruments"][0]["pan_split"] == "single"
    assert len(manifest["instruments"][0]["stems"]) == 2
    assert result.pan_split_supported is True


def test_supported_pocket_without_recorded_split_is_unchanged(tmp_path):
    client = FakeClient()
    synchronize(package(tmp_path), client, clock=CLOCK)
    manifest = _manifest_doc(client)
    assert manifest["schema_version"] == "1.0"
    assert "pan_split" not in str(manifest)


def test_resync_after_pocket_update_sends_only_left_and_right(tmp_path):
    local = package(tmp_path, "left_right")
    client = FakeClient(UNSUPPORTED)
    synchronize(local, client, clock=CLOCK)
    client.calls.clear()
    client.capabilities_value = SUPPORTED
    second = synchronize(local, client, clock=lambda: "2026-09-01T00:00:00Z")
    assert _uploads(client) == ["guitar.left.mp3", "guitar.right.mp3"]
    assert (second.media_uploaded, second.media_skipped) == (2, 3)
    assert (second.manifest_updated, second.library_updated) == (1, 1)
    assert _manifest_doc(client)["schema_version"] == "1.1"
    assert _manifest_doc(client)["updated_at"] == "2026-09-01T00:00:00Z"
    client.calls.clear()
    third = synchronize(local, client, clock=lambda: pytest.fail("clock called"))
    assert _uploads(client) == []
    assert (third.media_skipped, third.manifest_skipped, third.library_skipped) == (5, 1, 1)


def test_capabilities_are_read_once_before_the_first_manifest(tmp_path):
    client = FakeClient()
    synchronize(package(tmp_path, "left_right"), client, clock=CLOCK)
    assert client.calls.count(("CAPABILITIES",)) == 1
    assert client.calls.index(("CAPABILITIES",)) < client.calls.index(("GET", "manifest/" + "a" * 12))
    assert client.calls[0] == ("CAPABILITIES",)


def test_given_features_skip_the_capabilities_request(tmp_path):
    client = FakeClient(PocketHTTPError(500, "UNEXPECTED", "https://safe.invalid"))
    result = synchronize(package(tmp_path, "left_right"), client, clock=CLOCK, features=RemoteFeatures(pan_split=True))
    assert ("CAPABILITIES",) not in client.calls
    assert len(_uploads(client)) == 5 and result.pan_split_supported is True


def test_capabilities_failure_stops_before_any_request(tmp_path):
    client = FakeClient(PocketHTTPError(401, "UNAUTHORIZED", "https://safe.invalid"))
    with pytest.raises(PocketHTTPError) as caught:
        synchronize(package(tmp_path, "left_right"), client, clock=CLOCK)
    assert caught.value.status == 401
    assert client.calls == [("CAPABILITIES",)]


def test_initial_sync_and_idempotent_rerun(tmp_path):
    client=FakeClient(); local=package(tmp_path); clock=lambda:"2026-08-30T00:00:00Z"
    first=synchronize(local,client,clock=clock)
    assert (first.media_uploaded,first.manifest_updated,first.library_updated)==(3,1,1)
    before=len([x for x in client.calls if x[0].startswith("PUT")])
    second=synchronize(local,client,clock=clock)
    after=len([x for x in client.calls if x[0].startswith("PUT")])
    assert second.media_skipped==3 and second.manifest_skipped==1 and second.library_skipped==1 and before==after


def test_remote_digest_collision_stops_before_media(tmp_path):
    client=FakeClient(); local=package(tmp_path)
    manifest={"schema_version":"1.0","song_id":"a"*12,"title":"Other","source":{"algorithm":"sha1","digest":"a"*12+"b"*28,"cache_key":"a"*12},"original":None,"instruments":[],"updated_at":"2026-08-30T00:00:00Z"}
    client.docs["manifest/"+"a"*12]=JSONDocument(manifest,'"one"')
    with pytest.raises(SyncError,match="DIGEST_COLLISION"): synchronize(local,client)
    assert not any(call[0]=="PUT_MEDIA" for call in client.calls)


def test_unsupported_remote_manifest_and_server_409_have_same_update_guidance(tmp_path):
    expected = (
        "音源ポケットのデータ形式(schema major 2)にこの Bunri は対応していません(対応: major 1)。アップロードは開始していません。\n"
        "Bunri または Bunri Pocket を新しいバージョンに更新してから再実行してください。"
    )
    manifest = {
        "schema_version": "2.0", "song_id": "a" * 12, "title": "Song",
        "source": {"algorithm": "sha1", "digest": "a" * 40, "cache_key": "a" * 12},
        "original": None, "instruments": [], "updated_at": "2026-08-30T00:00:00Z",
    }
    document_client = FakeClient()
    document_client.docs["manifest/" + "a" * 12] = JSONDocument(manifest, '"one"')
    with pytest.raises(SyncError) as document_error:
        synchronize(package(tmp_path), document_client)

    class UnsupportedClient(FakeClient):
        def get_json(self, path):
            self.calls.append(("GET", path))
            if path.startswith("manifest/"):
                raise PocketHTTPError(409, "UNSUPPORTED_SCHEMA_MAJOR", "https://safe.invalid", supported_major=2)
            return self.docs.get(path)

    with pytest.raises(SyncError) as server_error:
        synchronize(package(tmp_path), UnsupportedClient())
    assert str(document_error.value) == str(server_error.value) == expected


def test_media_413_names_asset_size_and_limit(tmp_path):
    class TooLargeMediaClient(FakeClient):
        def put_media(self, song, name, path, size, checksum):
            if name == "guitar.backing.mp3":
                raise PocketHTTPError(413, "PAYLOAD_TOO_LARGE", "https://safe.invalid?token=secret")
            return super().put_media(song, name, path, size, checksum)

    with pytest.raises(SyncError) as error:
        synchronize(package(tmp_path), TooLargeMediaClient())
    assert str(error.value) == "送信するデータが音源ポケットの上限を超えています: media guitar.backing.mp3(18 バイト、上限 94371840 バイト)。アップロードは中断しました。"
    assert "secret" not in str(error.value)


def test_json_413_names_document_size_and_limit(tmp_path):
    class TooLargeJSONClient(FakeClient):
        def put_json(self, path, data, condition):
            if path.startswith("manifest/"):
                raise PocketHTTPError(413, "PAYLOAD_TOO_LARGE", "https://safe.invalid?token=secret")
            return super().put_json(path, data, condition)

    with pytest.raises(SyncError) as error:
        synchronize(package(tmp_path), TooLargeJSONClient(), clock=lambda: "2026-08-30T00:00:00Z")
    assert str(error.value) == "送信するデータが音源ポケットの上限を超えています: document manifest(741 バイト、上限 1048576 バイト)。アップロードは中断しました。"
    assert "secret" not in str(error.value)


def test_library_412_reloads_and_retries(tmp_path):
    client=FakeClient(); client.fail_library_412=2
    result=synchronize(package(tmp_path),client,clock=lambda:"2026-08-30T00:00:00Z")
    assert result.library_updated==1 and len([x for x in client.calls if x[:2]==("GET","library")])>=3


def test_final_manifest_412_reloads_and_reports_digest_collision(tmp_path):
    client = FakeClient(); client.fail_manifest_412 = 4
    client.manifest_collision = {
        "schema_version": "1.0", "song_id": "a" * 12, "title": "Other",
        "source": {"algorithm": "sha1", "digest": "a" * 12 + "b" * 28, "cache_key": "a" * 12},
        "original": None, "instruments": [], "updated_at": "2026-08-30T00:00:00Z",
    }
    with pytest.raises(SyncError, match="RACE_DIGEST_COLLISION"):
        synchronize(package(tmp_path), client, clock=lambda: "2026-08-30T00:00:00Z")
    assert len([call for call in client.calls if call[:2] == ("PUT_JSON", "manifest/" + "a" * 12)]) == 4
    assert client.calls[-1] == ("GET", "manifest/" + "a" * 12)
