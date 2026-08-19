"""Tests for separate().

audio_separator.separator.Separator is always monkeypatched with a fake: no model is
ever downloaded and no real separation ever runs. The fake writes real (tiny,
constant-valued) WAV stems via soundfile so separate()'s own guitar/backing
combination logic (soundfile+numpy summation) runs unmodified and its numeric
output can be verified exactly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pytest
import soundfile as sf
from audio_separator import separator as separator_module

from stemlab import separate as separate_module
from stemlab.registry import get_target
from stemlab.separate import SeparationResult, separate

# A direct binding to the real function, captured before the autouse
# `_stub_becruily_download` fixture below ever monkeypatches
# `separate_module._download_if_missing` into a no-op -- the SHA-256
# verification tests need the genuine implementation, not the stub every
# other test in this file relies on.
from stemlab.separate import _download_if_missing as _real_download_if_missing

_SR = 8000  # tiny sample rate: keeps generated WAVs (and test I/O) fast
_GUITAR_SPEC = get_target("guitar")


def _wave(value: float, n: int = 200) -> np.ndarray:
    """Deterministic constant-valued tiny stereo waveform: real, soundfile-
    readable audio, but with exact, hand-verifiable sample values so tests don't
    need sine-phase reasoning to check sums/clipping."""
    return np.full((n, 2), value, dtype=np.float32)


class FakeSeparator:
    """Stand-in for audio_separator.separator.Separator: writes real (tiny,
    constant-valued) WAV stems instead of running any model, and records how it
    was built and called.

    STEMS lists (stem label, default output name, sample value) triples. A
    label present in custom_output_names (matched case-insensitively, mirroring
    CommonSeparator.get_stem_output_path) is written under that requested name;
    every other stem keeps a default name shaped like audio-separator's own
    "<audio_base>_(<Stem>)_<model>" pattern, so separate.py's `_(...)_ ` label
    regex fallback has something realistic to parse.

    Default layout mimics a 6-stem Demucs model: Guitar plus two unrelated
    "other" stems (Bass/Vocals).
    """

    STEMS: ClassVar[list[tuple[str, str, float]]] = [
        ("Guitar", "mix_(Guitar)_htdemucs_6s", 0.20),
        ("Bass", "mix_(Bass)_htdemucs_6s", 0.10),
        ("Vocals", "mix_(Vocals)_htdemucs_6s", 0.15),
    ]
    instances: list["FakeSeparator"] = []

    def __init__(self, **kwargs: Any) -> None:
        import torch

        self.init_kwargs = kwargs
        self.output_dir = Path(kwargs["output_dir"])
        # Snapshot what the real availability checks report *right now*, exactly like
        # Separator.setup_torch_device() would during __init__, so tests can tell
        # whether separate() patched them for this construction.
        self.cuda_available_at_init = torch.cuda.is_available()
        self.mps_available_at_init = torch.backends.mps.is_available()
        self.loaded_model: str | None = None
        self.separate_calls: list[tuple[str, dict[str, str] | None]] = []
        FakeSeparator.instances.append(self)

    def list_supported_model_files(self) -> dict[str, Any]:
        # Real shape is {model_type: {friendly_name: info_dict}}; empty is a
        # valid (if uninteresting) catalog and lets separate.py's real
        # _inject_becruily_catalog wrapper (never faked -- only the network
        # download underneath it is) add its entry without special-casing.
        return {}

    def load_model(self, model_filename: str) -> None:
        self.loaded_model = model_filename

    def separate(
        self, audio_file_path: str, custom_output_names: dict[str, str] | None = None
    ) -> list[str]:
        self.separate_calls.append((audio_file_path, custom_output_names))
        custom = {k.lower(): v for k, v in (custom_output_names or {}).items()}
        written: list[str] = []
        for label, default_name, value in self.STEMS:
            name = custom.get(label.lower(), default_name)
            filename = f"{name}.wav"
            sf.write(str(self.output_dir / filename), _wave(value), _SR, subtype="FLOAT")
            written.append(filename)
        return written


class IgnoresCustomNamesFakeSeparator(FakeSeparator):
    """Simulates custom_output_names NOT being honored: every stem (including
    Guitar) keeps audio-separator's default "_(Stem)_" naming, exercising the
    stem-label-regex fallback in separate()'s guitar/backing split."""

    def separate(
        self, audio_file_path: str, custom_output_names: dict[str, str] | None = None
    ) -> list[str]:
        self.separate_calls.append((audio_file_path, custom_output_names))
        written: list[str] = []
        for label, default_name, value in self.STEMS:
            filename = f"{default_name}.wav"
            sf.write(str(self.output_dir / filename), _wave(value), _SR, subtype="FLOAT")
            written.append(filename)
        return written


class TwoStemFakeSeparator(FakeSeparator):
    """Simulates a 2-stem guitar/others model: backing.wav should equal the
    single "Other" stem directly (summing one stem is the identity)."""

    STEMS: ClassVar[list[tuple[str, str, float]]] = [
        ("Guitar", "mix_(Guitar)_2stem", 0.20),
        ("Other", "mix_(Other)_2stem", 0.30),
    ]


class ClippingFakeSeparator(FakeSeparator):
    """Non-guitar stems sum well past +/-1.0 (0.90 + 0.85 = 1.75), so
    backing.wav must be gain-normalized rather than left to clip."""

    STEMS: ClassVar[list[tuple[str, str, float]]] = [
        ("Guitar", "mix_(Guitar)_htdemucs_6s", 0.20),
        ("Bass", "mix_(Bass)_htdemucs_6s", 0.90),
        ("Vocals", "mix_(Vocals)_htdemucs_6s", 0.85),
    ]


class NoGuitarFakeSeparator(FakeSeparator):
    """Simulates a model with no Guitar stem at all."""

    STEMS: ClassVar[list[tuple[str, str, float]]] = [
        ("Bass", "mix_(Bass)_htdemucs_6s", 0.10),
        ("Vocals", "mix_(Vocals)_htdemucs_6s", 0.15),
    ]


class GuitarOnlyFakeSeparator(FakeSeparator):
    """Simulates a single-stem-only model: Guitar with no other stem to build
    a backing track from."""

    STEMS: ClassVar[list[tuple[str, str, float]]] = [
        ("Guitar", "mix_(Guitar)_htdemucs_6s", 0.20),
    ]


class EmptyFakeSeparator(FakeSeparator):
    """Simulates a model that writes nothing at all."""

    def separate(
        self, audio_file_path: str, custom_output_names: dict[str, str] | None = None
    ) -> list[str]:
        self.separate_calls.append((audio_file_path, custom_output_names))
        return []


class ExplodingFakeSeparator(FakeSeparator):
    """Simulates a hard failure inside audio-separator (e.g. a download error)."""

    def separate(
        self, audio_file_path: str, custom_output_names: dict[str, str] | None = None
    ) -> list[str]:
        raise OSError("network unreachable")


class SystemExitOnLoadFakeSeparator(FakeSeparator):
    """Simulates mdxc_separator.py's real failure mode: on a state_dict shape
    mismatch it logs an error and calls sys.exit(1) instead of raising, so
    load_model raises SystemExit (a BaseException, not an Exception)."""

    def load_model(self, model_filename: str) -> None:
        raise SystemExit(1)


class FailsFirstLoadFakeSeparator(FakeSeparator):
    """load_model fails on the very first call across all instances of this
    class and succeeds on every call after that. A fresh Separator/instance
    is constructed per separation attempt (see _construct_separator), so this
    simulates the becruily default failing on its (sole) attempt while a
    fresh htdemucs_6s fallback attempt right after then succeeds. The first
    failure is a SystemExit, mirroring mdxc_separator.py's real behaviour
    (see SystemExitOnLoadFakeSeparator) -- exercising the fallback and the
    SystemExit conversion together, the way they actually occur."""

    load_model_call_count: ClassVar[int] = 0

    def load_model(self, model_filename: str) -> None:
        type(self).load_model_call_count += 1
        if type(self).load_model_call_count == 1:
            raise SystemExit(1)
        super().load_model(model_filename)


@pytest.fixture(autouse=True)
def _reset_fake_separator_instances():
    FakeSeparator.instances = []
    FailsFirstLoadFakeSeparator.load_model_call_count = 0
    yield


@pytest.fixture(autouse=True)
def _stub_becruily_download(monkeypatch):
    """Every test in this file drives a Fake Separator; nothing here should
    ever touch the network. The guitar spec's default model is the becruily
    guitar Roformer, which triggers an HF-download bootstrap for whichever of
    its two files aren't already cached locally -- stub the download function
    to a no-op for every test by default. The bootstrap tests below override
    this again (monkeypatch supports repeated calls within one test) with a
    recording fake to verify it's actually invoked.
    """
    monkeypatch.setattr(
        separate_module, "_download_if_missing", lambda url, dest, expected_sha256: None
    )


def _prepare(tmp_path: Path, *, create_input: bool = True) -> tuple[Path, Path]:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    input_wav = work_dir / "input.wav"
    if create_input:
        input_wav.write_bytes(b"RIFF-fake-input-audio")
    return input_wav, work_dir


def test_run_missing_input_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", FakeSeparator)
    input_wav, work_dir = _prepare(tmp_path, create_input=False)
    with pytest.raises(RuntimeError, match="input.wav"):
        separate(input_wav, work_dir, spec=_GUITAR_SPEC)
    assert FakeSeparator.instances == []


def test_run_returns_expected_result_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", FakeSeparator)
    input_wav, work_dir = _prepare(tmp_path)

    result = separate(input_wav, work_dir, spec=_GUITAR_SPEC, model="htdemucs_6s.yaml")

    assert isinstance(result, SeparationResult)
    assert result.target_wav == work_dir / "guitar.wav"
    assert result.backing_wav == work_dir / "guitar.backing.wav"
    assert result.model_used == "htdemucs_6s.yaml"


def test_run_builds_separator_and_calls_api_correctly(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", FakeSeparator)
    input_wav, work_dir = _prepare(tmp_path)

    separate(input_wav, work_dir, spec=_GUITAR_SPEC, model="htdemucs_6s.yaml")

    assert len(FakeSeparator.instances) == 1
    fake = FakeSeparator.instances[0]

    assert fake.init_kwargs["model_file_dir"] == str(separate_module._MODEL_DIR)
    assert fake.init_kwargs["output_dir"] == str(work_dir)
    # No output_single_stem: every stem must come back so the non-guitar ones
    # can be combined into backing.wav.
    assert fake.init_kwargs["output_single_stem"] is None

    assert fake.loaded_model == "htdemucs_6s.yaml"

    assert len(fake.separate_calls) == 1
    called_path, custom_names = fake.separate_calls[0]
    assert called_path == str(input_wav)
    assert custom_names == {"Guitar": "guitar"}


def test_run_honors_custom_guitar_name(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", FakeSeparator)
    input_wav, work_dir = _prepare(tmp_path)

    separate(input_wav, work_dir, spec=_GUITAR_SPEC, model="htdemucs_6s.yaml")

    guitar = work_dir / "guitar.wav"
    assert guitar.exists()
    data, sr = sf.read(str(guitar), dtype="float32", always_2d=True)
    assert sr == _SR
    assert np.allclose(data, 0.20, atol=1e-6)


def test_run_falls_back_to_stem_label_when_custom_name_not_honored(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", IgnoresCustomNamesFakeSeparator)
    input_wav, work_dir = _prepare(tmp_path)

    separate(input_wav, work_dir, spec=_GUITAR_SPEC, model="htdemucs_6s.yaml")

    guitar = work_dir / "guitar.wav"
    assert guitar.exists()
    data, _ = sf.read(str(guitar), dtype="float32", always_2d=True)
    assert np.allclose(data, 0.20, atol=1e-6)
    # The default-named file it actually arrived under was renamed away, not copied.
    assert not (work_dir / "mix_(Guitar)_htdemucs_6s.wav").exists()


def test_run_backing_is_sum_of_other_stems(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", FakeSeparator)
    input_wav, work_dir = _prepare(tmp_path)

    separate(input_wav, work_dir, spec=_GUITAR_SPEC, model="htdemucs_6s.yaml")

    backing = work_dir / "guitar.backing.wav"
    assert backing.exists()
    data, sr = sf.read(str(backing), dtype="float32", always_2d=True)
    assert sr == _SR
    # Bass (0.10) + Vocals (0.15); Guitar (0.20) must NOT be folded in.
    assert np.allclose(data, 0.25, atol=1e-3)


def test_run_cleans_up_intermediate_stem_files(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", FakeSeparator)
    input_wav, work_dir = _prepare(tmp_path)

    separate(input_wav, work_dir, spec=_GUITAR_SPEC, model="htdemucs_6s.yaml")

    # Only guitar.wav/backing.wav are separate()'s contract; the raw per-stem
    # files that fed backing.wav shouldn't be left behind as cache dead weight.
    assert not (work_dir / "mix_(Bass)_htdemucs_6s.wav").exists()
    assert not (work_dir / "mix_(Vocals)_htdemucs_6s.wav").exists()
    assert sorted(p.name for p in work_dir.glob("*.wav")) == [
        "guitar.backing.wav",
        "guitar.wav",
        "input.wav",
    ]


def test_run_two_stem_model_backing_equals_the_other_stem(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", TwoStemFakeSeparator)
    input_wav, work_dir = _prepare(tmp_path)

    separate(input_wav, work_dir, spec=_GUITAR_SPEC, model="htdemucs_6s.yaml")

    backing = work_dir / "guitar.backing.wav"
    data, _ = sf.read(str(backing), dtype="float32", always_2d=True)
    assert np.allclose(data, 0.30, atol=1e-3)


def test_run_backing_gain_normalizes_when_sum_clips(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", ClippingFakeSeparator)
    input_wav, work_dir = _prepare(tmp_path)

    separate(input_wav, work_dir, spec=_GUITAR_SPEC, model="htdemucs_6s.yaml")

    backing = work_dir / "guitar.backing.wav"
    data, _ = sf.read(str(backing), dtype="float32", always_2d=True)
    peak = float(np.abs(data).max())
    # Raw sum would be 0.90 + 0.85 = 1.75 (well past full scale); normalized
    # down to unit peak, not left to clip/wrap.
    assert peak <= 1.0 + 1e-6
    assert np.allclose(data, 1.0, atol=1e-3)


def test_run_raises_when_no_stems_produced_at_all(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", EmptyFakeSeparator)
    input_wav, work_dir = _prepare(tmp_path)

    with pytest.raises(RuntimeError, match="no stems"):
        separate(input_wav, work_dir, spec=_GUITAR_SPEC, model="htdemucs_6s.yaml")


def test_run_raises_when_no_guitar_stem_among_produced(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", NoGuitarFakeSeparator)
    input_wav, work_dir = _prepare(tmp_path)

    with pytest.raises(RuntimeError, match="Guitar"):
        separate(input_wav, work_dir, spec=_GUITAR_SPEC, model="htdemucs_6s.yaml")


def test_run_raises_when_guitar_only_with_no_backing_source(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", GuitarOnlyFakeSeparator)
    input_wav, work_dir = _prepare(tmp_path)

    with pytest.raises(RuntimeError, match="backing"):
        separate(input_wav, work_dir, spec=_GUITAR_SPEC, model="htdemucs_6s.yaml")


def test_run_wraps_underlying_errors_in_runtime_error(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", ExplodingFakeSeparator)
    # Explicit (non-default) model: isolates plain error-wrapping from the
    # default-model fallback behaviour, which has its own dedicated tests
    # below.
    input_wav, work_dir = _prepare(tmp_path)

    with pytest.raises(RuntimeError, match="failed to extract"):
        separate(input_wav, work_dir, spec=_GUITAR_SPEC, model="htdemucs_6s.yaml")
    assert len(ExplodingFakeSeparator.instances) == 1


def test_run_cpu_device_forces_cpu_during_construction(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", FakeSeparator)
    input_wav, work_dir = _prepare(tmp_path)

    separate(input_wav, work_dir, spec=_GUITAR_SPEC, model="htdemucs_6s.yaml", device="cpu")

    fake = FakeSeparator.instances[0]
    assert fake.cuda_available_at_init is False
    assert fake.mps_available_at_init is False


def test_run_auto_device_leaves_real_availability_checks_alone(tmp_path, monkeypatch):
    import torch

    monkeypatch.setattr(separator_module, "Separator", FakeSeparator)
    input_wav, work_dir = _prepare(tmp_path)

    separate(input_wav, work_dir, spec=_GUITAR_SPEC, model="htdemucs_6s.yaml", device="auto")

    fake = FakeSeparator.instances[0]
    assert fake.cuda_available_at_init == torch.cuda.is_available()
    assert fake.mps_available_at_init == torch.backends.mps.is_available()


def test_run_restores_torch_availability_checks_after_forcing_cpu(tmp_path, monkeypatch):
    import torch

    monkeypatch.setattr(separator_module, "Separator", FakeSeparator)
    input_wav, work_dir = _prepare(tmp_path)
    original_cuda_is_available = torch.cuda.is_available
    original_mps_is_available = torch.backends.mps.is_available

    separate(input_wav, work_dir, spec=_GUITAR_SPEC, model="htdemucs_6s.yaml", device="cpu")

    assert torch.cuda.is_available is original_cuda_is_available
    assert torch.backends.mps.is_available is original_mps_is_available


# --- becruily bootstrap / graceful fallback ---------------------------------
#
# The guitar spec's default_model is the becruily guitar Mel-Band Roformer.
# Everything below drives that through separate() (model=None, i.e. the
# caller didn't override it) with FakeSeparator subclasses -- never a real
# audio-separator model -- and the _stub_becruily_download autouse fixture
# above keeps the HF download bootstrap from ever touching the network.


def test_run_becruily_default_triggers_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", FakeSeparator)
    download_calls: list[tuple[str, Path, str]] = []
    monkeypatch.setattr(
        separate_module,
        "_download_if_missing",
        lambda url, dest, expected_sha256: download_calls.append((url, dest, expected_sha256)),
    )
    input_wav, work_dir = _prepare(tmp_path)  # model=None -> becruily default

    result = separate(input_wav, work_dir, spec=_GUITAR_SPEC)

    # Both the checkpoint and the YAML config were (attempted to be) fetched,
    # each with its pinned SHA-256 expectation.
    assert len(download_calls) == 2
    assert {dest.name for _, dest, _ in download_calls} == {
        "mel_band_roformer_guitar_becruily.ckpt",
        "config_mel_band_roformer_guitar_becruily.yaml",
    }
    assert all(url.startswith("https://huggingface.co/") for url, _, _ in download_calls)
    # Pinned to a specific commit, not a movable ref like "main".
    assert all("/resolve/6409e7f88754b07ef7ca3bd1b76a15f010f1672a/" in url for url, _, _ in download_calls)
    hashes_by_name = {dest.name: sha for _, dest, sha in download_calls}
    assert hashes_by_name["mel_band_roformer_guitar_becruily.ckpt"] == (
        "83472bbf125774af5282d2e0b86df89eaf2dd45e8a4ec8d68e820ebf3e42a83c"
    )
    assert hashes_by_name["config_mel_band_roformer_guitar_becruily.yaml"] == (
        "b681c3f886251b04b666b3f06e87ce65d7ec610e40b5d75915c01782e5444b0e"
    )

    fake = FakeSeparator.instances[0]
    assert fake.loaded_model == _GUITAR_SPEC.default_model
    assert result.model_used == _GUITAR_SPEC.default_model
    # The catalog injection wrapper (never faked) really ran against this
    # instance: list_supported_model_files now reports the becruily entry.
    catalog = fake.list_supported_model_files()
    assert _GUITAR_SPEC.default_model in {
        info["filename"] for info in catalog.get("MDXC", {}).values()
    }


def test_run_htdemucs_model_skips_becruily_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", FakeSeparator)
    download_calls: list[tuple[str, Path, str]] = []
    monkeypatch.setattr(
        separate_module,
        "_download_if_missing",
        lambda url, dest, expected_sha256: download_calls.append((url, dest, expected_sha256)),
    )
    input_wav, work_dir = _prepare(tmp_path)

    separate(input_wav, work_dir, spec=_GUITAR_SPEC, model="htdemucs_6s.yaml")

    assert download_calls == []
    assert FakeSeparator.instances[0].loaded_model == "htdemucs_6s.yaml"


# --- SHA-256 verification of downloaded/cached model files ------------------
def _sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def test_download_if_missing_accepts_an_existing_file_with_a_matching_hash(tmp_path):
    dest = tmp_path / "model.ckpt"
    content = b"totally real model weights"
    dest.write_bytes(content)

    # Must not raise, and must not touch the file (no network call needed --
    # spied via a urlretrieve monkeypatch would over-specify; absence of a
    # raise plus content unchanged is the actual contract).
    _real_download_if_missing("https://example.invalid/model.ckpt", dest, _sha256_hex(content))

    assert dest.read_bytes() == content


def test_download_if_missing_deletes_and_raises_on_existing_hash_mismatch(tmp_path):
    dest = tmp_path / "model.ckpt"
    dest.write_bytes(b"corrupted or tampered bytes")

    with pytest.raises(RuntimeError, match="SHA-256"):
        _real_download_if_missing(
            "https://example.invalid/model.ckpt", dest, "0" * 64
        )

    assert not dest.exists(), "a hash-mismatched existing file must be removed, not left in place"


def test_download_if_missing_downloads_verifies_and_atomically_replaces(tmp_path, monkeypatch):
    dest = tmp_path / "model.ckpt"
    content = b"freshly downloaded model weights"
    expected = _sha256_hex(content)

    def fake_urlretrieve(url, filename):
        Path(filename).write_bytes(content)

    monkeypatch.setattr(separate_module.urllib.request, "urlretrieve", fake_urlretrieve)

    _real_download_if_missing("https://example.invalid/model.ckpt", dest, expected)

    assert dest.read_bytes() == content
    # No leftover temp file.
    leftovers = [p for p in tmp_path.iterdir() if p.name != "model.ckpt"]
    assert leftovers == [], leftovers


def test_download_if_missing_never_leaves_a_corrupt_file_on_hash_mismatch(tmp_path, monkeypatch):
    dest = tmp_path / "model.ckpt"

    def fake_urlretrieve(url, filename):
        Path(filename).write_bytes(b"wrong bytes entirely")

    monkeypatch.setattr(separate_module.urllib.request, "urlretrieve", fake_urlretrieve)

    with pytest.raises(RuntimeError, match="SHA-256"):
        _real_download_if_missing(
            "https://example.invalid/model.ckpt", dest, _sha256_hex(b"expected bytes")
        )

    assert not dest.exists()
    assert list(tmp_path.iterdir()) == [], "the failed download's temp file must be cleaned up"


def test_download_if_missing_uses_a_unique_temp_filename_not_a_fixed_dot_part(tmp_path, monkeypatch):
    dest = tmp_path / "model.ckpt"
    content = b"model weights"
    captured_tmp_names: list[str] = []

    def fake_urlretrieve(url, filename):
        captured_tmp_names.append(Path(filename).name)
        Path(filename).write_bytes(content)

    monkeypatch.setattr(separate_module.urllib.request, "urlretrieve", fake_urlretrieve)

    _real_download_if_missing(
        "https://example.invalid/model.ckpt", dest, _sha256_hex(content)
    )
    # Second, independent call (as if a concurrent bootstrap raced this one)
    # must pick a different temp name.
    dest.unlink()
    _real_download_if_missing(
        "https://example.invalid/model.ckpt", dest, _sha256_hex(content)
    )

    assert len(captured_tmp_names) == 2
    assert captured_tmp_names[0] != captured_tmp_names[1]
    assert all(name != "model.ckpt.part" for name in captured_tmp_names)


# --- device strictness: explicit mps/cuda must be verified, not silently
# --- swapped for whatever Separator's own autodetection would have picked.
# Driven through the public separate() entrypoint, same as the existing
# cpu/auto device tests above.
def test_run_explicit_mps_device_raises_when_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", FakeSeparator)
    import torch

    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)
    input_wav, work_dir = _prepare(tmp_path)

    with pytest.raises(RuntimeError, match="mps.*not available"):
        separate(input_wav, work_dir, spec=_GUITAR_SPEC, model="htdemucs_6s.yaml", device="mps")
    assert FakeSeparator.instances == []  # never even constructed


def test_run_explicit_mps_device_is_actually_selected_when_available(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", FakeSeparator)
    import torch

    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)  # both "available"
    input_wav, work_dir = _prepare(tmp_path)

    separate(input_wav, work_dir, spec=_GUITAR_SPEC, model="htdemucs_6s.yaml", device="mps")

    fake = FakeSeparator.instances[0]
    # mps requested and available: mps stays visible, cuda is hidden even
    # though it also reports available, so Separator's own CUDA>MPS priority
    # can't silently override the explicit request.
    assert fake.mps_available_at_init is True
    assert fake.cuda_available_at_init is False


def test_run_explicit_cuda_device_raises_when_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", FakeSeparator)
    import torch

    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    input_wav, work_dir = _prepare(tmp_path)

    with pytest.raises(RuntimeError, match="cuda.*not available"):
        separate(input_wav, work_dir, spec=_GUITAR_SPEC, model="htdemucs_6s.yaml", device="cuda")
    assert FakeSeparator.instances == []


def test_run_explicit_cuda_device_is_actually_selected_when_available(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", FakeSeparator)
    import torch

    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: True)
    input_wav, work_dir = _prepare(tmp_path)

    separate(input_wav, work_dir, spec=_GUITAR_SPEC, model="htdemucs_6s.yaml", device="cuda")

    fake = FakeSeparator.instances[0]
    assert fake.cuda_available_at_init is True
    assert fake.mps_available_at_init is False


def test_run_explicit_mps_device_restores_real_availability_checks_afterward(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", FakeSeparator)
    import torch

    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: True)
    original_cuda_is_available = torch.cuda.is_available
    original_mps_is_available = torch.backends.mps.is_available
    input_wav, work_dir = _prepare(tmp_path)

    separate(input_wav, work_dir, spec=_GUITAR_SPEC, model="htdemucs_6s.yaml", device="mps")

    assert torch.cuda.is_available is original_cuda_is_available
    assert torch.backends.mps.is_available is original_mps_is_available


def test_run_becruily_default_falls_back_to_htdemucs_on_load_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", FailsFirstLoadFakeSeparator)
    input_wav, work_dir = _prepare(tmp_path)  # model=None -> becruily default

    result = separate(input_wav, work_dir, spec=_GUITAR_SPEC)

    assert len(FailsFirstLoadFakeSeparator.instances) == 2
    first, second = FailsFirstLoadFakeSeparator.instances
    assert first.loaded_model is None  # failed inside load_model, never got set
    assert second.loaded_model == "htdemucs_6s.yaml"  # fresh instance, fallback model
    assert result.model_used == "htdemucs_6s.yaml"
    guitar = work_dir / "guitar.wav"
    assert guitar.exists()
    data, _ = sf.read(str(guitar), dtype="float32", always_2d=True)
    assert np.allclose(data, 0.20, atol=1e-6)


def test_run_explicit_model_does_not_fall_back_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", FailsFirstLoadFakeSeparator)
    # Explicitly requested and distinct from the default: a failure here must
    # be raised as-is, never silently retried against htdemucs_6s.
    input_wav, work_dir = _prepare(tmp_path)

    with pytest.raises(RuntimeError, match="failed to extract"):
        separate(input_wav, work_dir, spec=_GUITAR_SPEC, model="some_other_model.ckpt")

    assert len(FailsFirstLoadFakeSeparator.instances) == 1


def test_run_system_exit_from_load_model_becomes_runtime_error(tmp_path, monkeypatch):
    monkeypatch.setattr(separator_module, "Separator", SystemExitOnLoadFakeSeparator)
    # Explicit (non-default) model: isolates the SystemExit->RuntimeError
    # conversion from the default-model fallback behaviour (covered above),
    # per "SystemExit caught for every model" being a separate requirement.
    input_wav, work_dir = _prepare(tmp_path)

    with pytest.raises(RuntimeError, match="failed to extract"):
        separate(input_wav, work_dir, spec=_GUITAR_SPEC, model="htdemucs_6s.yaml")

    assert len(SystemExitOnLoadFakeSeparator.instances) == 1
