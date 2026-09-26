from __future__ import annotations

import json
from pathlib import Path

import pytest

from bunri.package_metadata import (
    PackageMetadata,
    SourceIdentity,
    TargetMetadata,
    write_package_metadata,
)
from bunri.pocket.local import LocalPreflightError, preflight


LR_MP3 = {"Song.guitar.left.mp3", "Song.guitar.right.mp3"}


@pytest.mark.parametrize(
    ("include_original", "pan_split", "hashed_names"),
    [
        (True, None, {"Song.original.mp3", "Song.guitar.mp3", "Song.guitar.backing.mp3"}),
        (False, None, {"Song.guitar.mp3", "Song.guitar.backing.mp3"}),
        (True, "single", {"Song.original.mp3", "Song.guitar.mp3", "Song.guitar.backing.mp3"}),
        (True, "left_right", {"Song.original.mp3", "Song.guitar.mp3", "Song.guitar.backing.mp3"} | LR_MP3),
        (False, "left_right", {"Song.guitar.mp3", "Song.guitar.backing.mp3"} | LR_MP3),
    ],
)
def test_preflight_reads_only_requested_mp3_assets(
    tmp_path, monkeypatch, include_original, pan_split, hashed_names
):
    out_dir = tmp_path / "out"
    package_dir = out_dir / "Song"
    package_dir.mkdir(parents=True)
    write_package_metadata(
        package_dir / ".bunri-package.json",
        PackageMetadata(
            "Song",
            "Song",
            SourceIdentity("sha1", "a" * 40, "a" * 12),
            (TargetMetadata("guitar", ("mp3", "wav"), pan_split),),
        ),
    )
    asset_names = {
        "Song.original.mp3",
        "Song.guitar.mp3",
        "Song.guitar.backing.mp3",
        "Song.guitar.wav",
        "Song.guitar.backing.wav",
        "Song.guitar.player.html",
        "Song.guitar.left.wav",
        "Song.guitar.right.wav",
    } | LR_MP3
    for name in asset_names:
        (package_dir / name).write_bytes(b"asset contents")

    original_open = Path.open
    reads: list[tuple[str, int]] = []

    class CountingReader:
        def __init__(self, path, stream):
            self.path = path
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def read(self, size=-1):
            reads.append((self.path.name, size))
            return self.stream.read(size)

        def __getattr__(self, name):
            return getattr(self.stream, name)

    def counting_open(path, *args, **kwargs):
        return CountingReader(path, original_open(path, *args, **kwargs))

    monkeypatch.setattr(Path, "open", counting_open)

    package = preflight(out_dir, "Song", include_original=include_original)

    read_asset_names = {name for name, _ in reads if name in asset_names}
    assert read_asset_names == hashed_names
    assert {asset.path.name for asset in package.assets} == hashed_names


def test_preflight_aggregates_invalid_array_target_and_missing_original(tmp_path):
    out_dir = tmp_path / "out"
    package_dir = out_dir / "Song"
    package_dir.mkdir(parents=True)
    (package_dir / ".bunri-package.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "title": "Song",
                "safe_name": "Song",
                "source": {
                    "algorithm": "sha1",
                    "digest": "a" * 40,
                    "cache_key": "a" * 12,
                },
                "targets": [{}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(LocalPreflightError) as exc_info:
        preflight(out_dir, "Song", include_original=True)

    assert "invalid or duplicate package target: None" in exc_info.value.issues
    assert any(
        issue.startswith("original:") and "通常ファイルではありません" in issue
        for issue in exc_info.value.issues
    )


def test_preflight_stops_before_assets_when_targets_is_not_an_array(tmp_path):
    out_dir = tmp_path / "out"
    package_dir = out_dir / "Song"
    package_dir.mkdir(parents=True)
    (package_dir / ".bunri-package.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "title": "Song",
                "safe_name": "Song",
                "source": {
                    "algorithm": "sha1",
                    "digest": "a" * 40,
                    "cache_key": "a" * 12,
                },
                "targets": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(LocalPreflightError) as exc_info:
        preflight(out_dir, "Song", include_original=True)

    assert exc_info.value.issues == ["package metadata targets must be an array"]
