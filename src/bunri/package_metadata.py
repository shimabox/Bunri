"""Validated, atomic metadata for generated practice packages."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from bunri.registry import REGISTRY
from bunri.safepath import is_real_file_in, replace_into

_SHA1 = re.compile(r"[0-9a-f]{40}\Z")
_KEY = re.compile(r"[0-9a-f]{12}\Z")
_FORMAT_ORDER = {"mp3": 0, "wav": 1}


@dataclass(frozen=True)
class SourceIdentity:
    algorithm: str
    digest: str
    cache_key: str


@dataclass(frozen=True)
class TargetMetadata:
    target: str
    formats: tuple[str, ...]


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
        seen.add(target)
        targets.append(TargetMetadata(target, tuple(sorted(formats, key=_FORMAT_ORDER.get))))
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
        "targets": [
            {"target": item.target, "formats": list(item.formats)} for item in metadata.targets
        ],
    }
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def write_package_metadata(path: Path, metadata: PackageMetadata) -> None:
    replace_into(path, lambda tmp: tmp.write_text(_payload(metadata), encoding="utf-8"))


def begin_target(path: Path, *, title: str, safe_name: str, digest: str, cache_key: str, target: str) -> PackageMetadata:
    source = SourceIdentity("sha1", digest, cache_key)
    if path.exists() or path.is_symlink():
        current = read_package_metadata(path, safe_name)
        if current.source != source:
            raise ValueError("package directory belongs to a different input digest")
        targets = tuple(item for item in current.targets if item.target != target)
    else:
        targets = ()
    result = PackageMetadata(title, safe_name, source, targets)
    write_package_metadata(path, result)
    return result


def complete_target(path: Path, *, expected: PackageMetadata, target: str, formats: tuple[str, ...]) -> PackageMetadata:
    current = read_package_metadata(path, expected.safe_name)
    if current.title != expected.title or current.source != expected.source:
        raise ValueError("package metadata identity changed while generating")
    preserved = tuple(item for item in current.targets if item.target != target)
    result = PackageMetadata(
        current.title,
        current.safe_name,
        current.source,
        tuple(sorted((*preserved, TargetMetadata(target, formats)), key=lambda x: x.target)),
    )
    write_package_metadata(path, result)
    return result
