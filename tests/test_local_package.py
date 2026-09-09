from __future__ import annotations

import json

from bunri.local_package import (
    inspect_artifact,
    inspect_package_artifacts,
    inspect_package_identity,
)
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

    result = inspect_package_artifacts(
        inspect_package_identity(out, "Song"), hash_files=True
    )

    target = result.targets[0]
    assert target.complete_formats == ("wav",)
    assert target.player.present is True
    assert target.player.sha256 is not None
    assert dict(target.target_files)["mp3"].issue.endswith("空です")
    assert dict(target.backing_files)["mp3"].present is False


def test_artifact_without_hash_reads_only_a_small_prefix(tmp_path, monkeypatch):
    artifact = tmp_path / "artifact.mp3"
    artifact.write_bytes(b"a" * 1024)
    original_open = type(artifact).open
    read_sizes = []

    class CountingReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def read(self, size=-1):
            read_sizes.append(size)
            return self.stream.read(size)

    def counting_open(path, *args, **kwargs):
        return CountingReader(original_open(path, *args, **kwargs))

    monkeypatch.setattr(type(artifact), "open", counting_open)

    result = inspect_artifact(artifact, tmp_path)

    assert result.present is True
    assert result.size == 1024
    assert result.sha256 is None
    assert read_sizes == [16]


def test_identity_rejects_a_directory_symlink(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "Alias").symlink_to(tmp_path, target_is_directory=True)
    result = inspect_package_identity(out, "Alias")
    assert result.state == "invalid"
    assert result.metadata is None
