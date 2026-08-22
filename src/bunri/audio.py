"""ffmpeg-backed audio I/O: normalize any input to 44.1kHz stereo WAV, and
encode a cached WAV down to mp3 for the exported practice package."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from bunri.safepath import replace_into


def normalize_to_wav(
    input_path: Path, dest: Path, *, sample_rate: int = 44100, channels: int = 2
) -> None:
    """Normalize `input_path` (any ffmpeg-readable format) to a PCM16 WAV at
    `dest`, resampled to `sample_rate` Hz with `channels` channels."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH (install with: brew install ffmpeg)")

    def _run(out: Path) -> None:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-ar", str(sample_rate),
            "-ac", str(channels),
            "-acodec", "pcm_s16le",
            str(out),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            tail = "\n".join(result.stderr.splitlines()[-8:])
            raise RuntimeError(f"ffmpeg failed for {input_path}:\n{tail}")

    # ffmpeg writes wherever it is pointed, symlink included, so it is pointed
    # at a temporary and the result renamed over `dest`. See
    # bunri/safepath.py; the temporary keeps the suffix ffmpeg needs to pick
    # its muxer.
    replace_into(dest, _run)


def encode_mp3(src: Path, dest: Path, *, bitrate: str = "192k") -> None:
    """Transcode a WAV at `src` to an mp3 at `dest`. Same subprocess style as
    normalize_to_wav: explicit ffmpeg arg list, no shell."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH (install with: brew install ffmpeg)")
    def _run(out: Path) -> None:
        cmd = ["ffmpeg", "-y", "-i", str(src), "-c:a", "libmp3lame", "-b:a", bitrate, str(out)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            tail = "\n".join(result.stderr.splitlines()[-8:])
            raise RuntimeError(f"ffmpeg failed to encode {dest} from {src}:\n{tail}")

    replace_into(dest, _run)  # same reason as normalize_to_wav above
