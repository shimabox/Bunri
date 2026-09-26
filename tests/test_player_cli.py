"""`bunri player`: rewriting existing packages' players from the current
template, driven through the Typer app."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from typer.testing import CliRunner

from bunri.package import build_package
from bunri.package_metadata import begin_target
from bunri.player_cli import app
from pan_split_helpers import FakeSeparator, install_fake_separator, strip_pan_split

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

_ENV = {"NO_COLOR": "1", "FORCE_COLOR": None, "TERM": "dumb", "COLUMNS": "200"}
_STALE = "<!doctype html><title>old player</title>"


class _CenterSeparator(FakeSeparator):
    panned = False


@pytest.fixture(autouse=True)
def _panned(monkeypatch):
    install_fake_separator(monkeypatch, FakeSeparator)


@pytest.fixture()
def centered(monkeypatch):
    install_fake_separator(monkeypatch, _CenterSeparator)


def _input(tmp_path: Path, seconds: float = 0.3) -> Path:
    src = tmp_path / f"input-{seconds}.wav"
    sf.write(str(src), np.zeros((int(44100 * seconds), 2), dtype=np.float32), 44100)
    return src


def _package(
    tmp_path: Path,
    title: str = "Song",
    *,
    targets: tuple[str, ...] = ("guitar",),
    mp3: bool = True,
    seconds: float = 0.3,
) -> Path:
    """A built package whose players all hold stale HTML; the input audio is
    gone, as it would be for a package made long ago."""
    out_dir = tmp_path / "out"
    src = _input(tmp_path, seconds)
    for target in targets:
        build_package(src, out_dir, title=title, target=target, mp3=mp3)
    src.unlink()
    for target in targets:
        _player(out_dir, target, title).write_text(_STALE, encoding="utf-8")
    return out_dir


def _player(out_dir: Path, target: str = "guitar", name: str = "Song") -> Path:
    return out_dir / name / f"{name}.{target}.player.html"


def _run(*args: str):
    result = CliRunner().invoke(app, list(args), env=_ENV)
    return result, re.sub(r"[ \t]+", " ", result.output)


def _sidecar_path(out_dir: Path, name: str = "Song") -> Path:
    return out_dir / name / ".bunri-package.json"


def _sidecar(out_dir: Path, name: str = "Song") -> dict:
    return json.loads(_sidecar_path(out_dir, name).read_text(encoding="utf-8"))


def _write_sidecar(out_dir: Path, value: dict, name: str = "Song") -> None:
    _sidecar_path(out_dir, name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _hashes(directory: Path, *, skip_players: bool = False) -> dict[str, str]:
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file() and not (skip_players and p.name.endswith(".player.html"))
    }


def _markup(html: str) -> str:
    return html.split("<script>")[0]


def _button(html: str, track: str) -> str | None:
    match = re.search(rf'<button[^>]*data-track="{track}"[^>]*>.*?</button>', html, re.S)
    return match.group(0) if match else None


def _result_lines(output: str) -> list[str]:
    return [
        line for line in output.splitlines()
        if line.startswith(("完了:", "スキップ:", "失敗:", "再生成が必要:"))
    ]


# --------------------------------------------------------------------------
# rewriting
# --------------------------------------------------------------------------
def test_rewrites_a_stale_player_and_nothing_else(tmp_path):
    out_dir = _package(tmp_path)
    package_before = _hashes(out_dir / "Song", skip_players=True)
    cache_before = _hashes(out_dir / ".cache")

    result, output = _run("Song", "-o", str(out_dir))

    assert result.exit_code == 0, output
    assert "完了: Song (ギター)" in output
    html = _player(out_dir).read_text(encoding="utf-8")
    assert "old player" not in html
    for track in ("original", "target", "backing"):
        button = _button(_markup(html), track)
        assert button and "disabled" not in button
    assert 'src="Song.original.mp3"' in html
    assert 'src="Song.guitar.mp3"' in html and 'src="Song.guitar.backing.mp3"' in html
    assert _hashes(out_dir / "Song", skip_players=True) == package_before
    assert _hashes(out_dir / ".cache") == cache_before
    assert not list((out_dir / "Song").glob(".*.tmp-*"))


def test_left_right_package_gets_playable_lr_buttons(tmp_path):
    out_dir = _package(tmp_path)
    assert _sidecar(out_dir)["targets"][0]["pan_split"] == "left_right"

    result, output = _run("Song", "-o", str(out_dir))

    assert result.exit_code == 0, output
    markup = _markup(_player(out_dir).read_text(encoding="utf-8"))
    assert 'src="Song.guitar.left.mp3"' in markup and 'src="Song.guitar.right.mp3"' in markup
    left, right = _button(markup, "left"), _button(markup, "right")
    assert left and "disabled" not in left
    assert right and "disabled" not in right
    assert 'id="tm-pan-note"' not in markup


def test_single_package_gets_disabled_lr_buttons_and_the_reason(tmp_path, centered):
    out_dir = _package(tmp_path)
    assert _sidecar(out_dir)["targets"][0]["pan_split"] == "single"

    result, output = _run("Song", "-o", str(out_dir))

    assert result.exit_code == 0, output
    markup = _markup(_player(out_dir).read_text(encoding="utf-8"))
    left, right = _button(markup, "left"), _button(markup, "right")
    assert left and "disabled" in left
    assert right and "disabled" in right
    assert 'id="tm-audio-left"' not in markup
    assert '<span class="tm-lr-group" title="L/R に分かれていない曲です">' in markup


def test_package_without_a_split_result_gets_no_lr_buttons(tmp_path):
    out_dir = _package(tmp_path)
    strip_pan_split(out_dir, "Song")
    _player(out_dir).write_text(_STALE, encoding="utf-8")
    assert "pan_split" not in _sidecar(out_dir)["targets"][0]

    result, output = _run("Song", "-o", str(out_dir))

    assert result.exit_code == 0, output
    markup = _markup(_player(out_dir).read_text(encoding="utf-8"))
    assert _button(markup, "target")
    assert _button(markup, "left") is None and _button(markup, "right") is None
    assert "L のみ" not in markup


def test_wav_only_package_refers_to_the_wavs(tmp_path):
    out_dir = _package(tmp_path, mp3=False)

    result, output = _run("Song", "-o", str(out_dir))

    assert result.exit_code == 0, output
    markup = _markup(_player(out_dir).read_text(encoding="utf-8"))
    assert 'src="Song.guitar.wav"' in markup and 'src="Song.guitar.backing.wav"' in markup
    assert 'src="Song.guitar.left.wav"' in markup and 'src="Song.guitar.right.wav"' in markup
    assert ".mp3" not in markup
    original = _button(markup, "original")
    assert original and "disabled" in original


def test_every_target_player_is_rewritten(tmp_path):
    out_dir = _package(tmp_path, targets=("guitar", "bass"))

    result, output = _run("Song", "-o", str(out_dir))

    assert result.exit_code == 0, output
    # In the sidecar's order, which is sorted by target.
    assert "完了: Song (ベース、ギター)" in output
    guitar = _markup(_player(out_dir, "guitar").read_text(encoding="utf-8"))
    bass = _markup(_player(out_dir, "bass").read_text(encoding="utf-8"))
    assert 'src="Song.guitar.mp3"' in guitar and _button(guitar, "left")
    assert 'src="Song.bass.mp3"' in bass and 'src="Song.bass.backing.mp3"' in bass
    assert "ベースのみ" in bass
    assert _button(bass, "left") is None


def test_unknown_targets_are_left_alone(tmp_path):
    out_dir = _package(tmp_path)
    value = _sidecar(out_dir)
    value["targets"].append({"target": "theremin", "formats": ["wav"]})
    _write_sidecar(out_dir, value)

    result, output = _run("Song", "-o", str(out_dir))

    assert result.exit_code == 0, output
    assert "完了: Song (ギター)" in output
    assert "old player" not in _player(out_dir).read_text(encoding="utf-8")
    assert not _player(out_dir, "theremin").exists()


def test_package_with_no_target_to_rewrite_is_skipped(tmp_path):
    out_dir = _package(tmp_path)
    source = _sidecar(out_dir)["source"]
    begin_target(
        _sidecar_path(out_dir),
        title="Song", safe_name="Song", digest=source["digest"],
        cache_key=source["cache_key"], target="guitar",
    )

    result, output = _run("Song", "-o", str(out_dir))

    assert result.exit_code == 0, output
    assert "スキップ: Song: 書き直せるプレイヤーがありません" in output
    assert _player(out_dir).read_text(encoding="utf-8") == _STALE


# --------------------------------------------------------------------------
# legacy, invalid, --all
# --------------------------------------------------------------------------
def test_legacy_and_invalid_packages_fail(tmp_path):
    out_dir = tmp_path / "out"
    (out_dir / "Old").mkdir(parents=True)
    broken = out_dir / "Broken"
    broken.mkdir()
    (broken / ".bunri-package.json").write_text("{not json", encoding="utf-8")

    result, output = _run("Old", "-o", str(out_dir))
    assert result.exit_code == 1
    assert "再生成が必要: Old" in output
    assert "元の入力音源から再生成してください" in output
    assert list((out_dir / "Old").iterdir()) == []

    result, output = _run("Broken", "-o", str(out_dir))
    assert result.exit_code == 1
    assert "失敗: Broken: invalid package metadata" in output

    result, output = _run("Missing", "-o", str(out_dir))
    assert result.exit_code == 1
    assert "失敗: Missing: パッケージが見つかりません" in output


def test_write_error_is_reported_as_a_failure(tmp_path):
    out_dir = _package(tmp_path)
    player = _player(out_dir)
    player.unlink()
    player.mkdir()

    result, output = _run("Song", "-o", str(out_dir))

    assert result.exit_code == 1
    assert "失敗: Song: " in output


def test_all_reports_each_package_and_a_summary(tmp_path):
    out_dir = _package(tmp_path, "A-done", seconds=0.3)
    _package(tmp_path, "B-skip", seconds=0.4)
    source = _sidecar(out_dir, "B-skip")["source"]
    begin_target(
        _sidecar_path(out_dir, "B-skip"),
        title="B-skip", safe_name="B-skip", digest=source["digest"],
        cache_key=source["cache_key"], target="guitar",
    )
    (out_dir / "C-fail").mkdir()
    (out_dir / "C-fail" / ".bunri-package.json").write_text("{not json", encoding="utf-8")
    (out_dir / "D-legacy").mkdir()

    result, output = _run("--all", "-o", str(out_dir))

    assert result.exit_code == 1
    lines = _result_lines(output)
    assert [line.split(":", 1)[0] for line in lines] == ["完了", "スキップ", "失敗", "再生成が必要"]
    assert [line.split()[1].rstrip(":") for line in lines] == [
        "A-done", "B-skip", "C-fail", "D-legacy"
    ]
    assert "集計: 完了=1 スキップ=1 失敗=1 再生成が必要=1" in output
    assert "旧パッケージは元の入力音源から再生成してください" in output
    assert "old player" not in _player(out_dir, "guitar", "A-done").read_text(encoding="utf-8")

    shutil.rmtree(out_dir / "C-fail")
    result, output = _run("--all", "-o", str(out_dir))
    assert result.exit_code == 0, output
    assert "集計: 完了=1 スキップ=1 失敗=0 再生成が必要=1" in output


def test_package_names_are_shown_without_control_characters(tmp_path):
    out_dir = tmp_path / "out"
    (out_dir / "Old\x1b[31m").mkdir(parents=True)

    result, output = _run("--all", "-o", str(out_dir))

    assert result.exit_code == 0, output
    assert "再生成が必要: Old�[31m" in output
    assert "\x1b" not in output


# --------------------------------------------------------------------------
# usage errors, dispatch
# --------------------------------------------------------------------------
@pytest.mark.parametrize("args", [["Song", "--all"], []])
def test_usage_errors(tmp_path, args):
    result, output = _run(*args, "-o", str(tmp_path))
    assert result.exit_code == 1
    assert "SAFE_NAME と --all のどちらか一方だけを指定してください。" in output


def test_dispatch_routes_player_to_its_own_app(monkeypatch):
    import sys

    import bunri.cli as cli
    import bunri.player_cli as player_cli

    calls = []
    monkeypatch.setattr(player_cli, "app", lambda **kwargs: calls.append((sys.argv[:], kwargs)))
    monkeypatch.setattr(cli, "app", lambda **kwargs: pytest.fail("main app used"))
    monkeypatch.setattr(sys, "argv", ["bunri", "player", "Song", "-o", "out"])

    cli.dispatch()

    assert calls == [(["bunri", "Song", "-o", "out"], {"prog_name": "bunri player"})]
