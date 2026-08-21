"""CLI flag validation (all cases fail before build_package runs, except
--help)."""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from stemlab.cli import app

runner = CliRunner()

# Typer renders help and errors through rich, which adapts to whatever
# terminal it believes it is writing to. Left to the ambient environment that
# makes these tests pass on one machine and fail on another, which is exactly
# what happened: GitHub Actions' runner reports colour where a local shell did
# not, and the raw output stopped matching.
_STABLE_TERMINAL = {
    "NO_COLOR": "1",
    "FORCE_COLOR": None,  # click's isolation() deletes a key whose value is None
    "TERM": "dumb",
    "COLUMNS": "200",  # wide enough that nothing wraps
}

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    """CLI output as a person reading it would see it, with no promises about
    where it sits on screen.

    Belt to `_STABLE_TERMINAL`'s braces, and worth having on its own, because
    the escapes are not merely *around* the text -- they land inside it. rich
    styles "--target" as two runs, so coloured output holds an escaped "-"
    followed by an escaped "-target" and the literal "--target" appears
    nowhere in it. Stripping the escapes puts the token back together.
    Whitespace is collapsed for the same reason one level up: rich wraps to
    the terminal width, so a phrase can arrive split across two lines.
    """
    return re.sub(r"\s+", " ", _ANSI_ESCAPE.sub("", output))


@pytest.fixture()
def audio(tmp_path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"fake")
    return f


def _run(*args):
    return runner.invoke(app, [str(a) for a in args], env=_STABLE_TERMINAL)


def test_help_shows_usage():
    result = _run("--help")
    assert result.exit_code == 0
    help_text = _plain(result.output)
    assert "input-file" in help_text.lower() or "input_file" in help_text.lower()
    assert "--target" in help_text
    assert "--model" in help_text
    assert "--device" in help_text


def test_unknown_target_is_rejected(audio):
    result = _run(audio, "--target", "theremin")
    assert result.exit_code != 0
    assert "unknown target" in _plain(result.output)


def test_unknown_device_is_rejected(audio):
    result = _run(audio, "--device", "gpu")
    assert result.exit_code != 0
    assert "must be auto, cpu, mps or cuda" in _plain(result.output)


def test_model_and_target_reach_build_package(audio, tmp_path, monkeypatch):
    import stemlab.cli as cli_module

    captured = {}

    def fake_build_package(input_path, out_dir, **kwargs):
        captured.update(kwargs)
        captured["input_path"] = input_path
        return tmp_path / "song"

    monkeypatch.setattr(cli_module, "build_package", fake_build_package)
    result = _run(audio, "--model", "htdemucs_6s.yaml", "--target", "guitar")
    assert result.exit_code == 0, result.output
    assert captured["model"] == "htdemucs_6s.yaml"
    assert captured["target"] == "guitar"


def test_missing_input_file_is_rejected(tmp_path):
    result = _run(tmp_path / "does-not-exist.mp3")
    assert result.exit_code != 0
