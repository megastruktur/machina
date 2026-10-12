"""MAI PostgreSQL access path: URL resolution, app engine ownership, health.

Ownership map (contracts.md C02/C05, job-engine ADR ``bc8d264``):

- ``resolve_database_url`` is the single secret reference for the database:
  every consumer (this module's async engine, the DBOS 3.2.0 durable engine
  via ``DBOSConfig.database_url``, the Alembic chain in
  ``backend/migrations/env.py``) resolves the URL from the environment; the
  URL is never hardcoded or stored in config files.
- The application query path (FastAPI/ASGI, profile projections) owns ONE
  async engine created here. No sync application engine exists: the durable
  engine builds its own connections from the same resolved URL, so a second
  app-side pool would be a competing owner, not a convenience (C05 rule 1:
  DBOS tables are the canonical run-state map; no second job-state authority).
- DBOS creates and owns its ``dbos`` system schema at engine launch
  (job-engine ADR, run-state ownership); this module never creates or
  migrates those tables. Schema revisions for the MAI application tables live
  in the linear Alembic chain under ``backend/migrations/versions/``
  (migration-chain resource, ``mai_SERVICES.md``).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

ENV_VAR = "MAI_DATABASE_URL"

_APPLICATION_NAME = "mai"

# The application engine runs on psycopg3 (async-capable dialect); bare
# ``postgresql://`` is accepted and kept as-is because SQLAlchemy resolves it
# to the same installed psycopg3 dialect. Drivers without a locked dependency
# (e.g. ``postgresql+asyncpg``) are rejected instead of failing at connect.
_ALLOWED_DRIVERS = frozenset({"postgresql", "postgresql+psycopg"})

_DETAIL_LIMIT = 300


class DatabaseHealth(StrEnum):
    """Classified database health (card 01-db acceptance criterion 2)."""

    OK = "ok"
    NOT_CONFIGURED = "not_configured"
    INVALID_CONFIG = "invalid_config"
    UNREACHABLE = "unreachable"
    AUTH_FAILED = "auth_failed"
    DATABASE_MISSING = "database_missing"


@dataclass(frozen=True, slots=True)
class DatabaseStatus:
    """Health classification with a secret-free detail string."""

    health: DatabaseHealth
    detail: str

    @property
    def ok(self) -> bool:
        return self.health is DatabaseHealth.OK


class DatabaseConfigError(RuntimeError):
    """Configuration failure carrying its health classification."""

    def __init__(self, health: DatabaseHealth, message: str) -> None:
        super().__init__(message)
        self.health = health


def _make_validated_url(url: str) -> URL:
    try:
        parsed = make_url(url)
    except ArgumentError as exc:
        raise DatabaseConfigError(
            DatabaseHealth.INVALID_CONFIG, f"unparseable database URL: {exc}"
        ) from exc
    if parsed.drivername not in _ALLOWED_DRIVERS:
        raise DatabaseConfigError(
            DatabaseHealth.INVALID_CONFIG,
            f"unsupported database driver {parsed.drivername!r};"
            f" expected one of {sorted(_ALLOWED_DRIVERS)}",
        )
    return parsed


def resolve_database_url(env: Mapping[str, str] | None = None) -> str:
    """Resolve the PostgreSQL URL from ``MAI_DATABASE_URL``.

    Raises :class:`DatabaseConfigError` classified as ``not_configured`` when
    the variable is absent/blank and ``invalid_config`` when the value is not
    a PostgreSQL (psycopg3) URL.
    """
    source = os.environ if env is None else env
    raw = source.get(ENV_VAR, "").strip()
    if not raw:
        raise DatabaseConfigError(
            DatabaseHealth.NOT_CONFIGURED,
            f"{ENV_VAR} is not set; configure the PostgreSQL secret reference",
        )
    _make_validated_url(raw)
    return raw


def create_app_engine(
    url: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Create the single application-side async engine and its sessionmaker.

    Connections are tagged with ``application_name=mai`` (unless the URL
    already sets one) so connection cleanup can be verified against
    ``pg_stat_activity``.
    """
    parsed = _make_validated_url(url)
    if not parsed.query.get("application_name"):
        parsed = parsed.set(query={**parsed.query, "application_name": _APPLICATION_NAME})
    engine = create_async_engine(parsed, pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _safe_detail(orig: BaseException) -> str:
    detail = f"{type(orig).__name__}: {orig}"
    if len(detail) > _DETAIL_LIMIT:
        detail = detail[: _DETAIL_LIMIT - 3] + "..."
    return detail


def _sqlstate_of(orig: BaseException) -> str | None:
    """Read the SQLSTATE from a psycopg error, when one exists as data.

    Server-side statement errors expose ``sqlstate`` directly. Connection-phase
    errors (rejected credentials, missing database, refused connections) carry
    no SQLSTATE object at all — libpq exposes only the server FATAL text, so
    those are classified from the message in
    :func:`classify_database_exception`.
    """
    sqlstate = getattr(orig, "sqlstate", None)
    if sqlstate:
        return str(sqlstate)
    diag_sqlstate = getattr(getattr(orig, "diag", None), "sqlstate", None)
    if diag_sqlstate:
        return str(diag_sqlstate)
    return None


# Connection-phase failures surface the PostgreSQL server FATAL text inside
# the psycopg message with no SQLSTATE field (observed against Postgres 18
# with psycopg 3.3: wrong password and missing database both arrive as
# ``psycopg.OperationalError`` with ``sqlstate=None``). The two markers below
# match server-generated wording that has been stable across PostgreSQL
# versions; anything unrecognized stays ``unreachable``.
_AUTH_MESSAGE_MARKER = "password authentication failed"
_MISSING_DB_MESSAGE_MARKER = 'database "'


def _message_of(orig: BaseException) -> str:
    return str(orig)


def classify_database_exception(exc: BaseException) -> DatabaseStatus:
    """Map a connection/query failure to its :class:`DatabaseStatus`.

    Classification order: psycopg SQLSTATE when present as data (``28P01``
    rejected credentials, ``3D000`` database does not exist); then the
    connection-phase server message (rejected password, missing database —
    libpq connection errors carry no SQLSTATE field); everything else —
    server down, DNS, refused, timeout, protocol — is ``unreachable``.
    Details never include the connection URL.
    """
    orig = getattr(exc, "orig", exc)
    sqlstate = _sqlstate_of(orig)
    if sqlstate == "28P01":
        return DatabaseStatus(DatabaseHealth.AUTH_FAILED, _safe_detail(orig))
    if sqlstate == "3D000":
        return DatabaseStatus(DatabaseHealth.DATABASE_MISSING, _safe_detail(orig))
    message = _message_of(orig).lower()
    if _AUTH_MESSAGE_MARKER in message:
        return DatabaseStatus(DatabaseHealth.AUTH_FAILED, _safe_detail(orig))
    if _MISSING_DB_MESSAGE_MARKER in message and "does not exist" in message:
        return DatabaseStatus(DatabaseHealth.DATABASE_MISSING, _safe_detail(orig))
    return DatabaseStatus(DatabaseHealth.UNREACHABLE, _safe_detail(orig))


async def check_database(url: str | None) -> DatabaseStatus:
    """Run one classified connect + ``SELECT 1`` round trip.

    Never raises: every failure returns a classified :class:`DatabaseStatus`.
    Uses a :class:`NullPool` engine that is disposed after the check, so a
    health probe leaves no pooled connections behind.
    """
    if url is None or not url.strip():
        return DatabaseStatus(
            DatabaseHealth.NOT_CONFIGURED, f"{ENV_VAR} is not set"
        )
    try:
        parsed = _make_validated_url(url.strip())
    except DatabaseConfigError as exc:
        return DatabaseStatus(exc.health, str(exc))
    engine = create_async_engine(parsed, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - classification is the contract
        return classify_database_exception(exc)
    finally:
        await engine.dispose()
    return DatabaseStatus(
        DatabaseHealth.OK, "connected; SELECT 1 round trip succeeded"
    )
