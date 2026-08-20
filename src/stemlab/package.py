"""Package orchestration: normalize -> separate -> export practice package.

Ported from tab-maker's pipeline.py run_stage cache-check pattern and its
--stem-only export path, collapsed into a single build_package() call since
StemLab has no downstream (transcription/tab) stages to sequence.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Callable

from rich.console import Console

from stemlab import audio, cache
from stemlab.player import render_player
from stemlab.registry import get_target
from stemlab.separate import separate

console = Console()

_NORMALIZE_VERSION = 1
_SEPARATE_VERSION = 1


# `#`/`%` are stripped too: left in, they'd survive into the on-disk slug and
# make the corresponding /packages/... URL ambiguous (# truncates a URL at the
# fragment, % starts a percent-escape) -- see web/app.py's URL-encoding fix and
# player.py's render_player for the other half of that story. Only the slug is
# affected; the *displayed* title (what the player's <h1> shows) keeps these
# characters verbatim.
_UNSAFE_FILENAME_CHARS = re.compile(r'[/\\:*?"<>|#%]')


def _safe_filename(title: str) -> str:
    # Leading dots are stripped after substitution (not just once) so a title
    # of ".." (or "...", "....") can never resolve to a package directory
    # outside out_dir once package_dir.mkdir() runs (".." -> "" -> "untitled",
    # same fallback as an empty/whitespace-only title). build_package() below
    # still double-checks containment with Path.is_relative_to() as defense in
    # depth, but this is what keeps a normal title from ever needing it.
    slug = _UNSAFE_FILENAME_CHARS.sub("_", title).strip().lstrip(".")
    if not slug:
        return "untitled"
    # "web" is StemLab's own private subdirectory (uploads/job records/logs --
    # see web/app.py's _block_private_package_paths); a package titled "web"
    # would otherwise land at out/web and either collide with it or, worse,
    # get served through the same /packages/web/... path the middleware
    # blocks, making the song unreachable. Renamed rather than rejected so a
    # song literally titled "Web" still gets a package.
    if slug.casefold() == "web":
        return "web-package"
    return slug


def _replace_into(dest: Path, write: Callable[[Path], None]) -> Path:
    """Produce `dest` by writing a uniquely-named temporary beside it and
    renaming that into place.

    Every export goes through this, and the reason is symlinks. Writing
    straight to `dest` -- copyfile, ffmpeg's output file, write_text -- all
    follow a link sitting there and clobber whatever it points at, which for
    a package path is a name an attacker (or a careless `ln -s`) can predict
    exactly. `os.replace` does not follow: it swaps the *name*, so a planted
    link is what gets replaced, not its target. Containment checks on the
    package directory cannot help here, because the link is the final
    component and resolves wherever it likes.

    Atomicity comes free with it -- a reader never sees a half-copied export
    -- and it is the same write-then-rename shape the model download and the
    job records already use.

    The temporary keeps `dest`'s suffix: ffmpeg picks its muxer from the
    output extension, so ".../x.tmp-ab12" would leave it guessing.
    """
    tmp = dest.with_name(f".{dest.stem}.tmp-{secrets.token_hex(4)}{dest.suffix}")
    try:
        write(tmp)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)  # no-op after a successful replace
    return dest


def _export(src: Path, dest: Path) -> Path:
    _replace_into(dest, lambda tmp: shutil.copyfile(src, tmp))
    console.print(f"→ [cyan]{dest}[/cyan]")
    return dest


def _export_mp3(src: Path, dest: Path) -> Path:
    _replace_into(dest, lambda tmp: audio.encode_mp3(src, tmp))
    console.print(f"→ [cyan]{dest}[/cyan]")
    return dest


def _normalize_step(
    input_path: Path, input_wav: Path, cache_dir: Path, *, no_cache: bool
) -> bool:
    """Returns True if normalization actually ran (vs. served from cache), so
    the caller can force downstream steps -- same cascade tab-maker's
    pipeline.run used: a re-run upstream means downstream caches were built
    against outputs that may no longer match what's on disk."""
    params = {"sample_rate": 44100, "channels": 2}
    outputs = [input_wav]
    if not no_cache and cache.stage_is_fresh(
        cache_dir, "normalize", _NORMALIZE_VERSION, params, outputs
    ):
        console.print("[dim]∙ normalize: cached[/dim]")
        return False
    with console.status("[bold]normalize[/bold] running…"):
        audio.normalize_to_wav(
            input_path,
            input_wav,
            sample_rate=params["sample_rate"],
            channels=params["channels"],
        )
    cache.write_stage_meta(cache_dir, "normalize", _NORMALIZE_VERSION, params)
    console.print("[green]✓[/green] normalize")
    return True


def build_package(
    input_path: Path,
    out_dir: Path,
    *,
    target: str = "guitar",
    model: str | None = None,
    title: str | None = None,
    device: str = "auto",
    mp3: bool = True,
    no_cache: bool = False,
) -> Path:
    """Build a practice package for input_path in out_dir/<safe_title>/:
    the target instrument alone, a "backing" track (everything else), the
    original mix, and an offline HTML player -- wav always, mp3 additionally
    when mp3=True.

    Intermediate artifacts (normalized input, separated stems) are cached
    under out_dir/.cache/<input-digest>/ and reused across runs unless
    no_cache is set.

    Returns the generated song folder (out_dir/<safe_title>).
    """
    spec = get_target(target)
    song_title = title if title is not None else input_path.stem
    safe = _safe_filename(song_title)

    digest = cache.file_digest(input_path)
    cache_dir = out_dir / ".cache" / digest
    cache_dir.mkdir(parents=True, exist_ok=True)

    input_wav = cache_dir / "input.wav"
    normalize_ran = _normalize_step(input_path, input_wav, cache_dir, no_cache=no_cache)

    # device is deliberately not a cache key here: it selects speed, not
    # semantics, and Demucs output is nondeterministic across runs anyway
    # (random shift augmentation) -- re-separating on a device switch would
    # burn minutes for no meaningful difference. Same reasoning tab-maker's
    # SeparateStage.params used.
    resolved_model = model if model is not None else spec.default_model
    separate_params = {
        "model": resolved_model,
        "target": spec.target,
        "stems": [spec.target, "backing"],
    }
    # Step name and stem files are target-scoped so different --target runs of
    # the same song coexist in one cache dir instead of invalidating each
    # other (a shared "separate" meta would flip-flop on every target switch,
    # re-running a minutes-long separation each time).
    separate_step = f"separate:{spec.target}"
    target_wav = cache_dir / f"{spec.target}.wav"
    backing_wav = cache_dir / f"{spec.target}.backing.wav"
    outputs = [target_wav, backing_wav]

    # normalize_ran forces a re-separation: if the upstream step re-ran, this
    # step's cached stems were built against an input.wav that may no longer
    # match what's on disk (tab-maker pipeline.run's force cascade).
    if not no_cache and not normalize_ran and cache.stage_is_fresh(
        cache_dir, separate_step, _SEPARATE_VERSION, separate_params, outputs
    ):
        console.print("[dim]∙ separate: cached[/dim]")
    else:
        # Separation can run for minutes with no other output. Print the model
        # name up front -- outside console.status's own live region, so it
        # can't clash with the spinner -- so something visibly happens even
        # without --verbose.
        console.print(
            f"[dim]  model {resolved_model!r} — this can take several minutes[/dim]"
        )
        start = time.perf_counter()
        with console.status("[bold]separate[/bold] running…"):
            result = separate(input_wav, cache_dir, spec=spec, model=model, device=device)
        elapsed = time.perf_counter() - start
        # The meta's cache key must use the *configured* model (the same
        # separate_params the freshness check above compares against): keying
        # on the post-fallback model would make every later run's check miss
        # and re-separate forever. Which model actually produced the stems
        # (after a possible fallback) is still worth keeping, so it goes in
        # the digest-exempt extra field.
        cache.write_stage_meta(
            cache_dir,
            separate_step,
            _SEPARATE_VERSION,
            separate_params,
            extra={"model_used": result.model_used},
        )
        console.print(f"[green]✓[/green] separate ({elapsed:.0f}s)")

    package_dir = out_dir / safe
    # Defense in depth on top of _safe_filename's own sanitizing: even if a
    # future change to that function (or a caller bypassing it) let a
    # path-separator-bearing title through, this refuses to write outside
    # out_dir rather than trusting the string ever looked safe.
    # is_relative_to() on the *resolved* paths (not a string-prefix compare)
    # so a `..`-bearing or symlinked component can't slip past the check.
    # A package folder is a real directory, never a link. Containment alone
    # does not give that: `out/Song -> out/victim` resolves inside out_dir
    # and passes the check below, and then every export -- temp file and
    # os.replace alike -- runs through the link and lands in somebody else's
    # package. lstat, so the test does not follow what it is testing; and
    # before mkdir, which would otherwise report success for the target.
    if package_dir.is_symlink():
        raise ValueError(f"refusing to write a package through a symlink: {package_dir}")
    if not package_dir.resolve().is_relative_to(out_dir.resolve()):
        raise ValueError(f"refusing to write package outside out_dir: {package_dir}")
    package_dir.mkdir(parents=True, exist_ok=True)

    # Backing and player are target-scoped like the stem itself: guitar's
    # backing (has vocals) and vocals' backing (karaoke) are different mixes,
    # so building a second target for the same song must add files to the
    # folder, not silently overwrite the first target's. Only original.mp3 is
    # shared -- it's the same audio whichever target produced it.
    _export(target_wav, package_dir / f"{safe}.{spec.target}.wav")
    _export(backing_wav, package_dir / f"{safe}.{spec.target}.backing.wav")

    if mp3:
        target_ref = f"{safe}.{spec.target}.mp3"
        backing_ref = f"{safe}.{spec.target}.backing.mp3"
        original_ref: str | None = f"{safe}.original.mp3"
        _export_mp3(target_wav, package_dir / target_ref)
        _export_mp3(backing_wav, package_dir / backing_ref)
        _export_mp3(input_wav, package_dir / original_ref)
    else:
        target_ref = f"{safe}.{spec.target}.wav"
        backing_ref = f"{safe}.{spec.target}.backing.wav"
        original_ref = None

    player_dest = package_dir / f"{safe}.{spec.target}.player.html"
    player_html = render_player(
        song_title,
        original=original_ref,
        target=target_ref,
        backing=backing_ref,
        instrument_label=spec.label_ja,
    )
    _replace_into(player_dest, lambda tmp: tmp.write_text(player_html, encoding="utf-8"))
    console.print(f"→ [cyan]{player_dest}[/cyan]")

    return package_dir
