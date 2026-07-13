# syntax=docker/dockerfile:1
#
# Build targets (select with `docker build --target <name>`):
#   cpu   (default) — runtime image, ENTRYPOINT ["stemlab"]
#   dev   — cpu + dev dependencies (pytest, playwright+chromium) for running the test suite
#   cuda  — GPU runtime image (nvidia CUDA base). Build-only on this project: no arm64/macOS
#           Docker host can actually run a GPU container, so this target has never been
#           executed end-to-end, only build-verified. See NOTES.md.
#
# uv is vendored from its official distroless image (COPY --from pattern) rather than
# pip-installed, per astral's recommended Docker recipe.

ARG UV_VERSION=0.11.14
ARG PYTHON_VERSION=3.13

# `COPY --from=` can't expand an ARG inside the image reference directly, so
# pull the uv image into its own named stage first and copy from that stage.
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv-dist

##############################################################################
# uv-base: common OS packages + uv binary, shared by the cpu/dev lineage.
##############################################################################
FROM python:${PYTHON_VERSION}-slim AS uv-base
COPY --from=uv-dist /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    STEMLAB_MODEL_DIR=/root/.cache/stemlab/models
# STEMLAB_MODEL_DIR pins the container's model location explicitly: on the
# host the default is now <project>/models (delete the folder, delete
# everything), but containers persist weights in the stemlab-models named
# volume, whose canonical mount point stays /root/.cache/stemlab/models.

# ffmpeg: required by stemlab's audio pipeline (mp3 <-> wav conversion).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

##############################################################################
# build-base: uv-base + a C compiler. audio-separator -> diffq==0.2.4 has no
# manylinux wheel for some platforms (notably linux/arm64), so uv falls back
# to building its sdist, which needs gcc. Kept out of uv-base so the final
# runtime images (cpu, dev) don't have to carry a toolchain.
##############################################################################
FROM uv-base AS build-base
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

##############################################################################
# deps: install locked runtime deps only, without the project source, so this
# (expensive) layer is cached independently of day-to-day source edits.
##############################################################################
FROM build-base AS deps
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

##############################################################################
# app-build: add the project source on top of the cached deps layer and
# install stemlab itself (still on build-base, in case any dep needs to
# recompile against it — stemlab itself is pure Python/uv_build, no compiler
# needed for this step alone).
##############################################################################
FROM deps AS app-build
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

##############################################################################
# cpu (default target): copy the fully-built venv + source onto the slim
# uv-base (no compiler, no build cache) to keep the shipped image small.
##############################################################################
FROM uv-base AS cpu
COPY --from=app-build /app/.venv /app/.venv
COPY --from=app-build /app/src /app/src

ENV PATH="/app/.venv/bin:${PATH}"
ENTRYPOINT ["stemlab"]

##############################################################################
# dev-deps / dev: same lineage but with the dev dependency group (pytest,
# playwright) and a real Chromium install, so the test suite (including the
# HTML player's Playwright-driven tests) can run inside the container. Left
# on build-base (not slimmed) since this image is a test runner, not shipped.
##############################################################################
FROM build-base AS dev-deps
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

FROM dev-deps AS dev
COPY README.md ./
COPY src ./src
COPY tests ./tests
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

ENV PATH="/app/.venv/bin:${PATH}"

# playwright install --with-deps pulls the browser binary plus the apt packages
# it needs (fonts, libnss3, etc.) — apt is available since we're still on the
# python:slim (Debian) base.
RUN playwright install --with-deps chromium

ENTRYPOINT ["pytest"]
CMD ["-q"]

##############################################################################
# cuda: GPU runtime image. linux/amd64 ONLY — onnxruntime-gpu publishes no
# aarch64 wheels on PyPI (checked 1.27.0: manylinux x86_64 + win_amd64 only),
# so dependency resolution is impossible for linux/arm64. On an Apple
# Silicon / arm64 host, build with:
#   docker build --platform linux/amd64 --target cuda
#
# NOT built on top of the cpu/deps lineage above, because pyproject.toml's
# [tool.uv.sources] pins *all* linux resolutions of torch/torchvision to the
# CPU wheel index (see NOTES.md) — correct for the cpu/dev targets, wrong
# here. Instead this stage builds its own venv with `uv pip install`, using
# uv's built-in --torch-backend (via UV_TORCH_BACKEND) to fetch CUDA wheels,
# and audio-separator's [gpu] extra (onnxruntime-gpu) instead of [cpu]
# (plain onnxruntime).
#
# Version alignment (all CUDA 13):
#   - onnxruntime-gpu 1.27 (what audio-separator[gpu]'s >=1.17 resolves to)
#     targets CUDA 13 on PyPI; its release notes deprecate the CUDA 12 build.
#   - torch 2.13.0 (the version in uv.lock) is published for cu126/cu129/
#     cu130; cu130 is the CUDA 13 build.
#   - base nvidia/cuda:13.1.2-runtime-ubuntu24.04 provides the matching
#     CUDA 13 runtime environment.
##############################################################################
FROM nvidia/cuda:13.1.2-runtime-ubuntu24.04 AS cuda

# build-essential: diffq==0.2.4 (audio-separator dep) publishes no cp313
# wheels at all, so its C extension is always built from the sdist.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv-dist /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_TORCH_BACKEND=cu130 \
    PYTHONUNBUFFERED=1 \
    STEMLAB_MODEL_DIR=/root/.cache/stemlab/models

WORKDIR /app

# nvidia/cuda's Ubuntu base has no Python 3.13 package; let uv fetch a
# standalone build instead of wiring up deadsnakes/apt.
RUN uv python install 3.13

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv venv --python 3.13 /app/.venv
ENV VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

# Deliberately not `uv sync --frozen`: see stage comment above.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --prerelease=if-necessary \
        "audio-separator[gpu]==0.44.3" \
        "jinja2>=3.1" \
        "numpy<2.5" \
        rich \
        soundfile \
        torch \
        torchvision \
        typer
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --no-deps -e .

ENTRYPOINT ["stemlab"]
