"""Package orchestration: normalize -> separate -> pan split -> export
practice package.

Ported from tab-maker's pipeline.py run_stage cache-check pattern and its
--stem-only export path, collapsed into a single build_package() call since
Bunri has no downstream (transcription/tab) stages to sequence.

add_pan_split() adds the L/R split to a package built before it existed,
from the separated stem still in the cache, sharing the same cache stage and
export steps as build_package().
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rich.console import Console

from bunri import audio, cache, pan_split
from bunri.local_package import inspect_package_identity
from bunri.lock import ProcessLockBusy
from bunri.pan_split import LEFT_RIGHT, PanSplitDecision, pan_label, split_stem
from bunri.player import render_player
from bunri.package_metadata import (
    TargetNotFoundError,
    begin_target,
    complete_target,
    read_package_metadata,
    set_target_pan_split,
)
from bunri.registry import TargetSpec, get_target
from bunri.safepath import (
    is_real_file_in,
    is_really,
    real_subdir,
    replace_into,
    verified_mkdir,
)
from bunri.separate import separate

console = Console()

_NORMALIZE_VERSION = 1
_SEPARATE_VERSION = 1
_PAN_SPLIT_VERSION = 1

PAN_SPLIT_SINGLE_NOTE = "L/R に分かれていない曲です"
LEGACY_MESSAGE = (
    "身元ファイルがありません。元の入力音源から再生成してください。"
    "キャッシュが残っていれば分離処理は省略されます。"
)


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


def _export_mp3(src: Path, dest: Path, *, title: str) -> Path:
    # No replace_into here: audio.encode_mp3 does its own, so that a caller
    # reaching for it directly is protected too.
    audio.encode_mp3(src, dest, title=title)
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


def _pan_split_params(target: str) -> dict[str, object]:
    return {
        "target": target,
        "n_fft": pan_split.N_FFT,
        "hop": pan_split.HOP,
        "pad_mode": pan_split.PAD_MODE,
        "bins": pan_split.BINS,
        "smooth": pan_split.SMOOTH,
        "min_peak_distance": pan_split.MIN_PEAK_DISTANCE,
        "softness": pan_split.SOFTNESS,
        "min_share": pan_split.MIN_SHARE,
    }


def describe_pan_split(decision: PanSplitDecision) -> str:
    """"境界 L12、L/R 45%/55%" for a split, "L/R に分かれていない曲" otherwise."""
    if decision.status != LEFT_RIGHT:
        return "L/R に分かれていない曲"
    assert decision.boundary_angle is not None and decision.left_share is not None
    left = round(decision.left_share * 100)
    return f"境界 {pan_label(decision.boundary_angle)}、L/R {left}%/{100 - left}%"


def _read_cached_decision(path: Path) -> PanSplitDecision | None:
    try:
        return PanSplitDecision.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _pan_split_step(
    cache_dir: Path, spec: TargetSpec, *, force: bool, upstream_ran: bool
) -> PanSplitDecision:
    """Cache stage `pan_split:<target>`: decide on the cached stem and, for a
    split, write <target>.left.wav / <target>.right.wav beside it.

    The decision json is always written and is the stage's declared output;
    the two wavs are required on top of it only when the json says they
    exist. `force` (no_cache, `lr-split --force`) and `upstream_ran` (the
    stem was just re-separated) recompute regardless.
    """
    stage = f"pan_split:{spec.target}"
    params = _pan_split_params(spec.target)
    result_json = cache_dir / f"{spec.target}.pan_split.json"
    left_wav = cache_dir / f"{spec.target}.left.wav"
    right_wav = cache_dir / f"{spec.target}.right.wav"
    if not force and not upstream_ran and cache.stage_is_fresh(
        cache_dir, stage, _PAN_SPLIT_VERSION, params, [result_json]
    ):
        decision = _read_cached_decision(result_json)
        expected_dir = cache_dir.resolve()
        if decision is not None and (
            decision.status != LEFT_RIGHT
            or (
                is_real_file_in(left_wav, expected_dir)
                and is_real_file_in(right_wav, expected_dir)
            )
        ):
            console.print("[dim]∙ pan_split: cached[/dim]")
            return decision
    # Same discipline as the other stages: no meta while the outputs are
    # being replaced (see cache.clear_stage_meta).
    cache.clear_stage_meta(cache_dir, stage)
    start = time.perf_counter()
    with console.status("[bold]pan_split[/bold] running…"):
        decision = split_stem(cache_dir / f"{spec.target}.wav", left_wav, right_wav)
    if decision.status != LEFT_RIGHT:
        left_wav.unlink(missing_ok=True)
        right_wav.unlink(missing_ok=True)
    payload = json.dumps(decision.to_json(), ensure_ascii=False) + "\n"
    replace_into(result_json, lambda tmp: tmp.write_text(payload, encoding="utf-8"))
    cache.write_stage_meta(cache_dir, stage, _PAN_SPLIT_VERSION, params)
    elapsed = time.perf_counter() - start
    detail = describe_pan_split(decision) if decision.status == LEFT_RIGHT else "山が 1 つ"
    console.print(f"[green]✓[/green] pan_split ({elapsed:.0f}s, {detail})")
    return decision


def _pan_split_names(safe: str, spec: TargetSpec) -> tuple[str, ...]:
    return tuple(
        f"{safe}.{spec.target}.{side}.{audio_format}"
        for side in ("left", "right")
        for audio_format in ("wav", "mp3")
    )


def _export_pan_split(
    package_dir: Path,
    safe: str,
    spec: TargetSpec,
    cache_dir: Path,
    decision: PanSplitDecision,
    *,
    mp3: bool,
    song_title: str,
) -> tuple[str | None, str | None, str | None]:
    """Publish a split into the package, or clear out a stale one.

    Returns the player's (left, right, note): the two file names for a
    split, or the reason text for a stem that has no split.
    """
    if decision.status != LEFT_RIGHT:
        # A package made by an earlier run can still hold L/R files that the
        # sidecar no longer vouches for. unlink removes the name, so a
        # symlink sitting there is removed rather than followed.
        for name in _pan_split_names(safe, spec):
            (package_dir / name).unlink(missing_ok=True)
        return None, None, PAN_SPLIT_SINGLE_NOTE
    refs: list[str] = []
    for side, label in (("left", "L のみ"), ("right", "R のみ")):
        wav_name = f"{safe}.{spec.target}.{side}.wav"
        src = cache_dir / f"{spec.target}.{side}.wav"
        _export(src, package_dir / wav_name)
        if mp3:
            mp3_name = f"{safe}.{spec.target}.{side}.mp3"
            _export_mp3(
                src,
                package_dir / mp3_name,
                title=f"{song_title} ({spec.label_ja} {label})",
            )
            refs.append(mp3_name)
        else:
            refs.append(wav_name)
    return refs[0], refs[1], None


def _player_refs(safe: str, spec: TargetSpec, mp3: bool) -> tuple[str | None, str, str]:
    """The player's (original, target, backing) file names: the mp3s when
    they were written, else the wavs (and no original, which is mp3 only)."""
    if mp3:
        return (
            f"{safe}.original.mp3",
            f"{safe}.{spec.target}.mp3",
            f"{safe}.{spec.target}.backing.mp3",
        )
    return None, f"{safe}.{spec.target}.wav", f"{safe}.{spec.target}.backing.wav"


def _write_player(
    package_dir: Path,
    safe: str,
    spec: TargetSpec,
    song_title: str,
    *,
    mp3: bool,
    left: str | None,
    right: str | None,
    note: str | None,
) -> Path:
    original_ref, target_ref, backing_ref = _player_refs(safe, spec, mp3)
    player_dest = package_dir / f"{safe}.{spec.target}.player.html"
    player_html = render_player(
        song_title,
        original=original_ref,
        target=target_ref,
        backing=backing_ref,
        instrument_label=spec.label_ja,
        left=left,
        right=right,
        pan_split_note=note,
    )
    replace_into(player_dest, lambda tmp: tmp.write_text(player_html, encoding="utf-8"))
    console.print(f"→ [cyan]{player_dest}[/cyan]")
    return player_dest


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
    separate_ran = True
    if not no_cache and not normalize_ran and cache.stage_is_fresh(
        cache_dir, separate_step, _SEPARATE_VERSION, separate_params, outputs
    ):
        console.print("[dim]∙ separate: cached[/dim]")
        separate_ran = False
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

    # A re-separated stem forces a new split, the same cascade as normalize
    # -> separate above.
    decision = (
        _pan_split_step(cache_dir, spec, force=no_cache, upstream_ran=separate_ran)
        if spec.pan_split
        else None
    )

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
        original_ref, target_ref, backing_ref = _player_refs(safe, spec, mp3)
        assert original_ref is not None
        _export_mp3(
            target_wav,
            package_dir / target_ref,
            title=f"{song_title} ({spec.label_ja}のみ)",
        )
        _export_mp3(
            backing_wav,
            package_dir / backing_ref,
            title=f"{song_title} ({spec.label_ja}なし)",
        )
        _export_mp3(input_wav, package_dir / original_ref, title=song_title)

    # begin_target above took this target out of the sidecar, so nothing
    # links to the L/R files (or their absence) until complete_target below.
    left = right = note = None
    if decision is not None:
        left, right, note = _export_pan_split(
            package_dir, safe, spec, cache_dir, decision, mp3=mp3, song_title=song_title
        )
    _write_player(
        package_dir, safe, spec, song_title, mp3=mp3, left=left, right=right, note=note
    )

    complete_target(
        sidecar,
        expected=pending,
        target=spec.target,
        formats=("mp3", "wav") if mp3 else ("wav",),
        pan_split=decision.status if decision is not None else None,
    )

    return package_dir


@dataclass(frozen=True)
class PanSplitOutcome:
    status: Literal["done", "skipped", "legacy", "failed"]
    reason: str | None = None
    decision: PanSplitDecision | None = None


def _same_file_content(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False

    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    return sha256(left) == sha256(right)


def add_pan_split(
    out_dir: Path, safe_name: str, *, target: str = "guitar", force: bool = False
) -> PanSplitOutcome:
    """Add the L/R split to an existing package, from its cached stem.

    No input audio is needed: the separated stem in out/.cache/<key>/ is the
    input, the same one build_package() splits. A package whose sidecar
    already records a split result is skipped unless `force`.

    Expected problems (no cache, a stem that does not match, the target being
    regenerated meanwhile) come back as a "failed" outcome; anything else
    propagates.
    """
    spec = get_target(target)
    if not spec.pan_split:
        return PanSplitOutcome("failed", "この target は L/R 分割に対応していません")
    identity = inspect_package_identity(out_dir, safe_name)
    if identity.state == "legacy":
        return PanSplitOutcome("legacy", LEGACY_MESSAGE)
    if identity.state != "ready" or identity.metadata is None:
        reason = identity.issues[0] if identity.issues else "身元ファイルを読み取れません"
        return PanSplitOutcome("failed", reason)
    metadata = identity.metadata
    item = next((x for x in metadata.targets if x.target == spec.target), None)
    if item is None:
        return PanSplitOutcome(
            "skipped",
            f"{spec.target} の項目がありません({spec.target} のパッケージでないか、"
            "分離ジョブの実行中)",
        )
    if item.pan_split is not None and not force:
        return PanSplitOutcome("skipped", f"記録済み ({item.pan_split})")

    key = metadata.source.cache_key
    cache_dir = out_dir / ".cache" / key
    if not (
        not cache_dir.is_symlink()
        and cache_dir.is_dir()
        and is_really(cache_dir, real_subdir(out_dir, ".cache", key))
    ):
        return PanSplitOutcome(
            "failed",
            f"キャッシュがありません: {cache_dir}。元の入力音源から再生成してください。",
        )
    try:
        cache.ensure_input_identity(
            cache_dir, cache.InputDigest(metadata.source.digest, key)
        )
    except ValueError:
        return PanSplitOutcome("failed", "キャッシュが別の入力のものです")
    stem = cache_dir / f"{spec.target}.wav"
    if not cache.stage_completed(cache_dir, f"separate:{spec.target}", [stem]):
        return PanSplitOutcome(
            "failed",
            "分離済み stem がキャッシュにありません。元の入力音源から再生成してください。",
        )
    # File names follow the directory, as the rest of the package's files do
    # (see local_package.inspect_package_artifacts); the sidecar is addressed
    # by the name it declares.
    safe = identity.name
    package_dir = identity.directory
    # The package's own copy of the stem, when it is still there, must be the
    # one the split is made from: another package sharing this cache may have
    # re-separated it with a different model since. The copy may have been
    # deleted (the README says the wavs can go), and then the cache is it.
    package_stem = package_dir / f"{safe}.{spec.target}.wav"
    if is_real_file_in(package_stem, package_dir.resolve()) and not _same_file_content(
        package_stem, stem
    ):
        return PanSplitOutcome(
            "failed",
            "パッケージの stem とキャッシュが一致しません。元の入力音源から再生成してください。",
        )

    mp3 = "mp3" in item.formats
    sidecar = package_dir / ".bunri-package.json"
    try:
        decision = _pan_split_step(cache_dir, spec, force=force, upstream_ran=False)
        # Ordered so the web UI, which only lists L/R files the sidecar says
        # exist, never links to a file that is missing: for a split the
        # files and player land first and the sidecar last; for no split the
        # sidecar stops vouching first, then the old files go.
        if decision.status == LEFT_RIGHT:
            left, right, note = _export_pan_split(
                package_dir, safe, spec, cache_dir, decision,
                mp3=mp3, song_title=metadata.title,
            )
            _write_player(
                package_dir, safe, spec, metadata.title,
                mp3=mp3, left=left, right=right, note=note,
            )
            set_target_pan_split(
                sidecar, safe_name=metadata.safe_name, target=spec.target,
                pan_split=decision.status,
            )
        else:
            set_target_pan_split(
                sidecar, safe_name=metadata.safe_name, target=spec.target,
                pan_split=decision.status,
            )
            left, right, note = _export_pan_split(
                package_dir, safe, spec, cache_dir, decision,
                mp3=mp3, song_title=metadata.title,
            )
            _write_player(
                package_dir, safe, spec, metadata.title,
                mp3=mp3, left=left, right=right, note=note,
            )
    except TargetNotFoundError:
        # A regeneration of this target began after the checks above. The
        # files written so far may stay behind, but that regeneration
        # rewrites all of them when it completes.
        return PanSplitOutcome(
            "failed",
            f"{spec.target} の項目が消えました(Web で分離ジョブ実行中の可能性)。"
            "完了後に再実行してください。",
        )
    except ProcessLockBusy as exc:
        return PanSplitOutcome("failed", str(exc))
    return PanSplitOutcome("done", decision=decision)
