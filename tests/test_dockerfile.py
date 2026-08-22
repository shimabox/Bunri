"""Static checks on the Dockerfile itself.

CI runs `uv sync` on the host (see .github/workflows/ci.yml), never inside a
built Docker image, so a missing `--extra web` on one of the Dockerfile's own
`uv sync` lines would never be caught by CI's own test run -- only a Docker
build (which CI deliberately doesn't do; see docs/plans's "やらないこと")
would surface it, and only at image-build time, not at pytest time. This
regression-tests the specific bug the dev-deps/dev stages had: their `uv
sync` lines installed the locked dependencies without the `web` extra, so
`import fastapi` (and therefore every tests/test_web_*.py test) failed inside
the `dev` image despite passing on a host `uv sync --extra web`.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOCKERFILE = _REPO_ROOT / "Dockerfile"


def _uv_sync_lines(stage_lines: list[str]) -> list[str]:
    return [line for line in stage_lines if "uv sync" in line]


def _stage_lines(dockerfile_text: str, stage_name: str) -> list[str]:
    """Lines belonging to `FROM ... AS <stage_name>` up to (not including)
    the next `FROM` line, or end of file."""
    lines = dockerfile_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.upper().startswith("FROM") and stripped.endswith(f"AS {stage_name}"):
            start = i
            break
    assert start is not None, f"stage {stage_name!r} not found in Dockerfile"
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip().upper().startswith("FROM"):
            end = i
            break
    return lines[start:end]


def test_dockerfile_exists():
    assert _DOCKERFILE.exists()


def test_dev_deps_stage_uv_sync_includes_web_extra():
    text = _DOCKERFILE.read_text(encoding="utf-8")
    sync_lines = _uv_sync_lines(_stage_lines(text, "dev-deps"))
    assert sync_lines, "expected at least one `uv sync` line in the dev-deps stage"
    assert all("--extra web" in line for line in sync_lines), sync_lines


def test_dev_stage_uv_sync_includes_web_extra():
    text = _DOCKERFILE.read_text(encoding="utf-8")
    sync_lines = _uv_sync_lines(_stage_lines(text, "dev"))
    assert sync_lines, "expected at least one `uv sync` line in the dev stage"
    assert all("--extra web" in line for line in sync_lines), sync_lines


def test_deps_and_cpu_runtime_stages_deliberately_omit_web_extra():
    """Sanity guard against an over-broad fix: the shipped runtime image
    (`deps` -> `cpu`) must stay web-free -- the web extra (fastapi/uvicorn)
    is dev/test tooling for exercising the web UI, not something the CLI
    runtime image needs to carry."""
    text = _DOCKERFILE.read_text(encoding="utf-8")
    sync_lines = _uv_sync_lines(_stage_lines(text, "deps"))
    assert sync_lines
    assert all("--extra web" not in line for line in sync_lines), sync_lines


# --- StemLab -> Bunri rename regressions ------------------------------------
#
# The cpu/cuda stages' ENTRYPOINT, the model-cache env var, and the mount
# path are all things a copy-paste-driven rename could silently miss (the
# cuda stage in particular duplicates cpu's ENV/ENTRYPOINT rather than
# inheriting it -- see the stage comment above it), so pin them explicitly
# rather than relying on `git grep -i stemlab` staying clean forever.


def test_cpu_stage_entrypoint_is_bunri():
    text = _DOCKERFILE.read_text(encoding="utf-8")
    lines = _stage_lines(text, "cpu")
    entrypoints = [line.strip() for line in lines if line.strip().startswith("ENTRYPOINT")]
    assert entrypoints == ['ENTRYPOINT ["bunri"]'], entrypoints


def test_cuda_stage_entrypoint_is_bunri():
    text = _DOCKERFILE.read_text(encoding="utf-8")
    lines = _stage_lines(text, "cuda")
    entrypoints = [line.strip() for line in lines if line.strip().startswith("ENTRYPOINT")]
    assert entrypoints == ['ENTRYPOINT ["bunri"]'], entrypoints


def test_cpu_and_cuda_stages_use_bunri_model_dir():
    text = _DOCKERFILE.read_text(encoding="utf-8")
    for stage in ("uv-base", "cuda"):
        lines = _stage_lines(text, stage)
        env_text = "\n".join(lines)
        assert "BUNRI_MODEL_DIR=/root/.cache/bunri/models" in env_text, (stage, lines)
        assert "STEMLAB_MODEL_DIR" not in env_text, (stage, lines)


def test_no_stemlab_entry_point_remains():
    text = _DOCKERFILE.read_text(encoding="utf-8")
    assert "stemlab" not in text.lower()
