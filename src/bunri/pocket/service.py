"""CLI/Web shared orchestration for Bunri Pocket synchronization."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from bunri.pocket.config import read_config
from bunri.pocket.http import PocketHTTPClient, PocketHTTPError
from bunri.pocket.local import LocalPackage, LocalPreflightError, all_package_names, preflight
from bunri.pocket.lock import SyncLock, SyncLockBusy
from bunri.pocket.protocol import ProtocolError, merge_library, merge_manifest
from bunri.pocket.sync import SyncError, SyncResult, _library, _manifest, synchronize


class PocketServiceError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "invalid") -> None:
        super().__init__(message)
        self.kind = kind


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


def _identity_issues(packages: Iterable[LocalPackage]) -> list[str]:
    digest_names: dict[str, set[str]] = {}
    id_digests: dict[str, set[str]] = {}
    for package in packages:
        source = package.metadata.source
        digest_names.setdefault(source.digest, set()).add(package.directory.name)
        id_digests.setdefault(source.cache_key, set()).add(source.digest)
    issues = []
    for digest, names in sorted(digest_names.items()):
        if len(names) > 1:
            issues.append(f"同じ入力音源が複数のパッケージ名にあります: {', '.join(sorted(names))}")
    for song_id, digests in sorted(id_digests.items()):
        if len(digests) > 1:
            issues.append(f"同じ12桁の song ID に異なる入力音源があります: {song_id}")
    return issues


def inventory(out_dir: Path, *, include_original: bool = True) -> PackageInventory:
    packages: list[LocalPackage] = []
    legacy: list[str] = []
    problems: list[str] = []
    for safe_name in all_package_names(out_dir):
        try:
            packages.append(preflight(out_dir, safe_name, include_original=include_original))
        except LocalPreflightError as exc:
            if exc.kind == "legacy":
                legacy.append(safe_name)
            else:
                problems.extend(f"{safe_name}: {issue}" for issue in exc.issues)
    problems.extend(_identity_issues(packages))
    if problems:
        raise PocketServiceError(
            "ローカルパッケージを安全に同期できません。アップロードは開始していません。\n- "
            + "\n- ".join(problems),
            kind="local",
        )
    return PackageInventory(tuple(packages), tuple(legacy))


def resolve_package(
    out_dir: Path,
    song_id: str,
    *,
    expected_digest: str | None = None,
    include_original: bool = True,
) -> LocalPackage:
    names = all_package_names(out_dir)
    if expected_digest is None and song_id in names:
        try:
            selected = preflight(out_dir, song_id, include_original=include_original)
        except LocalPreflightError as exc:
            if exc.kind == "legacy":
                raise PocketServiceError("このパッケージは再生成が必要です。", kind="legacy") from exc
            raise PocketServiceError(
                "パッケージを安全に同期できません。アップロードは開始していません。\n- "
                + "\n- ".join(exc.issues),
                kind="local",
            ) from exc
        peers: list[LocalPackage] = []
        for name in names:
            try:
                candidate = preflight(out_dir, name, include_original=include_original)
            except LocalPreflightError:
                continue
            source = candidate.metadata.source
            if (
                source.digest == selected.metadata.source.digest
                or source.cache_key == selected.metadata.source.cache_key
            ):
                peers.append(candidate)
        if _identity_issues(peers):
            raise PocketServiceError("曲の identity が競合しているためアップロードできません。", kind="conflict")
        return selected
    matches: list[LocalPackage] = []
    legacy_match = False
    for safe_name in names:
        try:
            package = preflight(out_dir, safe_name, include_original=include_original)
        except LocalPreflightError as exc:
            if exc.kind == "legacy" and safe_name == song_id:
                legacy_match = True
            continue
        source = package.metadata.source
        if source.cache_key == song_id or (expected_digest is not None and source.digest == expected_digest):
            matches.append(package)
    if legacy_match:
        raise PocketServiceError("このパッケージは再生成が必要です。", kind="legacy")
    if not matches:
        raise PocketServiceError("同期する曲が見つかりません。", kind="not_found")
    problems = _identity_issues(matches)
    if len(matches) != 1 or problems:
        raise PocketServiceError("曲の identity が競合しているためアップロードできません。", kind="conflict")
    package = matches[0]
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
            return RemoteStatus("different" if library_has_song else "not_synced", True)
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
    prepared: list[LocalPackage] = []
    statuses: list[PackageStatus] = []
    for safe_name in all_package_names(out_dir):
        try:
            prepared.append(preflight(out_dir, safe_name, include_original=True))
        except LocalPreflightError as exc:
            if exc.kind == "legacy":
                statuses.append(PackageStatus(
                    safe_name,
                    safe_name,
                    None,
                    None,
                    RemoteStatus(
                        "legacy",
                        False,
                        "元の入力音源から再生成してください。キャッシュが残っていれば分離処理は省略されます。",
                    ),
                ))
            else:
                statuses.append(PackageStatus(
                    safe_name,
                    exc.metadata.title if exc.metadata is not None else safe_name,
                    exc.metadata.source.cache_key if exc.metadata is not None else None,
                    exc.metadata.source.digest if exc.metadata is not None else None,
                    RemoteStatus("unknown", False, "ローカルパッケージを確認できません。"),
                ))
    conflict_ids: set[str] = set()
    conflict_digests: set[str] = set()
    digest_names: dict[str, set[str]] = {}
    id_digests: dict[str, set[str]] = {}
    for package in prepared:
        source = package.metadata.source
        digest_names.setdefault(source.digest, set()).add(package.directory.name)
        id_digests.setdefault(source.cache_key, set()).add(source.digest)
    conflict_digests.update(digest for digest, names in digest_names.items() if len(names) > 1)
    conflict_ids.update(song_id for song_id, digests in id_digests.items() if len(digests) > 1)
    for package in prepared:
        source = package.metadata.source
        remote = (
            RemoteStatus("different", False, "曲の identity が競合しているためアップロードできません。", True)
            if source.digest in conflict_digests or source.cache_key in conflict_ids
            else inspect_remote(package, client)
        )
        statuses.append(PackageStatus(
            package.directory.name,
            package.metadata.title,
            source.cache_key,
            source.digest,
            remote,
        ))
    return tuple(sorted(statuses, key=lambda item: (item.safe_name.casefold(), item.safe_name)))


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


def sync_one(
    out_dir: Path,
    song_id: str,
    *,
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
            song_id,
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
