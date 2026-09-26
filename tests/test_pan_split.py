"""Stereo-position split: the decision rule (pure numpy) and the torch
STFT/ISTFT round trip that writes the L/R stems."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from bunri import pan_split
from bunri.pan_split import (
    LEFT_RIGHT,
    SINGLE,
    PanSplitDecision,
    angle_energy,
    bin_mid,
    decide,
    split_stem,
    stft,
)

_SR = 8000


def _tone(freq: float, n: int, amplitude: float = 0.5) -> np.ndarray:
    t = np.arange(n) / _SR
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _panned(n: int) -> np.ndarray:
    """(n, 2): 220 Hz on the left channel, 440 Hz on the right."""
    return np.stack([_tone(220, n), _tone(440, n)], axis=1)


def _decide_stft(left: np.ndarray, right: np.ndarray) -> PanSplitDecision:
    return decide(*angle_energy(stft(np.stack([left, right]))))


def _decide_points(points: list[tuple[float, float]]) -> PanSplitDecision:
    angle = np.array([a for a, _ in points], dtype=np.float64)
    energy = np.array([w for _, w in points], dtype=np.float64)
    return decide(angle, energy)


# --------------------------------------------------------------------------
# decide() on a real (finite-length) STFT
# --------------------------------------------------------------------------
def test_hard_panned_tones_split_near_the_middle():
    n = 2 * _SR
    decision = _decide_stft(_tone(220, n), _tone(440, n))

    assert decision.status == LEFT_RIGHT
    assert decision.boundary_bin is not None and 5 <= decision.boundary_bin <= 39
    assert decision.left_share is not None and 0.45 <= decision.left_share <= 0.55


def test_identical_channels_are_single():
    n = 2 * _SR
    tone = _tone(220, n)
    assert _decide_stft(tone, tone).status == SINGLE


def test_same_waveform_at_a_tenth_on_the_right_is_single():
    n = 2 * _SR
    tone = _tone(220, n)
    assert _decide_stft(tone, 0.1 * tone).status == SINGLE


def test_empty_input_arrays_are_single():
    assert decide(np.array([]), np.array([])).status == SINGLE
    assert decide(np.zeros(10), np.zeros(10)).status == SINGLE


# --------------------------------------------------------------------------
# decide() on idealized angle/energy arrays
# --------------------------------------------------------------------------
def test_ideal_hard_left_and_right_split_at_first_zero_bin():
    decision = _decide_points([(0.0, 1.0), (math.pi / 2, 1.0)])

    assert decision.status == LEFT_RIGHT
    assert decision.boundary_bin == 2
    assert decision.left_share is not None and 0.40 <= decision.left_share <= 0.50


def test_ideal_single_center_bin_is_single():
    decision = _decide_points([(bin_mid(22), 1.0)])

    assert decision.status == SINGLE
    # Only the flat top 21..23 is positive; the zero bins around it are not
    # peaks, so there is no candidate at all.
    assert decision.peaks == (21,)


def test_zero_height_bins_are_never_peaks():
    # Without the positive-height rule the zero bins at the far end would be
    # "peaks" (equal to their zero neighbours) and a split far from the only
    # cluster would pass the 10% check.
    decision = _decide_points([(bin_mid(2), 1.0)])
    assert decision.status == SINGLE
    assert decision.peaks == (1,)


def test_ideal_two_clusters_split_between_them():
    decision = _decide_points([(bin_mid(10), 1.0), (bin_mid(30), 1.0)])

    assert decision.status == LEFT_RIGHT
    assert decision.boundary_bin == 12
    assert 11 < decision.boundary_bin < 29


def test_ideal_valley_ties_take_the_lowest_index():
    decision = _decide_points([(bin_mid(0), 1.0), (bin_mid(8), 1.0)])

    assert decision.status == LEFT_RIGHT
    assert decision.boundary_bin == 2


def test_small_second_cluster_fails_the_share_threshold():
    cluster = [(bin_mid(9), 0.33), (bin_mid(10), 0.33), (bin_mid(11), 0.33)]

    small = _decide_points([*cluster, (bin_mid(30), 0.01)])
    assert small.status == SINGLE

    large = _decide_points([*cluster, (bin_mid(30), 1.0)])
    assert large.status == LEFT_RIGHT
    assert large.peaks[0] == 29
    assert large.boundary_bin == 13
    assert large.left_share is not None and abs(large.left_share - 0.457) < 0.01


def test_decision_json_round_trip_and_validation():
    decision = PanSplitDecision(
        LEFT_RIGHT, boundary_bin=20, boundary_angle=bin_mid(20), left_share=0.5, peaks=(43, 1)
    )
    assert PanSplitDecision.from_json(decision.to_json()) == decision
    single = PanSplitDecision(SINGLE, peaks=(21,))
    assert PanSplitDecision.from_json(single.to_json()) == single
    for broken in (
        None,
        {"status": "maybe", "peaks": []},
        {"status": LEFT_RIGHT, "peaks": [], "boundary_bin": 99, "boundary_angle": 0.1, "left_share": 0.5},
        {"status": SINGLE, "peaks": "x"},
    ):
        with pytest.raises(ValueError):
            PanSplitDecision.from_json(broken)


# --------------------------------------------------------------------------
# split_stem(): file I/O and reconstruction
# --------------------------------------------------------------------------
def _write_stem(path: Path, samples: np.ndarray) -> Path:
    sf.write(str(path), samples, _SR, subtype="PCM_16")
    return path


@pytest.mark.parametrize("n", [400, 2048, 2049, 4095, 4096, 4097, 16000])
def test_split_stem_halves_add_back_to_the_stem(tmp_path, n):
    src = _write_stem(tmp_path / "guitar.wav", _panned(n))
    left, right = tmp_path / "left.wav", tmp_path / "right.wav"

    decision = split_stem(src, left, right)

    assert decision.status == LEFT_RIGHT
    original, _ = sf.read(str(src), dtype="float64", always_2d=True)
    parts = []
    for path in (left, right):
        info = sf.info(str(path))
        assert (info.samplerate, info.channels, info.frames) == (_SR, 2, n)
        assert info.subtype == "PCM_16"
        parts.append(sf.read(str(path), dtype="float64", always_2d=True)[0])
    error = np.sqrt(np.mean((parts[0] + parts[1] - original) ** 2))
    assert error / np.sqrt(np.mean(original**2)) < 1e-3


def test_split_stem_short_panned_stem_writes_both_sides(tmp_path):
    src = _write_stem(tmp_path / "guitar.wav", _panned(400))
    left, right = tmp_path / "left.wav", tmp_path / "right.wav"

    assert split_stem(src, left, right).status == LEFT_RIGHT
    assert left.stat().st_size > 0 and right.stat().st_size > 0
    left_samples = sf.read(str(left), dtype="float64")[0]
    right_samples = sf.read(str(right), dtype="float64")[0]
    # The left half carries the left channel's tone, the right half the
    # right channel's.
    assert np.abs(left_samples[:, 0]).sum() > np.abs(right_samples[:, 0]).sum()
    assert np.abs(right_samples[:, 1]).sum() > np.abs(left_samples[:, 1]).sum()


@pytest.mark.parametrize(
    "samples",
    [
        np.zeros((4000, 2), dtype=np.float32),  # silence
        _tone(220, 4000)[:, None],  # mono
        np.zeros((0, 2), dtype=np.float32),  # no samples
    ],
    ids=["silence", "mono", "empty"],
)
def test_split_stem_rejects_unsplittable_input_without_writing(tmp_path, samples):
    src = _write_stem(tmp_path / "guitar.wav", samples)
    left, right = tmp_path / "left.wav", tmp_path / "right.wav"

    assert split_stem(src, left, right).status == SINGLE
    assert not left.exists() and not right.exists()


def test_split_stem_center_stem_is_single(tmp_path):
    tone = _tone(220, 4000)
    src = _write_stem(tmp_path / "guitar.wav", np.stack([tone, tone], axis=1))
    left, right = tmp_path / "left.wav", tmp_path / "right.wav"

    assert split_stem(src, left, right).status == SINGLE
    assert not left.exists() and not right.exists()


def test_stft_uses_constant_padding():
    # Pinned in source as well as behaviour: reflect padding would fail on
    # any input of 2048 samples or fewer.
    source = Path(pan_split.__file__).read_text(encoding="utf-8")
    assert 'pad_mode="constant"' in source
    assert stft(np.zeros((2, 1), dtype=np.float32)).shape[-1] == 1
