"""Convergent media → manifest → library synchronization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from bunri.pocket.http import JSONDocument, PocketHTTPClient, PocketHTTPError
from bunri.pocket.local import LocalPackage
from bunri.pocket.protocol import merge_library, merge_manifest, stable_json, validate_library, validate_manifest


class SyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class SyncResult:
    media_uploaded: int = 0
    media_skipped: int = 0
    manifest_updated: int = 0
    manifest_skipped: int = 0
    library_updated: int = 0
    library_skipped: int = 0


def _manifest(client: PocketHTTPClient, song_id: str) -> JSONDocument | None:
    doc = client.get_json(f"manifest/{song_id}")
    if doc is not None: validate_manifest(doc.value, song_id)
    return doc


def _library(client: PocketHTTPClient) -> JSONDocument | None:
    doc = client.get_json("library")
    if doc is not None: validate_library(doc.value)
    return doc


def synchronize(package: LocalPackage, client: PocketHTTPClient, *, include_original: bool = True, clock: Callable[[], str] | None = None) -> SyncResult:
    song_id = package.metadata.source.cache_key
    pre_manifest, pre_library = _manifest(client, song_id), _library(client)
    if pre_manifest is not None and pre_manifest.value["source"]["digest"] != package.metadata.source.digest:
        raise SyncError(f"DIGEST_COLLISION:{pre_manifest.value['source']['digest']}")
    if pre_manifest is None and pre_library is not None and any(x["song_id"] == song_id for x in pre_library.value["songs"]):
        raise SyncError("library が存在しない manifest を参照しています。アップロードは開始していません。")
    uploaded = skipped = 0
    for asset in package.assets:
        info = client.head_media(song_id, asset.descriptor.remote_name)
        if info == (asset.descriptor.sha256, asset.descriptor.bytes): skipped += 1; continue
        returned = client.put_media(song_id, asset.descriptor.remote_name, asset.path, asset.descriptor.bytes, asset.descriptor.sha256)
        if returned != asset.descriptor.sha256: raise SyncError(f"media upload checksum mismatch: {asset.descriptor.remote_name}")
        uploaded += 1
    manifest_updated = manifest_skipped = 0
    for attempt in range(4):
        current = _manifest(client, song_id)
        if current is not None and current.value["source"]["digest"] != package.metadata.source.digest:
            raise SyncError(f"RACE_DIGEST_COLLISION:{current.value['source']['digest']}")
        kwargs = {"metadata": package.metadata, "assets": [x.descriptor for x in package.assets], "remote": current.value if current else None, "include_original": include_original}
        if clock is not None: kwargs["clock"] = clock
        manifest, changed = merge_manifest(**kwargs)
        if not changed: manifest_skipped = 1; break
        try: client.put_json(f"manifest/{song_id}", stable_json(manifest), current.etag if current else "*")
        except PocketHTTPError as exc:
            if exc.status == 412:
                latest = _manifest(client, song_id)
                if latest is not None and latest.value["source"]["digest"] != package.metadata.source.digest:
                    raise SyncError(f"RACE_DIGEST_COLLISION:{latest.value['source']['digest']}") from exc
                if attempt < 3: continue
                raise SyncError("manifest の競合が解消しません。再実行してください。") from exc
            raise
        manifest_updated = 1; break
    else: raise SyncError("manifest の競合が解消しません。再実行してください。")
    library_updated = library_skipped = 0
    for attempt in range(4):
        current = _library(client)
        kwargs = {"manifest": manifest, "remote": current.value if current else None}
        if clock is not None: kwargs["clock"] = clock
        library, changed = merge_library(**kwargs)
        if not changed: library_skipped = 1; break
        try: client.put_json("library", stable_json(library), current.etag if current else "*")
        except PocketHTTPError as exc:
            if exc.status == 412 and attempt < 3: continue
            if exc.status == 412: raise SyncError("library の競合が解消しません。再実行してください。") from exc
            raise
        library_updated = 1; break
    else: raise SyncError("library の競合が解消しません。再実行してください。")
    return SyncResult(uploaded, skipped, manifest_updated, manifest_skipped, library_updated, library_skipped)
