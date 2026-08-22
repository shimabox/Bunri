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

from bunri.safepath import is_real_file_in, replace_into


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
    # `exists()` was the whole check here, and `exists()` follows symlinks.
    # Leaving a valid meta in place and swapping an artifact for a link to
    # somewhere else therefore read as "cached", and the link's target went
    # out in the user's package. Protecting the writes does nothing if the
    # reads trust whatever is sitting there, so every file this function
    # vouches for -- the meta included -- has to be a real file, in this
    # directory. Anything else is stale: the stage re-runs, which is only
    # ever a cost in time.
    #
    # The directory itself being genuine is the caller's guarantee (package.py
    # gets it from safepath.verified_mkdir); this is about the files in it.
    expected_dir = cache_dir.resolve()
    meta_path = _meta_path(cache_dir, stage_name)
    if not is_real_file_in(meta_path, expected_dir):
        return False
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if meta.get("version") != version or meta.get("params") != params_digest(params):
        return False
    return all(is_real_file_in(o, expected_dir) for o in outputs)


def clear_stage_meta(cache_dir: Path, stage_name: str) -> None:
    """Drop a stage's meta, so nothing it used to vouch for counts as fresh
    until the stage completes and writes a new one.

    Called just before a stage recomputes. Without it, a run that died
    part-way through left the old meta next to a half-replaced set of
    artifacts -- separate() moves the target stem into the cache before it
    writes the backing track, so an interruption between the two leaves a new
    target beside an old backing, both present, both vouched for by a meta
    that describes neither. The next run called that fresh and packaged the
    mismatched pair.

    `unlink` removes the name, so a meta that has been replaced by a symlink
    is unlinked rather than followed.
    """
    _meta_path(cache_dir, stage_name).unlink(missing_ok=True)


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
    payload = json.dumps({"version": version, "params": params_digest(params), **(extra or {})})
    # Written through replace_into like every other file this app produces:
    # the name is derivable from the input digest and the stage, so a symlink
    # can be waiting at it, and write_text would follow the link and overwrite
    # whatever it points at. See bunri/safepath.py.
    replace_into(
        _meta_path(cache_dir, stage_name),
        lambda tmp: tmp.write_text(payload, encoding="utf-8"),
    )
