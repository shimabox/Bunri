"""Convergent media → manifest → library synchronization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from bunri.pocket.http import JSON_LIMIT_BYTES, MEDIA_LIMIT_BYTES, JSONDocument, PocketHTTPClient, PocketHTTPError
from bunri.pocket.local import LocalPackage
from bunri.pocket.protocol import ProtocolError, merge_library, merge_manifest, stable_json, validate_library, validate_manifest


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


def _schema_major(value: object) -> str:
    if isinstance(value, dict):
        version = value.get("schema_version")
        if isinstance(version, str) and "." in version:
            return version.split(".", 1)[0]
    return "不明"


def _unsupported_schema_message(remote_major: object, stage: str | None) -> str:
    progress = "アップロードは開始していません。" if stage is None else f"{stage} の更新段階でアップロードを中断しました。"
    return (
        f"棚のデータ形式(schema major {remote_major})にこの Bunri は対応していません(対応: major 1)。{progress}\n"
        "Bunri または棚(bunri-pocket)を新しいバージョンに更新してから再実行してください。"
    )


def _raise_for_unsupported_http(exc: PocketHTTPError, stage: str | None) -> None:
    if exc.status == 409 and exc.code == "UNSUPPORTED_SCHEMA_MAJOR":
        major = exc.supported_major if isinstance(exc.supported_major, int) and not isinstance(exc.supported_major, bool) else "不明"
        raise SyncError(_unsupported_schema_message(major, stage)) from exc


def _manifest(client: PocketHTTPClient, song_id: str, stage: str | None = None) -> JSONDocument | None:
    try:
        doc = client.get_json(f"manifest/{song_id}")
    except PocketHTTPError as exc:
        _raise_for_unsupported_http(exc, stage)
        raise
    if doc is not None:
        try: validate_manifest(doc.value, song_id)
        except ProtocolError as exc:
            if exc.code == "UNSUPPORTED_SCHEMA_MAJOR":
                raise SyncError(_unsupported_schema_message(_schema_major(doc.value), stage)) from exc
            raise
    return doc


def _library(client: PocketHTTPClient, stage: str | None = None) -> JSONDocument | None:
    try:
        doc = client.get_json("library")
    except PocketHTTPError as exc:
        _raise_for_unsupported_http(exc, stage)
        raise
    if doc is not None:
        try: validate_library(doc.value)
        except ProtocolError as exc:
            if exc.code == "UNSUPPORTED_SCHEMA_MAJOR":
                raise SyncError(_unsupported_schema_message(_schema_major(doc.value), stage)) from exc
            raise
    return doc


def _payload_too_large(kind: str, name: str, size: int, limit: int) -> SyncError:
    return SyncError(f"送信するデータが棚の上限を超えています: {kind} {name}({size} バイト、上限 {limit} バイト)。アップロードは中断しました。")


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
        try: returned = client.put_media(song_id, asset.descriptor.remote_name, asset.path, asset.descriptor.bytes, asset.descriptor.sha256)
        except PocketHTTPError as exc:
            _raise_for_unsupported_http(exc, "media")
            if exc.status == 413:
                raise _payload_too_large("media", asset.descriptor.remote_name, asset.descriptor.bytes, MEDIA_LIMIT_BYTES) from exc
            raise
        if returned != asset.descriptor.sha256: raise SyncError(f"media upload checksum mismatch: {asset.descriptor.remote_name}")
        uploaded += 1
    manifest_updated = manifest_skipped = 0
    for attempt in range(4):
        current = _manifest(client, song_id, "manifest")
        if current is not None and current.value["source"]["digest"] != package.metadata.source.digest:
            raise SyncError(f"RACE_DIGEST_COLLISION:{current.value['source']['digest']}")
        kwargs = {"metadata": package.metadata, "assets": [x.descriptor for x in package.assets], "remote": current.value if current else None, "include_original": include_original}
        if clock is not None: kwargs["clock"] = clock
        manifest, changed = merge_manifest(**kwargs)
        if not changed: manifest_skipped = 1; break
        payload = stable_json(manifest)
        try: client.put_json(f"manifest/{song_id}", payload, current.etag if current else "*")
        except PocketHTTPError as exc:
            _raise_for_unsupported_http(exc, "manifest")
            if exc.status == 413:
                raise _payload_too_large("document", "manifest", len(payload), JSON_LIMIT_BYTES) from exc
            if exc.status == 412:
                latest = _manifest(client, song_id, "manifest")
                if latest is not None and latest.value["source"]["digest"] != package.metadata.source.digest:
                    raise SyncError(f"RACE_DIGEST_COLLISION:{latest.value['source']['digest']}") from exc
                if attempt < 3: continue
                raise SyncError("manifest の競合が解消しません。再実行してください。") from exc
            raise
        manifest_updated = 1; break
    else: raise SyncError("manifest の競合が解消しません。再実行してください。")
    library_updated = library_skipped = 0
    for attempt in range(4):
        current = _library(client, "library")
        kwargs = {"manifest": manifest, "remote": current.value if current else None}
        if clock is not None: kwargs["clock"] = clock
        library, changed = merge_library(**kwargs)
        if not changed: library_skipped = 1; break
        payload = stable_json(library)
        try: client.put_json("library", payload, current.etag if current else "*")
        except PocketHTTPError as exc:
            _raise_for_unsupported_http(exc, "library")
            if exc.status == 413:
                raise _payload_too_large("document", "library", len(payload), JSON_LIMIT_BYTES) from exc
            if exc.status == 412 and attempt < 3: continue
            if exc.status == 412: raise SyncError("library の競合が解消しません。再実行してください。") from exc
            raise
        library_updated = 1; break
    else: raise SyncError("library の競合が解消しません。再実行してください。")
    return SyncResult(uploaded, skipped, manifest_updated, manifest_skipped, library_updated, library_skipped)
