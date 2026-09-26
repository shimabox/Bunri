"""`bunri lr-split`: adding the L/R split to existing packages from the
separated stem in the cache, driven through the Typer app."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from typer.testing import CliRunner

from bunri import package
from bunri.lr_split_cli import app
from bunri.package import build_package
from pan_split_helpers import FakeSeparator, install_fake_separator, strip_pan_split

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

_ENV = {"NO_COLOR": "1", "FORCE_COLOR": None, "TERM": "dumb", "COLUMNS": "200"}


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


def _existing_package(
    tmp_path: Path, title: str = "Song", *, mp3: bool = True, seconds: float = 0.3
) -> Path:
    """A package as a Bunri without the L/R split left it; the input audio is
    gone, so only the cache can supply the stem."""
    out_dir = tmp_path / "out"
    src = _input(tmp_path, seconds)
    build_package(src, out_dir, title=title, mp3=mp3)
    strip_pan_split(out_dir, title)
    src.unlink()
    return out_dir


def _run(*args: str):
    result = CliRunner().invoke(app, list(args), env=_ENV)
    return result, re.sub(r"[ \t]+", " ", result.output)


def _sidecar(out_dir: Path, name: str = "Song") -> dict:
    return json.loads((out_dir / name / ".bunri-package.json").read_text(encoding="utf-8"))


def _cache_dir(out_dir: Path, name: str = "Song") -> Path:
    return out_dir / ".cache" / _sidecar(out_dir, name)["source"]["cache_key"]


def _snapshot(directory: Path) -> dict[str, tuple[bytes, int]]:
    return {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in directory.iterdir()
        if p.is_file()
    }


def _lr_files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if ".left." in p.name or ".right." in p.name)


def _guitar(sidecar: dict) -> dict:
    return next(item for item in sidecar["targets"] if item["target"] == "guitar")


# --------------------------------------------------------------------------
# adding the split
# --------------------------------------------------------------------------
def test_adds_lr_to_an_existing_package_without_the_input(tmp_path):
    out_dir = tmp_path / "out"
    src = _input(tmp_path)
    build_package(src, out_dir, title="Song")
    build_package(src, out_dir, title="Song", target="bass")
    strip_pan_split(out_dir, "Song")
    src.unlink()
    before = _sidecar(out_dir)
    player = out_dir / "Song" / "Song.guitar.player.html"
    assert "Song.guitar.left.mp3" not in player.read_text(encoding="utf-8")

    result, output = _run("Song", "-o", str(out_dir))

    assert result.exit_code == 0, output
    assert "完了: Song (境界 " in output
    package_dir = out_dir / "Song"
    for side in ("left", "right"):
        for audio_format in ("wav", "mp3"):
            assert (package_dir / f"Song.guitar.{side}.{audio_format}").stat().st_size > 0
    after = _sidecar(out_dir)
    assert _guitar(after) == {"target": "guitar", "formats": ["mp3", "wav"], "pan_split": "left_right"}
    assert [x for x in after["targets"] if x["target"] != "guitar"] == [
        x for x in before["targets"] if x["target"] != "guitar"
    ]
    assert (after["title"], after["source"]) == (before["title"], before["source"])
    html = player.read_text(encoding="utf-8")
    assert 'src="Song.guitar.left.mp3"' in html and 'src="Song.guitar.right.mp3"' in html


def test_center_stem_records_single(tmp_path, centered):
    out_dir = _existing_package(tmp_path)

    result, output = _run("Song", "-o", str(out_dir))

    assert result.exit_code == 0, output
    assert "完了: Song (L/R に分かれていない曲)" in output
    assert _guitar(_sidecar(out_dir))["pan_split"] == "single"
    assert _lr_files(out_dir / "Song") == []
    html = (out_dir / "Song" / "Song.guitar.player.html").read_text(encoding="utf-8")
    assert "L/R に分かれていない曲です" in html


def test_wav_only_package_gets_wav_only_lr(tmp_path):
    out_dir = _existing_package(tmp_path, mp3=False)

    result, output = _run("Song", "-o", str(out_dir))

    assert result.exit_code == 0, output
    assert _lr_files(out_dir / "Song") == ["Song.guitar.left.wav", "Song.guitar.right.wav"]
    html = (out_dir / "Song" / "Song.guitar.player.html").read_text(encoding="utf-8")
    assert 'src="Song.guitar.left.wav"' in html and 'src="Song.guitar.right.wav"' in html


# --------------------------------------------------------------------------
# preconditions
# --------------------------------------------------------------------------
def test_missing_cache_directory_fails_without_touching_the_package(tmp_path):
    out_dir = _existing_package(tmp_path)
    cache_dir = _cache_dir(out_dir)
    shutil.rmtree(cache_dir)
    before = _snapshot(out_dir / "Song")

    result, output = _run("Song", "-o", str(out_dir))

    assert result.exit_code == 1
    assert "キャッシュがありません" in output and "元の入力音源から再生成してください" in output
    assert _snapshot(out_dir / "Song") == before
    assert not cache_dir.exists()


@pytest.mark.parametrize("removed", ["guitar.wav", "separate:guitar.meta.json"])
def test_missing_cached_stem_fails(tmp_path, removed):
    out_dir = _existing_package(tmp_path)
    (_cache_dir(out_dir) / removed).unlink()
    before = _sidecar(out_dir)

    result, output = _run("Song", "-o", str(out_dir))

    assert result.exit_code == 1
    assert "分離済み stem がキャッシュにありません" in output
    assert _sidecar(out_dir) == before


def test_package_stem_differing_from_the_cache_fails_until_it_is_removed(tmp_path):
    out_dir = _existing_package(tmp_path)
    package_stem = out_dir / "Song" / "Song.guitar.wav"
    sf.write(str(package_stem), np.full((100, 2), 0.1, dtype=np.float32), 8000)

    result, output = _run("Song", "-o", str(out_dir))

    assert result.exit_code == 1
    assert "一致しません" in output
    assert _lr_files(out_dir / "Song") == []

    package_stem.unlink()
    result, output = _run("Song", "-o", str(out_dir))
    assert result.exit_code == 0, output
    assert _guitar(_sidecar(out_dir))["pan_split"] == "left_right"


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

    result, output = _run("Broken", "-o", str(out_dir))
    assert result.exit_code == 1
    assert "失敗: Broken: invalid package metadata" in output


# --------------------------------------------------------------------------
# recorded results, --force
# --------------------------------------------------------------------------
@pytest.mark.parametrize("separator", [FakeSeparator, _CenterSeparator])
def test_recorded_result_is_skipped_until_forced(tmp_path, monkeypatch, separator):
    install_fake_separator(monkeypatch, separator)
    out_dir = tmp_path / "out"
    build_package(_input(tmp_path), out_dir, title="Song")
    recorded = _guitar(_sidecar(out_dir))["pan_split"]
    before = _snapshot(out_dir / "Song")

    result, output = _run("Song", "-o", str(out_dir))

    assert result.exit_code == 0, output
    assert f"スキップ: Song: 記録済み ({recorded})" in output
    assert _snapshot(out_dir / "Song") == before

    if recorded == "left_right":
        (out_dir / "Song" / "Song.guitar.left.wav").unlink()
    result, output = _run("Song", "-o", str(out_dir), "--force")
    assert result.exit_code == 0, output
    assert "完了: Song" in output
    if recorded == "left_right":
        assert (out_dir / "Song" / "Song.guitar.left.wav").stat().st_size > 0


def test_all_reports_each_package_and_a_summary(tmp_path):
    out_dir = _existing_package(tmp_path, "A-done", seconds=0.3)
    build_package(_input(tmp_path, 0.4), out_dir, title="B-skip")
    _existing_package(tmp_path, "C-fail", seconds=0.5)
    shutil.rmtree(_cache_dir(out_dir, "C-fail"))
    (out_dir / "D-legacy").mkdir()

    result, output = _run("--all", "-o", str(out_dir))

    assert result.exit_code == 1
    lines = [
        line for line in output.splitlines()
        if line.startswith(("完了:", "スキップ:", "失敗:", "再生成が必要:"))
    ]
    assert [line.split(":", 1)[0] for line in lines] == ["完了", "スキップ", "失敗", "再生成が必要"]
    assert [line.split()[1].rstrip(":") for line in lines] == [
        "A-done", "B-skip", "C-fail", "D-legacy"
    ]
    assert "集計: 完了=1 スキップ=1 失敗=1 再生成が必要=1" in output
    assert "旧パッケージは元の入力音源から再生成してください" in output

    shutil.rmtree(out_dir / "C-fail")
    result, output = _run("--all", "-o", str(out_dir))
    assert result.exit_code == 0, output
    assert "集計: 完了=0 スキップ=2 失敗=0 再生成が必要=1" in output


# --------------------------------------------------------------------------
# usage errors, lock
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["Song", "--all"], "SAFE_NAME と --all のどちらか一方だけを指定してください。"),
        ([], "SAFE_NAME と --all のどちらか一方だけを指定してください。"),
        (["Song", "--target", "bass"], "この target は L/R 分割に対応していません"),
        (["Song", "--target", "theremin"], "この target は L/R 分割に対応していません"),
    ],
)
def test_usage_errors(tmp_path, args, message):
    result, output = _run(*args, "-o", str(tmp_path))
    assert result.exit_code == 1
    assert message in output


def test_a_second_run_is_refused_while_the_lock_is_held(tmp_path):
    from bunri.lock import ProcessLock

    out_dir = _existing_package(tmp_path)
    held = ProcessLock(out_dir / ".cache" / "pan_split.lock", "held").acquire()
    try:
        result, output = _run("Song", "-o", str(out_dir))
    finally:
        held.release()

    assert result.exit_code == 1
    assert "別の lr-split が実行中です。完了後に再実行してください。" in output
    assert "pan_split" not in _guitar(_sidecar(out_dir))


# --------------------------------------------------------------------------
# the target being absent or regenerated
# --------------------------------------------------------------------------
_ABSENT = "スキップ: Song: guitar の項目がありません(guitar のパッケージでないか、分離ジョブの実行中)"


def test_package_without_the_target_is_skipped(tmp_path):
    out_dir = tmp_path / "out"
    src = _input(tmp_path)
    build_package(src, out_dir, title="Song", target="bass")
    before = _snapshot(out_dir / "Song")

    result, output = _run("Song", "-o", str(out_dir))

    assert result.exit_code == 0, output
    assert _ABSENT in output
    assert _snapshot(out_dir / "Song") == before


def test_target_being_regenerated_at_start_is_skipped(tmp_path):
    from bunri.package_metadata import begin_target

    out_dir = _existing_package(tmp_path)
    source = _sidecar(out_dir)["source"]
    begin_target(
        out_dir / "Song" / ".bunri-package.json",
        title="Song", safe_name="Song", digest=source["digest"],
        cache_key=source["cache_key"], target="guitar",
    )
    before = _snapshot(out_dir / "Song")

    result, output = _run("Song", "-o", str(out_dir))

    assert result.exit_code == 0, output
    assert _ABSENT in output
    assert _snapshot(out_dir / "Song") == before


def test_target_regenerated_after_the_split_started_fails(tmp_path, monkeypatch):
    from bunri.package_metadata import begin_target

    out_dir = _existing_package(tmp_path)
    sidecar = out_dir / "Song" / ".bunri-package.json"
    source = _sidecar(out_dir)["source"]
    original = package._pan_split_step
    calls = []

    def split_then_regeneration_starts(*args, **kwargs):
        decision = original(*args, **kwargs)
        if not calls:
            calls.append(1)
            begin_target(
                sidecar, title="Song", safe_name="Song", digest=source["digest"],
                cache_key=source["cache_key"], target="guitar",
            )
        return decision

    monkeypatch.setattr(package, "_pan_split_step", split_then_regeneration_starts)
    result, output = _run("Song", "-o", str(out_dir))

    assert result.exit_code == 1
    assert "guitar の項目が消えました" in output
    assert "完了後に再実行してください" in output
    assert _sidecar(out_dir)["targets"] == []


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------
def test_dispatch_routes_lr_split_to_its_own_app(monkeypatch):
    import sys

    import bunri.cli as cli
    import bunri.lr_split_cli as lr_split_cli

    calls = []
    monkeypatch.setattr(lr_split_cli, "app", lambda **kwargs: calls.append((sys.argv[:], kwargs)))
    monkeypatch.setattr(cli, "app", lambda **kwargs: pytest.fail("main app used"))
    monkeypatch.setattr(sys, "argv", ["bunri", "lr-split", "Song", "-o", "out"])

    cli.dispatch()

    assert calls == [(["bunri", "Song", "-o", "out"], {"prog_name": "bunri lr-split"})]
