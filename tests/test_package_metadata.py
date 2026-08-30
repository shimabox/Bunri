from __future__ import annotations

import json
from pathlib import Path

import pytest

from bunri import cache
from bunri.package_metadata import PackageMetadata, SourceIdentity, TargetMetadata, begin_target, complete_target, read_package_metadata, write_package_metadata


def metadata() -> PackageMetadata:
    return PackageMetadata("Song", "Song", SourceIdentity("sha1", "a" * 40, "a" * 12), (TargetMetadata("guitar", ("mp3", "wav")),))


def test_sidecar_roundtrip_invalidation_and_merge(tmp_path):
    directory = tmp_path / "Song"; directory.mkdir(); path = directory / ".bunri-package.json"
    write_package_metadata(path, metadata())
    pending = begin_target(path, title="Song", safe_name="Song", digest="a" * 40, cache_key="a" * 12, target="guitar")
    assert pending.targets == () and read_package_metadata(path).targets == ()
    complete_target(path, expected=pending, target="guitar", formats=("wav",))
    assert read_package_metadata(path).targets[0].formats == ("wav",)


@pytest.mark.parametrize("version", [True, "1", 2])
def test_sidecar_rejects_non_integer_v1(tmp_path, version):
    directory = tmp_path / "Song"; directory.mkdir(); path = directory / ".bunri-package.json"
    value = json.loads(json.dumps({"schema_version": 1, "title": "Song", "safe_name": "Song", "source": {"algorithm": "sha1", "digest": "a" * 40, "cache_key": "a" * 12}, "targets": []}))
    value["schema_version"] = version; path.write_text(json.dumps(value))
    with pytest.raises(ValueError): read_package_metadata(path)


def test_sidecar_refuses_symlink_and_digest_collision(tmp_path):
    directory = tmp_path / "Song"; directory.mkdir(); victim = tmp_path / "victim"; victim.write_text("x")
    path = directory / ".bunri-package.json"; path.symlink_to(victim)
    with pytest.raises(ValueError): read_package_metadata(path)
    path.unlink(); write_package_metadata(path, metadata())
    with pytest.raises(ValueError, match="different input"): begin_target(path, title="Song", safe_name="Song", digest="b" * 40, cache_key="b" * 12, target="guitar")


def test_input_digest_and_cache_identity_keep_short_directory(tmp_path):
    source = tmp_path / "input"; source.write_bytes(b"content")
    digest = cache.input_digest(source)
    assert len(digest.full_sha1) == 40 and digest.cache_key == digest.full_sha1[:12]
    assert cache.file_digest(source) == digest.cache_key
    cache_dir = tmp_path / digest.cache_key; cache_dir.mkdir()
    cache.ensure_input_identity(cache_dir, digest)
    cache.ensure_input_identity(cache_dir, digest)
    with pytest.raises(ValueError, match="collision"):
        cache.ensure_input_identity(cache_dir, cache.InputDigest("b" * 40, digest.cache_key))
