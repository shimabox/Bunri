"""CLI flag validation (all cases fail before build_package runs, except
--help)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from stemlab.cli import app

runner = CliRunner()


@pytest.fixture()
def audio(tmp_path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"fake")
    return f


def _run(*args):
    return runner.invoke(app, [str(a) for a in args])


def test_help_shows_usage():
    result = _run("--help")
    assert result.exit_code == 0
    assert "input-file" in result.output.lower() or "input_file" in result.output.lower()
    assert "--target" in result.output
    assert "--model" in result.output
    assert "--device" in result.output


def test_unknown_target_is_rejected(audio):
    result = _run(audio, "--target", "theremin")
    assert result.exit_code != 0
    assert "unknown target" in result.output


def test_unknown_device_is_rejected(audio):
    result = _run(audio, "--device", "gpu")
    assert result.exit_code != 0
    assert "must be auto, cpu, mps or cuda" in result.output


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
