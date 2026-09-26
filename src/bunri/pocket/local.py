"""Safe local package discovery and complete preflight before network I/O."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from bunri.local_package import (
    all_package_names,
    inspect_artifact,
    inspect_package_identity,
    package_candidates,
    package_name_key,
    package_names_equal,
    read_package_metadata_for_directory,
    safe_package_name_issue,
)
from bunri.package_metadata import PackageMetadata, SourceIdentity
from bunri.pocket.protocol import STEM_SUFFIXES, AssetInfo
from bunri.registry import REGISTRY


class LocalPreflightError(ValueError):
    def __init__(
        self,
        issues: list[str],
        *,
        kind: str = "general",
        metadata: PackageMetadata | None = None,
        identity: SourceIdentity | None = None,
    ) -> None:
        super().__init__("; ".join(issues))
        self.issues, self.kind, self.metadata = issues, kind, metadata
        self.identity = identity or (metadata.source if metadata is not None else None)


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
    issue = safe_package_name_issue(name)
    if issue is not None:
        raise LocalPreflightError([issue])


def preflight(
    out_dir: Path,
    safe_name: str,
    *,
    include_original: bool = True,
    expected_digest: str | None = None,
) -> LocalPackage:
    identity_result = inspect_package_identity(out_dir, safe_name)
    if identity_result.state == "invalid" and safe_package_name_issue(safe_name):
        raise LocalPreflightError(list(identity_result.issues))
    package_dir = identity_result.directory
    if identity_result.state == "invalid" and identity_result.issues and identity_result.issues[0].startswith("パッケージが見つかりません"):
        choices = package_candidates(out_dir)
        suffix = f"; 候補: {', '.join(choices)}" if choices else ""
        raise LocalPreflightError([f"パッケージが見つかりません: {package_dir}{suffix}"])
    if identity_result.state == "legacy":
        sidecar = package_dir / ".bunri-package.json"
        raise LocalPreflightError([f"{sidecar}: 見つかりません"], kind="legacy")
    issues = list(identity_result.issues)
    identity = identity_result.identity
    metadata = identity_result.metadata
    target_values = identity_result.targets
    if identity_result.state == "invalid" and identity_result.targets_is_array is None:
        raise LocalPreflightError(issues, identity=identity)
    if "package metadata targets must be an array" in identity_result.issues:
        raise LocalPreflightError(issues, identity=identity)
    pan_splits = {item.target: item.pan_split for item in metadata.targets} if metadata is not None else {}
    requested: list[tuple[str, str | None, str | None]] = []
    if include_original: requested.append((f"{safe_name}.original.mp3", None, None))
    for target, formats in target_values:
        if target not in REGISTRY:
            issues.append(f"{target}: 未知の target です")
        if "mp3" not in formats:
            issues.append(f"{target}: .bunri-package.json の formats に mp3 がありません")
        requested.extend(((f"{safe_name}.{target}.mp3", target, "target"), (f"{safe_name}.{target}.backing.mp3", target, "backing")))
        # L/R files are collected whenever the sidecar records a split; sync
        # decides per Pocket whether they are sent.
        if pan_splits.get(target) == "left_right":
            requested.extend(((f"{safe_name}.{target}.left.mp3", target, "left"), (f"{safe_name}.{target}.right.mp3", target, "right")))
    assets: list[LocalAsset] = []
    missing_lr = False
    for filename, target, role in requested:
        path = package_dir / filename
        # Pocket transfers only these requested MP3 assets. Keep their
        # checksum and size validation in the shared artifact layer without
        # hashing WAV exports, players, or an excluded original track.
        artifact = inspect_artifact(path, package_dir, hash_file=True)
        if not artifact.present:
            assert artifact.issue is not None
            issues.append(f"{target or 'original'}: {artifact.issue}")
            missing_lr = missing_lr or role in ("left", "right")
            continue
        size, checksum = artifact.size, artifact.sha256
        assert size is not None and checksum is not None
        remote = "original.mp3" if target is None else f"{target}{STEM_SUFFIXES[role]}.mp3"
        assets.append(LocalAsset(AssetInfo(remote, size, checksum, target, role), path))
    if missing_lr:
        # The recorded split makes `bunri lr-split` skip the package, so the
        # rebuild needs --force.
        command = f"bunri lr-split {shlex.quote(safe_name)} --force -o {shlex.quote(str(out_dir))}"
        issues.append(f"身元ファイルには L のみ / R のみありと記録されていますが、L/R の mp3 がありません。`{command}` で作り直してください")
    if issues:
        no_mp3 = any("formats に mp3" in issue for issue in issues)
        raise LocalPreflightError(
            issues,
            kind="no_mp3" if no_mp3 else "general",
            metadata=metadata,
            identity=identity,
        )
    assert metadata is not None
    if expected_digest is not None and metadata.source.digest != expected_digest:
        raise LocalPreflightError(
            ["package metadata source digest no longer matches the selected song"],
            kind="identity",
            metadata=metadata,
        )
    return LocalPackage(package_dir, metadata, tuple(assets))
