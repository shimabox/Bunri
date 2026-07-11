"""Stage artifact caching.

Layout: <out_dir>/.cache/<input-digest>/ holds every stage's artifacts plus a
<stage>.meta.json recording the params digest and stage version. A stage is
skipped when its meta matches and all declared outputs exist.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def file_digest(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def params_digest(params: dict[str, Any]) -> str:
    canonical = json.dumps(params, sort_keys=True, default=str)
    return hashlib.sha1(canonical.encode()).hexdigest()[:12]


def _meta_path(cache_dir: Path, stage_name: str) -> Path:
    return cache_dir / f"{stage_name}.meta.json"


def stage_is_fresh(
    cache_dir: Path,
    stage_name: str,
    version: int,
    params: dict[str, Any],
    outputs: list[Path],
) -> bool:
    meta_path = _meta_path(cache_dir, stage_name)
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if meta.get("version") != version or meta.get("params") != params_digest(params):
        return False
    return all(o.exists() for o in outputs)


def write_stage_meta(
    cache_dir: Path,
    stage_name: str,
    version: int,
    params: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> None:
    """Record a step's cache meta. `extra` is provenance only -- stored in the
    meta file for humans/debugging but never part of the freshness comparison
    (stage_is_fresh reads only "version" and "params"), so recording e.g. which
    model was actually used after a fallback can't invalidate the cache."""
    _meta_path(cache_dir, stage_name).write_text(
        json.dumps({"version": version, "params": params_digest(params), **(extra or {})})
    )
