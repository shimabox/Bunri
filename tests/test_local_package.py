from __future__ import annotations

import json

from bunri.local_package import inspect_package_artifacts, inspect_package_identity
from bunri.package_metadata import (
    PackageMetadata,
    SourceIdentity,
    TargetMetadata,
    write_package_metadata,
)


def _package(tmp_path, *, formats=("mp3", "wav")):
    out = tmp_path / "out"
    package = out / "Song"
    package.mkdir(parents=True)
    write_package_metadata(
        package / ".bunri-package.json",
        PackageMetadata(
            "Song",
            "Song",
            SourceIdentity("sha1", "a" * 40, "a" * 12),
            (TargetMetadata("guitar", formats),),
        ),
    )
    return out, package


def test_identity_distinguishes_ready_legacy_and_invalid(tmp_path):
    out, package = _package(tmp_path)
    assert inspect_package_identity(out, "Song").state == "ready"

    legacy = out / "Old"
    legacy.mkdir()
    assert inspect_package_identity(out, "Old").state == "legacy"

    value = json.loads((package / ".bunri-package.json").read_text())
    value["safe_name"] = "Copied"
    (package / ".bunri-package.json").write_text(json.dumps(value))
    invalid = inspect_package_identity(out, "Song")
    assert invalid.state == "invalid"
    assert invalid.identity == SourceIdentity("sha1", "a" * 40, "a" * 12)
    assert any("safe_name" in issue for issue in invalid.issues)


def test_artifacts_report_complete_formats_and_missing_files(tmp_path):
    out, package = _package(tmp_path)
    for suffix in ("guitar.wav", "guitar.backing.wav", "guitar.player.html"):
        (package / f"Song.{suffix}").write_bytes(b"audio")
    (package / "Song.guitar.mp3").write_bytes(b"")

    result = inspect_package_artifacts(inspect_package_identity(out, "Song"))

    target = result.targets[0]
    assert target.complete_formats == ("wav",)
    assert target.player.present is True
    assert dict(target.target_files)["mp3"].issue.endswith("空です")
    assert dict(target.backing_files)["mp3"].present is False
