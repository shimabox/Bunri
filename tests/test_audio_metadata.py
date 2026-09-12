from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from bunri.audio import encode_mp3, normalize_to_wav


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg required",
)
def test_exported_mp3_files_have_only_the_requested_title_tag(tmp_path):
    source = tmp_path / "input.m4a"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=0.2",
            "-metadata", "title=Private title",
            "-metadata", "artist=Private artist",
            "-metadata", "comment=Private comment",
            "-metadata", "album=Private album",
            "-c:a", "aac", str(source),
        ],
        check=True,
    )
    normalized = tmp_path / "normalized.wav"
    normalize_to_wav(source, normalized)

    outputs = {
        tmp_path / "Song.original.mp3": "Song",
        tmp_path / "Song.guitar.mp3": "Song (ギターのみ)",
        tmp_path / "Song.guitar.backing.mp3": "Song (ギターなし)",
    }
    for output, title in outputs.items():
        encode_mp3(normalized, output, title=title)

    for output, title in outputs.items():
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format_tags",
                "-of", "json", str(output),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(probe.stdout).get("format", {}).get("tags", {}) == {
            "title": title
        }
