"""CLI flag validation (all cases fail before build_package runs, except
--help)."""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from bunri.cli import app


@pytest.mark.parametrize("argument", ["pocket-song.mp3", "./pocket"])
def test_dispatch_reserves_only_exact_pocket(monkeypatch, argument):
    import sys
    import bunri.cli as cli

    calls = []
    monkeypatch.setattr(cli, "app", lambda **kwargs: calls.append(("main", sys.argv[:], kwargs)))
    monkeypatch.setattr(sys, "argv", ["bunri", argument])
    cli.dispatch()
    assert calls == [("main", ["bunri", argument], {})]


@pytest.mark.parametrize("arguments", [["--help"], ["/definitely/missing.mp3"]])
def test_module_invocation_preserves_original_usage(arguments):
    import os
    import subprocess
    import sys

    environment = {**os.environ, "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"}
    module = subprocess.run([sys.executable, "-m", "bunri.cli", *arguments], text=True, capture_output=True, env=environment)
    output = _plain(module.stdout + module.stderr)
    assert module.returncode == (0 if arguments == ["--help"] else 2)
    assert "Usage: python -m bunri.cli [OPTIONS] INPUT_FILE" in output


def test_console_entrypoint_points_to_dispatch():
    import tomllib
    from pathlib import Path

    project = tomllib.loads(Path("pyproject.toml").read_text())
    assert project["project"]["scripts"]["bunri"] == "bunri.cli:dispatch"


def test_pocket_connect_has_no_token_argv_option():
    from bunri.pocket.cli import app as pocket_app

    result = CliRunner().invoke(pocket_app, ["connect", "--help"], env=_STABLE_TERMINAL)
    assert result.exit_code == 0
    help_text = _plain(result.output)
    assert "--token-stdin" in help_text
    assert "--token " not in help_text


def test_pocket_connect_reads_token_from_stdin_without_displaying_it(tmp_path, monkeypatch):
    import base64
    import bunri.pocket.cli as pocket_cli
    token = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")
    capabilities = {"api": {"major": 1}, "schemas": {"manifest": {"major": 1, "latest": "1.0"}, "library": {"major": 1, "latest": "1.0"}}, "limits": {"media_bytes": 94371840, "json_bytes": 1048576}, "media": {"content_types": ["audio/mpeg"], "hash": "SHA-256", "conditional_json_put": True}}

    class Client:
        def __init__(self, base_url, supplied_token):
            assert supplied_token == token
        def capabilities(self): return capabilities

    monkeypatch.setattr(pocket_cli, "PocketHTTPClient", Client)
    result = CliRunner().invoke(pocket_cli.app, ["connect", "https://example.invalid", "-o", str(tmp_path), "--token-stdin"], input=token + "\n")
    assert result.exit_code == 0, result.output
    assert token not in result.output
    assert (tmp_path / ".pocket/config.json").exists()


def test_pocket_sync_without_config_does_not_create_http_client(tmp_path, monkeypatch):
    import bunri.pocket.cli as pocket_cli

    monkeypatch.setattr(pocket_cli, "PocketHTTPClient", lambda *args: pytest.fail("HTTP started"))
    result = CliRunner().invoke(pocket_cli.app, ["sync", "Song", "-o", str(tmp_path)])
    assert result.exit_code == 1
    assert "Pocket の接続設定がありません" in _plain(result.output)


def test_pocket_sync_resolves_its_argument_only_as_a_safe_name(tmp_path, monkeypatch):
    import bunri.pocket.cli as pocket_cli
    from bunri.pocket.config import PocketConfig
    from bunri.pocket.sync import SyncResult

    calls = []
    monkeypatch.setattr(
        pocket_cli,
        "read_config",
        lambda _out: PocketConfig("https://example.invalid", "unused-token"),
    )
    monkeypatch.setattr(
        pocket_cli,
        "sync_one",
        lambda *args, **kwargs: calls.append((args, kwargs)) or SyncResult(),
    )

    result = CliRunner().invoke(
        pocket_cli.app,
        ["sync", "aaaaaaaaaaaa", "-o", str(tmp_path)],
        env=_STABLE_TERMINAL,
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0][1] == "aaaaaaaaaaaa"
    assert calls[0][1]["resolution"] == "safe_name"


@pytest.mark.parametrize("arguments", [[], ["Song", "--all"]])
def test_pocket_sync_requires_exactly_one_selector(arguments):
    from bunri.pocket.cli import app as pocket_app

    result = CliRunner().invoke(pocket_app, ["sync", *arguments], env=_STABLE_TERMINAL)
    assert result.exit_code == 1
    assert "SAFE_NAME と --all のどちらか一方" in _plain(result.output)


def test_pocket_sync_all_reports_every_legacy_package_on_local_validation_error(
    tmp_path, monkeypatch
):
    import bunri.pocket.cli as pocket_cli
    from bunri.pocket.config import PocketConfig
    from bunri.pocket.service import PocketServiceError

    monkeypatch.setattr(
        pocket_cli,
        "read_config",
        lambda _out: PocketConfig("https://example.invalid", "unused-token"),
    )

    def fail_sync_all(*_args, **_kwargs):
        raise PocketServiceError(
            "ローカル検証に失敗しました。",
            kind="local",
            legacy=("Old One", "Old Two"),
        )

    monkeypatch.setattr(pocket_cli, "sync_all", fail_sync_all)
    result = CliRunner().invoke(
        pocket_cli.app,
        ["sync", "--all", "-o", str(tmp_path)],
        env=_STABLE_TERMINAL,
    )

    output = _plain(result.output)
    assert result.exit_code == 1
    assert "再生成が必要: Old One" in output
    assert "再生成が必要: Old Two" in output
    assert "ローカル検証に失敗しました。" in output


def test_pocket_sync_all_failure_does_not_recapture_exit(tmp_path, monkeypatch):
    import bunri.pocket.cli as pocket_cli
    from bunri.pocket.config import PocketConfig
    from bunri.pocket.service import BatchItem, BatchResult

    monkeypatch.setattr(
        pocket_cli,
        "read_config",
        lambda _out: PocketConfig("https://example.invalid", "unused-token"),
    )
    monkeypatch.setattr(
        pocket_cli,
        "sync_all",
        lambda *_args, **_kwargs: BatchResult(
            total=1,
            items=[BatchItem("Song", "error", error="個別の同期エラー")],
        ),
    )
    result = CliRunner().invoke(
        pocket_cli.app,
        ["sync", "--all", "-o", str(tmp_path)],
        env=_STABLE_TERMINAL,
    )

    output = _plain(result.output)
    assert result.exit_code == 1
    assert "失敗: Song: 個別の同期エラー" in output
    assert "Pocket の同期に失敗しました。後で再実行してください。" not in output


def test_pocket_delete_song_id_with_yes_needs_no_local_package(tmp_path, monkeypatch):
    import bunri.pocket.cli as pocket_cli
    from bunri.pocket.config import PocketConfig
    from bunri.pocket.service import DeleteResult

    calls = []
    monkeypatch.setattr(pocket_cli, "read_config", lambda _out: PocketConfig("https://example.invalid", "unused"))
    monkeypatch.setattr(
        pocket_cli,
        "delete_track",
        lambda _out, target, **kwargs: calls.append(target) or DeleteResult(target.song_id),
    )
    result = CliRunner().invoke(
        pocket_cli.app,
        ["delete", "--song-id", "abcdef123456", "--yes", "-o", str(tmp_path)],
        env=_STABLE_TERMINAL,
    )
    assert result.exit_code == 0, result.output
    assert calls[0].song_id == "abcdef123456" and calls[0].safe_name is None
    assert "棚から削除しました: abcdef123456" in _plain(result.output)


def test_pocket_delete_invalid_song_id_prints_one_error(tmp_path, monkeypatch):
    import bunri.pocket.cli as pocket_cli
    from bunri.pocket.config import PocketConfig

    monkeypatch.setattr(
        pocket_cli,
        "read_config",
        lambda _out: PocketConfig("https://example.invalid", "unused"),
    )
    result = CliRunner().invoke(
        pocket_cli.app,
        ["delete", "--song-id", "INVALID", "--yes", "-o", str(tmp_path)],
        env=_STABLE_TERMINAL,
    )

    output = _plain(result.output)
    assert result.exit_code == 1
    assert output.count("error:") == 1
    assert "song ID は小文字16進12桁" in output
    assert "Pocket の削除に失敗しました" not in output


@pytest.mark.parametrize("arguments", [[], ["Song", "--song-id", "abcdef123456"], ["--select", "--song-id", "abcdef123456"]])
def test_pocket_delete_requires_exactly_one_selector(arguments):
    from bunri.pocket.cli import app as pocket_app

    result = CliRunner().invoke(pocket_app, ["delete", *arguments], env=_STABLE_TERMINAL)
    assert result.exit_code == 1
    assert "いずれか1つだけ" in _plain(result.output)


def test_pocket_delete_treats_twelve_hex_safe_name_only_as_safe_name(tmp_path, monkeypatch):
    import bunri.pocket.cli as pocket_cli
    from bunri.pocket.config import PocketConfig
    from bunri.pocket.service import DeleteResult, DeleteTargetIdentity

    resolved = DeleteTargetIdentity("b" * 12, "b" * 40, "aaaaaaaaaaaa", "Song")
    monkeypatch.setattr(pocket_cli, "read_config", lambda _out: PocketConfig("https://example.invalid", "unused"))
    monkeypatch.setattr(pocket_cli, "resolve_delete_target", lambda _out, name: resolved if name == "aaaaaaaaaaaa" else None)
    calls = []
    monkeypatch.setattr(pocket_cli, "delete_track", lambda _out, target, **kwargs: calls.append(target) or DeleteResult(target.song_id))
    result = CliRunner().invoke(
        pocket_cli.app,
        ["delete", "aaaaaaaaaaaa", "--yes", "-o", str(tmp_path)],
        env=_STABLE_TERMINAL,
    )
    assert result.exit_code == 0, result.output
    assert calls == [resolved]


def test_pocket_delete_select_rejects_non_tty_and_yes_without_side_effect(tmp_path, monkeypatch):
    import bunri.pocket.cli as pocket_cli
    from bunri.pocket.config import PocketConfig

    monkeypatch.setattr(pocket_cli, "read_config", lambda _out: PocketConfig("https://example.invalid", "unused"))
    monkeypatch.setattr(pocket_cli, "list_library_tracks", lambda *_args, **_kwargs: pytest.fail("library fetched"))
    result = CliRunner().invoke(pocket_cli.app, ["delete", "--select", "-o", str(tmp_path)], env=_STABLE_TERMINAL)
    assert result.exit_code == 1 and "TTY" in _plain(result.output)
    result = CliRunner().invoke(pocket_cli.app, ["delete", "--select", "--yes", "-o", str(tmp_path)], env=_STABLE_TERMINAL)
    assert result.exit_code == 1 and "同時に指定できません" in _plain(result.output)

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
    import bunri.cli as cli_module

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


def test_whitespace_title_uses_input_stem_in_banner_and_build(
    audio, tmp_path, monkeypatch
):
    import bunri.cli as cli_module

    captured = {}

    def fake_build_package(input_path, out_dir, **kwargs):
        captured.update(kwargs)
        return tmp_path / "song"

    monkeypatch.setattr(cli_module, "build_package", fake_build_package)
    result = _run(audio, "--title", "   ")

    assert result.exit_code == 0, result.output
    assert f"Bunri v{cli_module.__version__} — song" in _plain(result.output)
    assert captured["title"] == "song"


def test_missing_input_file_is_rejected(tmp_path):
    result = _run(tmp_path / "does-not-exist.mp3")
    assert result.exit_code != 0
