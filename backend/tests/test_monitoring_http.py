"""Focused tests for the monitoring HTTP API (card 10-monitor-api).

Brief acceptance coverage over the reviewed producers (0007 schema,
``mai.notifications.service``) on a REAL disposable Postgres through the
real linear Alembic chain — the HTTP surface is exercised through the real
ASGI app (``create_app`` + lifespan + middleware + handlers), never mocks:

1. **Authenticated bounded server-scoped queries with freshness; context
   refs usable in chat** — every monitoring route rejects the
   unauthenticated session (401 envelope); observations/findings/inbox
   pages are bounded (``limit``/``offset`` with a hard max), name their
   server explicitly and can never contain another server's row;
   ``freshness`` is derived at request time (fresh/stale/unknown);
   observation/finding/notice views carry the bounded chat-usable
   references (``observation:<uuid>`` / ``finding:<uuid>`` /
   ``run:<uuid>``).
2. **Ack does not delete source evidence; cross-server/unauthed access
   denied** — the ack stamps ``acknowledged_at`` (idempotently) while the
   notice, its finding and its producing observation all survive; a
   cross-server ack/read is the same value-free 404 as a missing row and
   leaves the row untouched; the ack state survives a full app
   re-creation over the same database.
3. **Real app registration + OpenAPI contract** — the routes are reached
   through ``create_app``'s actual registration (api-wiring), the schema
   is regenerated mechanically from ``app.openapi()`` (evidence files),
   and the re-evaluation route composes the REAL notification service
   under the effective policy the policy endpoint reports.

Secret hygiene: the fixture password is an inert constant for the
disposable fixture DB; session/CSRF tokens are minted at runtime and
never printed; all seeded payloads are synthetic bounded text.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from datetime import datetime as DateTime
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI

from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE
from mai.database import create_app_engine
from mai.notifications.service import (
    DEFAULT_COOLDOWN,
    evaluate_findings,
    finding_dedup_key,
    notify_job_result,
)

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: Synthetic fixture credential (NOT a secret; disposable fixture DB only).
FIXTURE_PASSWORD = "fixture-password-10-monitor-api-only"
ORIGIN = "http://127.0.0.1:8443"  # loopback allowlist entry
EVIL_ORIGIN = "https://attacker.example"

#: Frozen fixed fixture identities (canonical lowercase UUID strings,
#: distinct from every other card's constants).
SERVER_A = "5c9f3e1a-7d4b-4c82-a033-2f1a4b5c6d7e"
SERVER_B = "6da04f2b-8e5c-4d93-b144-3a2b5c6d7e8f"
APPLICATION_1 = "7eb1503c-9f6d-4ea4-c255-4b3c6d7e8f90"
APPLICATION_2 = "8fc2614d-0a7e-4fb5-d366-5c4d7e8f90a1"

OBS_CPU = uuid.UUID("a1b2c3d4-0000-4001-8001-000000000010")
OBS_MEM = uuid.UUID("a1b2c3d4-0000-4002-8002-000000000010")
OBS_STALE = uuid.UUID("a1b2c3d4-0000-4003-8003-000000000010")
OBS_UNKNOWN = uuid.UUID("a1b2c3d4-0000-4004-8004-000000000010")
OBS_B = uuid.UUID("a1b2c3d4-0000-4005-8005-000000000010")
OBS_APP1 = uuid.UUID("a1b2c3d4-0000-4006-8006-000000000010")
OBS_APP2 = uuid.UUID("a1b2c3d4-0000-4007-8007-000000000010")

FINDING_UPDATE = uuid.UUID("b2c3d4e5-0000-4001-9001-000000000020")
FINDING_SECURITY = uuid.UUID("b2c3d4e5-0000-4002-9002-000000000020")
FINDING_INFO = uuid.UUID("b2c3d4e5-0000-4003-9003-000000000020")
FINDING_RESOLVED = uuid.UUID("b2c3d4e5-0000-4004-9004-000000000020")
FINDING_B = uuid.UUID("b2c3d4e5-0000-4005-9005-000000000020")
FINDING_INBOX = uuid.UUID("b2c3d4e5-0000-4006-9006-000000000020")
FINDING_LINKED = uuid.UUID("b2c3d4e5-0000-4007-9007-000000000020")

RUN_OK = uuid.UUID("c3d4e5f6-0000-4001-a001-000000000030")

_ATOMIC = timedelta(seconds=1)
_FRESH = timedelta(hours=1)
_STALE = timedelta(hours=-1)
_RETAIN = timedelta(days=7)


def _utc() -> DateTime:
    return DateTime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures: real disposable Postgres + real app through lifespan
# ---------------------------------------------------------------------------


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(os.path.join(BACKEND_ROOT, "alembic.ini"))


def _drop_app_tables(url: str) -> None:
    with psycopg.connect(url) as conn:
        # Full app set at the chain tip (same reconciliation list as the
        # 10-notifications / 10-observation-schema fixtures):
        # order-independent zero-to-head fixture.
        conn.execute(
            "DROP TABLE IF EXISTS notification_alerts, observation_findings, "
            "server_observations, application_projection_journal, "
            "application_projection, knowledge_doc_chunks, knowledge_documents, "
            "job_schedule_fires, job_schedules, "
            "job_events, job_steps, job_runs, approvals, "
            "profile_projection_journal, profile_projection, admin_sessions, "
            "admin_credential, alembic_version"
        )
        conn.execute("DROP SEQUENCE IF EXISTS server_observations_seq")
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``head``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the monitoring HTTP surface claims "
            "need a real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        _drop_app_tables(url)
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    return url


def _truncate(url: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            "TRUNCATE notification_alerts, observation_findings, "
            "server_observations, job_runs, job_steps, job_events, "
            "job_schedules, job_schedule_fires, approvals, admin_sessions, "
            "admin_credential RESTART IDENTITY CASCADE"
        )
        conn.commit()


def _build_app(url: str | None, data_root: str) -> FastAPI:
    env: dict[str, str] = {
        "MAI_DATA_ROOT": data_root,
        "MAI_PUBLIC_ORIGINS": ORIGIN,
    }
    if url is not None:
        env["MAI_DATABASE_URL"] = url
    return create_app(env=env)


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[FastAPI]:
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def monitoring_app(migrated_url: str, tmp_path: Any):
    """App on the real fixture DB with a fresh confined data root."""
    _truncate(migrated_url)
    app = _build_app(migrated_url, str(tmp_path / "data-root"))
    async with _running(app):
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()


@asynccontextmanager
async def _service_factory(
    url: str,
) -> AsyncIterator[Any]:
    """A direct session factory for seeding through the REAL service."""
    engine, factory = create_app_engine(url)
    try:
        yield factory
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Storage seed helpers (real rows in the real 0007 tables; synthetic text)
# ---------------------------------------------------------------------------


def _seed_observation(
    url: str,
    observation_id: uuid.UUID,
    *,
    server_id: str = SERVER_A,
    application_id: str | None = None,
    source: str = "system",
    method: str = "command",
    quality: str = "measured",
    metric: str | None = "disk.used_pct",
    value: str | None = "42",
    observed_at: DateTime | None = None,
    collected_at: DateTime | None = None,
    fresh_until: DateTime | None = None,
) -> None:
    now = _utc()
    _explicit_fresh = fresh_until is not None
    observed_at = observed_at if observed_at is not None else now - timedelta(minutes=1)
    collected_at = (
        collected_at if collected_at is not None else now - timedelta(minutes=1)
    )
    fresh_until = fresh_until if fresh_until is not None else now + _FRESH
    if quality == "unknown" and not _explicit_fresh:
        # An unavailable sample carries no horizon either (nothing to keep
        # fresh): the schema CHECK only pins metric/value/observed_at NULL.
        fresh_until = None
    if quality == "unknown":
        metric = value = observed_at = None
    with psycopg.connect(url) as conn:
        conn.execute(
            """
            INSERT INTO server_observations
                (observation_id, server_id, application_id, source, method,
                 quality, metric, value, observed_at, collected_at,
                 fresh_until, retain_until)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                observation_id,
                uuid.UUID(server_id),
                None if application_id is None else uuid.UUID(application_id),
                source,
                method,
                quality,
                metric,
                value,
                observed_at,
                collected_at,
                fresh_until,
                now + _RETAIN,
            ),
        )
        conn.commit()


def _seed_finding(
    url: str,
    finding_id: uuid.UUID,
    *,
    server_id: str = SERVER_A,
    application_id: str | None = None,
    observation_id: uuid.UUID | None = None,
    kind: str = "update.available",
    dedup_key: str = "fixture-condition",
    severity: str = "warning",
    summary: str = "synthetic condition for the fixture server",
    detail: str | None = None,
    opened_at: DateTime | None = None,
    updated_at: DateTime | None = None,
    resolved_at: DateTime | None = None,
) -> None:
    now = _utc()
    opened_at = opened_at if opened_at is not None else now
    updated_at = updated_at if updated_at is not None else now
    with psycopg.connect(url) as conn:
        conn.execute(
            """
            INSERT INTO observation_findings
                (finding_id, server_id, application_id, observation_id, kind,
                 dedup_key, severity, summary, detail, opened_at, updated_at,
                 resolved_at, retain_until)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                finding_id,
                uuid.UUID(server_id),
                None if application_id is None else uuid.UUID(application_id),
                observation_id,
                kind,
                dedup_key,
                severity,
                summary,
                detail,
                opened_at,
                updated_at,
                resolved_at,
                now + _RETAIN,
            ),
        )
        conn.commit()


def _seed_job_run(url: str, run_id: uuid.UUID, *, server_id: str = SERVER_A) -> None:
    now = _utc()
    with psycopg.connect(url) as conn:
        conn.execute(
            """
            INSERT INTO job_runs
                (run_id, server_id, parent_run_id, depth, profile_revision,
                 prompt_revision, model_profile, requested_grants,
                 root_budget, error_ref, evidence_ref, cancel_requested_at,
                 submitted_at, updated_at)
            VALUES
                (%s, %s, NULL, 0, 1, 1, 'fixture-profile',
                 '[]'::jsonb, NULL, NULL, NULL, NULL, %s, %s)
            """,
            (run_id, uuid.UUID(server_id), now, now),
        )
        conn.commit()


def _count(url: str, table: str) -> int:
    with psycopg.connect(url) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _ack_at(url: str, alert_id: uuid.UUID) -> DateTime | None:
    with psycopg.connect(url) as conn:
        row = conn.execute(
            "SELECT acknowledged_at FROM notification_alerts WHERE alert_id = %s",
            (str(alert_id),),
        ).fetchone()
    return None if row is None else row[0]


# ---------------------------------------------------------------------------
# Minimal ASGI harness (no test-client dependency; bodies + cookies)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HttpResponse:
    status: int | None = None
    headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    body: bytes = b""

    def header(self, name: str) -> str | None:
        for key, value in self.headers:
            if key.decode("ascii").lower() == name:
                return value.decode("ascii")
        return None

    def header_all(self, name: str) -> list[str]:
        return [
            value.decode("ascii")
            for key, value in self.headers
            if key.decode("ascii").lower() == name
        ]

    def json(self) -> Any:
        return json.loads(self.body)


async def call_app(
    app: FastAPI,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    headers: Mapping[str, str] | None = None,
    cookies: Mapping[str, str] | None = None,
    client: tuple[str, int] = ("127.0.0.1", 0),
) -> HttpResponse:
    """Run one HTTP request through the real ASGI callable."""
    path, _, query = path.partition("?")
    scope_headers = [(b"host", b"fixture")]
    scope_headers.extend(
        (key.encode("ascii").lower(), value.encode("ascii"))
        for key, value in (headers or {}).items()
    )
    if cookies:
        scope_headers.append(
            (b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode())
        )
    body = b""
    if json_body is not None:
        body = json.dumps(json_body).encode()
        scope_headers.append((b"content-type", b"application/json"))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query.encode("ascii"),
        "root_path": "",
        "headers": scope_headers,
        "client": client,
        "server": ("fixture", 80),
    }
    response = HttpResponse()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            response.status = message["status"]
            response.headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            response.body += message.get("body", b"")

    try:
        await app(scope, receive, send)
    except Exception:  # noqa: BLE001 - ServerErrorMiddleware re-raises after reply
        pass
    return response


def _set_cookies(response: HttpResponse) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw in response.header_all("set-cookie"):
        name, _, value = raw.partition("=")
        parsed[name] = value.split(";", 1)[0]
    return parsed


def _error(response: HttpResponse) -> dict[str, Any]:
    envelope = response.json()
    assert set(envelope) == {"error"}
    body = envelope["error"]
    assert set(body) == {"code", "message", "request_id", "fields"}
    assert "Traceback" not in response.body.decode()
    return body


async def _login_only(app: FastAPI) -> dict[str, str]:
    """Login against an EXISTING credential; return the cookie jar."""
    login = await call_app(
        app,
        "POST",
        "/api/v1/auth/login",
        json_body={"password": FIXTURE_PASSWORD},
        headers={"origin": ORIGIN},
    )
    assert login.status in (200, 204), login.body
    cookies = _set_cookies(login)
    assert SESSION_COOKIE in cookies and CSRF_COOKIE in cookies
    return cookies


async def login_session(app: FastAPI) -> dict[str, str]:
    """Bootstrap once and return the authenticated cookie jar."""
    bootstrap = await call_app(
        app,
        "POST",
        "/api/v1/auth/bootstrap",
        json_body={"password": FIXTURE_PASSWORD},
        headers={"origin": ORIGIN},
    )
    assert bootstrap.status == 201, bootstrap.body
    return await _login_only(app)


def _mutation_headers() -> dict[str, str]:
    """Origin + CSRF headers for one authenticated mutation."""
    return {"origin": ORIGIN, "x-csrf-token": _AUTH_JAR[CSRF_COOKIE]}


_AUTH_JAR: dict[str, str] = {}


@pytest.fixture
async def session_jar(monitoring_app: FastAPI) -> dict[str, str]:
    """One authenticated cookie jar per test (fresh bootstrap per test)."""
    global _AUTH_JAR
    _AUTH_JAR = await login_session(monitoring_app)
    return _AUTH_JAR


def _monitoring_prefix(server_id: str) -> str:
    return f"/api/v1/monitoring/servers/{server_id}"


# ---------------------------------------------------------------------------
# TestObservationQueries
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestObservationQueries:
    """Bounded server-scoped observation queries with derived freshness."""

    async def test_list_returns_rows_newest_first_with_context_ref(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        now = _utc()
        _seed_observation(
            migrated_url, OBS_CPU, collected_at=now - timedelta(minutes=5)
        )
        _seed_observation(
            migrated_url,
            OBS_MEM,
            source="process",
            metric="proc.count",
            value="210",
            collected_at=now - timedelta(minutes=1),
        )
        response = await call_app(
            monitoring_app,
            "GET",
            f"{_monitoring_prefix(SERVER_A)}/observations",
            cookies=session_jar,
        )
        assert response.status == 200, response.body
        page = response.json()
        assert page["server_id"] == SERVER_A
        assert page["total"] == 2 and page["limit"] == 50 and page["offset"] == 0
        [newest, oldest] = page["observations"]
        assert newest["observation_id"] == str(OBS_MEM)
        assert oldest["observation_id"] == str(OBS_CPU)
        assert newest["source"] == "process" and oldest["source"] == "system"
        assert newest["metric"] == "proc.count" and newest["value"] == "210"
        for view in (newest, oldest):
            assert view["quality"] == "measured"
            assert view["method"] == "command"
            assert view["freshness"] == "fresh"
            assert view["context_ref"].startswith("observation:")
            uuid.UUID(view["context_ref"].split(":", 1)[1])  # chat-usable UUID
            assert view["collected_at"].endswith("Z")

    async def test_server_scope_excludes_other_server_rows(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        _seed_observation(migrated_url, OBS_CPU)
        _seed_observation(migrated_url, OBS_B, server_id=SERVER_B)
        page_a = (
            await call_app(
                monitoring_app,
                "GET",
                f"{_monitoring_prefix(SERVER_A)}/observations",
                cookies=session_jar,
            )
        ).json()
        page_b = (
            await call_app(
                monitoring_app,
                "GET",
                f"{_monitoring_prefix(SERVER_B)}/observations",
                cookies=session_jar,
            )
        ).json()
        assert [o["observation_id"] for o in page_a["observations"]] == [str(OBS_CPU)]
        assert all(o["server_id"] == SERVER_B for o in page_b["observations"])
        assert page_b["total"] == 1

    async def test_application_filter_narrows(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        _seed_observation(migrated_url, OBS_APP1, application_id=APPLICATION_1)
        _seed_observation(migrated_url, OBS_APP2, application_id=APPLICATION_2)
        _seed_observation(migrated_url, OBS_CPU, application_id=None)
        response = await call_app(
            monitoring_app,
            "GET",
            f"{_monitoring_prefix(SERVER_A)}/observations"
            f"?application_id={APPLICATION_1}",
            cookies=session_jar,
        )
        assert response.status == 200, response.body
        page = response.json()
        assert page["total"] == 1
        assert page["observations"][0]["observation_id"] == str(OBS_APP1)
        assert page["observations"][0]["application_id"] == APPLICATION_1

    async def test_source_filter_narrows(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        _seed_observation(migrated_url, OBS_CPU, source="system")
        _seed_observation(migrated_url, OBS_MEM, source="cron")
        response = await call_app(
            monitoring_app,
            "GET",
            f"{_monitoring_prefix(SERVER_A)}/observations?source=cron",
            cookies=session_jar,
        )
        page = response.json()
        assert page["total"] == 1
        assert page["observations"][0]["observation_id"] == str(OBS_MEM)

    async def test_freshness_stale_past_horizon(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        _seed_observation(migrated_url, OBS_STALE, fresh_until=_utc() + _STALE)
        response = await call_app(
            monitoring_app,
            "GET",
            f"{_monitoring_prefix(SERVER_A)}/observations",
            cookies=session_jar,
        )
        view = response.json()["observations"][0]
        assert view["freshness"] == "stale"
        assert view["fresh_until"].endswith("Z")

    async def test_freshness_unknown_without_sample(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        _seed_observation(
            migrated_url,
            OBS_UNKNOWN,
            quality="unknown",
            method="derived",
            fresh_until=None,
        )
        response = await call_app(
            monitoring_app,
            "GET",
            f"{_monitoring_prefix(SERVER_A)}/observations",
            cookies=session_jar,
        )
        view = response.json()["observations"][0]
        assert view["freshness"] == "unknown"
        assert view["metric"] is None and view["value"] is None
        assert view["observed_at"] is None and view["fresh_until"] is None

    async def test_read_single_observation(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        _seed_observation(migrated_url, OBS_CPU)
        response = await call_app(
            monitoring_app,
            "GET",
            f"{_monitoring_prefix(SERVER_A)}/observations/{OBS_CPU}",
            cookies=session_jar,
        )
        assert response.status == 200, response.body
        view = response.json()
        assert view["observation_id"] == str(OBS_CPU)
        assert view["context_ref"] == f"observation:{OBS_CPU}"
        assert view["server_id"] == SERVER_A

    async def test_unknown_and_cross_server_reads_are_value_free_404(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        _seed_observation(migrated_url, OBS_B, server_id=SERVER_B)
        missing = await call_app(
            monitoring_app,
            "GET",
            f"{_monitoring_prefix(SERVER_A)}/observations/{uuid.uuid4()}",
            cookies=session_jar,
        )
        foreign = await call_app(
            monitoring_app,
            "GET",
            f"{_monitoring_prefix(SERVER_A)}/observations/{OBS_B}",
            cookies=session_jar,
        )
        for response in (missing, foreign):
            assert response.status == 404, response.body
            assert _error(response)["code"] == "not_found"
        # Same value-free message: a foreign id is indistinguishable from a
        # missing one (no existence leak across the scope boundary).
        assert _error(missing)["message"] == _error(foreign)["message"]

    async def test_oversized_limit_is_field_scoped_422(
        self, monitoring_app: FastAPI, session_jar: dict[str, str]
    ) -> None:
        response = await call_app(
            monitoring_app,
            "GET",
            f"{_monitoring_prefix(SERVER_A)}/observations?limit=201",
            cookies=session_jar,
        )
        assert response.status == 422, response.body
        error = _error(response)
        assert error["code"] == "validation_failed"
        assert error["fields"] == {"query.limit": "invalid"}

    async def test_pagination_is_bounded(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        now = _utc()
        for index in range(3):
            _seed_observation(
                migrated_url,
                uuid.uuid4(),
                collected_at=now - timedelta(minutes=index + 1),
            )
        page = (
            await call_app(
                monitoring_app,
                "GET",
                f"{_monitoring_prefix(SERVER_A)}/observations?limit=2&offset=1",
                cookies=session_jar,
            )
        ).json()
        assert page["total"] == 3 and len(page["observations"]) == 2
        assert page["limit"] == 2 and page["offset"] == 1


# ---------------------------------------------------------------------------
# TestFindingQueries
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestFindingQueries:
    """Bounded server-scoped finding inspection, attention-ordered."""

    async def test_list_orders_by_severity_then_opened(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        now = _utc()
        _seed_finding(
            migrated_url,
            FINDING_UPDATE,
            kind="update.available",
            severity="warning",
            opened_at=now - timedelta(hours=3),
        )
        _seed_finding(
            migrated_url,
            FINDING_SECURITY,
            kind="security.baseline",
            severity="critical",
            opened_at=now - timedelta(hours=1),
        )
        _seed_finding(
            migrated_url,
            FINDING_INFO,
            kind="cron.drift",
            severity="info",
            opened_at=now - timedelta(minutes=10),
        )
        response = await call_app(
            monitoring_app,
            "GET",
            f"{_monitoring_prefix(SERVER_A)}/findings",
            cookies=session_jar,
        )
        assert response.status == 200, response.body
        page = response.json()
        assert page["total"] == 3
        assert [f["severity"] for f in page["findings"]] == [
            "critical",
            "warning",
            "info",
        ]
        for view in page["findings"]:
            assert view["open"] is True
            assert view["context_ref"].startswith("finding:")
            uuid.UUID(view["context_ref"].split(":", 1)[1])

    async def test_open_filter_narrows(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        now = _utc()
        _seed_finding(migrated_url, FINDING_UPDATE, severity="warning")
        _seed_finding(
            migrated_url,
            FINDING_RESOLVED,
            kind="process.anomaly",
            severity="info",
            opened_at=now - timedelta(hours=2),
            resolved_at=now - timedelta(minutes=30),
        )
        open_page = (
            await call_app(
                monitoring_app,
                "GET",
                f"{_monitoring_prefix(SERVER_A)}/findings?open=true",
                cookies=session_jar,
            )
        ).json()
        resolved_page = (
            await call_app(
                monitoring_app,
                "GET",
                f"{_monitoring_prefix(SERVER_A)}/findings?open=false",
                cookies=session_jar,
            )
        ).json()
        all_page = (
            await call_app(
                monitoring_app,
                "GET",
                f"{_monitoring_prefix(SERVER_A)}/findings",
                cookies=session_jar,
            )
        ).json()
        assert [f["finding_id"] for f in open_page["findings"]] == [str(FINDING_UPDATE)]
        assert open_page["findings"][0]["open"] is True
        assert [f["finding_id"] for f in resolved_page["findings"]] == [
            str(FINDING_RESOLVED)
        ]
        assert resolved_page["findings"][0]["open"] is False
        assert resolved_page["findings"][0]["resolved_at"] is not None
        assert all_page["total"] == 2

    async def test_read_single_finding(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        _seed_observation(migrated_url, OBS_CPU)
        _seed_finding(
            migrated_url,
            FINDING_LINKED,
            observation_id=OBS_CPU,
            detail="remediation hint",
        )
        response = await call_app(
            monitoring_app,
            "GET",
            f"{_monitoring_prefix(SERVER_A)}/findings/{FINDING_LINKED}",
            cookies=session_jar,
        )
        assert response.status == 200, response.body
        view = response.json()
        assert view["finding_id"] == str(FINDING_LINKED)
        assert view["observation_id"] == str(OBS_CPU)
        assert view["context_ref"] == f"finding:{FINDING_LINKED}"
        assert view["detail"] == "remediation hint"
        assert view["dedup_key"] == "fixture-condition"

    async def test_unknown_finding_is_404(
        self, monitoring_app: FastAPI, session_jar: dict[str, str]
    ) -> None:
        response = await call_app(
            monitoring_app,
            "GET",
            f"{_monitoring_prefix(SERVER_A)}/findings/{uuid.uuid4()}",
            cookies=session_jar,
        )
        assert response.status == 404
        assert _error(response)["code"] == "not_found"

    async def test_cross_server_finding_read_is_404(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        _seed_finding(migrated_url, FINDING_B, server_id=SERVER_B)
        response = await call_app(
            monitoring_app,
            "GET",
            f"{_monitoring_prefix(SERVER_A)}/findings/{FINDING_B}",
            cookies=session_jar,
        )
        assert response.status == 404
        assert _error(response)["code"] == "not_found"

    async def test_finding_server_scope_in_list(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        _seed_finding(migrated_url, FINDING_UPDATE)
        _seed_finding(migrated_url, FINDING_B, server_id=SERVER_B)
        page_a = (
            await call_app(
                monitoring_app,
                "GET",
                f"{_monitoring_prefix(SERVER_A)}/findings",
                cookies=session_jar,
            )
        ).json()
        assert [f["finding_id"] for f in page_a["findings"]] == [str(FINDING_UPDATE)]


# ---------------------------------------------------------------------------
# TestNotificationInbox
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestNotificationInbox:
    """The inbox view over notices the REAL notification service wrote."""

    async def _evaluate_open(self, migrated_url: str, server_id: str) -> None:
        async with _service_factory(migrated_url) as factory:
            await evaluate_findings(
                factory,
                uuid.UUID(server_id),
                now=_utc(),
                policy=_default_policy(),
            )

    async def test_inbox_lists_service_created_notices(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        now = _utc()
        _seed_finding(
            migrated_url,
            FINDING_UPDATE,
            severity="warning",
            opened_at=now - timedelta(hours=2),
        )
        _seed_finding(
            migrated_url,
            FINDING_INFO,
            kind="cron.drift",
            severity="info",
            opened_at=now - timedelta(hours=1),
        )
        await self._evaluate_open(migrated_url, SERVER_A)
        response = await call_app(
            monitoring_app,
            "GET",
            f"{_monitoring_prefix(SERVER_A)}/notifications",
            cookies=session_jar,
        )
        assert response.status == 200, response.body
        page = response.json()
        assert page["total"] == 2 and page["server_id"] == SERVER_A
        [newest, oldest] = page["alerts"]
        # Both notices share one creation instant (a single evaluation
        # round), so within-round order is the deterministic alert_id
        # tiebreak — never asserted as chronology here.
        by_finding = {view["finding_id"]: view for view in page["alerts"]}
        assert set(by_finding) == {str(FINDING_INFO), str(FINDING_UPDATE)}
        assert newest["origin"] == "finding"
        # Severity comes from the LIVE anchor join (the finding's severity).
        assert by_finding[str(FINDING_INFO)]["severity"] == "info"
        assert by_finding[str(FINDING_UPDATE)]["severity"] == "warning"
        for view in (newest, oldest):
            assert view["acked"] is False and view["acknowledged_at"] is None
            assert view["delivery_status"] == "delivered"  # inbox-only policy
            assert view["context_ref"].startswith("finding:")
            uuid.UUID(view["context_ref"].split(":", 1)[1])
            assert view["dedup_key"].startswith("finding:")

    async def test_inbox_severity_falls_back_to_summary_tag_for_job_result(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        _seed_job_run(migrated_url, RUN_OK)
        async with _service_factory(migrated_url) as factory:
            await notify_job_result(
                factory,
                uuid.UUID(SERVER_A),
                RUN_OK,
                "failed",
                model_profile="fixture-profile",
                now=_utc(),
                policy=_default_policy(),
            )
        response = await call_app(
            monitoring_app,
            "GET",
            f"{_monitoring_prefix(SERVER_A)}/notifications",
            cookies=session_jar,
        )
        page = response.json()
        assert page["total"] == 1
        view = page["alerts"][0]
        assert view["origin"] == "job_result"
        assert view["finding_id"] is None  # no anchor: the tag is the source
        assert view["severity"] == "warning"  # read from the frozen summary tag
        assert view["context_ref"] == f"run:{RUN_OK}"
        assert view["run_id"] == str(RUN_OK)

    async def test_unacknowledged_filter(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        _seed_finding(migrated_url, FINDING_UPDATE)
        _seed_finding(migrated_url, FINDING_INFO, kind="cron.drift", severity="info")
        await self._evaluate_open(migrated_url, SERVER_A)
        with psycopg.connect(migrated_url) as conn:
            # Test-only state setup for the filter: acknowledge exactly one
            # notice directly (the ack ENDPOINT itself is TestAcknowledgment).
            conn.execute(
                "UPDATE notification_alerts SET acknowledged_at = now() "
                "WHERE dedup_key = %s",
                (finding_dedup_key("update.available", "fixture-condition"),),
            )
            conn.commit()
        unacked = (
            await call_app(
                monitoring_app,
                "GET",
                f"{_monitoring_prefix(SERVER_A)}/notifications?unacknowledged=true",
                cookies=session_jar,
            )
        ).json()
        acked = (
            await call_app(
                monitoring_app,
                "GET",
                f"{_monitoring_prefix(SERVER_A)}/notifications?unacknowledged=false",
                cookies=session_jar,
            )
        ).json()
        assert unacked["total"] == 1
        assert unacked["alerts"][0]["finding_id"] == str(FINDING_INFO)
        assert acked["total"] == 1
        assert acked["alerts"][0]["finding_id"] == str(FINDING_UPDATE)
        assert acked["alerts"][0]["acked"] is True

    async def test_single_notice_read(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        _seed_finding(migrated_url, FINDING_INBOX)
        await self._evaluate_open(migrated_url, SERVER_A)
        with psycopg.connect(migrated_url) as conn:
            alert_id = conn.execute(
                "SELECT alert_id FROM notification_alerts"
            ).fetchone()[0]
        response = await call_app(
            monitoring_app,
            "GET",
            f"{_monitoring_prefix(SERVER_A)}/notifications/{alert_id}",
            cookies=session_jar,
        )
        assert response.status == 200, response.body
        view = response.json()
        assert str(view["alert_id"]) == str(alert_id)
        assert view["finding_id"] == str(FINDING_INBOX)
        assert view["title"].startswith("update.available:")

    async def test_unknown_notice_is_404(
        self, monitoring_app: FastAPI, session_jar: dict[str, str]
    ) -> None:
        response = await call_app(
            monitoring_app,
            "GET",
            f"{_monitoring_prefix(SERVER_A)}/notifications/{uuid.uuid4()}",
            cookies=session_jar,
        )
        assert response.status == 404
        assert _error(response)["code"] == "not_found"

    async def test_cross_server_notice_read_is_404(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        _seed_finding(migrated_url, FINDING_B, server_id=SERVER_B)
        async with _service_factory(migrated_url) as factory:
            await evaluate_findings(
                factory,
                uuid.UUID(SERVER_B),
                now=_utc(),
                policy=_default_policy(),
            )
        with psycopg.connect(migrated_url) as conn:
            alert_id = conn.execute(
                "SELECT alert_id FROM notification_alerts"
            ).fetchone()[0]
        response = await call_app(
            monitoring_app,
            "GET",
            f"{_monitoring_prefix(SERVER_A)}/notifications/{alert_id}",
            cookies=session_jar,
        )
        assert response.status == 404
        assert _error(response)["code"] == "not_found"

    async def test_inbox_pagination_is_bounded(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        now = _utc()
        _seed_finding(
            migrated_url,
            FINDING_UPDATE,
            opened_at=now - timedelta(hours=2),
        )
        _seed_finding(
            migrated_url,
            FINDING_INFO,
            kind="cron.drift",
            severity="info",
            opened_at=now - timedelta(hours=1),
        )
        await self._evaluate_open(migrated_url, SERVER_A)
        page = (
            await call_app(
                monitoring_app,
                "GET",
                f"{_monitoring_prefix(SERVER_A)}/notifications?limit=1&offset=1",
                cookies=session_jar,
            )
        ).json()
        assert page["total"] == 2 and len(page["alerts"]) == 1
        assert page["limit"] == 1 and page["offset"] == 1


# ---------------------------------------------------------------------------
# TestAcknowledgment
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestAcknowledgment:
    """The evidence-preserving, idempotent, scope-checked ack."""

    async def _seed_one_notice(self, migrated_url: str) -> uuid.UUID:
        _seed_observation(migrated_url, OBS_CPU)
        _seed_finding(migrated_url, FINDING_UPDATE, observation_id=OBS_CPU)
        async with _service_factory(migrated_url) as factory:
            await evaluate_findings(
                factory,
                uuid.UUID(SERVER_A),
                now=_utc(),
                policy=_default_policy(),
            )
        with psycopg.connect(migrated_url) as conn:
            alert_id = conn.execute(
                "SELECT alert_id FROM notification_alerts"
            ).fetchone()[0]
        return uuid.UUID(str(alert_id))

    async def test_ack_stamps_and_preserves_all_source_evidence(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        alert_id = await self._seed_one_notice(migrated_url)
        before = (
            _count(migrated_url, "notification_alerts"),
            _count(migrated_url, "observation_findings"),
            _count(migrated_url, "server_observations"),
        )
        assert before == (1, 1, 1)
        response = await call_app(
            monitoring_app,
            "POST",
            f"{_monitoring_prefix(SERVER_A)}/notifications/{alert_id}/ack",
            headers=_mutation_headers(),
            cookies=session_jar,
        )
        assert response.status == 200, response.body
        receipt = response.json()["alert"]
        assert receipt["acked"] is True
        assert receipt["acknowledged_at"] is not None
        assert receipt["delivery_status"] == "delivered"
        # THE acceptance: ack deletes NOTHING — notice, finding, observation
        # all survive (raw table counts, not a view claim).
        after = (
            _count(migrated_url, "notification_alerts"),
            _count(migrated_url, "observation_findings"),
            _count(migrated_url, "server_observations"),
        )
        assert after == before == (1, 1, 1)
        assert _ack_at(migrated_url, alert_id) is not None
        # The finding is still reachable through the API after the ack.
        finding = await call_app(
            monitoring_app,
            "GET",
            f"{_monitoring_prefix(SERVER_A)}/findings/{FINDING_UPDATE}",
            cookies=session_jar,
        )
        assert finding.status == 200, finding.body

    async def test_ack_is_idempotent_never_re_stamps(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        alert_id = await self._seed_one_notice(migrated_url)
        first = await call_app(
            monitoring_app,
            "POST",
            f"{_monitoring_prefix(SERVER_A)}/notifications/{alert_id}/ack",
            headers=_mutation_headers(),
            cookies=session_jar,
        )
        assert first.status == 200, first.body
        stamped = _ack_at(migrated_url, alert_id)
        second = await call_app(
            monitoring_app,
            "POST",
            f"{_monitoring_prefix(SERVER_A)}/notifications/{alert_id}/ack",
            headers=_mutation_headers(),
            cookies=session_jar,
        )
        assert second.status == 200, second.body
        assert _ack_at(migrated_url, alert_id) == stamped  # never re-stamped

    async def test_cross_server_ack_is_denied_and_leaves_row_unacked(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        _seed_finding(migrated_url, FINDING_B, server_id=SERVER_B)
        async with _service_factory(migrated_url) as factory:
            await evaluate_findings(
                factory,
                uuid.UUID(SERVER_B),
                now=_utc(),
                policy=_default_policy(),
            )
        with psycopg.connect(migrated_url) as conn:
            alert_id = uuid.UUID(
                str(
                    conn.execute("SELECT alert_id FROM notification_alerts").fetchone()[
                        0
                    ]
                )
            )
        response = await call_app(
            monitoring_app,
            "POST",
            f"{_monitoring_prefix(SERVER_A)}/notifications/{alert_id}/ack",
            headers=_mutation_headers(),
            cookies=session_jar,
        )
        assert response.status == 404, response.body
        assert _error(response)["code"] == "not_found"
        assert _ack_at(migrated_url, alert_id) is None  # untouched

    async def test_unknown_notice_ack_is_404(
        self, monitoring_app: FastAPI, session_jar: dict[str, str]
    ) -> None:
        response = await call_app(
            monitoring_app,
            "POST",
            f"{_monitoring_prefix(SERVER_A)}/notifications/{uuid.uuid4()}/ack",
            headers=_mutation_headers(),
            cookies=session_jar,
        )
        assert response.status == 404
        assert _error(response)["code"] == "not_found"

    async def test_ack_without_csrf_is_403_and_never_stamps(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        alert_id = await self._seed_one_notice(migrated_url)
        response = await call_app(
            monitoring_app,
            "POST",
            f"{_monitoring_prefix(SERVER_A)}/notifications/{alert_id}/ack",
            headers={"origin": ORIGIN},  # no X-CSRF-Token
            cookies=session_jar,
        )
        assert response.status == 403, response.body
        assert _error(response)["code"] == "csrf_rejected"
        assert _ack_at(migrated_url, alert_id) is None

    async def test_ack_with_evil_origin_is_403_and_never_stamps(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        alert_id = await self._seed_one_notice(migrated_url)
        response = await call_app(
            monitoring_app,
            "POST",
            f"{_monitoring_prefix(SERVER_A)}/notifications/{alert_id}/ack",
            headers={"origin": EVIL_ORIGIN, "x-csrf-token": session_jar[CSRF_COOKIE]},
            cookies=session_jar,
        )
        assert response.status == 403, response.body
        assert _error(response)["code"] == "origin_rejected"
        assert _ack_at(migrated_url, alert_id) is None

    async def test_ack_persists_across_app_recreation(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        alert_id = await self._seed_one_notice(migrated_url)
        ack = await call_app(
            monitoring_app,
            "POST",
            f"{_monitoring_prefix(SERVER_A)}/notifications/{alert_id}/ack",
            headers=_mutation_headers(),
            cookies=session_jar,
        )
        assert ack.status == 200, ack.body
        # A brand-new app over the same database (the restart shape) — the
        # first app stays up; the recreated one must observe the acked state.
        recreated = _build_app(
            migrated_url, "/tmp/mai-fxt-10-monitor-api/recreated-root"
        )
        try:
            async with _running(recreated):
                jar = await _login_only(recreated)
                inbox = await call_app(
                    recreated,
                    "GET",
                    f"{_monitoring_prefix(SERVER_A)}/notifications",
                    cookies=jar,
                )
                assert inbox.status == 200, inbox.body
                [view] = inbox.json()["alerts"]
                assert view["acked"] is True and view["acknowledged_at"] is not None
        finally:
            if recreated.state.engine is not None:
                await recreated.state.engine.dispose()


# ---------------------------------------------------------------------------
# TestReEvaluation
# ---------------------------------------------------------------------------


def _default_policy() -> Any:
    """The surface's effective policy, imported from the module itself."""
    from mai.api.monitoring import _EFFECTIVE_POLICY

    return _EFFECTIVE_POLICY


@pytest.mark.anyio
class TestReEvaluation:
    """The re-evaluation route composes the REAL notification producer."""

    async def test_evaluate_creates_notices_for_open_findings(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        _seed_finding(migrated_url, FINDING_UPDATE, severity="warning")
        _seed_finding(migrated_url, FINDING_INFO, kind="cron.drift", severity="info")
        response = await call_app(
            monitoring_app,
            "POST",
            f"{_monitoring_prefix(SERVER_A)}/findings/evaluate",
            headers=_mutation_headers(),
            cookies=session_jar,
        )
        assert response.status == 200, response.body
        result = response.json()
        assert result["server_id"] == SERVER_A and result["evaluated"] == 2
        by_dedup = {o["dedup_key"]: o for o in result["outcomes"]}
        assert (
            by_dedup[finding_dedup_key("update.available", "fixture-condition")][
                "disposition"
            ]
            == "created"
        )
        for outcome in result["outcomes"]:
            assert outcome["delivery"] == "delivered"  # inbox-only policy
            assert outcome["channels_attempted"] == []
        assert _count(migrated_url, "notification_alerts") == 2

    async def test_repeat_evaluate_dedups_inside_cooldown(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        _seed_finding(migrated_url, FINDING_UPDATE, severity="warning")
        first = await call_app(
            monitoring_app,
            "POST",
            f"{_monitoring_prefix(SERVER_A)}/findings/evaluate",
            headers=_mutation_headers(),
            cookies=session_jar,
        )
        second = await call_app(
            monitoring_app,
            "POST",
            f"{_monitoring_prefix(SERVER_A)}/findings/evaluate",
            headers=_mutation_headers(),
            cookies=session_jar,
        )
        assert first.status == 200 and second.status == 200
        assert first.json()["outcomes"][0]["disposition"] == "created"
        assert second.json()["outcomes"][0]["disposition"] == "deduped"
        assert _count(migrated_url, "notification_alerts") == 1  # no stacking

    async def test_acked_notice_is_never_re_nagged(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        _seed_finding(migrated_url, FINDING_UPDATE, severity="warning")
        first = await call_app(
            monitoring_app,
            "POST",
            f"{_monitoring_prefix(SERVER_A)}/findings/evaluate",
            headers=_mutation_headers(),
            cookies=session_jar,
        )
        [outcome] = first.json()["outcomes"]
        ack = await call_app(
            monitoring_app,
            "POST",
            f"{_monitoring_prefix(SERVER_A)}/notifications/{outcome['alert_id']}/ack",
            headers=_mutation_headers(),
            cookies=session_jar,
        )
        assert ack.status == 200, ack.body
        after = await call_app(
            monitoring_app,
            "POST",
            f"{_monitoring_prefix(SERVER_A)}/findings/evaluate",
            headers=_mutation_headers(),
            cookies=session_jar,
        )
        assert after.status == 200, after.body
        [post] = after.json()["outcomes"]
        assert post["disposition"] == "unchanged"  # acknowledged: never nagged
        assert post["delivery"] == "skipped"

    async def test_evaluate_on_server_without_findings_is_empty(
        self, monitoring_app: FastAPI, session_jar: dict[str, str]
    ) -> None:
        response = await call_app(
            monitoring_app,
            "POST",
            f"{_monitoring_prefix(SERVER_B)}/findings/evaluate",
            headers=_mutation_headers(),
            cookies=session_jar,
        )
        assert response.status == 200, response.body
        result = response.json()
        assert result["evaluated"] == 0 and result["outcomes"] == []

    async def test_resolved_findings_are_not_evaluated(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        _seed_finding(
            migrated_url,
            FINDING_RESOLVED,
            kind="process.anomaly",
            severity="info",
            opened_at=_utc() - timedelta(hours=2),
            resolved_at=_utc() - timedelta(minutes=5),
        )
        response = await call_app(
            monitoring_app,
            "POST",
            f"{_monitoring_prefix(SERVER_A)}/findings/evaluate",
            headers=_mutation_headers(),
            cookies=session_jar,
        )
        assert response.status == 200, response.body
        assert response.json()["evaluated"] == 0
        assert _count(migrated_url, "notification_alerts") == 0

    async def test_evaluate_requires_csrf(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        _seed_finding(migrated_url, FINDING_UPDATE, severity="warning")
        response = await call_app(
            monitoring_app,
            "POST",
            f"{_monitoring_prefix(SERVER_A)}/findings/evaluate",
            headers={"origin": ORIGIN},  # no X-CSRF-Token
            cookies=session_jar,
        )
        assert response.status == 403, response.body
        assert _error(response)["code"] == "csrf_rejected"
        assert _count(migrated_url, "notification_alerts") == 0


# ---------------------------------------------------------------------------
# TestPolicyEndpoint
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestPolicyEndpoint:
    """The policy view states verbatim what re-evaluation applies."""

    async def test_policy_reports_the_frozen_default(
        self, monitoring_app: FastAPI, session_jar: dict[str, str]
    ) -> None:
        response = await call_app(
            monitoring_app,
            "GET",
            "/api/v1/monitoring/notification-policy",
            cookies=session_jar,
        )
        assert response.status == 200, response.body
        policy = response.json()
        assert policy["cooldown_seconds"] == int(DEFAULT_COOLDOWN.total_seconds())
        assert policy["quiet_hours"] is None
        assert policy["channels_by_severity"] == {
            "info": [],
            "warning": [],
            "critical": [],
        }

    async def test_reported_policy_matches_applied_delivery(
        self, monitoring_app: FastAPI, migrated_url: str, session_jar: dict[str, str]
    ) -> None:
        # The endpoint reports inbox-only channels; the evaluate route must
        # behave exactly that way (no outbound attempts, immediate delivery).
        _seed_finding(migrated_url, FINDING_UPDATE, severity="critical")
        evaluated = await call_app(
            monitoring_app,
            "POST",
            f"{_monitoring_prefix(SERVER_A)}/findings/evaluate",
            headers=_mutation_headers(),
            cookies=session_jar,
        )
        [outcome] = evaluated.json()["outcomes"]
        assert outcome["delivery"] == "delivered"
        assert outcome["channels_attempted"] == []
        inbox = (
            await call_app(
                monitoring_app,
                "GET",
                f"{_monitoring_prefix(SERVER_A)}/notifications",
                cookies=session_jar,
            )
        ).json()
        assert inbox["alerts"][0]["delivery_status"] == "delivered"


# ---------------------------------------------------------------------------
# TestAuthBoundary
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestAuthBoundary:
    """Every monitoring route is session-guarded (unauthed = 401 envelope)."""

    async def test_observation_list_unauthenticated_is_401(
        self, monitoring_app: FastAPI
    ) -> None:
        response = await call_app(
            monitoring_app, "GET", f"{_monitoring_prefix(SERVER_A)}/observations"
        )
        assert response.status == 401, response.body
        assert _error(response)["code"] == "unauthenticated"

    async def test_finding_list_unauthenticated_is_401(
        self, monitoring_app: FastAPI
    ) -> None:
        response = await call_app(
            monitoring_app, "GET", f"{_monitoring_prefix(SERVER_A)}/findings"
        )
        assert response.status == 401, response.body
        assert _error(response)["code"] == "unauthenticated"

    async def test_inbox_list_unauthenticated_is_401(
        self, monitoring_app: FastAPI
    ) -> None:
        response = await call_app(
            monitoring_app, "GET", f"{_monitoring_prefix(SERVER_A)}/notifications"
        )
        assert response.status == 401, response.body
        assert _error(response)["code"] == "unauthenticated"

    async def test_policy_unauthenticated_is_401(self, monitoring_app: FastAPI) -> None:
        response = await call_app(
            monitoring_app, "GET", "/api/v1/monitoring/notification-policy"
        )
        assert response.status == 401, response.body
        assert _error(response)["code"] == "unauthenticated"

    async def test_single_reads_unauthenticated_are_401(
        self, monitoring_app: FastAPI
    ) -> None:
        for path in (
            f"{_monitoring_prefix(SERVER_A)}/observations/{uuid.uuid4()}",
            f"{_monitoring_prefix(SERVER_A)}/findings/{uuid.uuid4()}",
            f"{_monitoring_prefix(SERVER_A)}/notifications/{uuid.uuid4()}",
        ):
            response = await call_app(monitoring_app, "GET", path)
            assert response.status == 401, response.body

    async def test_ack_unauthenticated_is_401_and_never_stamps(
        self, monitoring_app: FastAPI
    ) -> None:
        response = await call_app(
            monitoring_app,
            "POST",
            f"{_monitoring_prefix(SERVER_A)}/notifications/{uuid.uuid4()}/ack",
        )
        assert response.status == 401, response.body
        assert _error(response)["code"] == "unauthenticated"

    async def test_evaluate_unauthenticated_is_401_and_never_evaluates(
        self, monitoring_app: FastAPI
    ) -> None:
        response = await call_app(
            monitoring_app,
            "POST",
            f"{_monitoring_prefix(SERVER_A)}/findings/evaluate",
        )
        assert response.status == 401, response.body
        assert _error(response)["code"] == "unauthenticated"
