# syntax=docker/dockerfile:1
#
# Multi-stage build.
#
#   base    - shared OS layer + non-root user, used by every other stage
#   builder - installs the package (and its pinned deps) into a venv
#   dev     - builder + test/lint tools; the base image for the devcontainer
#   runtime - "base" (NOT builder/dev, so no build tooling) + the venv from
#             "builder" copied in; this is what docker-compose.yml runs
#
# The devcontainer and the runtime image share the same "base" stage, so the
# interpreter, OS packages and non-root user are identical to production; only
# the dev tooling on top differs.

ARG PYTHON_VERSION=3.12-slim

FROM python:${PYTHON_VERSION} AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:${PATH}"

# Non-root user shared by dev and runtime stages (fixed uid/gid so bind
# mounts from the host keep sane permissions in the devcontainer).
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --create-home --shell /bin/bash appuser

WORKDIR /app


# ---------------------------------------------------------------------------
FROM base AS builder

RUN python -m venv /opt/venv

COPY pyproject.toml README.md ./
COPY stiebel_heatpump ./stiebel_heatpump

# Installing the package pulls in exactly the dependencies declared in
# pyproject.toml - that file is the single source of truth, there is no
# separate requirements.txt to keep in sync.
RUN pip install --upgrade pip \
    && pip install .


# ---------------------------------------------------------------------------
# Adds test/lint tooling on top of "builder". Used as the devcontainer image;
# source code is bind-mounted over /app by the devcontainer rather than baked
# in here, so editable-install it once the mount is live (see devcontainer.json
# postCreateCommand).
FROM builder AS dev

RUN pip install '.[dev]' \
    && apt-get update \
    && apt-get install --no-install-recommends -y git less procps \
    && rm -rf /var/lib/apt/lists/* \
    # The devcontainer bind-mounts the workspace over /app and reinstalls the
    # package in editable mode as `appuser` (see devcontainer.json), so that
    # user needs write access to the venv.
    && chown -R appuser:appuser /opt/venv

USER appuser
CMD ["bash"]


# ---------------------------------------------------------------------------
# Slim final image: no compilers/build tooling, just the venv + runtime data.
FROM base AS runtime

COPY --from=builder /opt/venv /opt/venv
COPY device_configs ./device_configs
COPY config ./config

USER appuser
EXPOSE 8000

ENTRYPOINT ["stiebel-heatpump-api"]
CMD ["--host", "0.0.0.0", "--port", "8000"]
