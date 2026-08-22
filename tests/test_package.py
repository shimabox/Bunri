"""End-to-end tests for build_package(): normalize -> separate -> export,
with a fake Separator (no real model download/inference) but real ffmpeg for
normalization and mp3 encoding.

Ported in spirit from tab-maker's tests/test_pipeline.py stem_only tests,
adapted to Bunri's single-call build_package() API (no Stage/Context, no
tab/transcription steps to sequence around).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pytest
import soundfile as sf

from bunri.package import build_package

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
    import bunri.package as package_module

    monkeypatch.setattr(package_module, "_safe_filename", lambda title: "../escaped")
    out_dir = tmp_path / "out"

    with pytest.raises(ValueError, match="outside out_dir"):
        build_package(song_input, out_dir, title="whatever")

    assert not (tmp_path / "escaped").exists()


@_NEED_FFMPEG
@pytest.mark.parametrize(
    "planted",
    ["song.guitar.player.html", "song.guitar.wav", "song.guitar.mp3", "song.original.mp3"],
)
def test_build_package_never_writes_through_a_planted_output_symlink(
    tmp_path, song_input, planted
):
    """Every name a package writes is derivable from the title, so a symlink
    can be waiting at any of them. Containment checks on the package
    *directory* say nothing about this -- the link is the final component and
    resolves wherever it likes -- and copyfile, ffmpeg and write_text all
    follow it, overwriting the target.

    So exports are written to a temporary beside the destination and renamed
    into place: `os.replace` swaps the name, leaving the link's target
    untouched.
    """
    out_dir = tmp_path / "out"
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)
    victim = outside / "precious.dat"
    victim.write_bytes(b"a file with nothing to do with this package")
    original = victim.read_bytes()

    package_dir = out_dir / "song"
    package_dir.mkdir(parents=True)
    (package_dir / planted).symlink_to(victim)

    build_package(song_input, out_dir, title="song")

    assert victim.read_bytes() == original, f"writing {planted} followed the symlink"
    produced = package_dir / planted
    assert not produced.is_symlink(), "the export must have replaced the link, not followed it"
    assert produced.stat().st_size > 0, "and the real output must still be there"


@_NEED_FFMPEG
def test_build_package_refuses_a_package_directory_that_is_a_symlink(tmp_path, song_input):
    """Containment says a link is fine as long as it points inside out_dir --
    but `out/Song -> out/victim` points at somebody else's package, and every
    export then runs through it, temporary file and rename together. A
    package folder has to be a real directory."""
    out_dir = tmp_path / "out"
    victim = out_dir / "victim"
    victim.mkdir(parents=True)
    kept = victim / "victim.guitar.player.html"
    kept.write_text("another song's package", encoding="utf-8")
    original = kept.read_bytes()

    (out_dir / "song").symlink_to(victim, target_is_directory=True)

    refused = None
    try:
        build_package(song_input, out_dir, title="song")
    except ValueError as exc:
        refused = exc

    # Damage first, so a failure says what was written rather than what was
    # not raised.
    assert sorted(p.name for p in victim.iterdir()) == [kept.name], (
        f"wrote {sorted(p.name for p in victim.iterdir())} through the link into "
        "another song's package"
    )
    assert kept.read_bytes() == original
    assert refused is not None and "symlink" in str(refused)


@_NEED_FFMPEG
def test_build_package_refuses_a_symlinked_cache_directory(tmp_path, song_input):
    """The cache layer runs before any package output, so protecting the
    package files was only half the job. `out/.cache -> outside` had
    mkdir(parents=True) follow the link and put the normalized input, the
    separated stems and every meta file out there -- none of it ever passing
    through the write-time protections, because it never got that far."""
    out_dir = tmp_path / "out"
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)
    out_dir.mkdir(parents=True)
    (out_dir / ".cache").symlink_to(outside, target_is_directory=True)

    refused = None
    try:
        build_package(song_input, out_dir, title="song")
    except OSError as exc:
        refused = exc

    # Damage first, so a failure names what was written rather than what was
    # not raised.
    assert sorted(p.name for p in outside.rglob("*")) == [], (
        f"created {sorted(p.name for p in outside.rglob('*'))} outside the output tree"
    )
    assert refused is not None and "symlink" in str(refused)


@_NEED_FFMPEG
@pytest.mark.parametrize("planted", ["normalize.meta.json", "input.wav"])
def test_build_package_never_writes_through_a_planted_cache_symlink(
    tmp_path, song_input, planted
):
    """Same rule as the package exports, applied to the cache: every name in
    there is derivable from the input digest, so a symlink can be waiting at
    one, and write_text (the meta) or ffmpeg (input.wav) would follow it."""
    from bunri.cache import file_digest

    out_dir = tmp_path / "out"
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)
    victim = outside / "victim.txt"
    victim.write_bytes(b"a file with nothing to do with this cache")
    original = victim.read_bytes()

    cache_dir = out_dir / ".cache" / file_digest(song_input)
    cache_dir.mkdir(parents=True)
    (cache_dir / planted).symlink_to(victim)

    build_package(song_input, out_dir, title="song")

    assert victim.read_bytes() == original, f"writing {planted} followed the symlink"
    produced = cache_dir / planted
    assert not produced.is_symlink(), "the write must have replaced the link, not followed it"
    assert produced.stat().st_size > 0


@_NEED_FFMPEG
@pytest.mark.parametrize("swapped", ["guitar.wav", "guitar.backing.wav", "input.wav"])
def test_build_package_does_not_trust_a_cached_artifact_that_is_a_symlink(
    tmp_path, song_input, swapped
):
    """Guarding what the cache *writes* does nothing if what it *reads* trusts
    whatever is sitting there. `exists()` follows symlinks, so leaving a valid
    meta in place and swapping a cached artifact for a link elsewhere read as
    "cached" -- and the link's target was copied into the package the user
    shares."""
    from bunri.cache import file_digest

    out_dir = tmp_path / "out"
    build_package(song_input, out_dir, title="song")
    assert len(_PackageFakeSeparator.instances) == 1

    # Real audio, so the pre-fix path runs all the way through and the leak
    # shows up as the outsider's audio inside the user's package -- rather
    # than as ffmpeg choking on a file of nonsense.
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.wav"
    sf.write(str(victim), np.full((44100, 2), 0.77, dtype=np.float32), 44100)
    original = victim.read_bytes()

    cache_dir = out_dir / ".cache" / file_digest(song_input)
    (cache_dir / swapped).unlink()
    (cache_dir / swapped).symlink_to(victim)

    build_package(song_input, out_dir, title="song")

    # The harm first: what ends up in the package the user shares.
    package_dir = out_dir / "song"
    for produced in package_dir.glob("*.wav"):
        assert produced.read_bytes() != original, (
            f"the link's target was published as {produced.name}"
        )
    assert victim.read_bytes() == original, "and the link's target must be left alone"
    assert len(_PackageFakeSeparator.instances) == 2, (
        f"a symlinked {swapped} must not count as a cached artifact"
    )


@_NEED_FFMPEG
def test_build_package_does_not_trust_a_cache_meta_that_is_a_symlink(tmp_path, song_input):
    """The meta is read for the same reason and gets the same treatment: a
    link standing where it belongs cannot be what decides a stage is fresh."""
    from bunri.cache import file_digest

    out_dir = tmp_path / "out"
    build_package(song_input, out_dir, title="song")
    assert len(_PackageFakeSeparator.instances) == 1

    cache_dir = out_dir / ".cache" / file_digest(song_input)
    meta = cache_dir / "separate:guitar.meta.json"
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_bytes(meta.read_bytes())
    meta.unlink()
    meta.symlink_to(elsewhere)

    build_package(song_input, out_dir, title="song")

    assert len(_PackageFakeSeparator.instances) == 2, (
        "a symlinked meta must not be what says the stage is fresh"
    )


@_NEED_FFMPEG
def test_build_package_reseparates_after_a_run_that_died_between_the_two_stems(
    tmp_path, song_input
):
    """separate() moves the target stem into the cache before it writes the
    backing track, so a failure between the two leaves a *new* target beside
    an *old* backing. Both exist, so the old meta called the pair fresh and
    the next run packaged one stem from each separation.

    The meta is cleared before a stage recomputes, so an interrupted run
    leaves nothing claiming to be cached."""
    from bunri.cache import file_digest

    out_dir = tmp_path / "out"
    build_package(song_input, out_dir, title="song")
    cache_dir = out_dir / ".cache" / file_digest(song_input)
    meta = cache_dir / "separate:guitar.meta.json"
    assert meta.exists()

    class _DiesAfterTargetSeparator(_PackageFakeSeparator):
        """Writes its stems, then fails the way a crash between the target
        move and the backing write does -- by leaving separate() to blow up
        after the target has already landed in the cache."""

        def separate(self, audio_file_path, custom_output_names=None):
            written = super().separate(audio_file_path, custom_output_names)
            # Remove every non-target stem: separate() then moves the target
            # into place and raises on the missing backing material.
            for name in list(written):
                if "Other" in name:
                    (self.output_dir / name).unlink()
                    written.remove(name)
            return written

    from audio_separator import separator as separator_module

    original_cls = separator_module.Separator
    separator_module.Separator = _DiesAfterTargetSeparator
    try:
        with pytest.raises(Exception):
            build_package(song_input, out_dir, title="song", no_cache=True)
    finally:
        separator_module.Separator = original_cls

    assert not meta.exists(), (
        "an interrupted stage must not leave a meta vouching for its artifacts"
    )

    before = len(_PackageFakeSeparator.instances)
    build_package(song_input, out_dir, title="song")
    assert len(_PackageFakeSeparator.instances) == before + 1, (
        "the next run must re-separate rather than package a mismatched pair"
    )
    assert (cache_dir / "guitar.backing.wav").stat().st_size > 0
