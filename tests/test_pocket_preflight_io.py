from __future__ import annotations

from pathlib import Path

import pytest

from bunri.package_metadata import (
    PackageMetadata,
    SourceIdentity,
    TargetMetadata,
    write_package_metadata,
)
from bunri.pocket.local import preflight


@pytest.mark.parametrize(
    ("include_original", "hashed_names"),
    [
        (True, {"Song.original.mp3", "Song.guitar.mp3", "Song.guitar.backing.mp3"}),
        (False, {"Song.guitar.mp3", "Song.guitar.backing.mp3"}),
    ],
)
def test_preflight_reads_only_requested_mp3_assets(
    tmp_path, monkeypatch, include_original, hashed_names
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
            (TargetMetadata("guitar", ("mp3", "wav")),),
        ),
    )
    asset_names = {
        "Song.original.mp3",
        "Song.guitar.mp3",
        "Song.guitar.backing.mp3",
        "Song.guitar.wav",
        "Song.guitar.backing.wav",
        "Song.guitar.player.html",
    }
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
