# Live demo of the Agent Game Review evidence browser.
#
# Builds the package with the HTTP transport, bakes the GR-4 demo store (real
# Terminal-Bench runs + their pre-computed model reviews, plus the synthetic
# comparison slice) into the image, and serves the read API + SPA. Portable across any container host
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
# layer caches across source-only changes. LICENSE has to be here too:
# pyproject.toml's license-files = ["LICENSE"] pattern must match a real file
# on modern setuptools, or the build now warns (and errors from 2027-02-18).
COPY pyproject.toml README.md LICENSE ./
COPY agr ./agr
RUN pip install --upgrade pip && pip install ".[api]"

# Bake the read-only demo store into the image at build time. It ships the real
# published runs and their pre-computed model reviews (no credentials, no model
# calls) alongside the synthetic comparison slice (source_type=synthetic_demo).
RUN python -m agr --store /app/.agr-demo demo-store

# The platform provides $PORT at runtime; AGR_HOST=0.0.0.0 (set above) makes the
# server reachable from outside the container. Fall back to 8000 for a plain
# `docker run -p 8000:8000`.
#
# P0-5: this is a public, unauthenticated demo, so it serves --read-only — no
# visitor can write a disposition, feedback, or lesson into the baked-in store.
EXPOSE 8000
CMD ["sh", "-c", "agr --store /app/.agr-demo serve --host 0.0.0.0 --port ${PORT:-8000} --read-only"]
