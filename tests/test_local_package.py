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


def _pan_split_package(tmp_path, pan_split):
    out = tmp_path / "out"
    package = out / "Song"
    package.mkdir(parents=True)
    write_package_metadata(
        package / ".bunri-package.json",
        PackageMetadata(
            "Song",
            "Song",
            SourceIdentity("sha1", "a" * 40, "a" * 12),
            (TargetMetadata("guitar", ("mp3", "wav"), pan_split),),
        ),
    )
    for suffix in ("guitar", "guitar.backing"):
        for audio_format in ("mp3", "wav"):
            (package / f"Song.{suffix}.{audio_format}").write_bytes(b"audio")
    (package / "Song.guitar.player.html").write_bytes(b"<html>")
    (package / "Song.original.mp3").write_bytes(b"audio")
    return out, package


def test_left_right_files_are_inspected_without_affecting_completeness(tmp_path):
    out, package = _pan_split_package(tmp_path, "left_right")
    (package / "Song.guitar.left.mp3").write_bytes(b"audio")
    (package / "Song.guitar.right.wav").write_bytes(b"audio")

    result = inspect_package_artifacts(inspect_package_identity(out, "Song"))

    target = result.targets[0]
    assert target.pan_split == "left_right"
    assert {k: v.present for k, v in target.left_files} == {"mp3": True, "wav": False}
    assert {k: v.present for k, v in target.right_files} == {"mp3": False, "wav": True}
    assert target.complete_formats == ("mp3", "wav")
    assert result.issues == ()


def test_single_and_unrecorded_split_inspect_no_lr_files(tmp_path):
    out, package = _pan_split_package(tmp_path, "single")
    (package / "Song.guitar.left.mp3").write_bytes(b"stale")
    target = inspect_package_artifacts(inspect_package_identity(out, "Song")).targets[0]
    assert (target.pan_split, target.left_files, target.right_files) == ("single", (), ())

    (package / ".bunri-package.json").unlink()
    write_package_metadata(
        package / ".bunri-package.json",
        PackageMetadata(
            "Song", "Song", SourceIdentity("sha1", "a" * 40, "a" * 12),
            (TargetMetadata("guitar", ("mp3", "wav")),),
        ),
    )
    target = inspect_package_artifacts(inspect_package_identity(out, "Song")).targets[0]
    assert (target.pan_split, target.left_files, target.right_files) == (None, (), ())


def test_invalid_pan_split_makes_the_identity_invalid(tmp_path):
    out, package = _package(tmp_path)
    sidecar = package / ".bunri-package.json"
    value = json.loads(sidecar.read_text())
    value["targets"][0]["pan_split"] = "both"
    sidecar.write_text(json.dumps(value))

    result = inspect_package_identity(out, "Song")

    assert result.state == "invalid"
    assert any("pan_split" in issue for issue in result.issues)
