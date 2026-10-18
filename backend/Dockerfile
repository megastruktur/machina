# syntax=docker/dockerfile:1
# mai-api image (card 01-compose). mai-worker reuses this same image with an
# explicit different command (SERVICES row: one backend package, no second
# build). ASGI server choice belongs to the compose lane per
# backend/pyproject.toml — uvicorn is pinned HERE, outside the locked
# dependency set, and stays out of backend/uv.lock.
#
# Pins: python:3.14-slim (requires-python >=3.14) and uv 0.11.26 (the exact uv
# recorded by 00-environment/01-host-toolchain and used for backend/.venv).

FROM python:3.14-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
COPY backend/src ./src
# --locked: never resolve anything outside backend/uv.lock. --no-editable: the
# project is installed into site-packages so the runtime stage needs no source
# tree. --no-dev: pytest stays out of the image.
RUN uv sync --locked --no-dev --no-editable \
 && uv pip install --python /app/.venv/bin/python "uvicorn==0.54.0"

FROM python:3.14-slim
# Non-root runtime user; /var/lib/mai is pre-created with this ownership so the
# named volume `mai-data` inherits it on first use (C02 rule 1: service-owned
# writable data root).
RUN groupadd --system --gid 10001 mai \
 && useradd --system --uid 10001 --gid mai --home-dir /var/lib/mai --no-create-home mai \
 && mkdir -p /var/lib/mai \
 && chown mai:mai /var/lib/mai
COPY --from=build --chown=10001:10001 /app/.venv /app/.venv
# Migration harness rides along (card 01-db chain); nothing auto-runs it.
# Two separate COPYs: a multi-source COPY flattens both sources into the
# destination, which broke script_location = %(here)s/migrations (the
# versions/env.py landed at /app instead of /app/migrations — found during
# M-2 bring-up when the one-shot `alembic upgrade head` failed).
COPY backend/alembic.ini /app/alembic.ini
COPY backend/migrations/ /app/migrations/
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1
WORKDIR /app
USER 10001:10001
EXPOSE 8000
# Factory form documented by mai.api.app.
CMD ["uvicorn", "mai.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
