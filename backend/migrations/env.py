"""Alembic migration environment for the MAI application schema chain.

Contract anchors:

- The database URL comes from the environment through
  :func:`mai.database.resolve_database_url` — the single secret reference
  (contracts.md C03/C05). No URL is stored in alembic.ini.
- A missing/unreachable database aborts with a classified health code
  (card 01-db acceptance criterion 2) instead of a traceback that could echo
  connection material.
- This chain covers the MAI application schema only. DBOS 3.2.0 creates and
  owns its ``dbos`` system schema at engine launch (job-engine ADR, run-state
  ownership); those tables are intentionally absent from this chain.
- ``target_metadata`` stays ``None``: schema-bearing cards add revisions under
  ``migrations/versions/`` (one linear chain, exclusive ``migration-chain``
  resource) together with their own autogenerate metadata.
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from mai.database import (
    DatabaseConfigError,
    classify_database_exception,
    resolve_database_url,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _abort(detail: str) -> None:
    """Abort with a classified one-line failure (exit code 2)."""
    print(f"migration aborted: {detail}", file=sys.stderr)
    sys.exit(2)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live connection."""
    try:
        url = resolve_database_url()
    except DatabaseConfigError as exc:
        _abort(f"health={exc.health.value} {exc}")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    try:
        url = resolve_database_url()
    except DatabaseConfigError as exc:
        _abort(f"health={exc.health.value} {exc}")
    engine = create_async_engine(url, poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
    except Exception as exc:  # noqa: BLE001 - classified abort is the contract
        status = classify_database_exception(exc)
        _abort(f"health={status.health.value} detail={status.detail}")
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
