"""Split a separated stem by stereo position into "L のみ" / "R のみ".

Rock/pop mixes often pan two parts of the same instrument (typically two
guitars) hard left and right. A single separated stem plays both at once;
splitting it at the stereo position between them lets a learner hear each
part alone.

Every time-frequency point of the stem's STFT gets a stereo angle
``atan2(|R|, |L|)`` (0 = hard left, pi/4 = center, pi/2 = hard right) and an
energy ``|L|^2 + |R|^2``. An energy-weighted histogram of the angles shows
where the parts sit; when it has two peaks far enough apart and both sides
of the valley between them carry a real share of the energy, the stem is
split at that valley with a soft mask. The left mask and the right mask sum
to one at every point, so ``left + right`` reproduces the stem.

When there is only one peak (a single part, or parts mixed to the same
place) nothing is written: the decision is "single". The decision is
deterministic, so the same stem always splits the same way.

Only the STFT/ISTFT runs on torch (CPU); the decision itself is plain numpy
(``decide``), which is what the unit tests drive directly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
import torch

from bunri.safepath import replace_into

N_FFT = 4096
HOP = 1024
# Zero padding at both ends (center=True pads n_fft // 2 on each side): the
# same edge treatment as scipy.signal.stft's default boundary="zeros", and
# unlike the default "reflect" it works for inputs of any length >= 1.
PAD_MODE = "constant"
BINS = 45
SMOOTH = 3
MIN_PEAK_DISTANCE = 4
SOFTNESS = 0.04
MIN_SHARE = 0.10

LEFT_RIGHT = "left_right"
SINGLE = "single"

_HALF_PI = math.pi / 2


def bin_mid(index: int) -> float:
    """Center angle of histogram bin `index`."""
    return (index + 0.5) * _HALF_PI / BINS


def pan_label(angle: float) -> str:
    """Human-readable stereo position: "L100" .. "C" .. "R100"."""
    pan = round((angle / (math.pi / 4) - 1) * 100)
    if pan == 0:
        return "C"
    return f"L{-pan}" if pan < 0 else f"R{pan}"


@dataclass(frozen=True)
class PanSplitDecision:
    status: str  # LEFT_RIGHT or SINGLE
    # Set only for LEFT_RIGHT: the valley bin the split is made at, its
    # center angle, and the share of the stem's energy on the left side.
    boundary_bin: int | None = None
    boundary_angle: float | None = None
    left_share: float | None = None
    # (first peak, adopted second peak) for LEFT_RIGHT; (first peak,) for a
    # SINGLE with any energy; () when the input was rejected up front.
    peaks: tuple[int, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "boundary_bin": self.boundary_bin,
            "boundary_angle": self.boundary_angle,
            "left_share": self.left_share,
            "peaks": list(self.peaks),
        }

    @classmethod
    def from_json(cls, value: object) -> "PanSplitDecision":
        if not isinstance(value, dict):
            raise ValueError("pan split result must be an object")
        status = value.get("status")
        peaks = value.get("peaks")
        if not isinstance(peaks, list) or any(
            isinstance(p, bool) or not isinstance(p, int) for p in peaks
        ):
            raise ValueError("pan split peaks are invalid")
        if status == SINGLE:
            return cls(SINGLE, peaks=tuple(peaks))
        if status != LEFT_RIGHT:
            raise ValueError("pan split status is invalid")
        boundary_bin = value.get("boundary_bin")
        boundary_angle = value.get("boundary_angle")
        left_share = value.get("left_share")
        if (
            isinstance(boundary_bin, bool)
            or not isinstance(boundary_bin, int)
            or not 0 <= boundary_bin < BINS
            or isinstance(boundary_angle, bool)
            or not isinstance(boundary_angle, (int, float))
            or isinstance(left_share, bool)
            or not isinstance(left_share, (int, float))
            or not 0.0 <= left_share <= 1.0
        ):
            raise ValueError("pan split boundary is invalid")
        return cls(
            LEFT_RIGHT,
            boundary_bin=boundary_bin,
            boundary_angle=float(boundary_angle),
            left_share=float(left_share),
            peaks=tuple(peaks),
        )


def _left_mask(angle: np.ndarray, boundary: float) -> np.ndarray:
    # Logistic step: ~1 left of the boundary, ~0 right of it. The right
    # side's mask is 1 minus this, so the two always sum to one.
    return 1.0 / (1.0 + np.exp((angle - boundary) / SOFTNESS))


def decide(angle: np.ndarray, energy: np.ndarray) -> PanSplitDecision:
    """Decide whether and where to split, from per-point angles and energies.

    1. Energy-weighted histogram of the angle over [0, pi/2] in 45 bins.
    2. Moving average over 3 bins (zero padded at the ends).
    3. Peaks: bins with a positive height, >= each existing neighbor.
    4. First peak: the highest bin (lowest index on ties). Candidates: other
       peaks at least 4 bins from it, highest first (lowest index on ties).
    5. For each candidate, the valley is the lowest bin between the two
       (lowest index on ties); the first candidate whose split leaves at
       least 10% of the energy on each side is adopted.
    """
    angle = np.asarray(angle).ravel()
    energy = np.asarray(energy).ravel()
    total = float(np.sum(energy))
    if angle.size == 0 or not total > 0.0:
        return PanSplitDecision(SINGLE)
    hist = np.histogram(angle, bins=BINS, range=(0.0, _HALF_PI), weights=energy)[0]
    smooth = np.convolve(hist, np.ones(SMOOTH) / SMOOTH, mode="same")

    peaks = [
        i
        for i in range(BINS)
        if smooth[i] > 0
        and (i == 0 or smooth[i] >= smooth[i - 1])
        and (i == BINS - 1 or smooth[i] >= smooth[i + 1])
    ]
    first = int(np.argmax(smooth))
    if not smooth[first] > 0:
        return PanSplitDecision(SINGLE)
    # sorted() is stable, and `peaks` is in index order, so equal heights
    # stay lowest-index first.
    candidates = sorted(
        (i for i in peaks if abs(i - first) >= MIN_PEAK_DISTANCE),
        key=lambda i: -smooth[i],
    )
    for second in candidates:
        lo, hi = min(first, second), max(first, second)
        valley = lo + int(np.argmin(smooth[lo : hi + 1]))
        boundary = bin_mid(valley)
        share = float(np.sum(_left_mask(angle, boundary) * energy) / total)
        if min(share, 1.0 - share) >= MIN_SHARE:
            return PanSplitDecision(
                LEFT_RIGHT,
                boundary_bin=valley,
                boundary_angle=boundary,
                left_share=share,
                peaks=(first, second),
            )
    return PanSplitDecision(SINGLE, peaks=(first,))


def _window() -> torch.Tensor:
    return torch.hann_window(N_FFT)  # periodic=True by default


def stft(channels: np.ndarray) -> torch.Tensor:
    """(channels, n) float32 -> (channels, freq, frames) complex64."""
    return torch.stft(
        torch.from_numpy(np.ascontiguousarray(channels, dtype=np.float32)),
        n_fft=N_FFT,
        hop_length=HOP,
        window=_window(),
        center=True,
        pad_mode="constant",
        return_complex=True,
    )


def istft(spec: torch.Tensor, length: int) -> np.ndarray:
    """Inverse of `stft`, trimmed/padded back to exactly `length` samples."""
    return torch.istft(
        spec,
        n_fft=N_FFT,
        hop_length=HOP,
        window=_window(),
        center=True,
        length=length,
    ).numpy()


def angle_energy(spec: torch.Tensor) -> tuple[np.ndarray, np.ndarray]:
    """Per-point stereo angle and energy of a (2, freq, frames) spectrum."""
    mag_l = spec[0].abs().numpy()
    mag_r = spec[1].abs().numpy()
    angle = np.arctan2(mag_r, mag_l)
    energy = mag_l**2 + mag_r**2
    return angle, energy


def _wav_subtype(info: Any) -> str:
    subtype = getattr(info, "subtype", None)
    if isinstance(subtype, str) and sf.check_format("WAV", subtype):
        return subtype
    return "PCM_16"


def split_stem(src: Path, left_dest: Path, right_dest: Path) -> PanSplitDecision:
    """Decide on `src` and, for LEFT_RIGHT, write the two halves.

    Outputs keep the stem's sample rate, length, channel count and WAV
    subtype. Nothing is written for SINGLE (the caller removes stale files).
    """
    info = sf.info(str(src))
    data, sample_rate = sf.read(str(src), dtype="float32", always_2d=True)
    # Rejected before the STFT: no stereo angle to measure, or an all-zero
    # stem whose energies would divide by zero.
    if data.shape[1] != 2 or data.shape[0] == 0 or not np.any(data):
        return PanSplitDecision(SINGLE)
    length = data.shape[0]
    spec = stft(data.T)
    del data
    angle, energy = angle_energy(spec)
    decision = decide(angle, energy)
    del energy
    if decision.status != LEFT_RIGHT:
        return decision
    assert decision.boundary_angle is not None
    left_mask = torch.from_numpy(_left_mask(angle, decision.boundary_angle))
    del angle
    subtype = _wav_subtype(info)
    # One side at a time, each dropped once written, to keep peak memory
    # near a single extra spectrum.
    for dest, mask in ((left_dest, left_mask), (right_dest, 1.0 - left_mask)):
        samples = np.clip(istft(spec * mask, length).T, -1.0, 1.0)
        replace_into(
            dest,
            lambda tmp, samples=samples: sf.write(
                str(tmp), samples, sample_rate, subtype=subtype
            ),
        )
        del samples
    return decision
