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
# openssh-client: the execution/discovery transports exec the real ssh(1)
# (mai.execution.ssh ssh_arguments / commands.execute_command) — the slim
# base ships no ssh binary, and Popen raises OSError → 503 "transport
# unavailable" without it. Found in M-3 smoke.
# 1password-cli: the optional op:// credential provider
# (mai.credentials.onepassword) execs the real op(1) the same way. Installed
# from the official 1Password apt repository
# (developer.1password.com/docs/cli/get-started, "APT" lane): the archive
# key is dearmored into a signed-by keyring, the repo is bound to dpkg's
# architecture and the `stable` suite, and apt verifies the repo metadata
# against that keyring. The op >= 2.18.0 service-account floor is enforced
# at runtime by OP_SERVICE_ACCOUNT_FLOOR, so no apt-level pin is needed.
# curl+gnupg are fetch/verify tooling and stay installed (same posture as
# openssh-client).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg openssh-client \
 && curl -fsSL https://downloads.1password.com/linux/keys/1password.asc \
    | gpg --dearmor -o /usr/share/keyrings/1password-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/1password-archive-keyring.gpg] https://downloads.1password.com/linux/debian/$(dpkg --print-architecture) stable main" \
    > /etc/apt/sources.list.d/1password.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends 1password-cli \
 && rm -rf /var/lib/apt/lists/* \
 && op --version \
 && groupadd --system --gid 10001 mai \
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
