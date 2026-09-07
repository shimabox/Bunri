"""CLI/Web shared orchestration for Bunri Pocket synchronization and deletion."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Literal

from bunri.package_metadata import SourceIdentity
from bunri.pocket.config import read_config
from bunri.pocket.http import PocketHTTPClient, PocketHTTPError
from bunri.pocket.local import (
    LocalPackage,
    LocalPreflightError,
    all_package_names,
    package_name_key,
    preflight,
)
from bunri.pocket.lock import SyncLock, SyncLockBusy
from bunri.pocket.protocol import ProtocolError, merge_library, merge_manifest
from bunri.pocket.sync import SyncError, SyncResult, _library, _manifest, synchronize


class PocketServiceError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: str = "invalid",
        legacy: Iterable[str] = (),
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.legacy = tuple(legacy)


@dataclass(frozen=True)
class PackageInventory:
    packages: tuple[LocalPackage, ...]
    legacy: tuple[str, ...]


@dataclass(frozen=True)
class RemoteStatus:
    state: str
    can_sync: bool
    message: str | None = None
    conflict: bool = False


@dataclass(frozen=True)
class PackageStatus:
    safe_name: str
    title: str
    song_id: str | None
    digest: str | None
    remote: RemoteStatus


@dataclass(frozen=True)
class LibraryTrack:
    song_id: str
    title: str


@dataclass(frozen=True)
class DeleteTargetIdentity:
    song_id: str
    digest: str | None = None
    safe_name: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class DeleteResult:
    song_id: str


@dataclass(frozen=True)
class BatchItem:
    safe_name: str
    status: str
    result: SyncResult | None = None
    error: str | None = None


@dataclass
class BatchResult:
    total: int
    legacy: list[str] = field(default_factory=list)
    items: list[BatchItem] = field(default_factory=list)

    @property
    def completed(self) -> int:
        return sum(item.status == "done" for item in self.items)

    @property
    def failed(self) -> int:
        return sum(item.status == "error" for item in self.items)

    @property
    def pending(self) -> int:
        return sum(item.status == "pending" for item in self.items)


@dataclass(frozen=True)
class ScannedPackage:
    """One package directory as preflight saw it, plus its sidecar identity.

    Identity is kept separately from the preflight outcome on purpose: a
    package whose audio assets are missing or unreadable still claims a
    source digest and a song ID, so it has to take part in the duplicate
    identity check. Otherwise a broken copy of a song would hide the
    ambiguity and let the remaining copy be uploaded as if it were the only
    one. Directories without a sidecar (legacy) claim no identity and stay
    out of that check.
    """

    safe_name: str
    package: LocalPackage | None
    error: LocalPreflightError | None
    identity: SourceIdentity | None


def _scan_packages(out_dir: Path, *, include_original: bool) -> list[ScannedPackage]:
    scanned: list[ScannedPackage] = []
    for safe_name in all_package_names(out_dir):
        try:
            package = preflight(out_dir, safe_name, include_original=include_original)
        except LocalPreflightError as exc:
            scanned.append(ScannedPackage(
                safe_name,
                None,
                exc,
                exc.identity,
            ))
        else:
            scanned.append(ScannedPackage(safe_name, package, None, package.metadata.source))
    return scanned


def _identity_issues(entries: Iterable[ScannedPackage]) -> list[str]:
    digest_names: dict[str, set[str]] = {}
    id_digests: dict[str, set[str]] = {}
    for entry in entries:
        if entry.identity is None:
            continue
        digest_names.setdefault(entry.identity.digest, set()).add(
            package_name_key(entry.safe_name)
        )
        id_digests.setdefault(entry.identity.cache_key, set()).add(entry.identity.digest)
    issues = []
    for digest, names in sorted(digest_names.items()):
        if len(names) > 1:
            issues.append(f"同じ入力音源が複数のパッケージ名にあります: {', '.join(sorted(names))}")
    for song_id, digests in sorted(id_digests.items()):
        if len(digests) > 1:
            issues.append(f"同じ12桁の song ID に異なる入力音源があります: {song_id}")
    return issues


def _package_name_groups(
    entries: Iterable[ScannedPackage],
) -> dict[str, tuple[ScannedPackage, ...]]:
    groups: dict[str, list[ScannedPackage]] = {}
    for entry in entries:
        groups.setdefault(package_name_key(entry.safe_name), []).append(entry)
    return {key: tuple(group) for key, group in groups.items()}


def _package_name_issues(entries: Iterable[ScannedPackage]) -> list[str]:
    issues = []
    for group in _package_name_groups(entries).values():
        if len(group) > 1:
            names = ", ".join(repr(entry.safe_name) for entry in group)
            issues.append(f"NFC正規化後に同じ名前になるパッケージがあります: {names}")
    return issues


def inventory(out_dir: Path, *, include_original: bool = True) -> PackageInventory:
    scanned = _scan_packages(out_dir, include_original=include_original)
    packages: list[LocalPackage] = []
    legacy: list[str] = []
    problems: list[str] = []
    for entry in scanned:
        if entry.package is not None:
            packages.append(entry.package)
        elif entry.error is not None and entry.error.kind == "legacy":
            legacy.append(entry.safe_name)
        elif entry.error is not None:
            problems.extend(f"{entry.safe_name}: {issue}" for issue in entry.error.issues)
    problems.extend(_package_name_issues(scanned))
    problems.extend(_identity_issues(scanned))
    if problems:
        raise PocketServiceError(
            "ローカルパッケージを安全に同期できません。アップロードは開始していません。\n- "
            + "\n- ".join(problems),
            kind="local",
            legacy=legacy,
        )
    return PackageInventory(tuple(packages), tuple(legacy))


def resolve_package(
    out_dir: Path,
    selector: str,
    *,
    resolution: Literal["safe_name", "song_id"],
    expected_digest: str | None = None,
    include_original: bool = True,
) -> LocalPackage:
    scanned = _scan_packages(out_dir, include_original=include_original)
    by_name = _package_name_groups(scanned)
    if resolution == "safe_name":
        if expected_digest is not None:
            raise ValueError("expected_digest is only valid for song_id resolution")
        entries = by_name.get(package_name_key(selector), ())
        if not entries:
            raise PocketServiceError("同期する曲が見つかりません。", kind="not_found")
        if len(entries) > 1:
            raise PocketServiceError(
                "正規化すると同じ名前になるパッケージが複数あるためアップロードできません。",
                kind="conflict",
            )
        entry = entries[0]
        if entry.package is None:
            exc = entry.error
            assert exc is not None
            if exc.kind == "legacy":
                raise PocketServiceError("このパッケージは再生成が必要です。", kind="legacy") from exc
            raise PocketServiceError(
                "パッケージを安全に同期できません。アップロードは開始していません。\n- "
                + "\n- ".join(exc.issues),
                kind="local",
            ) from exc
        selected = entry.package
        source = selected.metadata.source
        peers = [
            other for other in scanned
            if other.identity is not None
            and (other.identity.digest == source.digest or other.identity.cache_key == source.cache_key)
        ]
        if _identity_issues(peers):
            raise PocketServiceError("曲の identity が競合しているためアップロードできません。", kind="conflict")
        return selected
    if resolution != "song_id":
        raise ValueError(f"unknown package resolution mode: {resolution}")
    matches = [
        entry for entry in scanned
        if entry.identity is not None
        and entry.identity.cache_key == selector
    ]
    if not matches:
        raise PocketServiceError("同期する曲が見つかりません。", kind="not_found")
    name_groups = _package_name_groups(scanned)
    if (
        len(matches) != 1
        or _identity_issues(matches)
        or len(name_groups[package_name_key(matches[0].safe_name)]) > 1
    ):
        raise PocketServiceError("曲の identity が競合しているためアップロードできません。", kind="conflict")
    match = matches[0]
    if match.package is None:
        exc = match.error
        assert exc is not None
        raise PocketServiceError(
            "パッケージを安全に同期できません。アップロードは開始していません。\n- "
            + "\n- ".join(exc.issues),
            kind="local",
        ) from exc
    package = match.package
    if expected_digest is not None:
        try:
            package = preflight(
                out_dir,
                package.directory.name,
                include_original=include_original,
                expected_digest=expected_digest,
            )
        except LocalPreflightError as exc:
            raise PocketServiceError("選択した曲の同期情報が変更されました。", kind="conflict") from exc
    return package


def inspect_remote(package: LocalPackage, client: PocketHTTPClient) -> RemoteStatus:
    song_id = package.metadata.source.cache_key
    try:
        manifest_doc = _manifest(client, song_id)
        library_doc = _library(client)
        library_has_song = bool(
            library_doc
            and any(item.get("song_id") == song_id for item in library_doc.value.get("songs", []))
        )
        if manifest_doc is None:
            if library_has_song:
                return RemoteStatus(
                    "different",
                    False,
                    "棚の状態に不整合があるためアップロードできません。",
                )
            return RemoteStatus("not_synced", True)
        if manifest_doc.value["source"]["digest"] != package.metadata.source.digest:
            return RemoteStatus("different", False, "song ID が競合しています。", True)
        manifest, manifest_changed = merge_manifest(
            metadata=package.metadata,
            assets=[asset.descriptor for asset in package.assets],
            remote=manifest_doc.value,
            include_original=True,
        )
        media_match = all(
            client.head_media(song_id, asset.descriptor.remote_name)
            == (asset.descriptor.sha256, asset.descriptor.bytes)
            for asset in package.assets
        )
        library_changed = True
        if library_doc is not None:
            _, library_changed = merge_library(manifest=manifest, remote=library_doc.value)
        if not manifest_changed and media_match and library_has_song and not library_changed:
            return RemoteStatus("synced", True)
        return RemoteStatus("different", True)
    except (OSError, PocketHTTPError, ProtocolError, SyncError, ValueError):
        return RemoteStatus("unknown", False, "棚の状態を確認できません。")


def inspect_packages(out_dir: Path, client: PocketHTTPClient) -> tuple[PackageStatus, ...]:
    scanned = _scan_packages(out_dir, include_original=True)
    conflict_names = {
        key for key, entries in _package_name_groups(scanned).items() if len(entries) > 1
    }
    digest_names: dict[str, set[str]] = {}
    id_digests: dict[str, set[str]] = {}
    for entry in scanned:
        if entry.identity is None:
            continue
        digest_names.setdefault(entry.identity.digest, set()).add(
            package_name_key(entry.safe_name)
        )
        id_digests.setdefault(entry.identity.cache_key, set()).add(entry.identity.digest)
    conflict_digests = {digest for digest, names in digest_names.items() if len(names) > 1}
    conflict_ids = {song_id for song_id, digests in id_digests.items() if len(digests) > 1}

    conflicted = RemoteStatus(
        "different",
        False,
        "曲のパッケージ名または identity が競合しているためアップロードできません。",
        True,
    )
    statuses: list[PackageStatus] = []
    for entry in scanned:
        source = entry.identity
        in_conflict = package_name_key(entry.safe_name) in conflict_names or (
            source is not None
            and (source.digest in conflict_digests or source.cache_key in conflict_ids)
        )
        if entry.package is None:
            exc = entry.error
            assert exc is not None
            if in_conflict:
                remote = conflicted
            elif exc.kind == "legacy":
                remote = RemoteStatus(
                    "legacy",
                    False,
                    "元の入力音源から再生成してください。キャッシュが残っていれば分離処理は省略されます。",
                )
            else:
                remote = RemoteStatus("unknown", False, "ローカルパッケージを確認できません。")
            statuses.append(PackageStatus(
                entry.safe_name,
                exc.metadata.title if exc.metadata is not None else entry.safe_name,
                source.cache_key if source is not None else None,
                source.digest if source is not None else None,
                remote,
            ))
            continue
        assert source is not None
        statuses.append(PackageStatus(
            entry.safe_name,
            entry.package.metadata.title,
            source.cache_key,
            source.digest,
            conflicted if in_conflict else inspect_remote(entry.package, client),
        ))
    return tuple(sorted(statuses, key=lambda item: (item.safe_name.casefold(), item.safe_name)))


def list_library_tracks(
    out_dir: Path,
    *,
    client: PocketHTTPClient | None = None,
) -> tuple[LibraryTrack, ...]:
    """Return the validated shelf library in its current display order."""
    config = read_config(out_dir)
    if config is None:
        raise PocketServiceError("Pocket の接続設定がありません。", kind="not_connected")
    remote = client or PocketHTTPClient(config.base_url, config.token)
    try:
        document = _library(remote)
    except ProtocolError as exc:
        raise PocketServiceError("棚の library が破損しています。", kind="remote_invalid") from exc
    if document is None:
        return ()
    return tuple(
        LibraryTrack(song_id=item["song_id"], title=item["title"])
        for item in document.value["songs"]
    )


def resolve_delete_target(out_dir: Path, safe_name: str) -> DeleteTargetIdentity:
    package = resolve_package(out_dir, safe_name, resolution="safe_name", include_original=True)
    source = package.metadata.source
    return DeleteTargetIdentity(
        song_id=source.cache_key,
        digest=source.digest,
        safe_name=package.directory.name,
        title=package.metadata.title,
    )


def delete_track(
    out_dir: Path,
    target: DeleteTargetIdentity,
    *,
    lock: SyncLock | None = None,
    client: PocketHTTPClient | None = None,
) -> DeleteResult:
    """Delete one shelf track while holding the shared Pocket mutation lock."""
    config = read_config(out_dir)
    if config is None:
        raise PocketServiceError("Pocket の接続設定がありません。", kind="not_connected")
    if not re.fullmatch(r"[0-9a-f]{12}", target.song_id):
        raise PocketServiceError("song ID が不正です。", kind="invalid_song_id")
    owned_lock = lock is None
    active_lock = lock or SyncLock(out_dir).acquire()
    try:
        if target.safe_name is not None:
            current = resolve_delete_target(out_dir, target.safe_name)
            if current.song_id != target.song_id or (
                target.digest is not None and current.digest != target.digest
            ):
                raise PocketServiceError("選択した曲の identity が変更されました。", kind="conflict")
        remote = client or PocketHTTPClient(config.base_url, config.token)
        try:
            remote.delete_track(target.song_id)
        except PocketHTTPError:
            raise
        except (TimeoutError, OSError) as exc:
            raise PocketServiceError(
                "棚からの削除を確認できませんでした。",
                kind="delete_unknown",
            ) from exc
        return DeleteResult(target.song_id)
    finally:
        if owned_lock:
            active_lock.release()


def safe_error(exc: BaseException) -> str:
    if isinstance(exc, SyncLockBusy):
        return "別の Pocket 同期が実行中です。完了後に再実行してください。"
    if isinstance(exc, PocketHTTPError):
        messages = {
            401: "Pocket の認証に失敗しました。接続設定を更新してください。",
            409: "Pocket と Bunri のデータ形式に互換性がありません。",
            413: "送信するデータが Pocket の上限を超えています。",
            429: "Pocket が要求を制限しました。後で再実行してください。",
            503: "Pocket を利用できません。後で再実行してください。",
        }
        return messages.get(exc.status, "Pocket との通信に失敗しました。後で再実行してください。")
    if isinstance(exc, SyncError):
        if str(exc).startswith(("DIGEST_COLLISION:", "RACE_DIGEST_COLLISION:")):
            return "同じ song ID に別の入力音源があるため同期できません。"
        return "Pocket の同期に失敗しました。棚の状態を確認して再実行してください。"
    if isinstance(exc, PocketServiceError):
        messages = {
            "not_connected": "Pocket の接続設定がありません。",
            "legacy": "このパッケージは再生成が必要です。",
            "not_found": "同期する曲が見つかりません。",
            "conflict": "曲の identity が競合しているためアップロードできません。",
            "local": "ローカルパッケージを安全に同期できません。",
        }
        return messages.get(exc.kind, "Pocket の同期を開始できません。")
    if isinstance(exc, LocalPreflightError):
        return "ローカルパッケージを安全に同期できません。"
    return "Pocket の同期に失敗しました。後で再実行してください。"


def safe_delete_error(exc: BaseException) -> str:
    """Return a secret-free error message for a shelf deletion."""
    if isinstance(exc, SyncLockBusy):
        return "別の Pocket 操作が実行中です。完了後に再実行してください。"
    if isinstance(exc, PocketHTTPError):
        messages = {
            401: "Pocket の認証に失敗しました。接続設定を更新してください。",
            409: "Pocket と Bunri のデータ形式に互換性がありません。",
            413: "送信するデータが Pocket の上限を超えています。",
            422: "棚の library が破損しています。棚の内容は変更されていません。",
            404: "Pocket の接続先または protocol を確認してください。",
            503: "棚からの削除を確認できませんでした。同じ song ID で再実行できます。",
        }
        if exc.status == 429:
            return "Pocket が要求を制限しました。後で再実行してください。" + (
                f" Retry-After: {exc.retry_after}" if exc.retry_after else ""
            )
        return messages.get(exc.status, "Pocket との通信に失敗しました。後で再実行してください。")
    if isinstance(exc, PocketServiceError):
        messages = {
            "not_connected": "Pocket の接続設定がありません。",
            "legacy": "このパッケージは再生成が必要です。",
            "not_found": "削除する曲が見つかりません。",
            "conflict": "曲の identity が競合しているため削除できません。",
            "local": "ローカルパッケージを安全に確認できません。",
            "remote_invalid": "棚の library が破損しています。棚の内容は変更されていません。",
            "invalid_song_id": "song ID は小文字16進12桁で指定してください。",
            "delete_unknown": "棚からの削除を確認できませんでした。同じ song ID で再実行できます。",
        }
        return messages.get(exc.kind, "Pocket の削除を開始できません。")
    if isinstance(exc, LocalPreflightError):
        return "ローカルパッケージを安全に確認できません。"
    return "Pocket の削除に失敗しました。後で再実行してください。"


def sync_one(
    out_dir: Path,
    selector: str,
    *,
    resolution: Literal["safe_name", "song_id"],
    expected_digest: str | None = None,
    include_original: bool = True,
    lock: SyncLock | None = None,
    client: PocketHTTPClient | None = None,
) -> SyncResult:
    config = read_config(out_dir)
    if config is None:
        raise PocketServiceError("Pocket の接続設定がありません。", kind="not_connected")
    owned_lock = lock is None
    active_lock = lock or SyncLock(out_dir).acquire()
    try:
        package = resolve_package(
            out_dir,
            selector,
            resolution=resolution,
            expected_digest=expected_digest,
            include_original=include_original,
        )
        remote = client or PocketHTTPClient(config.base_url, config.token)
        return synchronize(package, remote, include_original=include_original)
    finally:
        if owned_lock:
            active_lock.release()


def sync_all(
    out_dir: Path,
    *,
    include_original: bool = True,
    lock: SyncLock | None = None,
    client: PocketHTTPClient | None = None,
    progress: Callable[[BatchResult, str | None], None] | None = None,
) -> BatchResult:
    config = read_config(out_dir)
    if config is None:
        raise PocketServiceError("Pocket の接続設定がありません。", kind="not_connected")
    owned_lock = lock is None
    active_lock = lock or SyncLock(out_dir).acquire()
    try:
        found = inventory(out_dir, include_original=include_original)
        result = BatchResult(total=len(found.packages), legacy=list(found.legacy))
        result.items = [BatchItem(package.directory.name, "pending") for package in found.packages]
        remote = client or PocketHTTPClient(config.base_url, config.token)
        for index, package in enumerate(found.packages):
            if progress:
                progress(result, package.directory.name)
            try:
                synced = synchronize(package, remote, include_original=include_original)
            except Exception as exc:
                result.items[index] = BatchItem(package.directory.name, "error", error=safe_error(exc))
                if progress:
                    progress(result, None)
                return result
            result.items[index] = BatchItem(package.directory.name, "done", result=synced)
            if progress:
                progress(result, None)
        return result
    finally:
        if owned_lock:
            active_lock.release()
