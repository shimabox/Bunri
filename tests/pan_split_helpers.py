"""Shared helpers for the L/R split tests (not a test module itself)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import soundfile as sf

SR = 8000


def panned_samples(seconds: float = 2.0) -> np.ndarray:
    """(n, 2): a 220 Hz tone on the left channel and 440 Hz on the right."""
    t = np.arange(int(SR * seconds)) / SR
    return np.stack(
        [0.5 * np.sin(2 * np.pi * 220 * t), 0.5 * np.sin(2 * np.pi * 440 * t)], axis=1
    ).astype(np.float32)


class FakeSeparator:
    """Stands in for audio_separator's Separator: writes a target stem (hard
    panned when `panned` is set, else a centered constant) plus one "Other"
    stem for the backing track."""

    panned: ClassVar[bool] = True
    instances: ClassVar[list["FakeSeparator"]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.output_dir = Path(kwargs["output_dir"])
        self.model_file_dir = kwargs["model_file_dir"]
        type(self).instances.append(self)

    def list_supported_model_files(self) -> dict[str, Any]:
        return {}

    def load_model(self, model_filename: str) -> None:
        pass

    def download_file_if_not_exists(self, url: str, output_path: str) -> None:
        raise AssertionError(f"unexpected model download: {url} -> {output_path}")

    def separate(
        self, audio_file_path: str, custom_output_names: dict[str, str] | None = None
    ) -> list[str]:
        target_name = next(iter((custom_output_names or {"Guitar": "mix_(Guitar)_fake"}).values()))
        target = panned_samples() if self.panned else np.full((400, 2), 0.2, dtype=np.float32)
        other = np.full((target.shape[0], 2), 0.3, dtype=np.float32)
        written = []
        for name, samples in ((target_name, target), ("mix_(Other)_fake", other)):
            filename = f"{name}.wav"
            sf.write(str(self.output_dir / filename), samples, SR)
            written.append(filename)
        return written


def install_fake_separator(monkeypatch, cls: type[FakeSeparator]) -> None:
    from audio_separator import separator as separator_module
    from bunri import separate as separate_module

    cls.instances = []
    monkeypatch.setattr(separator_module, "Separator", cls)
    monkeypatch.setattr(
        separate_module, "_download_if_missing", lambda url, dest, expected_sha256: None
    )


def strip_pan_split(out_dir: Path, safe: str, target: str = "guitar") -> None:
    """Turn a freshly built package back into what a Bunri without the L/R
    split left behind: no pan_split in the sidecar, no L/R files or split
    stage in the cache or the package, and a player without L/R buttons."""
    from bunri import package
    from bunri.registry import get_target

    package_dir = out_dir / safe
    sidecar = package_dir / ".bunri-package.json"
    value = json.loads(sidecar.read_text(encoding="utf-8"))
    for item in value["targets"]:
        item.pop("pan_split", None)
    sidecar.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    cache_dir = out_dir / ".cache" / value["source"]["cache_key"]
    for name in (
        f"pan_split:{target}.meta.json",
        f"{target}.pan_split.json",
        f"{target}.left.wav",
        f"{target}.right.wav",
    ):
        (cache_dir / name).unlink(missing_ok=True)
    for side in ("left", "right"):
        for audio_format in ("wav", "mp3"):
            (package_dir / f"{safe}.{target}.{side}.{audio_format}").unlink(missing_ok=True)
    item = next(x for x in value["targets"] if x["target"] == target)
    package._write_player(
        package_dir,
        safe,
        get_target(target),
        value["title"],
        mp3="mp3" in item["formats"],
        left=None,
        right=None,
        note=None,
    )
