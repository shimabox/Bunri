"""Safe local package discovery and complete preflight before network I/O."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from bunri.package_metadata import PackageMetadata, read_package_metadata
from bunri.pocket.protocol import AssetInfo
from bunri.registry import REGISTRY
from bunri.safepath import is_real_file_in


class LocalPreflightError(ValueError):
    def __init__(self, issues: list[str], *, kind: str = "general", metadata: PackageMetadata | None = None) -> None:
        super().__init__("; ".join(issues))
        self.issues, self.kind, self.metadata = issues, kind, metadata


@dataclass(frozen=True)
class LocalAsset:
    descriptor: AssetInfo
    path: Path


@dataclass(frozen=True)
class LocalPackage:
    directory: Path
    metadata: PackageMetadata
    assets: tuple[LocalAsset, ...]


def validate_safe_name(name: str) -> None:
    if not name or Path(name).is_absolute() or name in (".", "..") or "/" in name or "\\" in name:
        raise LocalPreflightError([f"安全でないパッケージ名です: {name!r}"])
    if name.startswith(".") or name.casefold() in {"web", ".cache", ".pocket"}:
        raise LocalPreflightError([f"内部用のパッケージ名は指定できません: {name}"])


def package_candidates(out_dir: Path) -> list[str]:
    try: children = list(out_dir.iterdir())
    except OSError: return []
    return sorted(x.name for x in children if not x.name.startswith(".") and x.name.casefold() != "web" and not x.is_symlink() and x.is_dir())[:20]


def _hash(path: Path) -> tuple[int, str]:
    size = 0; digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            size += len(chunk); digest.update(chunk)
    return size, digest.hexdigest()


def _sidecar_issues(value: object, safe_name: str) -> list[str]:
    """Collect all metadata violations while preserving enumerable targets."""
    if not isinstance(value, dict):
        return ["package metadata must be an object"]
    issues: list[str] = []
    version = value.get("schema_version")
    if isinstance(version, bool) or version != 1:
        issues.append("unsupported package metadata schema_version")
    if not isinstance(value.get("title"), str) or not value["title"]:
        issues.append("package metadata title must be non-empty")
    if not isinstance(value.get("safe_name"), str) or value["safe_name"] != safe_name:
        issues.append("package metadata safe_name does not match its directory")
    source = value.get("source")
    if not isinstance(source, dict):
        issues.append("package metadata source must be an object")
    else:
        digest, key = source.get("digest"), source.get("cache_key")
        if source.get("algorithm") != "sha1":
            issues.append("package metadata source algorithm is invalid")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{40}", digest):
            issues.append("package metadata source digest is invalid")
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{12}", key):
            issues.append("package metadata source cache_key is invalid")
        elif isinstance(digest, str) and key != digest[:12]:
            issues.append("package metadata source identity is inconsistent")
    targets = value.get("targets")
    if not isinstance(targets, list):
        issues.append("package metadata targets must be an array")
    else:
        seen: set[str] = set()
        for index, item in enumerate(targets):
            if not isinstance(item, dict):
                issues.append(f"package metadata target {index} must be an object")
                continue
            target, formats = item.get("target"), item.get("formats")
            if not isinstance(target, str) or target == "original" or target in seen:
                issues.append(f"invalid or duplicate package target: {target!r}")
            else:
                seen.add(target)
            if (
                not isinstance(formats, list)
                or not formats
                or any(not isinstance(item, str) or item not in {"mp3", "wav"} for item in formats)
                or len(set(formats)) != len(formats)
            ):
                issues.append(f"invalid formats for package target {target}")
    return issues


def preflight(out_dir: Path, safe_name: str, *, include_original: bool = True) -> LocalPackage:
    validate_safe_name(safe_name)
    package_dir = out_dir / safe_name
    if package_dir.is_symlink() or not package_dir.is_dir() or package_dir.resolve().parent != out_dir.resolve():
        choices = package_candidates(out_dir)
        suffix = f"; 候補: {', '.join(choices)}" if choices else ""
        raise LocalPreflightError([f"パッケージが見つかりません: {package_dir}{suffix}"])
    sidecar = package_dir / ".bunri-package.json"
    if not sidecar.exists() and not sidecar.is_symlink():
        raise LocalPreflightError([f"{sidecar}: 見つかりません"], kind="legacy")
    if not is_real_file_in(sidecar, package_dir.resolve()):
        raise LocalPreflightError([f"package metadata is not a regular file: {sidecar}"])
    try:
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LocalPreflightError([f"invalid package metadata: {sidecar}"]) from exc
    issues = _sidecar_issues(raw, safe_name)
    metadata: PackageMetadata | None = None
    if not issues:
        metadata = read_package_metadata(sidecar, safe_name, allow_unknown_targets=True)
        target_values = [(target.target, target.formats) for target in metadata.targets]
    else:
        raw_targets = raw.get("targets") if isinstance(raw, dict) else None
        if not isinstance(raw_targets, list):
            raise LocalPreflightError(issues)
        target_values = []
        for item in raw_targets:
            if not isinstance(item, dict) or not isinstance(item.get("target"), str):
                continue
            formats = item.get("formats")
            target_values.append((item["target"], tuple(formats) if isinstance(formats, list) else ()))
    requested: list[tuple[str, str | None, str | None]] = []
    if include_original: requested.append((f"{safe_name}.original.mp3", None, None))
    for target, formats in target_values:
        if target not in REGISTRY:
            issues.append(f"{target}: 未知の target です")
        if "mp3" not in formats:
            issues.append(f"{target}: .bunri-package.json の formats に mp3 がありません")
        requested.extend(((f"{safe_name}.{target}.mp3", target, "target"), (f"{safe_name}.{target}.backing.mp3", target, "backing")))
    assets: list[LocalAsset] = []
    for filename, target, role in requested:
        path = package_dir / filename
        if not is_real_file_in(path, package_dir.resolve()):
            issues.append(f"{target or 'original'}: {path}: 通常ファイルではありません"); continue
        try: size, checksum = _hash(path)
        except OSError as exc: issues.append(f"{target or 'original'}: {path}: 読み取れません ({exc})"); continue
        if size <= 0: issues.append(f"{target or 'original'}: {path}: 空です"); continue
        remote = "original.mp3" if target is None else (f"{target}.mp3" if role == "target" else f"{target}.backing.mp3")
        assets.append(LocalAsset(AssetInfo(remote, size, checksum, target, role), path))
    if issues:
        no_mp3 = any("formats に mp3" in issue for issue in issues)
        raise LocalPreflightError(issues, kind="no_mp3" if no_mp3 else "general", metadata=metadata)
    assert metadata is not None
    return LocalPackage(package_dir, metadata, tuple(assets))
