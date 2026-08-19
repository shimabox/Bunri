"""End-to-end tests for build_package(): normalize -> separate -> export,
with a fake Separator (no real model download/inference) but real ffmpeg for
normalization and mp3 encoding.

Ported in spirit from tab-maker's tests/test_pipeline.py stem_only tests,
adapted to StemLab's single-call build_package() API (no Stage/Context, no
tab/transcription steps to sequence around).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pytest
import soundfile as sf

from stemlab.package import build_package

_NEED_FFMPEG = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def _write_silence(path: Path, seconds: float = 0.3, sr: int = 44100) -> None:
    n = int(sr * seconds)
    sf.write(str(path), np.zeros((n, 2), dtype=np.float32), sr)


class _PackageFakeSeparator:
    """Writes two tiny, real WAV stems (Guitar + Other) so separate()'s real
    guitar/backing combination logic (soundfile+numpy) runs unmodified; only
    the separation model itself is faked. Deliberately independent of
    test_separate.py's FakeSeparator so the two test files stay decoupled."""

    instances: ClassVar[list["_PackageFakeSeparator"]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.output_dir = Path(kwargs["output_dir"])
        _PackageFakeSeparator.instances.append(self)

    def list_supported_model_files(self) -> dict[str, Any]:
        # Needed because the default target model (becruily) triggers the
        # catalog-injection bootstrap in separate.py; an empty catalog is a
        # valid stand-in (see test_separate.py's FakeSeparator).
        return {}

    def load_model(self, model_filename: str) -> None:
        pass

    def separate(
        self, audio_file_path: str, custom_output_names: dict[str, str] | None = None
    ) -> list[str]:
        # Honor whatever stem the caller asked to rename (Guitar for the
        # guitar target, Vocals for vocals, ...) so this fake works for any
        # TargetSpec, plus one "Other" stem for the backing track.
        target_name = next(iter((custom_output_names or {"Guitar": "mix_(Guitar)_fake"}).values()))
        written = []
        for name, value in ((target_name, 0.2), ("mix_(Other)_fake", 0.3)):
            filename = f"{name}.wav"
            samples = np.full((400, 2), value, dtype=np.float32)
            sf.write(str(self.output_dir / filename), samples, 8000)
            written.append(filename)
        return written


@pytest.fixture(autouse=True)
def _reset_instances():
    _PackageFakeSeparator.instances = []
    yield


@pytest.fixture(autouse=True)
def _fake_separator(monkeypatch):
    from audio_separator import separator as separator_module

    monkeypatch.setattr(separator_module, "Separator", _PackageFakeSeparator)


@pytest.fixture()
def song_input(tmp_path: Path) -> Path:
    src = tmp_path / "input.wav"
    _write_silence(src)
    return src


@_NEED_FFMPEG
def test_build_package_exports_expected_files_and_layout(tmp_path, song_input):
    out_dir = tmp_path / "out"

    package_dir = build_package(song_input, out_dir, title="song")

    assert package_dir == out_dir / "song"
    for name in (
        "song.guitar.wav", "song.guitar.mp3",
        "song.guitar.backing.wav", "song.guitar.backing.mp3",
        "song.original.mp3",
        "song.guitar.player.html",
    ):
        f = package_dir / name
        assert f.exists() and f.stat().st_size > 0, f"missing or empty: {f}"

    html = (package_dir / "song.guitar.player.html").read_text(encoding="utf-8")
    for src in ("song.original.mp3", "song.guitar.mp3", "song.guitar.backing.mp3"):
        assert src in html, f"player must reference {src}"
    assert "ギター" in html


@_NEED_FFMPEG
def test_build_package_skips_mp3_when_disabled(tmp_path, song_input):
    out_dir = tmp_path / "out"

    package_dir = build_package(song_input, out_dir, title="song", mp3=False)

    assert (package_dir / "song.guitar.wav").exists()
    assert (package_dir / "song.guitar.backing.wav").exists()
    for name in ("song.guitar.mp3", "song.guitar.backing.mp3", "song.original.mp3"):
        assert not (package_dir / name).exists()

    html = (package_dir / "song.guitar.player.html").read_text(encoding="utf-8")
    assert "song.guitar.wav" in html
    assert "song.guitar.backing.wav" in html
    assert "song.original.mp3" not in html


@_NEED_FFMPEG
def test_build_package_second_run_uses_cache(tmp_path, song_input):
    out_dir = tmp_path / "out"

    build_package(song_input, out_dir, title="song")
    assert len(_PackageFakeSeparator.instances) == 1

    build_package(song_input, out_dir, title="song")
    assert len(_PackageFakeSeparator.instances) == 1, "second run should hit the cache"


@_NEED_FFMPEG
def test_build_package_no_cache_forces_rerun(tmp_path, song_input):
    out_dir = tmp_path / "out"

    build_package(song_input, out_dir, title="song")
    assert len(_PackageFakeSeparator.instances) == 1

    build_package(song_input, out_dir, title="song", no_cache=True)
    assert len(_PackageFakeSeparator.instances) == 2, "no_cache should force a re-separation"


@_NEED_FFMPEG
def test_build_package_uses_input_stem_as_default_title(tmp_path):
    out_dir = tmp_path / "out"
    src = tmp_path / "my-track.wav"
    _write_silence(src)

    package_dir = build_package(src, out_dir)

    assert package_dir == out_dir / "my-track"
    assert (package_dir / "my-track.guitar.wav").exists()


@_NEED_FFMPEG
def test_build_package_sanitizes_unsafe_title_characters(tmp_path, song_input):
    out_dir = tmp_path / "out"

    package_dir = build_package(song_input, out_dir, title="a/b:c")

    assert package_dir == out_dir / "a_b_c"
    assert (package_dir / "a_b_c.guitar.wav").exists()


@_NEED_FFMPEG
def test_build_package_rejects_unknown_target(tmp_path, song_input):
    out_dir = tmp_path / "out"

    with pytest.raises(ValueError, match="unknown target"):
        build_package(song_input, out_dir, target="theremin")


class _FallbackFakeSeparator(_PackageFakeSeparator):
    """Fails load_model for the becruily default so separate() falls back to
    htdemucs; the fallback load succeeds. Exercises the post-fallback cache
    path: the meta must be keyed on the *configured* model, or every later
    run misses the cache and re-separates (the tab-maker parity bug this
    guards against)."""

    def load_model(self, model_filename: str) -> None:
        if "becruily" in model_filename:
            raise RuntimeError("simulated default-model load failure")


@_NEED_FFMPEG
def test_build_package_fallback_result_still_cached_on_second_run(
    tmp_path, song_input, monkeypatch
):
    from audio_separator import separator as separator_module

    monkeypatch.setattr(separator_module, "Separator", _FallbackFakeSeparator)
    out_dir = tmp_path / "out"

    build_package(song_input, out_dir, title="song")
    # First run: one separator for the failed default attempt, one for the
    # successful fallback.
    assert len(_PackageFakeSeparator.instances) == 2

    build_package(song_input, out_dir, title="song")
    assert len(_PackageFakeSeparator.instances) == 2, (
        "a fallback run must still be a cache hit next time; if this grew, the "
        "separate meta was keyed on the post-fallback model instead of the "
        "configured one"
    )


@_NEED_FFMPEG
def test_build_package_targets_coexist_in_cache(tmp_path, song_input):
    """Switching --target on the same song must not invalidate the other
    target's cached separation: step meta and stem files are target-scoped."""
    out_dir = tmp_path / "out"

    build_package(song_input, out_dir, title="song")  # guitar
    assert len(_PackageFakeSeparator.instances) == 1

    vocals_dir = build_package(song_input, out_dir, title="song", target="vocals")
    assert len(_PackageFakeSeparator.instances) == 2
    assert (vocals_dir / "song.vocals.wav").exists()
    html = (vocals_dir / "song.vocals.player.html").read_text(encoding="utf-8")
    assert "ボーカル" in html
    # The vocals build must have added its own files, not clobbered guitar's.
    assert (vocals_dir / "song.guitar.backing.wav").exists()
    assert (vocals_dir / "song.vocals.backing.wav").exists()
    assert (vocals_dir / "song.guitar.player.html").exists()

    build_package(song_input, out_dir, title="song")  # guitar again
    assert len(_PackageFakeSeparator.instances) == 2, (
        "returning to a previously separated target must be a cache hit"
    )


@_NEED_FFMPEG
def test_build_package_normalize_rerun_forces_reseparation(tmp_path, song_input):
    out_dir = tmp_path / "out"

    build_package(song_input, out_dir, title="song")
    assert len(_PackageFakeSeparator.instances) == 1

    # Losing input.wav makes normalize re-run; separate's own meta and stems
    # are still intact, but the force cascade must re-separate anyway because
    # its cached stems were built against an input.wav that no longer
    # provably matches.
    [cache_dir] = (out_dir / ".cache").iterdir()
    (cache_dir / "input.wav").unlink()

    build_package(song_input, out_dir, title="song")
    assert len(_PackageFakeSeparator.instances) == 2, (
        "an upstream (normalize) re-run must force re-separation"
    )


# ---------------------------------------------------------------------------
# sanitizer hardening / containment (security review fixes)
# ---------------------------------------------------------------------------
@_NEED_FFMPEG
def test_build_package_dot_only_title_falls_back_to_untitled(tmp_path, song_input):
    # "--title .." must never resolve to a package folder outside out_dir.
    out_dir = tmp_path / "out"

    package_dir = build_package(song_input, out_dir, title="..")

    assert package_dir == out_dir / "untitled"
    assert package_dir.resolve().is_relative_to(out_dir.resolve())
    assert (package_dir / "untitled.guitar.wav").exists()


@_NEED_FFMPEG
def test_build_package_web_title_is_renamed_to_web_package(tmp_path, song_input):
    # "web" is the web UI's own private out_dir subfolder; a package titled
    # "web" must not land on top of (or masquerade as) it.
    out_dir = tmp_path / "out"

    package_dir = build_package(song_input, out_dir, title="Web")

    assert package_dir == out_dir / "web-package"
    assert (package_dir / "web-package.guitar.wav").exists()


@_NEED_FFMPEG
def test_build_package_strips_hash_and_percent_from_slug_but_keeps_display_title(
    tmp_path, song_input
):
    out_dir = tmp_path / "out"

    package_dir = build_package(song_input, out_dir, title="Song #1 100%")

    assert package_dir == out_dir / "Song _1 100_"
    html = (package_dir / "Song _1 100_.guitar.player.html").read_text(encoding="utf-8")
    # The on-disk slug is sanitized, but the *displayed* title is verbatim.
    assert "Song #1 100%" in html


@_NEED_FFMPEG
def test_build_package_refuses_to_write_outside_out_dir_if_safe_filename_is_bypassed(
    tmp_path, song_input, monkeypatch
):
    """Defense in depth: even if _safe_filename's own sanitizing were somehow
    bypassed (a future regression, or some other caller), build_package must
    still refuse to write a package directory that resolves outside out_dir.
    Forced here by directly patching _safe_filename to hand back a
    traversal string, isolating this check from the sanitizer itself."""
    import stemlab.package as package_module

    monkeypatch.setattr(package_module, "_safe_filename", lambda title: "../escaped")
    out_dir = tmp_path / "out"

    with pytest.raises(ValueError, match="outside out_dir"):
        build_package(song_input, out_dir, title="whatever")

    assert not (tmp_path / "escaped").exists()
