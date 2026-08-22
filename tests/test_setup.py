"""Behavioural tests for the user-facing setup script.

Every external command is supplied from an isolated temporary PATH, so these
tests never install packages or mutate the real project environment.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SETUP_SCRIPT = _REPO_ROOT / "setup.sh"
_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="setup.sh targets macOS/Linux")


def _write_command(bin_dir: Path, name: str, body: str) -> Path:
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\nset -eu\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _isolated_bin(tmp_path: Path) -> Path:
    """Create the minimum PATH setup.sh needs, intentionally excluding any
    real uv, ffmpeg, brew and curl from the host."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("cat", "cut", "dirname", "head"):
        source = shutil.which(name)
        assert source is not None, f"test requires {name}"
        (bin_dir / name).symlink_to(source)
    return bin_dir


def _fake_uv(bin_dir: Path) -> None:
    _write_command(
        bin_dir,
        "uv",
        """
if [ "${1:-}" = "--version" ]; then
  echo "uv 0.test"
  exit 0
fi
printf '%s\\n' "$*" >> "$BUNRI_TEST_LOG"
""".strip(),
    )


def _fake_ffmpeg(bin_dir: Path) -> None:
    _write_command(
        bin_dir,
        "ffmpeg",
        """
if [ "${1:-}" = "-version" ]; then
  echo "ffmpeg version test"
  exit 0
fi
exit 2
""".strip(),
    )


def _run_setup(bin_dir: Path, log_path: Path, *, stdin: str = "") -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = str(bin_dir)
    env["BUNRI_TEST_LOG"] = str(log_path)
    return subprocess.run(
        ["/bin/bash", str(_SETUP_SCRIPT)],
        cwd=_REPO_ROOT,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=10,
    )


@_POSIX_ONLY
def test_setup_repeated_run_uses_locked_sync_without_installing_tools(tmp_path: Path) -> None:
    bin_dir = _isolated_bin(tmp_path)
    log_path = tmp_path / "uv.log"
    _fake_uv(bin_dir)
    _fake_ffmpeg(bin_dir)

    first = _run_setup(bin_dir, log_path)
    second = _run_setup(bin_dir, log_path)

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        "sync --locked --extra web",
        "sync --locked --extra web",
    ]


@_POSIX_ONLY
def test_setup_declining_missing_uv_stops_before_other_changes(tmp_path: Path) -> None:
    bin_dir = _isolated_bin(tmp_path)
    log_path = tmp_path / "uv.log"
    _fake_ffmpeg(bin_dir)

    result = _run_setup(bin_dir, log_path, stdin="n\n")

    assert result.returncode == 1
    assert "中断しました" in result.stdout
    assert not log_path.exists()


@_POSIX_ONLY
def test_setup_missing_ffmpeg_on_linux_stops_before_sync(tmp_path: Path) -> None:
    bin_dir = _isolated_bin(tmp_path)
    log_path = tmp_path / "uv.log"
    _fake_uv(bin_dir)
    _write_command(bin_dir, "uname", "echo Linux")

    result = _run_setup(bin_dir, log_path)

    assert result.returncode == 1
    assert "sudo apt install ffmpeg" in result.stdout
    assert not log_path.exists()


@_POSIX_ONLY
def test_setup_verifies_ffmpeg_is_on_path_after_brew_reports_success(tmp_path: Path) -> None:
    bin_dir = _isolated_bin(tmp_path)
    log_path = tmp_path / "uv.log"
    _fake_uv(bin_dir)
    _write_command(bin_dir, "uname", "echo Darwin")
    _write_command(bin_dir, "brew", ":")

    result = _run_setup(bin_dir, log_path, stdin="y\n")

    assert result.returncode == 1
    assert "ffmpeg が PATH に見つかりません" in result.stdout
    assert not log_path.exists()
