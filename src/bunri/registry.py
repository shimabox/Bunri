"""Target-instrument registry: maps a ``--target`` name to the separation
model (and stem name) that extracts it, plus the fallback model and player
label to use for it.

Phase 1 only registers "guitar"; the shape is deliberately generic so future
targets (bass, drums, vocals, piano -- see the Bunri founding plan) can be
added as new REGISTRY entries without touching separate.py/package.py.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TargetSpec:
    target: str  # registry key, e.g. "guitar"
    stem_name: str  # audio-separator canonical stem name, e.g. "Guitar"
    default_model: str  # e.g. "mel_band_roformer_guitar_becruily.ckpt"
    fallback_model: str | None  # e.g. "htdemucs_6s.yaml"; None if no fallback
    label_ja: str  # player UI label, e.g. "ギター"


REGISTRY: dict[str, TargetSpec] = {
    "guitar": TargetSpec(
        target="guitar",
        stem_name="Guitar",
        # audio-separator model filename used when the caller doesn't pick
        # one: a guitar-specialised Mel-Band Roformer
        # (becruily/mel-band-roformer-guitar on HuggingFace) that measurably
        # beats htdemucs_6s on real recordings -- a third of the vocal bleed
        # and better quiet-passage guitar retention. It isn't one of
        # audio-separator's built-in models, so separate.py bootstraps it
        # (HF download + catalog injection + a MaskEstimator patch) whenever
        # it sees this exact filename. separate() also compares the
        # requested model against this spec's default_model to decide
        # whether a load/separation failure is safe to retry with
        # fallback_model: only when the caller left the default in place,
        # never when --model asked for this model explicitly.
        default_model="mel_band_roformer_guitar_becruily.ckpt",
        fallback_model="htdemucs_6s.yaml",
        label_ja="ギター",
    ),
    # bass/drums/piano reuse the 6-stem Demucs: no single-instrument
    # specialist for them ships in the catalog with a better measured score,
    # and htdemucs_6s is already this project's proven fallback model. No
    # fallback_model: they'd only fall back to themselves.
    "bass": TargetSpec(
        target="bass",
        stem_name="Bass",
        default_model="htdemucs_6s.yaml",
        fallback_model=None,
        label_ja="ベース",
    ),
    "drums": TargetSpec(
        target="drums",
        stem_name="Drums",
        default_model="htdemucs_6s.yaml",
        fallback_model=None,
        label_ja="ドラム",
    ),
    "vocals": TargetSpec(
        target="vocals",
        stem_name="Vocals",
        # Top vocals model in audio-separator's own catalog by measured SDR
        # (12.60, vs 12.52 for the next Mel-Roformer and 10.79 for
        # htdemucs_ft) as of 0.44.3. A catalog built-in, so audio-separator
        # downloads it itself -- none of the becruily bootstrap applies. Its
        # two stems are vocals/other, so the backing track is simply the
        # "other" stem: a ready-made karaoke mix.
        default_model="vocals_mel_band_roformer.ckpt",
        fallback_model="htdemucs_6s.yaml",
        label_ja="ボーカル",
    ),
    "piano": TargetSpec(
        target="piano",
        stem_name="Piano",
        default_model="htdemucs_6s.yaml",
        fallback_model=None,
        label_ja="ピアノ",
    ),
}


def get_target(name: str) -> TargetSpec:
    """Look up a TargetSpec by registry key.

    Raises:
        ValueError: if `name` isn't a registered target, listing the valid keys.
    """
    try:
        return REGISTRY[name]
    except KeyError:
        valid = ", ".join(sorted(REGISTRY))
        raise ValueError(f"unknown target {name!r}; valid targets: {valid}") from None
