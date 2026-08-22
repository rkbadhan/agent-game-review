# Live demo of the Agent Game Review evidence browser.
#
# Builds the package with the HTTP transport, bakes the synthetic demo store into
# the image, and serves the read API + SPA. Portable across any container host
# (Fly.io, Render, Railway, a plain VM) — the platform injects $PORT and the
# server binds it via the env-aware defaults in `agr serve`.
FROM python:3.12-slim

# Keep Python lean and unbuffered so logs stream to the platform.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    AGR_HOST=0.0.0.0

WORKDIR /app

# Install first with only the metadata needed for dependency resolution so the
# layer caches across source-only changes.
COPY pyproject.toml README.md ./
COPY agr ./agr
RUN pip install --upgrade pip && pip install ".[api]"

# Bake the read-only synthetic demo store into the image at build time. The demo
# store is the only fabricated data in the package (source_type=synthetic_demo)
# and needs no credentials or model calls.
RUN python -m agr --store /app/.agr-demo demo-store

# The platform provides $PORT at runtime; AGR_HOST=0.0.0.0 (set above) makes the
# server reachable from outside the container. Fall back to 8000 for a plain
# `docker run -p 8000:8000`.
EXPOSE 8000
CMD ["sh", "-c", "agr --store /app/.agr-demo serve --host 0.0.0.0 --port ${PORT:-8000}"]
