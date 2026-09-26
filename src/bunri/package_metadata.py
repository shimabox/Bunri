"""Validated, atomic metadata for generated practice packages."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from bunri.lock import ProcessLock
from bunri.registry import REGISTRY
from bunri.safepath import is_real_file_in, replace_into, verified_mkdir

_SHA1 = re.compile(r"[0-9a-f]{40}\Z")
_KEY = re.compile(r"[0-9a-f]{12}\Z")
_FORMAT_ORDER = {"mp3": 0, "wav": 1}
# "left_right": the package has <safe>.<target>.left/right files.
# "single": the stem has one stereo position cluster, so none were made.
PAN_SPLIT_VALUES = ("left_right", "single")

# Every read-modify-write of a sidecar holds out/.cache/metadata.lock. A CLI
# run and a web separation job are separate processes that can update the
# same sidecar; without the lock, one reading before the other writes and
# then writing back what it read drops the other's change (a whole target, or
# a pan_split field). The critical section is a JSON read and write, so the
# wait is normally nil; the timeout only guards against a stuck holder.
_LOCK_TIMEOUT = 10.0
_LOCK_TIMEOUT_MESSAGE = (
    "身元ファイルの更新待ちがタイムアウトしました。別の Bunri が実行中です。"
    "完了後に再実行してください。"
)


class TargetNotFoundError(ValueError):
    """The sidecar has no item for the target being updated."""


@dataclass(frozen=True)
class SourceIdentity:
    algorithm: str
    digest: str
    cache_key: str


@dataclass(frozen=True)
class TargetMetadata:
    target: str
    formats: tuple[str, ...]
    # None when the split was never attempted (a target without
    # TargetSpec.pan_split, or a package made before the split existed).
    #
    # Known limitation on downgrade: older Bunri versions validate this field
    # away as an unknown key and only write target/formats back, so an older
    # version adding another target to the package drops it (the L/R files
    # stay, but the web UI stops listing them). `bunri lr-split` restores it.
    pan_split: str | None = None


@dataclass(frozen=True)
class PackageMetadata:
    title: str
    safe_name: str
    source: SourceIdentity
    targets: tuple[TargetMetadata, ...]


def _validate(
    value: object, directory_name: str, *, allow_unknown_targets: bool = False
) -> PackageMetadata:
    if not isinstance(value, dict):
        raise ValueError("package metadata must be an object")
    version = value.get("schema_version")
    if isinstance(version, bool) or version != 1:
        raise ValueError("unsupported package metadata schema_version")
    title, safe = value.get("title"), value.get("safe_name")
    if not isinstance(title, str) or not title:
        raise ValueError("package metadata title must be non-empty")
    if not isinstance(safe, str) or safe != directory_name:
        raise ValueError("package metadata safe_name does not match its directory")
    source = value.get("source")
    if not isinstance(source, dict):
        raise ValueError("package metadata source must be an object")
    algorithm, digest, key = source.get("algorithm"), source.get("digest"), source.get("cache_key")
    if algorithm != "sha1" or not isinstance(digest, str) or not _SHA1.fullmatch(digest):
        raise ValueError("package metadata source digest is invalid")
    if not isinstance(key, str) or not _KEY.fullmatch(key) or key != digest[:12]:
        raise ValueError("package metadata source identity is inconsistent")
    raw_targets = value.get("targets")
    if not isinstance(raw_targets, list):
        raise ValueError("package metadata targets must be an array")
    seen: set[str] = set()
    targets: list[TargetMetadata] = []
    for item in raw_targets:
        if not isinstance(item, dict):
            raise ValueError("package metadata target must be an object")
        target, formats = item.get("target"), item.get("formats")
        if (
            not isinstance(target, str)
            or target == "original"
            or target in seen
            or (target not in REGISTRY and not allow_unknown_targets)
        ):
            raise ValueError(f"invalid or duplicate package target: {target!r}")
        if not isinstance(formats, list) or not formats or any(
            not isinstance(x, str) or x not in _FORMAT_ORDER for x in formats
        ) or len(set(formats)) != len(formats):
            raise ValueError(f"invalid formats for package target {target}")
        pan_split = item.get("pan_split")
        if "pan_split" in item and (
            not isinstance(pan_split, str) or pan_split not in PAN_SPLIT_VALUES
        ):
            raise ValueError(f"invalid pan_split for package target {target}")
        seen.add(target)
        targets.append(
            TargetMetadata(
                target, tuple(sorted(formats, key=_FORMAT_ORDER.get)), pan_split
            )
        )
    return PackageMetadata(
        title, safe, SourceIdentity(algorithm, digest, key), tuple(sorted(targets, key=lambda x: x.target))
    )


def read_package_metadata(
    path: Path,
    directory_name: str | None = None,
    *,
    allow_unknown_targets: bool = False,
) -> PackageMetadata:
    expected_dir = path.parent.resolve()
    if not is_real_file_in(path, expected_dir):
        raise ValueError(f"package metadata is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid package metadata: {path}") from exc
    return _validate(
        value,
        directory_name or path.parent.name,
        allow_unknown_targets=allow_unknown_targets,
    )


def _payload(metadata: PackageMetadata) -> str:
    value = {
        "schema_version": 1,
        "title": metadata.title,
        "safe_name": metadata.safe_name,
        "source": {
            "algorithm": metadata.source.algorithm,
            "digest": metadata.source.digest,
            "cache_key": metadata.source.cache_key,
        },
        "targets": [_target_payload(item) for item in metadata.targets],
    }
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _target_payload(item: TargetMetadata) -> dict[str, object]:
    value: dict[str, object] = {"target": item.target, "formats": list(item.formats)}
    if item.pan_split is not None:
        value["pan_split"] = item.pan_split
    return value


def write_package_metadata(path: Path, metadata: PackageMetadata) -> None:
    replace_into(path, lambda tmp: tmp.write_text(_payload(metadata), encoding="utf-8"))


def _check_pan_split(pan_split: str | None) -> None:
    if pan_split is not None and pan_split not in PAN_SPLIT_VALUES:
        raise ValueError(f"invalid pan_split: {pan_split!r}")


def _locked_update(
    path: Path,
    safe_name: str,
    mutate: Callable[[PackageMetadata | None], PackageMetadata],
    *,
    allow_unknown_targets: bool = False,
) -> PackageMetadata:
    """Read the sidecar (None if absent), apply `mutate`, write the result --
    all under out/.cache/metadata.lock, so a concurrent update by another
    process is re-read rather than overwritten. The sidecar always sits
    directly in out/<safe>/, which puts out_dir two levels up."""
    out_dir = path.parent.parent
    lock = ProcessLock(
        verified_mkdir(out_dir, ".cache") / "metadata.lock",
        _LOCK_TIMEOUT_MESSAGE,
        timeout=_LOCK_TIMEOUT,
    )
    with lock:
        current = (
            read_package_metadata(
                path, safe_name, allow_unknown_targets=allow_unknown_targets
            )
            if path.exists() or path.is_symlink()
            else None
        )
        result = mutate(current)
        write_package_metadata(path, result)
    return result


def begin_target(path: Path, *, title: str, safe_name: str, digest: str, cache_key: str, target: str) -> PackageMetadata:
    source = SourceIdentity("sha1", digest, cache_key)

    def mutate(current: PackageMetadata | None) -> PackageMetadata:
        if current is None:
            targets: tuple[TargetMetadata, ...] = ()
        else:
            if current.source != source:
                raise ValueError("package directory belongs to a different input digest")
            # Other targets' items are kept whole, pan_split included.
            targets = tuple(item for item in current.targets if item.target != target)
        return PackageMetadata(title, safe_name, source, targets)

    return _locked_update(path, safe_name, mutate)


def complete_target(
    path: Path,
    *,
    expected: PackageMetadata,
    target: str,
    formats: tuple[str, ...],
    pan_split: str | None = None,
) -> PackageMetadata:
    _check_pan_split(pan_split)

    def mutate(current: PackageMetadata | None) -> PackageMetadata:
        if current is None:
            raise ValueError(f"package metadata is not a regular file: {path}")
        if current.title != expected.title or current.source != expected.source:
            raise ValueError("package metadata identity changed while generating")
        preserved = tuple(item for item in current.targets if item.target != target)
        return PackageMetadata(
            current.title,
            current.safe_name,
            current.source,
            tuple(
                sorted(
                    (*preserved, TargetMetadata(target, formats, pan_split)),
                    key=lambda x: x.target,
                )
            ),
        )

    return _locked_update(path, expected.safe_name, mutate)


def set_target_pan_split(
    path: Path, *, safe_name: str, target: str, pan_split: str
) -> PackageMetadata:
    """Replace only `target`'s pan_split, leaving everything else as it is.

    Unlike begin_target, the item is never removed in between, so a reader
    never sees the target vanish. Raises TargetNotFoundError if the sidecar
    has no item for `target` (e.g. a regeneration of that target has started
    since the caller last looked)."""
    _check_pan_split(pan_split)
    if pan_split is None:
        raise ValueError("pan_split must be set")

    def mutate(current: PackageMetadata | None) -> PackageMetadata:
        if current is None:
            raise ValueError(f"package metadata is not a regular file: {path}")
        if not any(item.target == target for item in current.targets):
            raise TargetNotFoundError(f"package metadata has no {target} target")
        return replace(
            current,
            targets=tuple(
                replace(item, pan_split=pan_split) if item.target == target else item
                for item in current.targets
            ),
        )

    return _locked_update(path, safe_name, mutate, allow_unknown_targets=True)
