# ================================================================
# Multi-Stage Dockerfile for Production
# ================================================================

# ----------------------------------------------------------------
# Stage 1: Builder - Install Dependencies with Poetry
# ----------------------------------------------------------------
FROM python:3.11-slim AS builder

# Set environment variables for build
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_NO_INTERACTION=1

# The venv is built at the path it runs from (/app/.venv): its console
# scripts (uvicorn, alembic) name their interpreter by absolute path.
WORKDIR /app

# Install system build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry
RUN curl -sSL https://install.python-poetry.org | python3 - && \
    ln -s /root/.local/bin/poetry /usr/local/bin/poetry

# Copy dependency files
COPY pyproject.toml poetry.lock* ./

# Install dependencies (ohne dev dependencies, ohne das Projekt selbst)
# --only main installiert nur Produktions-Dependencies
RUN poetry install --no-root --no-interaction --no-ansi --only main 2>/dev/null || \
    poetry install --no-root --no-interaction --no-ansi

# The venv's own pip is not needed to run the service, and it bundles copies of
# urllib3, msgpack and pkg_resources that Trivy reports (see the runtime stage).
RUN .venv/bin/python -m pip uninstall -y pip

# ----------------------------------------------------------------
# Stage 2: Runtime - Production Image
# ----------------------------------------------------------------
FROM python:3.11-slim

# Metadata
LABEL maintainer="your.email@example.com" \
      version="1.0.0" \
      description="FastAPI Backend with Auth, Git & Kubernetes"

# Non-root user with a fixed UID: Kubernetes can only verify runAsNonRoot
# for a numeric user (USER below), and git needs a passwd entry for it.
RUN useradd --uid 10001 --user-group --home-dir /nonexistent --no-create-home \
    --shell /usr/sbin/nologin appuser

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PATH=/app/.venv/bin:$PATH \
    APP_HOME=/app

# Install runtime system dependencies. ``apt-get upgrade`` runs first so
# the base ``python:3.11-slim`` tag picks up Debian-security backports
# released after the upstream image was last rebuilt — that's where
# things like CVE-2026-45447 (openssl 3.5.6-1~deb13u2) come from. Trivy
# blocks the push on any HIGH/CRITICAL OS finding, so even though it
# enlarges the layer slightly we'd rather take the bytes than burn a
# .trivyignore line every time upstream debian releases a CVE-fix.
# SECURITY_REFRESH changes on every CI build (ci.yml passes the run id), so this
# layer is never taken from the build cache: a cached layer kept the package
# state of its first build and missed every Debian fix released since (the
# Trivy gate then failed on e.g. libbrotli1 CVE-2026-111497 although
# 1.1.0-2+deb13u1 was out).
ARG SECURITY_REFRESH=""
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    git \
    postgresql-client \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# No pip, setuptools or wheel in the base image's Python: the service runs
# from /app/.venv and installs nothing at runtime. Trivy scans these system
# site-packages too, and pip bundles its own urllib3, msgpack and
# pkg_resources (pip/_vendor/bom.cdx.json); even the newest pip ships
# versions with HIGH CVEs, so removing it is the fix, not an upgrade.
RUN python -m pip uninstall -y pip setuptools wheel

# Create application directory
WORKDIR $APP_HOME

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Application code, owned by root: the service reads it, it cannot change
# it. It writes only below /tmp (sparse clones of app repositories; an
# emptyDir in Kubernetes, where the root filesystem is read-only).
COPY . .
RUN mkdir -p /tmp/repos && chown 10001:10001 /tmp/repos

# Switch to non-root user
USER 10001:10001

# Expose application port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Default environment variables (can be overridden). HOST "::" on an
# IPv6-only or dual-stack cluster. FORWARDED_ALLOW_IPS: whose X-Forwarded-*
# headers count (the reverse proxy in front; "*" only where nothing but the
# proxy can reach this port). The default trusts nobody but localhost.
ENV DEBUG=False \
    PORT=8000 \
    HOST=0.0.0.0 \
    WORKERS=4 \
    FORWARDED_ALLOW_IPS=127.0.0.1 \
    LOG_LEVEL=info

# Run uvicorn directly
CMD ["sh", "-c", "exec uvicorn app.main:app --host \"$HOST\" --port \"$PORT\" --workers \"$WORKERS\" --proxy-headers --forwarded-allow-ips \"$FORWARDED_ALLOW_IPS\""]