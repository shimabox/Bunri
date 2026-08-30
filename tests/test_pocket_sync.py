from __future__ import annotations

from pathlib import Path

import pytest

from bunri.package_metadata import PackageMetadata, SourceIdentity, TargetMetadata
from bunri.pocket.http import JSONDocument, PocketHTTPError
from bunri.pocket.local import LocalAsset, LocalPackage
from bunri.pocket.protocol import AssetInfo
from bunri.pocket.sync import SyncError, synchronize


class FakeClient:
    def __init__(self): self.docs = {}; self.media = {}; self.calls = []; self.fail_library_412 = 0; self.fail_manifest_412 = 0; self.manifest_collision = None
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


def package(tmp_path: Path) -> LocalPackage:
    metadata=PackageMetadata("Song","Song",SourceIdentity("sha1","a"*40,"a"*12),(TargetMetadata("guitar",("mp3","wav")),))
    assets=[]
    for name,target,role in (("original.mp3",None,None),("guitar.mp3","guitar","target"),("guitar.backing.mp3","guitar","backing")):
        path=tmp_path/name; path.write_bytes(name.encode()); info=AssetInfo(name,path.stat().st_size,(name.encode().hex()+"0"*64)[:64],target,role); assets.append(LocalAsset(info,path))
    return LocalPackage(tmp_path,metadata,tuple(assets))


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
        "棚のデータ形式(schema major 2)にこの Bunri は対応していません(対応: major 1)。アップロードは開始していません。\n"
        "Bunri または棚(bunri-pocket)を新しいバージョンに更新してから再実行してください。"
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
    assert str(error.value) == "送信するデータが棚の上限を超えています: media guitar.backing.mp3(18 バイト、上限 94371840 バイト)。アップロードは中断しました。"
    assert "secret" not in str(error.value)


def test_json_413_names_document_size_and_limit(tmp_path):
    class TooLargeJSONClient(FakeClient):
        def put_json(self, path, data, condition):
            if path.startswith("manifest/"):
                raise PocketHTTPError(413, "PAYLOAD_TOO_LARGE", "https://safe.invalid?token=secret")
            return super().put_json(path, data, condition)

    with pytest.raises(SyncError) as error:
        synchronize(package(tmp_path), TooLargeJSONClient(), clock=lambda: "2026-08-30T00:00:00Z")
    assert str(error.value) == "送信するデータが棚の上限を超えています: document manifest(741 バイト、上限 1048576 バイト)。アップロードは中断しました。"
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
