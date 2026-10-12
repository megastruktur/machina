"""Focused tests for the MAI database path (card 01-db).

Unit tests are deterministic and need no external service. The ``unreachable``
negative control performs a real TCP connect against a closed loopback port —
a real connection failure, not a mock. Live-Postgres tests run only when
``MAI_TEST_DATABASE_URL`` points at a disposable fixture; the full feature-gate
scenario (fresh disposable Postgres, migration entrypoint from zero, verified
connection cleanup, unavailable-DB simulation) is recorded in
``mai_evidence/01-db/``.
"""

from __future__ import annotations

import os
import socket

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from mai.database import (
    DatabaseConfigError,
    DatabaseHealth,
    check_database,
    classify_database_exception,
    create_app_engine,
    resolve_database_url,
)

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

requires_live_postgres = pytest.mark.skipif(
    not os.environ.get(LIVE_URL_VAR),
    reason=f"{LIVE_URL_VAR} is not set; live path needs a disposable Postgres fixture",
)


def _closed_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --- URL resolution (acceptance 1: configured through secret reference/env) ---


def test_resolve_missing_env_is_classified() -> None:
    with pytest.raises(DatabaseConfigError) as excinfo:
        resolve_database_url(env={})
    assert excinfo.value.health is DatabaseHealth.NOT_CONFIGURED


def test_resolve_blank_env_is_classified() -> None:
    with pytest.raises(DatabaseConfigError) as excinfo:
        resolve_database_url(env={"MAI_DATABASE_URL": "   "})
    assert excinfo.value.health is DatabaseHealth.NOT_CONFIGURED


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://mai:pw@localhost:5432/mai",
        "postgresql+psycopg://mai:pw@localhost:5432/mai",
    ],
)
def test_resolve_accepts_postgres_urls(url: str) -> None:
    assert resolve_database_url(env={"MAI_DATABASE_URL": url}) == url


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///./local.db",
        "mysql://mai:pw@localhost/mai",
        "postgresql+asyncpg://mai:pw@localhost:5432/mai",
        "not-a-url",
    ],
)
def test_resolve_rejects_non_postgres_or_unparseable(url: str) -> None:
    with pytest.raises(DatabaseConfigError) as excinfo:
        resolve_database_url(env={"MAI_DATABASE_URL": url})
    assert excinfo.value.health is DatabaseHealth.INVALID_CONFIG


# --- engine ownership (acceptance 1: async app engine, DBOS owns its own) ---


def test_create_app_engine_tags_application_name() -> None:
    engine, session_factory = create_app_engine(
        "postgresql://mai:pw@localhost:5432/mai"
    )
    try:
        assert engine.url.query["application_name"] == "mai"
        assert engine.driver == "postgresql+psycopg" or engine.url.drivername in (
            "postgresql",
            "postgresql+psycopg",
        )
        assert session_factory.kw["bind"] is engine
    finally:
        # creation connects to nothing; dispose closes the (empty) pool
        assert engine.sync_engine.pool is not None


def test_create_app_engine_preserves_existing_application_name() -> None:
    engine, _ = create_app_engine(
        "postgresql://mai:pw@localhost:5432/mai?application_name=other"
    )
    try:
        assert engine.url.query["application_name"] == "other"
    finally:
        engine.sync_engine.pool.dispose()


@pytest.mark.anyio
async def test_create_app_engine_disposes_cleanly() -> None:
    engine, _ = create_app_engine("postgresql://mai:pw@localhost:5432/mai")
    await engine.dispose()


# --- classified health (acceptance 2) ---


@pytest.mark.anyio
async def test_health_not_configured() -> None:
    status = await check_database(None)
    assert status.health is DatabaseHealth.NOT_CONFIGURED
    assert not status.ok


@pytest.mark.anyio
async def test_health_invalid_config() -> None:
    status = await check_database("sqlite:///./local.db")
    assert status.health is DatabaseHealth.INVALID_CONFIG
    assert not status.ok


@pytest.mark.anyio
async def test_health_unreachable_on_closed_loopback_port() -> None:
    url = (
        f"postgresql://mai:must-never-appear@127.0.0.1:{_closed_loopback_port()}/mai"
    )
    status = await check_database(url)
    assert status.health is DatabaseHealth.UNREACHABLE
    assert not status.ok
    # the URL (and any credential inside it) never leaks into health details
    assert "must-never-appear" not in status.detail


def test_classify_rejected_credentials() -> None:
    exc = OperationalError(
        "SELECT 1",
        {},
        psycopg.errors.InvalidPassword(
            'password authentication failed for user "mai"'
        ),
    )
    status = classify_database_exception(exc)
    assert status.health is DatabaseHealth.AUTH_FAILED
    assert "password authentication failed" in status.detail


def test_classify_missing_database() -> None:
    exc = OperationalError(
        "SELECT 1", {}, psycopg.errors.InvalidCatalogName('database "mai" does not exist')
    )
    status = classify_database_exception(exc)
    assert status.health is DatabaseHealth.DATABASE_MISSING


def test_classify_connection_refused_falls_back_to_unreachable() -> None:
    exc = OperationalError(
        "SELECT 1", {}, psycopg.OperationalError("connection refused")
    )
    status = classify_database_exception(exc)
    assert status.health is DatabaseHealth.UNREACHABLE


def _connection_phase_error(message: str) -> OperationalError:
    """Wrap a psycopg error exactly as observed for connection-phase failures:
    server FATAL text in the message, no SQLSTATE field anywhere on the
    exception (regression for the wrong-password / missing-database negative
    controls against Postgres 18 + psycopg 3.3)."""
    return OperationalError("SELECT 1", {}, psycopg.OperationalError(message))


def test_classify_connection_phase_auth_failure() -> None:
    exc = _connection_phase_error(
        'connection failed: connection to server at "127.0.0.1", port 53093'
        ' failed: FATAL:  password authentication failed for user "postgres"'
    )
    status = classify_database_exception(exc)
    assert status.health is DatabaseHealth.AUTH_FAILED


def test_classify_connection_phase_missing_database() -> None:
    exc = _connection_phase_error(
        'connection failed: connection to server at "127.0.0.1", port 53093'
        ' failed: FATAL:  database "no_such_db" does not exist'
    )
    status = classify_database_exception(exc)
    assert status.health is DatabaseHealth.DATABASE_MISSING


def test_classify_connection_phase_role_message_stays_unreachable() -> None:
    # 'role "x" does not exist' is an authorization failure, not a missing
    # database; only the database-marker wording may classify DATABASE_MISSING.
    exc = _connection_phase_error(
        'connection failed: FATAL:  role "mai" does not exist'
    )
    status = classify_database_exception(exc)
    assert status.health is DatabaseHealth.UNREACHABLE


# --- live disposable-Postgres path (feature-gate scenario, healthy half) ---


@requires_live_postgres
@pytest.mark.anyio
async def test_live_health_ok() -> None:
    status = await check_database(os.environ[LIVE_URL_VAR])
    assert status.ok, status.detail


@requires_live_postgres
@pytest.mark.anyio
async def test_live_engine_query_round_trip_and_dispose() -> None:
    engine, session_factory = create_app_engine(os.environ[LIVE_URL_VAR])
    try:
        async with session_factory() as session:
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await engine.dispose()
