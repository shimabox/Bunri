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
    assert calls[0][1]["expected_connection_fingerprint"] == (
        pocket_cli.connection_fingerprint(
            PocketConfig("https://example.invalid", "unused-token")
        )
    )


def test_pocket_delete_sanitizes_local_safe_name_in_confirmation(tmp_path, monkeypatch):
    import bunri.pocket.cli as pocket_cli
    from bunri.pocket.config import PocketConfig
    from bunri.pocket.service import DeleteTargetIdentity

    monkeypatch.setattr(
        pocket_cli,
        "read_config",
        lambda _out: PocketConfig("https://example.invalid", "unused-token"),
    )
    monkeypatch.setattr(
        pocket_cli,
        "resolve_delete_target",
        lambda *_args: DeleteTargetIdentity(
            song_id="a" * 12,
            safe_name="Local\x1b[2J[/x]",
            title="[bold red]Remote[/]",
        ),
    )

    result = CliRunner().invoke(
        pocket_cli.app,
        ["delete", "Song", "-o", str(tmp_path)],
        env=_STABLE_TERMINAL,
    )

    assert result.exit_code == 1
    assert "\x1b[2J" not in result.output
    assert "Local�[2J[/x]" in result.output
    assert "[bold red]Remote[/]" in result.output


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

    calls = []

    def fail_sync_all(*_args, **kwargs):
        calls.append(kwargs)
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
    assert calls[0]["expected_connection_fingerprint"] == (
        pocket_cli.connection_fingerprint(
            PocketConfig("https://example.invalid", "unused-token")
        )
    )


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


PAN_SPLIT_UNSUPPORTED_NOTICE = (
    "この音源ポケットは L のみ / R のみに未対応です。音源ポケットを更新して再同期すると送られます"
)


class _MemoryPocket:
    def __init__(self, capabilities):
        self.capabilities_value = capabilities
        self.docs = {}
        self.media = {}

    def capabilities(self):
        return self.capabilities_value

    def get_json(self, path):
        return self.docs.get(path)

    def head_media(self, song_id, name):
        return self.media.get((song_id, name))

    def put_media(self, song_id, name, path, size, checksum):
        self.media[(song_id, name)] = (checksum, size)
        return checksum

    def put_json(self, path, data, condition):
        import json

        from bunri.pocket.http import JSONDocument

        self.docs[path] = JSONDocument(json.loads(data), '"next"')
        return '"next"'


def _pocket_output_with_lr_song(tmp_path, monkeypatch, capabilities):
    import base64

    import bunri.pocket.service as service_module
    from bunri.package_metadata import (
        PackageMetadata,
        SourceIdentity,
        TargetMetadata,
        write_package_metadata,
    )
    from bunri.pocket.config import PocketConfig, save_config

    token = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")
    save_config(tmp_path, PocketConfig("https://example.invalid", token))
    for name, digest, pan_split in (("Song", "a" * 40, "left_right"), ("Other", "b" * 40, None)):
        directory = tmp_path / name
        directory.mkdir()
        write_package_metadata(
            directory / ".bunri-package.json",
            PackageMetadata(
                name, name, SourceIdentity("sha1", digest, digest[:12]),
                (TargetMetadata("guitar", ("mp3",), pan_split),),
            ),
        )
        for suffix in ("original", "guitar", "guitar.backing", "guitar.left", "guitar.right"):
            (directory / f"{name}.{suffix}.mp3").write_bytes(suffix.encode())
    pocket = _MemoryPocket(capabilities)
    monkeypatch.setattr(service_module, "PocketHTTPClient", lambda *_args, **_kwargs: pocket)
    return pocket


@pytest.mark.parametrize("arguments", [["Song"], ["Other"], ["--all"]])
@pytest.mark.parametrize("supported", [False, True])
def test_pocket_sync_mentions_an_unsupported_pocket_once(tmp_path, monkeypatch, arguments, supported):
    from bunri.pocket.cli import app as pocket_app

    capabilities = {"api": {"major": 1}, "features": {"pan_split": supported}}
    pocket = _pocket_output_with_lr_song(tmp_path, monkeypatch, capabilities)
    result = CliRunner().invoke(
        pocket_app, ["sync", *arguments, "-o", str(tmp_path)], env=_STABLE_TERMINAL
    )

    assert result.exit_code == 0, result.output
    assert _plain(result.output).count(PAN_SPLIT_UNSUPPORTED_NOTICE) == (0 if supported else 1)
    lr_sent = ("aaaaaaaaaaaa", "guitar.left.mp3") in pocket.media
    assert lr_sent is (supported and arguments != ["Other"])
    if not supported:
        lines = result.output.splitlines()
        # The notice follows the counts: the library line, or the batch summary.
        anchor = "集計:" if arguments == ["--all"] else "library:"
        assert lines.index(PAN_SPLIT_UNSUPPORTED_NOTICE) > next(
            index for index, line in enumerate(lines) if line.startswith(anchor)
        )


def test_pocket_sync_capabilities_failure_uploads_nothing(tmp_path, monkeypatch):
    from bunri.pocket.cli import app as pocket_app
    from bunri.pocket.http import PocketHTTPError

    pocket = _pocket_output_with_lr_song(tmp_path, monkeypatch, None)

    def unauthorized():
        raise PocketHTTPError(401, "UNAUTHORIZED", "https://example.invalid")

    pocket.capabilities = unauthorized
    for arguments in (["Song"], ["--all"]):
        result = CliRunner().invoke(
            pocket_app, ["sync", *arguments, "-o", str(tmp_path)], env=_STABLE_TERMINAL
        )
        assert result.exit_code == 1
        assert "Pocket の認証に失敗しました。" in _plain(result.output)
        assert PAN_SPLIT_UNSUPPORTED_NOTICE not in _plain(result.output)
    assert pocket.media == {} and pocket.docs == {}


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
    assert "音源ポケットから削除しました: abcdef123456" in _plain(result.output)


def test_pocket_delete_stops_when_connection_changes_after_confirmation(tmp_path, monkeypatch):
    import base64
    import bunri.pocket.cli as pocket_cli
    import bunri.pocket.service as service_module
    from bunri.pocket.config import PocketConfig, save_config

    token = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")
    save_config(tmp_path, PocketConfig("https://shelf-a.invalid", token))
    remote_calls = []

    class UnexpectedClient:
        def __init__(self, *_args, **_kwargs):
            remote_calls.append("client-created")

    def change_connection(*_args, **_kwargs):
        save_config(tmp_path, PocketConfig("https://shelf-b.invalid", token))
        return True

    monkeypatch.setattr(pocket_cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(pocket_cli.typer, "confirm", change_connection)
    monkeypatch.setattr(service_module, "PocketHTTPClient", UnexpectedClient)

    result = CliRunner().invoke(
        pocket_cli.app,
        ["delete", "--song-id", "abcdef123456", "-o", str(tmp_path)],
        env=_STABLE_TERMINAL,
    )

    assert result.exit_code == 1
    assert "接続先が変更されたため削除を中止しました。対象を選び直してください" in _plain(result.output)
    assert remote_calls == []


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


# ---------------------------------------------------------------------------
# bunri pocket reset-password
# ---------------------------------------------------------------------------
_RESET_URL = "https://reset-pocket.invalid/shelf"
_RESET_PASSWORD = "new-viewing-password-01"
_RESET_CAPABILITIES = {"api": {"major": 1}, "features": {"password_reset": True}}
_RESET_COMPLETED = (
    "音源ポケットにログインしていたすべての端末はログアウトされました。新しいパスワードで入り直してください。",
    "直前にログインに失敗していた場合は、待ち時間(最大 60 秒)が明けるまでログインできないことがあります。",
    "曲と upload token、Bunri の接続設定はそのままです。",
)


def _squash(text: str) -> str:
    """Output with every space and line break removed.

    The long Japanese lines wrap at the console width, which puts a line
    break in the middle of a sentence; comparing without whitespace keeps the
    assertions independent of where that happens, and keeps a wrapped secret
    from slipping past a "not in" check.
    """
    return re.sub(r"\s+", "", _ANSI_ESCAPE.sub("", text))


def _shown(result, text: str) -> bool:
    return _squash(text) in _squash(result.output)


def _reset_token() -> str:
    import base64

    return base64.urlsafe_b64encode(b"\x7e" * 33).decode().rstrip("=")


class _ResetPocket:
    """Stands in for PocketHTTPClient in the reset-password command."""

    MATCH = object()  # auth_params answers with the salt that was just sent

    def __init__(self, capabilities=_RESET_CAPABILITIES, reset=None, params=()):
        self.capabilities_value = capabilities
        self.reset = reset
        self.params = list(params)
        self.created: list[tuple[str, str]] = []
        self.bodies: list[bytes] = []
        self.auth_calls = 0

    def __call__(self, base_url, token):
        self.created.append((base_url, token))
        return self

    def capabilities(self):
        if isinstance(self.capabilities_value, BaseException):
            raise self.capabilities_value
        return self.capabilities_value

    def reset_password(self, body):
        self.bodies.append(body)
        if isinstance(self.reset, BaseException):
            raise self.reset

    def auth_params(self):
        import json

        self.auth_calls += 1
        item = self.params.pop(0) if self.params else OSError("no answer")
        if isinstance(item, BaseException):
            raise item
        salt = json.loads(self.bodies[-1])["kdf"]["salt"] if item is self.MATCH else item
        return {"schema_version": 1, "kdf": {"name": "PBKDF2-HMAC-SHA256", "iterations": 600000, "salt": salt, "dk_len": 32}}


def _reset_setup(tmp_path, monkeypatch, pocket=None, *, tty=True, connected=True):
    import bunri.pocket.cli as pocket_cli
    import bunri.pocket.password as password_module
    from bunri.pocket.config import PocketConfig, save_config

    pocket = pocket or _ResetPocket()
    if connected:
        save_config(tmp_path, PocketConfig(_RESET_URL, _reset_token()))
    monkeypatch.setattr(pocket_cli, "PocketHTTPClient", pocket)
    monkeypatch.setattr(pocket_cli, "_stdin_is_tty", lambda: tty)
    monkeypatch.setattr(password_module, "CONFIRM_INTERVAL_SECONDS", 0)
    return pocket


def _reset_run(tmp_path, *arguments, answers=("y", _RESET_PASSWORD, _RESET_PASSWORD)):
    import bunri.pocket.cli as pocket_cli

    return CliRunner().invoke(
        pocket_cli.app,
        ["reset-password", "-o", str(tmp_path), *arguments],
        input="".join(answer + "\n" for answer in answers),
        env=_STABLE_TERMINAL,
    )


def _assert_no_reset_secrets(result, pocket, *passwords):
    """Nothing typed, derived or stored may be echoed, whatever the outcome."""
    import base64
    import json

    output = _squash(result.output)
    assert _reset_token() not in output
    for password in passwords or (_RESET_PASSWORD,):
        raw = password.encode("utf-8", "surrogatepass")
        assert _squash(password) not in output
        for encoded in (base64.b64encode(raw), base64.urlsafe_b64encode(raw)):
            assert encoded.decode().rstrip("=") not in output
    for body in pocket.bodies:
        assert json.loads(body)["derived_key"] not in output
        assert body.decode() not in output


def test_pocket_reset_password_help_offers_no_password_option():
    from bunri.pocket.cli import app as pocket_app

    result = CliRunner().invoke(pocket_app, ["reset-password", "--help"], env=_STABLE_TERMINAL)
    assert result.exit_code == 0
    help_text = _plain(result.output)
    assert "--yes" in help_text and "--output" in help_text
    assert "--password" not in help_text


@pytest.mark.parametrize(
    ("config_text", "message"),
    [
        (None, "Pocket の接続設定がありません。"),
        ("not json", "Pocket の接続設定を確認できません。"),
        ('{"schema_version": 1}', "Pocket の接続設定を確認できません。"),
    ],
)
def test_pocket_reset_password_without_usable_config_starts_no_http(tmp_path, monkeypatch, config_text, message):
    pocket = _reset_setup(tmp_path, monkeypatch, connected=False)
    if config_text is not None:
        (tmp_path / ".pocket").mkdir()
        (tmp_path / ".pocket" / "config.json").write_text(config_text, encoding="utf-8")
    result = _reset_run(tmp_path)
    assert result.exit_code == 1
    assert _shown(result, message)
    assert pocket.created == [] and pocket.bodies == []
    _assert_no_reset_secrets(result, pocket)


@pytest.mark.parametrize("arguments", [[], ["--yes"]])
def test_pocket_reset_password_needs_a_terminal_even_with_yes(tmp_path, monkeypatch, arguments):
    import bunri.pocket.cli as pocket_cli

    pocket = _reset_setup(tmp_path, monkeypatch, tty=False)
    monkeypatch.setattr(pocket_cli.typer, "prompt", lambda *a, **k: pytest.fail("stdin was read"))
    monkeypatch.setattr(pocket_cli.typer, "confirm", lambda *a, **k: pytest.fail("stdin was read"))
    result = _reset_run(tmp_path, *arguments)
    assert result.exit_code == 1
    assert _shown(result, "閲覧パスワードの入力には TTY が必要です。端末から実行するか、Web UI から再設定してください。")
    assert pocket.created == [] and pocket.bodies == []
    _assert_no_reset_secrets(result, pocket)


@pytest.mark.parametrize(
    "capabilities",
    [
        {"api": {"major": 1}, "features": {"pan_split": True}},
        {"api": {"major": 1}, "features": {"password_reset": False}},
        {"api": {"major": 1}},
    ],
)
def test_pocket_reset_password_points_an_older_pocket_to_the_update(tmp_path, monkeypatch, capabilities):
    pocket = _reset_setup(tmp_path, monkeypatch, _ResetPocket(capabilities))
    result = _reset_run(tmp_path)
    assert result.exit_code == 1
    assert _shown(result, "この音源ポケットは閲覧パスワードの再設定に未対応です。音源ポケットを 0.4.0 以降へ更新してください。閲覧パスワードは変わっていません。")
    assert not _shown(result, "新しい閲覧パスワード")
    assert pocket.bodies == []
    _assert_no_reset_secrets(result, pocket)


@pytest.mark.parametrize("capabilities", [["features"], "password_reset"])
def test_pocket_reset_password_does_not_call_unreadable_capabilities_unsupported(tmp_path, monkeypatch, capabilities):
    pocket = _reset_setup(tmp_path, monkeypatch, _ResetPocket(capabilities))
    result = _reset_run(tmp_path)
    assert result.exit_code == 1
    assert _shown(result, "音源ポケットの対応状況を確認できませんでした。閲覧パスワードは変わっていません。")
    assert not _shown(result, "未対応")
    assert pocket.bodies == []
    _assert_no_reset_secrets(result, pocket)


def test_pocket_reset_password_reports_a_failed_capabilities_read(tmp_path, monkeypatch):
    from bunri.pocket.http import PocketHTTPError

    for failure, message in (
        (PocketHTTPError(401, "UNAUTHORIZED", _RESET_URL), "upload token が無効です。音源ポケットで token を再発行した場合は接続し直してください。"),
        (OSError(f"cannot reach {_RESET_URL}"), "音源ポケットに接続できませんでした。"),
    ):
        pocket = _reset_setup(tmp_path, monkeypatch, _ResetPocket(failure))
        result = _reset_run(tmp_path)
        assert result.exit_code == 1
        assert _shown(result, message + "閲覧パスワードは変わっていません。")
        assert not _shown(result, "reset-pocket.invalid")
        assert pocket.bodies == []
        _assert_no_reset_secrets(result, pocket)


def test_pocket_reset_password_declined_asks_for_no_password(tmp_path, monkeypatch):
    pocket = _reset_setup(tmp_path, monkeypatch)
    result = _reset_run(tmp_path, answers=("n",))
    assert result.exit_code == 0, result.output
    assert _shown(result, "閲覧パスワードの再設定を取り消しました。")
    assert not _shown(result, "新しい閲覧パスワード")
    assert pocket.bodies == []
    _assert_no_reset_secrets(result, pocket)


def test_pocket_reset_password_sends_a_key_derived_from_a_fresh_salt(tmp_path, monkeypatch):
    import json

    from bunri.pocket.password import derive_key, parse_reset_request

    pocket = _reset_setup(tmp_path, monkeypatch)
    result = _reset_run(tmp_path)
    assert result.exit_code == 0, result.output
    assert pocket.created == [(_RESET_URL, _reset_token())]
    # The confirmation names the destination and what success does.
    assert _shown(result, f"接続先: {_RESET_URL}")
    assert _shown(result, "成功すると、音源ポケットにログインしていたすべての端末がログアウトされます。")
    assert _shown(result, "この操作を続けますか？")
    (body,) = pocket.bodies
    salt, derived_key = parse_reset_request(json.loads(body))
    assert len(salt) == 16
    assert derived_key == derive_key(_RESET_PASSWORD.encode("utf-8"), salt)
    assert _shown(result, "閲覧パスワードを決め直しました。")
    for line in _RESET_COMPLETED:
        assert _shown(result, line)
    assert pocket.auth_calls == 0
    _assert_no_reset_secrets(result, pocket)


def test_pocket_reset_password_yes_skips_only_the_confirmation(tmp_path, monkeypatch):
    pocket = _reset_setup(tmp_path, monkeypatch)
    result = _reset_run(tmp_path, "--yes", answers=(_RESET_PASSWORD, _RESET_PASSWORD))
    assert result.exit_code == 0, result.output
    assert not _shown(result, "この操作を続けますか？")
    assert _shown(result, f"接続先: {_RESET_URL}")
    assert len(pocket.bodies) == 1
    _assert_no_reset_secrets(result, pocket)


@pytest.mark.parametrize(
    ("first", "second", "message"),
    [
        (_RESET_PASSWORD, _RESET_PASSWORD + "x", "2 回の入力が一致しません。閲覧パスワードは変わっていません。"),
        ("elevenchars", "elevenchars", "閲覧パスワードは 12 文字(Unicode コードポイント)以上にしてください。"),
    ],
)
def test_pocket_reset_password_rejects_bad_input_before_sending(tmp_path, monkeypatch, first, second, message):
    pocket = _reset_setup(tmp_path, monkeypatch)
    result = _reset_run(tmp_path, answers=("y", first, second))
    assert result.exit_code == 1
    assert _shown(result, message)
    assert pocket.bodies == [] and pocket.auth_calls == 0
    _assert_no_reset_secrets(result, pocket, first, second)


@pytest.mark.parametrize(
    ("status", "code", "retry_after", "message"),
    [
        (401, "UNAUTHORIZED", None, "upload token が無効です。音源ポケットで token を再発行した場合は接続し直してください。"),
        (403, "ORIGIN_MISMATCH", None, "音源ポケットが Origin の不一致として拒否しました。接続先 URL と音源ポケットの設定を確認してください。"),
        (404, "NOT_FOUND", None, "音源ポケットに再設定の経路がありません。音源ポケットを 0.4.0 以降へ更新するか、接続先 URL を確認してください。"),
        (422, "UNSUPPORTED_KDF", None, "この Bunri の鍵導出方式を音源ポケットが受け付けません。Bunri を更新してください。"),
        (400, "INVALID_REQUEST", None, "音源ポケットが要求の形式を拒否しました(HTTP 400 INVALID_REQUEST)。Bunri と音源ポケットの版の組み合わせを確認してください。"),
        (429, "RATE_LIMITED", "30", "音源ポケットが要求を制限しました。しばらく待って再実行してください。(目安: 30 秒後)"),
    ],
)
def test_pocket_reset_password_reports_a_rejection_as_unchanged(tmp_path, monkeypatch, status, code, retry_after, message):
    from bunri.pocket.http import PocketHTTPError

    failure = PocketHTTPError(status, code, _RESET_URL, retry_after=retry_after)
    pocket = _reset_setup(tmp_path, monkeypatch, _ResetPocket(reset=failure))
    result = _reset_run(tmp_path)
    assert result.exit_code == 1
    assert _shown(result, message)
    assert _shown(result, "閲覧パスワードは変わっていません。")
    assert not _shown(result, "決め直しました")
    assert len(pocket.bodies) == 1 and pocket.auth_calls == 0
    _assert_no_reset_secrets(result, pocket)


def test_pocket_reset_password_503_only_asks_for_a_retry(tmp_path, monkeypatch):
    from bunri.pocket.http import PocketHTTPError

    failure = PocketHTTPError(503, "AUTH_UNAVAILABLE", _RESET_URL)
    pocket = _reset_setup(tmp_path, monkeypatch, _ResetPocket(reset=failure))
    result = _reset_run(tmp_path)
    assert result.exit_code == 1
    assert _shown(result, "音源ポケットの認証情報または保存領域を利用できませんでした。時間をおいて同じ操作を再実行してください。")
    assert not _shown(result, "変わっていません") and not _shown(result, "決め直しました")
    assert pocket.auth_calls == 0
    _assert_no_reset_secrets(result, pocket)


def _lost_responses():
    from bunri.pocket.http import PocketHTTPError

    return {
        "os_error": OSError(f"timed out talking to {_RESET_URL}"),
        "502": PocketHTTPError(502, None, _RESET_URL),
        "response_too_large": PocketHTTPError(413, "RESPONSE_TOO_LARGE", _RESET_URL),
    }


@pytest.mark.parametrize("failure", ["os_error", "502", "response_too_large"])
def test_pocket_reset_password_confirms_a_lost_response_as_completed(tmp_path, monkeypatch, failure):
    pocket = _reset_setup(
        tmp_path, monkeypatch,
        _ResetPocket(reset=_lost_responses()[failure], params=[_ResetPocket.MATCH]),
    )
    result = _reset_run(tmp_path)
    assert result.exit_code == 0, result.output
    assert _shown(result, "音源ポケットからの応答を確認できませんでした。音源ポケットに結果を問い合わせています…")
    assert _shown(result, "音源ポケットからの応答を確認できなかったため、結果を問い合わせました。再設定は完了していました。")
    for line in _RESET_COMPLETED:
        assert _shown(result, line)
    assert not _shown(result, "変わっていません") and not _shown(result, "reset-pocket.invalid/shelf/api")
    assert pocket.auth_calls == 1
    _assert_no_reset_secrets(result, pocket)


def test_pocket_reset_password_reports_not_applied_when_the_salt_stays_different(tmp_path, monkeypatch):
    pocket = _reset_setup(
        tmp_path, monkeypatch,
        _ResetPocket(reset=_lost_responses()["os_error"], params=["AAECAwQFBgcICQoLDA0ODw"] * 3),
    )
    result = _reset_run(tmp_path)
    assert result.exit_code == 1
    assert _shown(result, "再設定は反映されていません。閲覧パスワードは以前のままです。")
    assert _shown(result, "再実行では今回と同じパスワードを使ってください")
    assert not _shown(result, "決め直しました")
    assert pocket.auth_calls == 3
    _assert_no_reset_secrets(result, pocket)


def test_pocket_reset_password_says_so_when_the_result_cannot_be_read(tmp_path, monkeypatch):
    from bunri.pocket.http import PocketHTTPError

    pocket = _reset_setup(
        tmp_path, monkeypatch,
        _ResetPocket(
            reset=_lost_responses()["502"],
            params=[OSError("down"), PocketHTTPError(503, "AUTH_UNAVAILABLE", _RESET_URL), OSError("down")],
        ),
    )
    result = _reset_run(tmp_path)
    assert result.exit_code == 1
    assert _shown(result, "結果を問い合わせましたが、確認できませんでした。")
    assert _shown(result, "同じ操作をもう一度実行しても安全です。再実行では今回と同じパスワードを使ってください。")
    assert not _shown(result, "変わっていません") and not _shown(result, "決め直しました")
    assert pocket.auth_calls == 3
    _assert_no_reset_secrets(result, pocket)


def test_pocket_reset_password_keeps_typed_whitespace(tmp_path, monkeypatch):
    import json

    from bunri.pocket.password import derive_key, parse_reset_request

    spaced = "  spaced out password  "
    pocket = _reset_setup(tmp_path, monkeypatch)
    result = _reset_run(tmp_path, answers=("y", spaced, spaced))
    assert result.exit_code == 0, result.output
    salt, derived_key = parse_reset_request(json.loads(pocket.bodies[0]))
    assert derived_key == derive_key(spaced.encode("utf-8"), salt)
    assert derived_key != derive_key(spaced.strip().encode("utf-8"), salt)
    _assert_no_reset_secrets(result, pocket, spaced)


def test_pocket_reset_password_runs_while_the_mutation_lock_is_held(tmp_path, monkeypatch):
    from bunri.pocket.lock import SyncLock

    pocket = _reset_setup(tmp_path, monkeypatch)
    with SyncLock(tmp_path):
        result = _reset_run(tmp_path)
    assert result.exit_code == 0, result.output
    assert _shown(result, "閲覧パスワードを決め直しました。")
    assert len(pocket.bodies) == 1
    _assert_no_reset_secrets(result, pocket)
