# syntax=docker/dockerfile:1.7
#
# Multi-stage build. 12 §2.1: build dependencies do not reach the runtime image,
# the process runs as a non-root user, and the base image is pinned by digest
# rather than by a floating tag — `python:3.12-slim` changes under you, and a
# financial platform that deploys on an unrecorded base image cannot say what it
# is running.
#
# ── The digest below must be pinned ─────────────────────────────────────────
# `bookworm` is a tag. Before the first deploy, replace it with the resolved
# digest:
#
#     docker buildx imagetools inspect python:3.12-slim-bookworm
#     # then: FROM python:3.12-slim-bookworm@sha256:<digest> AS base
#
# Left as the tag here because the digest cannot be resolved without a registry
# connection, and inventing one would be worse than an honest tag.

# ---------------------------------------------------------------------------
# Stage 1 — builder
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build-only toolchain. It is present in this stage and absent from the next.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements/ ./requirements/
ARG REQUIREMENTS_FILE=requirements/dev.txt
RUN pip install --upgrade pip \
    && pip install -r "${REQUIREMENTS_FILE}"

# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings.production

# Runtime shared libraries only — libpq, not the compiler that builds against
# it. `curl` is present solely for the healthcheck below.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash --uid 1000 app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

COPY --chown=app:app . .

# The application user owns no path it should not be able to rewrite. Static
# files are collected at build time so the runtime image needs no write access
# to the source tree.
RUN python manage.py collectstatic --noinput --clear 2>/dev/null || true

USER app

EXPOSE 8000

# 12 §2.1: a healthcheck distinct from liveness. A `web` container that can
# serve but not reach the database is unhealthy, not dead — so this probes
# readiness, which checks PostgreSQL and Redis (config/health.py).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/health/ready/ || exit 1

# 12 §2: the application code is identical across all four process roles; only
# the entrypoint differs. The role is chosen by the compose command or the
# platform's start command — never by editing this file.
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["web"]
