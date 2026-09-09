"""Shared validation and artifact inspection for local Bunri packages."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from bunri.package_metadata import PackageMetadata, SourceIdentity, read_package_metadata
from bunri.safepath import is_real_file_in


PackageState = Literal["ready", "legacy", "invalid"]


@dataclass(frozen=True)
class PackageIdentityInspection:
    name: str
    directory: Path
    state: PackageState
    issues: tuple[str, ...]
    metadata: PackageMetadata | None = None
    identity: SourceIdentity | None = None
    targets: tuple[tuple[str, tuple[object, ...]], ...] = ()


@dataclass(frozen=True)
class ArtifactInspection:
    path: Path
    present: bool
    size: int | None = None
    sha256: str | None = None
    issue: str | None = None


@dataclass(frozen=True)
class TargetArtifactInspection:
    target: str
    formats: tuple[str, ...]
    target_files: tuple[tuple[str, ArtifactInspection], ...]
    backing_files: tuple[tuple[str, ArtifactInspection], ...]
    player: ArtifactInspection

    @property
    def complete_formats(self) -> tuple[str, ...]:
        target_files = dict(self.target_files)
        backing_files = dict(self.backing_files)
        return tuple(
            audio_format
            for audio_format in self.formats
            if target_files[audio_format].present and backing_files[audio_format].present
        )


@dataclass(frozen=True)
class PackageArtifactInspection:
    directory: Path
    original: ArtifactInspection
    targets: tuple[TargetArtifactInspection, ...]
    issues: tuple[str, ...]


def package_name_key(name: str) -> str:
    return unicodedata.normalize("NFC", name)


def package_names_equal(left: str, right: str) -> bool:
    return package_name_key(left) == package_name_key(right)


def safe_package_name_issue(name: str) -> str | None:
    if (
        not name
        or Path(name).is_absolute()
        or name in (".", "..")
        or "/" in name
        or "\\" in name
    ):
        return f"安全でないパッケージ名です: {name!r}"
    if name.startswith(".") or name.casefold() in {"web", ".cache", ".pocket"}:
        return f"内部用のパッケージ名は指定できません: {name}"
    return None


def package_candidates(out_dir: Path) -> list[str]:
    try:
        children = list(out_dir.iterdir())
    except OSError:
        return []
    return sorted(
        child.name
        for child in children
        if not child.name.startswith(".")
        and child.name.casefold() != "web"
        and not child.is_symlink()
        and child.is_dir()
    )[:20]


def all_package_names(out_dir: Path) -> list[str]:
    try:
        children = list(out_dir.iterdir())
    except OSError:
        return []
    blocked = {"web", ".cache", ".pocket"}
    return sorted(
        (
            child.name
            for child in children
            if not child.name.startswith(".")
            and child.name.casefold() not in blocked
            and not child.is_symlink()
            and child.is_dir()
        ),
        key=lambda name: (name.casefold(), name),
    )


def read_package_metadata_for_directory(
    path: Path,
    directory_name: str,
    *,
    allow_unknown_targets: bool = False,
) -> PackageMetadata:
    if not is_real_file_in(path, path.parent.resolve()):
        raise ValueError(f"package metadata is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid package metadata: {path}") from exc
    declared_name = value.get("safe_name") if isinstance(value, dict) else None
    if not isinstance(declared_name, str) or not package_names_equal(
        declared_name, directory_name
    ):
        raise ValueError("package metadata safe_name does not match its directory")
    return read_package_metadata(
        path,
        declared_name,
        allow_unknown_targets=allow_unknown_targets,
    )


def _source_identity(value: object) -> tuple[SourceIdentity | None, list[str]]:
    if not isinstance(value, dict):
        return None, ["package metadata source must be an object"]
    digest, key = value.get("digest"), value.get("cache_key")
    issues: list[str] = []
    if value.get("algorithm") != "sha1":
        issues.append("package metadata source algorithm is invalid")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{40}", digest):
        issues.append("package metadata source digest is invalid")
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{12}", key):
        issues.append("package metadata source cache_key is invalid")
    elif isinstance(digest, str) and key != digest[:12]:
        issues.append("package metadata source identity is inconsistent")
    if issues:
        return None, issues
    assert isinstance(digest, str) and isinstance(key, str)
    return SourceIdentity("sha1", digest, key), []


def _metadata_issues(
    value: object, directory_name: str
) -> tuple[list[str], SourceIdentity | None, tuple[tuple[str, tuple[object, ...]], ...]]:
    if not isinstance(value, dict):
        return ["package metadata must be an object"], None, ()
    issues: list[str] = []
    version = value.get("schema_version")
    if isinstance(version, bool) or version != 1:
        issues.append("unsupported package metadata schema_version")
    if not isinstance(value.get("title"), str) or not value["title"]:
        issues.append("package metadata title must be non-empty")
    declared_name = value.get("safe_name")
    if not isinstance(declared_name, str) or not package_names_equal(
        declared_name, directory_name
    ):
        issues.append("package metadata safe_name does not match its directory")
    identity, source_issues = _source_identity(value.get("source"))
    issues.extend(source_issues)
    raw_targets = value.get("targets")
    enumerable: list[tuple[str, tuple[object, ...]]] = []
    if not isinstance(raw_targets, list):
        issues.append("package metadata targets must be an array")
    else:
        seen: set[str] = set()
        for index, item in enumerate(raw_targets):
            if not isinstance(item, dict):
                issues.append(f"package metadata target {index} must be an object")
                continue
            target, formats = item.get("target"), item.get("formats")
            if isinstance(target, str):
                enumerable.append(
                    (target, tuple(formats) if isinstance(formats, list) else ())
                )
            if not isinstance(target, str) or target == "original" or target in seen:
                issues.append(f"invalid or duplicate package target: {target!r}")
            else:
                seen.add(target)
            if (
                not isinstance(formats, list)
                or not formats
                or any(
                    not isinstance(audio_format, str)
                    or audio_format not in {"mp3", "wav"}
                    for audio_format in formats
                )
                or len(set(formats)) != len(formats)
            ):
                issues.append(f"invalid formats for package target {target}")
    return issues, identity, tuple(enumerable)


def inspect_package_identity(out_dir: Path, name: str) -> PackageIdentityInspection:
    directory = out_dir / name
    name_issue = safe_package_name_issue(name)
    if name_issue is not None:
        return PackageIdentityInspection(name, directory, "invalid", (name_issue,))
    try:
        valid_directory = (
            not directory.is_symlink()
            and directory.is_dir()
            and directory.resolve().parent == out_dir.resolve()
        )
    except OSError:
        valid_directory = False
    if not valid_directory:
        return PackageIdentityInspection(
            name, directory, "invalid", (f"パッケージが見つかりません: {directory}",)
        )
    sidecar = directory / ".bunri-package.json"
    if not sidecar.exists() and not sidecar.is_symlink():
        return PackageIdentityInspection(name, directory, "legacy", ())
    if not is_real_file_in(sidecar, directory.resolve()):
        return PackageIdentityInspection(
            name,
            directory,
            "invalid",
            (f"package metadata is not a regular file: {sidecar}",),
        )
    try:
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return PackageIdentityInspection(
            name, directory, "invalid", (f"invalid package metadata: {sidecar}",)
        )
    issues, identity, targets = _metadata_issues(raw, name)
    if issues:
        return PackageIdentityInspection(
            name, directory, "invalid", tuple(issues), identity=identity, targets=targets
        )
    assert isinstance(raw, dict) and isinstance(raw.get("safe_name"), str)
    metadata = read_package_metadata(
        sidecar, raw["safe_name"], allow_unknown_targets=True
    )
    return PackageIdentityInspection(
        name,
        directory,
        "ready",
        (),
        metadata=metadata,
        identity=metadata.source,
        targets=tuple((item.target, tuple(item.formats)) for item in metadata.targets),
    )


def _inspect_artifact(path: Path, directory: Path, *, hash_file: bool) -> ArtifactInspection:
    if not is_real_file_in(path, directory.resolve()):
        return ArtifactInspection(path, False, issue=f"{path}: 通常ファイルではありません")
    try:
        digest = hashlib.sha256() if hash_file else None
        size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                size += len(chunk)
                if digest is not None:
                    digest.update(chunk)
    except OSError as exc:
        return ArtifactInspection(path, False, issue=f"{path}: 読み取れません ({exc})")
    if size <= 0:
        return ArtifactInspection(path, False, size=0, issue=f"{path}: 空です")
    return ArtifactInspection(
        path, True, size=size, sha256=digest.hexdigest() if digest is not None else None
    )


def inspect_package_artifacts(
    identity: PackageIdentityInspection, *, hash_files: bool = False
) -> PackageArtifactInspection:
    directory = identity.directory
    original = _inspect_artifact(
        directory / f"{identity.name}.original.mp3", directory, hash_file=hash_files
    )
    issues: list[str] = []
    if original.issue:
        issues.append(f"original: {original.issue}")
    targets: list[TargetArtifactInspection] = []
    for target, raw_formats in identity.targets:
        formats = tuple(
            audio_format
            for audio_format in raw_formats
            if isinstance(audio_format, str) and audio_format in {"mp3", "wav"}
        )
        target_files = tuple(
            (
                audio_format,
                _inspect_artifact(
                    directory / f"{identity.name}.{target}.{audio_format}",
                    directory,
                    hash_file=hash_files,
                ),
            )
            for audio_format in formats
        )
        backing_files = tuple(
            (
                audio_format,
                _inspect_artifact(
                    directory / f"{identity.name}.{target}.backing.{audio_format}",
                    directory,
                    hash_file=hash_files,
                ),
            )
            for audio_format in formats
        )
        player = _inspect_artifact(
            directory / f"{identity.name}.{target}.player.html",
            directory,
            hash_file=hash_files,
        )
        for role, files in (("target", target_files), ("backing", backing_files)):
            for _, artifact in files:
                if artifact.issue:
                    issues.append(f"{target} {role}: {artifact.issue}")
        if player.issue:
            issues.append(f"{target} player: {player.issue}")
        targets.append(
            TargetArtifactInspection(
                target, formats, target_files, backing_files, player
            )
        )
    return PackageArtifactInspection(directory, original, tuple(targets), tuple(issues))
