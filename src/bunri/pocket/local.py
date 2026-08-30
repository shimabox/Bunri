"""Safe local package discovery and complete preflight before network I/O."""

from __future__ import annotations

import hashlib
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
    try:
        metadata = read_package_metadata(sidecar, safe_name, allow_unknown_targets=True)
    except ValueError as exc:
        raise LocalPreflightError([str(exc)]) from exc
    issues: list[str] = []; requested: list[tuple[str, str | None, str | None]] = []
    if include_original: requested.append((f"{safe_name}.original.mp3", None, None))
    for target in metadata.targets:
        if target.target not in REGISTRY:
            issues.append(f"{target.target}: 未知の target です")
        if "mp3" not in target.formats:
            issues.append(f"{target.target}: .bunri-package.json の formats に mp3 がありません")
        requested.extend(((f"{safe_name}.{target.target}.mp3", target.target, "target"), (f"{safe_name}.{target.target}.backing.mp3", target.target, "backing")))
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
    return LocalPackage(package_dir, metadata, tuple(assets))
