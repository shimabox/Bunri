"""Package orchestration: normalize -> separate -> export practice package.

Ported from tab-maker's pipeline.py run_stage cache-check pattern and its
--stem-only export path, collapsed into a single build_package() call since
Bunri has no downstream (transcription/tab) stages to sequence.
"""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

from rich.console import Console

from bunri import audio, cache
from bunri.player import render_player
from bunri.package_metadata import begin_target, complete_target, read_package_metadata
from bunri.registry import get_target
from bunri.safepath import replace_into, verified_mkdir
from bunri.separate import separate

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


def resolve_title(title: str | None, input_path: Path) -> str:
    """Return the display title used throughout a generated package."""
    requested = title if title is not None and title.strip() else input_path.stem
    return requested if requested.strip() else "untitled"


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
    # "web" is Bunri's own private subdirectory (uploads/job records/logs --
    # see web/app.py's _block_private_package_paths); a package titled "web"
    # would otherwise land at out/web and either collide with it or, worse,
    # get served through the same /packages/web/... path the middleware
    # blocks, making the song unreachable. Renamed rather than rejected so a
    # song literally titled "Web" still gets a package.
    if slug.casefold() == "web":
        return "web-package"
    return slug


def _export(src: Path, dest: Path) -> Path:
    replace_into(dest, lambda tmp: shutil.copyfile(src, tmp))
    console.print(f"→ [cyan]{dest}[/cyan]")
    return dest


def _export_mp3(src: Path, dest: Path) -> Path:
    # No replace_into here: audio.encode_mp3 does its own, so that a caller
    # reaching for it directly is protected too.
    audio.encode_mp3(src, dest)
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
    # The old meta goes before the work starts, not after it succeeds: from
    # here until write_stage_meta below, this stage's artifacts are being
    # replaced, and a meta that survives an interruption vouches for a
    # half-updated set. See cache.clear_stage_meta.
    cache.clear_stage_meta(cache_dir, "normalize")
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
    song_title = resolve_title(title, input_path)
    safe = _safe_filename(song_title)

    digest = cache.input_digest(input_path)
    # The output root itself is the user's own `-o`, so creating it (symlink
    # and all, if that is what they pointed at) is doing as asked. Everything
    # below it is a different matter.
    out_dir.mkdir(parents=True, exist_ok=True)
    # `mkdir(parents=True)` here followed `out/.cache` if it was a symlink and
    # built the tree on the far side, putting the normalized input, the
    # separated stems and every meta file outside out_dir -- before a single
    # one of the write-time protections got a say. Created a component at a
    # time instead, refusing any that is a link. See bunri/safepath.py.
    #
    # This is also what keeps audio-separator's own writes in bounds: the
    # files that library creates inside the directory are not ours to route
    # through replace_into, so the guarantee they rest on is that the
    # directory they land in is genuinely inside out_dir.
    cache_dir = verified_mkdir(out_dir, ".cache", digest.cache_key)

    package_dir = out_dir / safe
    if package_dir.is_symlink():
        raise ValueError(f"refusing to write a package through a symlink: {package_dir}")
    if not package_dir.resolve().is_relative_to(out_dir.resolve()):
        raise ValueError(f"refusing to write package outside out_dir: {package_dir}")
    package_dir.mkdir(parents=True, exist_ok=True)
    sidecar = package_dir / ".bunri-package.json"
    if sidecar.exists() or sidecar.is_symlink():
        existing = read_package_metadata(sidecar, safe)
        if (
            existing.source.digest != digest.full_sha1
            or existing.source.cache_key != digest.cache_key
        ):
            raise ValueError("package directory belongs to a different input digest")
    cache.ensure_input_identity(cache_dir, digest)
    pending = begin_target(
        sidecar,
        title=song_title,
        safe_name=safe,
        digest=digest.full_sha1,
        cache_key=digest.cache_key,
        target=spec.target,
    )

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
        # Same discipline as normalize above, and this stage is where it
        # actually bit: separate() moves the target stem into the cache
        # before writing the backing track, so a failure between the two
        # leaves a new target beside an old backing. Clearing the meta first
        # means such a run is simply not cached, and the next one re-separates
        # instead of packaging the mismatched pair.
        cache.clear_stage_meta(cache_dir, separate_step)
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
    replace_into(player_dest, lambda tmp: tmp.write_text(player_html, encoding="utf-8"))
    console.print(f"→ [cyan]{player_dest}[/cyan]")

    complete_target(
        sidecar,
        expected=pending,
        target=spec.target,
        formats=("mp3", "wav") if mp3 else ("wav",),
    )

    return package_dir
