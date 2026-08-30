from __future__ import annotations

from pathlib import Path

import pytest

from bunri.package_metadata import PackageMetadata, SourceIdentity, TargetMetadata, write_package_metadata
from bunri.pocket.local import LocalPreflightError, preflight, validate_safe_name


@pytest.mark.parametrize("name", ["", ".", "..", "/tmp/x", "a/b", "a\\b", ".hidden", ".pocket", ".cache", "web"])
def test_rejects_unsafe_names(name):
    with pytest.raises(LocalPreflightError): validate_safe_name(name)


def _package(tmp_path: Path, formats=("mp3", "wav")) -> tuple[Path, Path]:
    out = tmp_path / "out"; directory = out / "Song"; directory.mkdir(parents=True)
    metadata = PackageMetadata("A title", "Song", SourceIdentity("sha1", "a" * 40, "a" * 12), (TargetMetadata("guitar", formats),))
    write_package_metadata(directory / ".bunri-package.json", metadata)
    return out, directory


def test_preflight_hashes_assets_in_contract_order(tmp_path):
    out, directory = _package(tmp_path)
    for name in ("Song.original.mp3", "Song.guitar.mp3", "Song.guitar.backing.mp3"): (directory / name).write_bytes(name.encode())
    package = preflight(out, "Song")
    assert [x.descriptor.remote_name for x in package.assets] == ["original.mp3", "guitar.mp3", "guitar.backing.mp3"]
    assert all(x.descriptor.bytes > 0 and len(x.descriptor.sha256) == 64 for x in package.assets)


def test_no_original_excludes_existing_file(tmp_path):
    out, directory = _package(tmp_path)
    for name in ("Song.original.mp3", "Song.guitar.mp3", "Song.guitar.backing.mp3"): (directory / name).write_bytes(b"x")
    assert [x.descriptor.remote_name for x in preflight(out, "Song", include_original=False).assets] == ["guitar.mp3", "guitar.backing.mp3"]


def test_collects_missing_mp3_and_invalid_assets(tmp_path):
    out, directory = _package(tmp_path, formats=("wav",))
    (directory / "Song.guitar.mp3").write_bytes(b"")
    (directory / "Song.guitar.backing.mp3").symlink_to(tmp_path / "missing")
    with pytest.raises(LocalPreflightError) as exc: preflight(out, "Song")
    assert exc.value.kind == "no_mp3" and len(exc.value.issues) >= 4


def test_unknown_target_is_reported_with_its_missing_assets(tmp_path):
    import json

    out, directory = _package(tmp_path)
    sidecar = directory / ".bunri-package.json"
    value = json.loads(sidecar.read_text())
    value["targets"] = [{"target": "zither", "formats": ["mp3"]}]
    sidecar.write_text(json.dumps(value))
    with pytest.raises(LocalPreflightError) as exc:
        preflight(out, "Song", include_original=False)
    assert any("未知の target" in issue for issue in exc.value.issues)
    assert len(exc.value.issues) == 3


def test_legacy_package_and_directory_symlink_are_refused(tmp_path):
    out = tmp_path / "out"; package = out / "Old"; package.mkdir(parents=True)
    with pytest.raises(LocalPreflightError) as exc: preflight(out, "Old")
    assert exc.value.kind == "legacy"
    package.rmdir(); package.symlink_to(tmp_path)
    with pytest.raises(LocalPreflightError): preflight(out, "Old")
