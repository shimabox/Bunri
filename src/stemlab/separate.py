"""Extract a target instrument's stem, and a "backing" (= everything else)
stem, from a normalized mix using audio-separator. Which model is tried, and
what its fallback is, comes from the caller's TargetSpec (registry.py) --
e.g. for guitar this defaults to a guitar-specialised Mel-Band Roformer
(becruily/mel-band-roformer-guitar); if that default fails to load or
separate, falls back to Demucs htdemucs_6s -- unless the caller picked the
model explicitly, in which case a failure is raised, not silently swapped
out. See TargetSpec.default_model in registry.py and the becruily bootstrap
block below."""

from __future__ import annotations

import functools
import os
import re
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
from rich.console import Console

from stemlab.registry import TargetSpec

console = Console()

# audio_separator.separator.common_separator.CommonSeparator.get_stem_output_path:
# matching a stem name against custom_output_names is case-insensitive on the
# audio-separator side (both sides get lower-cased before comparing), but
# spec.stem_name is the spelling audio-separator itself uses for default
# filenames and log messages (e.g. "Guitar").

# audio-separator's default filename for any stem NOT covered by custom_output_names:
# "<audio_base>_(<stem_name>)_<model_name>.<ext>" (CommonSeparator.get_stem_output_path).
# audio-separator's own code recovers a stem's name from a filename the same way --
# Separator._process_with_chunking and Separator._separate_ensemble both do
# `re.search(r'_\(([^)]+)\)', filename)` -- so reusing that pattern to identify stems
# in the returned file list is the library's own convention, not a guess.
_STEM_LABEL_RE = re.compile(r"_\(([^)]+)\)")

# Kept outside work_dir and never cleared by this function: model weights are
# hundreds of MB, and this directory is reused across inputs/out_dirs so they
# are only ever downloaded once per machine. STEMLAB_MODEL_DIR lets Docker
# images and tests point this at a different location (e.g. a mounted named
# volume, or a throwaway dir) without touching the real user cache.
_MODEL_DIR = Path(os.environ.get("STEMLAB_MODEL_DIR", str(Path.home() / ".cache" / "stemlab" / "models")))


@dataclass(frozen=True)
class SeparationResult:
    target_wav: Path
    backing_wav: Path
    model_used: str


def _construct_separator(separator_cls: Any, work_dir: Path, device: str) -> Any:
    """Build a Separator that writes every stem the loaded model produces into
    work_dir. No output_single_stem: this function needs every stem so the
    non-target ones can be summed into a backing track -- see separate() below.
    Confirmed from source (demucs_separator.py, mdx_separator.py, vr_separator.py,
    mdxc_separator.py): all four architectures write every stem whenever
    output_single_stem is falsy, gated per-stem via `self.output_single_stem`.
    """
    kwargs: dict[str, Any] = {
        "model_file_dir": str(_MODEL_DIR),
        "output_dir": str(work_dir),
        "output_format": "WAV",
        "output_single_stem": None,
    }
    if device != "cpu":
        # "auto"/"mps": leave the choice to Separator's own CUDA > MPS > CPU
        # autodetection in setup_torch_device(), run during __init__ below.
        return separator_cls(**kwargs)

    # Separator has no constructor argument to force CPU; setup_torch_device() always
    # re-derives the device from torch.cuda.is_available()/torch.backends.mps.is_available()
    # during __init__. Hide accelerators from those checks for the instant of
    # construction so it concludes CPU-only, then restore them immediately -- the
    # conclusion is baked into the instance's torch_device attribute, not re-checked
    # afterwards, so this is safe even though the patch is global for that instant.
    import torch

    has_mps = hasattr(torch.backends, "mps")
    real_cuda_available = torch.cuda.is_available
    real_mps_available = torch.backends.mps.is_available if has_mps else None
    torch.cuda.is_available = lambda: False
    if has_mps:
        torch.backends.mps.is_available = lambda: False
    try:
        return separator_cls(**kwargs)
    finally:
        torch.cuda.is_available = real_cuda_available
        if has_mps:
            torch.backends.mps.is_available = real_mps_available


# --- becruily guitar Mel-Band Roformer bootstrap ----------------------------
#
# TargetSpec.default_model (registry.py) isn't one of audio-separator's built-in
# models -- HuggingFace's becruily/mel-band-roformer-guitar needs three things
# to work, each verified against a live load_model()+separate() call before
# being ported here:
#
#   1. audio-separator's own catalog (Separator.list_supported_model_files)
#      has never heard of this model, so Separator.load_model() would refuse
#      the filename outright. _inject_becruily_catalog wraps the catalog
#      method to add an entry, in the same shape audio-separator builds for
#      its own MDXC-architecture models.
#   2. That entry's files aren't hosted where audio-separator's own
#      downloader looks (generic UVR/audio-separator release URLs), so they
#      must already be sitting in model_file_dir before load_model() runs --
#      Separator.download_model_files() then finds them present and skips
#      downloading them itself (download_file_if_not_exists does a plain
#      os.path.isfile check). audio-separator also picks its YAML-parsing
#      path by filename (needs a "roformer"/"mel" substring -- see
#      load_model_data_from_yaml), which is why the local names below differ
#      from the names HuggingFace hosts the files under.
#   3. audio-separator 0.44.3 has its own bug, independent of the above:
#      uvr_lib_v5/roformer/mel_band_roformer.py:314 constructs each
#      MaskEstimator without forwarding mlp_expansion_factor, so it silently
#      takes the class default (4) instead of this checkpoint's training-time
#      value (1). The checkpoint's tensors are then the wrong shape for the
#      model that gets built, and load_state_dict(strict=True) fails with a
#      size mismatch on every mask-estimator layer.
#      _patched_mask_estimator_mlp_expansion_factor binds the right factor in
#      memory for the duration of model construction only; nothing on disk or
#      in the installed package is modified.
_BECRUILY_REPO_BASE = "https://huggingface.co/becruily/mel-band-roformer-guitar/resolve/main"
_BECRUILY_CKPT_HF_NAME = "becruily_guitar.ckpt"
_BECRUILY_YAML_HF_NAME = "config_guitar_becruily.yaml"
_BECRUILY_CKPT_LOCAL_NAME = "mel_band_roformer_guitar_becruily.ckpt"
_BECRUILY_YAML_LOCAL_NAME = "config_mel_band_roformer_guitar_becruily.yaml"
_BECRUILY_CATALOG_NAME = "Roformer Model: MelBand Roformer | Guitar by becruily"
_BECRUILY_MLP_EXPANSION_FACTOR = 1


def _is_becruily_model(model_filename: str) -> bool:
    """Gate for every becruily-specific code path below, so any other model
    (including the htdemucs_6s fallback) never pays for HF downloads, the
    catalog wrapper, or the MaskEstimator patch."""
    return "becruily" in model_filename.lower()


def _download_if_missing(url: str, dest: Path) -> None:
    """Fetch url to dest with urllib (stdlib -- no extra dependency for a
    one-time bootstrap), skipping entirely if dest already exists. Downloads
    to a sibling .part file and renames into place only once complete, so an
    interrupted download can never be mistaken for a complete model file on a
    later run."""
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    urllib.request.urlretrieve(url, part)
    part.replace(dest)


def _inject_becruily_catalog(separator: Any) -> None:
    """Wrap this Separator instance's list_supported_model_files so it also
    reports the becruily guitar model. Instance-local, not a package patch:
    only Separator objects this function constructs are ever affected."""
    original = separator.list_supported_model_files

    def patched() -> dict[str, Any]:
        catalog = original()
        catalog.setdefault("MDXC", {})[_BECRUILY_CATALOG_NAME] = {
            "filename": _BECRUILY_CKPT_LOCAL_NAME,
            "scores": {},
            "stems": ["Guitar", "Other"],
            "target_stem": "Guitar",
            "download_files": [_BECRUILY_CKPT_LOCAL_NAME, _BECRUILY_YAML_LOCAL_NAME],
        }
        return catalog

    separator.list_supported_model_files = patched


@contextmanager
def _patched_mask_estimator_mlp_expansion_factor():
    """See point 3 of the becruily comment above. Binds mlp_expansion_factor
    for the duration of model construction only, then restores the original
    class -- global for that instant (audio-separator gives no per-instance
    hook into MaskEstimator construction), but scoped tightly around
    load_model() so nothing else ever observes the patched class."""
    import audio_separator.separator.uvr_lib_v5.roformer.mel_band_roformer as mbr

    original = mbr.MaskEstimator
    base = original.func if isinstance(original, functools.partial) else original
    mbr.MaskEstimator = functools.partial(
        base, mlp_expansion_factor=_BECRUILY_MLP_EXPANSION_FACTOR
    )
    try:
        yield
    finally:
        mbr.MaskEstimator = original


def _bootstrap_becruily(separator: Any) -> None:
    """Everything the becruily guitar model needs beyond a stock
    audio-separator install, short of the MaskEstimator patch (that one has
    to stay scoped around load_model() itself, not run ahead of it)."""
    _download_if_missing(
        f"{_BECRUILY_REPO_BASE}/{_BECRUILY_CKPT_HF_NAME}",
        _MODEL_DIR / _BECRUILY_CKPT_LOCAL_NAME,
    )
    _download_if_missing(
        f"{_BECRUILY_REPO_BASE}/{_BECRUILY_YAML_HF_NAME}",
        _MODEL_DIR / _BECRUILY_YAML_LOCAL_NAME,
    )
    _inject_becruily_catalog(separator)


def _run_separation(
    separator_cls: Any,
    work_dir: Path,
    device: str,
    model_name: str,
    input_wav: Path,
    target_wav: Path,
    stem_name: str,
) -> list[str]:
    """Construct a Separator, load model_name, and separate input_wav into
    work_dir, requesting the target stem be written as target_wav.stem.
    Applies the becruily bootstrap iff model_name is the becruily guitar
    model -- every other model pays nothing for it.

    A failure raised as an ordinary exception and one raised as SystemExit
    (mdxc_separator.py calls sys.exit(1), not a plain raise, when a
    checkpoint's state_dict doesn't match the model it's loaded into) are
    both wrapped into the same RuntimeError, so callers only need to catch
    one type regardless of failure mode.
    """
    try:
        separator = _construct_separator(separator_cls, work_dir, device)
        if _is_becruily_model(model_name):
            _bootstrap_becruily(separator)
            with _patched_mask_estimator_mlp_expansion_factor():
                separator.load_model(model_name)
        else:
            separator.load_model(model_name)
        return separator.separate(
            str(input_wav), custom_output_names={stem_name: target_wav.stem}
        )
    except (Exception, SystemExit) as exc:
        raise RuntimeError(
            f"audio-separator failed to extract stems from {input_wav} "
            f"with model {model_name!r}: {exc}"
        ) from exc


def _stem_label(path: Path) -> str | None:
    """Best-effort stem name embedded in an audio-separator default filename
    (e.g. "input_(Bass)_htdemucs_6s.wav" -> "Bass"); None if unrecognized."""
    match = _STEM_LABEL_RE.search(path.stem)
    return match.group(1) if match else None


def _partition_stems(
    produced: list[Path], target_wav: Path, stem_name: str
) -> tuple[Path | None, list[Path]]:
    """Split audio-separator's produced files into (the target stem, every
    other stem), without assuming a fixed stem count or set of stem names:
    this works the same for a 2-stem target/others model and a 6-stem Demucs
    model.

    A file counts as the target stem if either:
      - the custom_output_names rename request was honored, so it is already
        named target_wav (compared case-insensitively), or
      - it kept audio-separator's default name and that name's embedded stem
        label is stem_name (case-insensitive) -- the fallback for when the
        rename request wasn't honored, mirroring the rename-fallback behaviour
        this function already had for the single-stem case.
    """
    target: Path | None = None
    others: list[Path] = []
    for path in produced:
        label = _stem_label(path)
        is_target = path.stem.lower() == target_wav.stem.lower() or (
            label is not None and label.lower() == stem_name.lower()
        )
        if is_target and target is None:
            target = path
        else:
            others.append(path)
    return target, others


def _write_backing_track(stem_paths: list[Path], dest: Path) -> None:
    """Sum every non-target stem sample-by-sample into dest (soundfile+numpy).
    Each stem is already peak-normalized by audio-separator on its own, but the
    sum of several can still exceed +/-1.0; gain-normalize only if it does, so
    quiet mixes are left untouched and only loud ones get scaled down."""
    total: np.ndarray | None = None
    samplerate: int | None = None
    for path in stem_paths:
        data, sr = sf.read(str(path), dtype="float32", always_2d=True)
        if total is None:
            total, samplerate = data, sr
            continue
        if sr != samplerate:
            raise RuntimeError(
                f"cannot build backing track: {path} is {sr}Hz, expected {samplerate}Hz"
            )
        if data.shape != total.shape:
            raise RuntimeError(
                f"cannot build backing track: {path} has shape {data.shape}, expected "
                f"{total.shape} (stems from the same separation run should always match)"
            )
        total = total + data

    assert total is not None and samplerate is not None  # stem_paths is never empty
    peak = float(np.abs(total).max()) if total.size else 0.0
    if peak > 1.0:
        total = total / peak
    sf.write(str(dest), total, samplerate, subtype="PCM_16")


def separate(
    input_wav: Path,
    work_dir: Path,
    *,
    spec: TargetSpec,
    model: str | None = None,
    device: str = "auto",
) -> SeparationResult:
    """Separate input_wav (already-normalized mix) into spec's target stem
    and a "backing" stem (everything else), writing both into work_dir.

    model=None uses spec.default_model, with a fallback to
    spec.fallback_model if the default fails and a fallback is registered
    (and differs from the default). An explicitly-given model never falls
    back -- its failure is raised as-is.
    """
    if not input_wav.exists():
        raise RuntimeError(f"separate requires {input_wav} to exist")
    target_wav = work_dir / f"{spec.target}.wav"
    # Target-scoped name: each --target's backing is a different mix (guitar's
    # backing contains vocals, vocals' backing doesn't), so sharing one
    # backing.wav across targets in the same work_dir would let a later
    # --target run silently overwrite an earlier one's cached backing.
    backing_wav = work_dir / f"{spec.target}.backing.wav"

    # audio-separator pulls in torch/onnxruntime; import lazily so callers that
    # don't need it stay fast to load.
    from audio_separator.separator import Separator

    # custom_output_names (inside _run_separation) asks audio-separator to
    # write the target stem as "<target_wav.stem>.wav" directly; every other
    # stem it produces is left at its default name and identified afterwards
    # (_partition_stems).
    is_default = model is None
    model_name = model if model is not None else spec.default_model
    try:
        output_files = _run_separation(
            Separator, work_dir, device, model_name, input_wav, target_wav, spec.stem_name
        )
    except RuntimeError as exc:
        # Only fall back when the caller didn't override the model (i.e. the
        # default was used) and a fallback is actually registered and
        # distinct from the default. A model the caller picked explicitly
        # must fail loudly, not be silently swapped out from under them, and
        # the fallback target itself must never retry against itself.
        can_fall_back = (
            is_default
            and spec.fallback_model is not None
            and spec.default_model != spec.fallback_model
        )
        if not can_fall_back:
            raise
        assert spec.fallback_model is not None
        console.print(
            f"[yellow]separate: default model {model_name!r} failed ({exc}); "
            f"falling back to {spec.fallback_model!r}[/yellow]"
        )
        try:
            output_files = _run_separation(
                Separator, work_dir, device, spec.fallback_model, input_wav, target_wav,
                spec.stem_name,
            )
        except RuntimeError as fallback_exc:
            raise RuntimeError(
                f"separation failed with both the default model ({exc}) and "
                f"the {spec.fallback_model!r} fallback ({fallback_exc})"
            ) from fallback_exc
        model_name = spec.fallback_model

    if not output_files:
        raise RuntimeError(
            f"audio-separator produced no stems for {input_wav} with model "
            f"{model_name!r}"
        )

    # Separator.separate() returns filenames relative to output_dir, not full paths.
    produced: list[Path] = []
    for f in output_files:
        p = Path(f)
        if not p.is_absolute():
            p = work_dir / p
        if not p.exists():
            raise RuntimeError(f"expected separated stem at {p} but it is missing")
        produced.append(p)

    target_path, other_paths = _partition_stems(produced, target_wav, spec.stem_name)
    if target_path is None:
        raise RuntimeError(
            f"audio-separator produced no '{spec.stem_name}' stem for {input_wav} "
            f"with model {model_name!r} "
            f"(stems produced: {[p.name for p in produced]}; "
            f"does this model have a '{spec.stem_name}' stem?)"
        )
    if not other_paths:
        raise RuntimeError(
            f"model {model_name!r} produced only a '{spec.stem_name}' "
            "stem; a backing track needs at least one other stem to combine"
        )

    if target_path != target_wav:
        target_path.replace(target_wav)

    _write_backing_track(other_paths, backing_wav)

    # The per-stem files folded into backing.wav aren't part of separate()'s
    # contract (target_wav + backing_wav); drop them so they don't sit around
    # as dead weight in work_dir.
    for p in other_paths:
        p.unlink(missing_ok=True)

    return SeparationResult(target_wav=target_wav, backing_wav=backing_wav, model_used=model_name)
