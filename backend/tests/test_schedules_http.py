"""Focused tests for the schedules HTTP API (card 05-schedules-api).

Card ``05-schedules-api`` against contracts.md **C05 rule 8** (schedules
over the ONE durable facade, frozen misfire policy), **C08** (session/
origin/CSRF boundary on every privileged route), **C01** (envelope,
opaque identity, explicit server scope, bounded lists) and the SERVICES
``mai-api`` row.

Boundary discipline: every claim runs through the real ASGI app
(``create_app`` + lifespan + middleware + handlers) against a real
disposable Postgres (fixture ``migrated_url``) through the real linear
Alembic chain; the live-scenario half uses a REAL worker subprocess
executing the real scheduler + fire dispatch (the ``05-schedules``
harness shape). Mocks are used only where no fixture can produce the
condition (none needed here).

Registered P05 scenario coverage owned here (writer half; assembly is
``05-verify``): *actual API schedules near-term benign job, pauses
across due time, resumes under selected misfire policy and verifies one
resulting intent plus revocation denial.*

Secret hygiene: fixture credentials are inert constants for the
disposable fixture DB; session/CSRF tokens are minted at runtime and
never printed; schedules carry opaque server UUIDs and tmp-namespace
journal paths only.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI

from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE
from mai.jobs.engine import RUN_WORKFLOW_NAME
from mai.jobs.schedules import planned_run_id

BACKEND_ROOT = Path(__file__).resolve().parents[1]

#: The suites run async tests through the anyio pytest plugin (the repo's
#: async test convention); sync helpers stay unmarked and pass through.
pytestmark = pytest.mark.anyio

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: Synthetic fixture credential (NOT a secret; disposable fixture DB only).
FIXTURE_PASSWORD = "fixture-password-05-schedules-api-only"
ORIGIN = "http://127.0.0.1:8443"  # loopback allowlist entry

READY_TIMEOUT = 90.0
FIRE_TIMEOUT = 120.0

HANDLER_MODULE_NAME = "schedules_http_scenario_handlers"

#: Journaled fire handler (the 05-schedules scenario shape): the worker
#: subprocess imports this module, which also registers the durable fire
#: workflow exactly the way production worker wiring does. The journal
#: path travels INSIDE the payload (references only, C05 rule 3).
HANDLER_MODULE_SOURCE = '''
"""Scenario handlers for 05-schedules-api: harmless, journaled effects."""

import json

from dbos import DBOS

import mai.jobs.schedules  # noqa: F401 - registers the fire workflow
from mai.jobs import engine


@DBOS.step()
def _mark(path: str, marker: str) -> None:
    with open(path, "a") as f:
        f.write(marker + "\\n")


def echo(payload_json: str) -> str:
    payload = json.loads(payload_json)
    _mark(payload["journal"], payload["marker"])
    return payload["marker"]


engine.register_job_handler("schedule.echo", echo)
'''


# ---------------------------------------------------------------------------
# minimal ASGI harness (the test_jobs_http shape, without SSE)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HttpResponse:
    status: int | None = None
    headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    body: bytes = b""

    def header_all(self, name: str) -> list[str]:
        return [
            value.decode("ascii")
            for key, value in self.headers
            if key.decode("ascii").lower() == name
        ]

    def json(self) -> Any:
        return json.loads(self.body)


def _scope(method: str, path: str, headers: Mapping[str, str], body: bytes) -> dict:
    scope_headers = [(b"host", b"fixture")]
    scope_headers.extend(
        (key.encode("ascii").lower(), value.encode("ascii"))
        for key, value in headers.items()
    )
    url, _, query = path.partition("?")
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": url,
        "raw_path": path.encode("ascii"),
        "query_string": query.encode("ascii"),
        "root_path": "",
        "headers": scope_headers,
        "client": ("127.0.0.1", 0),
        "server": ("fixture", 80),
    }


async def call_app(
    app: FastAPI,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    headers: Mapping[str, str] | None = None,
    cookies: Mapping[str, str] | None = None,
) -> HttpResponse:
    """Run one bounded HTTP request through the real ASGI callable."""
    flat_headers = dict(headers or {})
    if cookies:
        flat_headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    body = b""
    if json_body is not None:
        body = json.dumps(json_body).encode()
        flat_headers["content-type"] = "application/json"
    response = HttpResponse()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            response.status = message["status"]
            response.headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            response.body += message.get("body", b"")

    await app(_scope(method, path, flat_headers, body), receive, send)
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
    login = await call_app(
        app,
        "POST",
        "/api/v1/auth/login",
        json_body={"password": FIXTURE_PASSWORD},
        headers={"origin": ORIGIN},
    )
    assert login.status == 204, login.body
    jar = _set_cookies(login)
    assert SESSION_COOKIE in jar and CSRF_COOKIE in jar
    return jar


def _mutation_headers(jar: Mapping[str, str]) -> dict[str, str]:
    return {
        "origin": ORIGIN,
        "x-csrf-token": jar[CSRF_COOKIE],
    }


async def create_profile(app: FastAPI, jar: dict[str, str], name: str) -> uuid.UUID:
    """Create one canonical server profile through the real HTTP surface."""
    response = await call_app(
        app,
        "POST",
        "/api/v1/servers",
        json_body={
            "name": name,
            "prompt_revision": 7,
            "environment": "linux_ssh",
            "ssh": {
                "host": "fixture.internal",
                "port": 22,
                "user": "fixture",
                "host_key": "SHA256:6VhOyUC6fvXW0aQ9WNe7Kx3nZZf+zxgupEWLoZXoTMU",
                "credential_ref": "MAI_SSH_KEY_FIXTURE",
            },
            "privilege": "none",
            "capabilities": [],
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert response.status == 201, response.body
    return uuid.UUID(response.json()["server_id"])


# ---------------------------------------------------------------------------
# schedule helpers
# ---------------------------------------------------------------------------


def _schedule_body(
    server_id: uuid.UUID,
    cron: str,
    *,
    cron_timezone: str | None = None,
    marker: str = "scheduled-fire",
    journal: Path | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "server_id": str(server_id),
        "cron": cron,
        "run_spec": {
            "handler_name": "schedule.echo",
            "payload": {"marker": marker},
        },
    }
    if cron_timezone is not None:
        body["cron_timezone"] = cron_timezone
    if journal is not None:
        body["run_spec"]["payload"]["journal"] = str(journal)
    if extra:
        body.update(extra)
    return body


async def _create_schedule(
    app: FastAPI,
    jar: dict[str, str],
    body: Mapping[str, Any],
) -> HttpResponse:
    return await call_app(
        app,
        "POST",
        "/api/v1/schedules",
        json_body=dict(body),
        headers=_mutation_headers(jar),
        cookies=jar,
    )


def _cron_for_seconds(seconds: set[int]) -> str:
    """A seconds-allowed cron firing at the given seconds of every minute."""
    if not seconds or any(not 0 <= s <= 59 for s in seconds):
        raise AssertionError("fire seconds must be within one minute")
    return f"{','.join(str(s) for s in sorted(seconds))} * * * * *"


def _next_fire(at_seconds: set[int], after: datetime) -> datetime:
    """The next whole second after ``after`` whose second-of-minute fires."""
    moment = (after + timedelta(seconds=1)).replace(microsecond=0)
    for _ in range(120):
        if moment.second in at_seconds:
            return moment
        moment += timedelta(seconds=1)
    raise AssertionError("no fire second within 120s of the probe")


def _journal(scenario: Path) -> Path:
    return scenario / "journal.log"


def _journal_count(scenario: Path, marker: str) -> int:
    try:
        lines = _journal(scenario).read_text().splitlines()
    except FileNotFoundError:
        return 0
    return sum(1 for line in lines if line == marker)


def _wait_until(predicate, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _pg_scalar(url: str, sql: str, params: dict[str, object]) -> object:
    """Fixture-side single-value read (harness inspection, not API)."""
    with psycopg.connect(url) as conn:
        value = conn.execute(sql, params).fetchone()[0]
        conn.commit()
    return value


def _pg_rows(url: str, sql: str, params: dict[str, object]) -> list[tuple]:
    """Fixture-side row reads (harness inspection, not API)."""
    with psycopg.connect(url) as conn:
        rows = conn.execute(sql, params).fetchall()
        conn.commit()
    return rows


def _engine_run_row(url: str, run_id: str, status: str) -> object:
    """Count the engine's run workflows under one run ID and status.

    The engine run workflow UUID is the planned-fire identity: this is
    where a fire's run intent is durably inspected.
    """
    return _pg_scalar(
        url,
        "SELECT count(*) FROM dbos.workflow_status "
        "WHERE workflow_uuid = %(r)s AND name = %(n)s AND status = %(st)s",
        {"r": run_id, "n": RUN_WORKFLOW_NAME, "st": status},
    )


# ---------------------------------------------------------------------------
# worker-side composition (REAL worker subprocess for the live scenario)
# ---------------------------------------------------------------------------


def _spawn_worker(
    url: str, handler_dir: Path, scenario: Path, tag: str
) -> subprocess.Popen:
    """Launch a REAL worker process against the fixture database."""
    env = dict(os.environ)
    env.pop(LIVE_URL_VAR, None)  # workers speak the production env contract
    env["MAI_DATABASE_URL"] = url
    env["MAI_WORKER_HANDLERS"] = HANDLER_MODULE_NAME
    env["PYTHONPATH"] = os.pathsep.join(
        [str(handler_dir), *(p for p in [env.get("PYTHONPATH")] if p)]
    )
    log = open(scenario / f"{tag}.log", "w")  # noqa: SIM115 - closed with proc
    return subprocess.Popen(
        [sys.executable, "-m", "mai.jobs.worker", "--concurrency", "2"],
        cwd=str(BACKEND_ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def _stop(proc: subprocess.Popen) -> int:
    """SIGTERM, then SIGKILL if the engine's graceful shutdown stalls."""
    proc.send_signal(signal.SIGTERM)
    try:
        return proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait(timeout=30)


# ---------------------------------------------------------------------------
# fixture database + real app harness
# ---------------------------------------------------------------------------


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(str(BACKEND_ROOT / "alembic.ini"))


def _drop_app_tables(url: str) -> None:
    """Reset the fixture database to pre-bootstrap state.

    Domain tables AND the engine-owned ``dbos`` schema: an interrupted
    earlier attempt can leave PARKED workflows or stale triggers.
    """
    with psycopg.connect(url) as conn:
        # Whole-schema reset (D09): FK-safe and immune to future revisions.
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


def _truncate(url: str) -> None:
    """Per-test isolation: fresh auth + fresh jobs domain (engine untouched)."""
    with psycopg.connect(url) as conn:
        present = conn.execute(
            "SELECT to_regclass('public.job_resource_locks') IS NOT NULL"
        ).fetchone()
        tables = [
            "job_schedule_fires",
            "job_steps",
            "job_events",
            "job_schedules",
            "job_tags",
            "job_runs",
            "approvals",
            "admin_sessions",
            "admin_credential",
        ]
        # 0007 observation domain: notification_alerts references
        # job_runs.run_id, so TRUNCATE of job_runs is refused unless the
        # referencing table is emptied in the same statement. Only the
        # tables that actually exist are named.
        observation_tables = [
            "notification_alerts",
            "observation_findings",
            "server_observations",
        ]
        for table in observation_tables:
            exists = conn.execute(
                "SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",)
            ).fetchone()
            if exists and exists[0]:
                tables.append(table)
        if present and present[0]:
            tables.insert(0, "job_resource_locks")
        conn.execute(f"TRUNCATE {', '.join(tables)}")
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``head``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the schedules HTTP surface claims "
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


@pytest.fixture(scope="session")
def bootstrapped_url(migrated_url: str) -> str:
    """Create the engine-owned ``dbos`` schema the way production does.

    The DBOS system schema is created by a full engine launch, never by
    the bare client and never by Alembic (engine module contract). One
    launch + clean shutdown plays the worker's bootstrap role, so the
    HTTP surface can derive triggers exactly like the real API process.
    """
    from mai.jobs import engine as jobs_engine

    jobs_engine.launch_worker(migrated_url)
    jobs_engine.shutdown()
    return migrated_url


def _build_app(url: str, data_root: Path) -> FastAPI:
    return create_app(
        env={
            "MAI_DATABASE_URL": url,
            "MAI_PUBLIC_ORIGINS": ORIGIN,
            "MAI_DATA_ROOT": str(data_root),
        }
    )


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[FastAPI]:
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def schedules_app(bootstrapped_url: str, tmp_path: Path):
    """App on the real fixture DB with a fresh confined data root."""
    _truncate(bootstrapped_url)
    app = _build_app(bootstrapped_url, tmp_path / "data-root")
    async with _running(app):
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()
    schedules = getattr(app.state, "schedules", None)
    if schedules is not None:
        schedules.close()


@pytest.fixture
async def jar(schedules_app: FastAPI) -> dict[str, str]:
    return await login_session(schedules_app)


@pytest.fixture
async def server_a(schedules_app: FastAPI, jar: dict[str, str]) -> uuid.UUID:
    return await create_profile(schedules_app, jar, "fixture-a")


@pytest.fixture
async def server_b(schedules_app: FastAPI, jar: dict[str, str]) -> uuid.UUID:
    return await create_profile(schedules_app, jar, "fixture-b")


@pytest.fixture
def scenario(tmp_path: Path) -> Path:
    """Fresh scenario namespace: handler module + journal."""
    root = tmp_path / "scenario"
    (root / "handlers").mkdir(parents=True)
    (root / "handlers" / f"{HANDLER_MODULE_NAME}.py").write_text(
        HANDLER_MODULE_SOURCE, encoding="utf-8"
    )
    _journal(root).touch()
    return root


# ---------------------------------------------------------------------------
# boundary: session → origin → CSRF on every privileged route
# ---------------------------------------------------------------------------


class TestAuthBoundary:
    async def test_mutations_without_session_are_401(
        self, schedules_app: FastAPI, server_a: uuid.UUID
    ) -> None:
        created = await call_app(
            schedules_app,
            "POST",
            "/api/v1/schedules",
            json_body=_schedule_body(server_a, "*/7 * * * * *"),
            headers={"origin": ORIGIN},
        )
        assert created.status == 401
        assert _error(created)["code"] == "unauthenticated"
        patched = await call_app(
            schedules_app,
            "PATCH",
            f"/api/v1/schedules/{uuid.uuid4()}",
            json_body={"enabled": False},
            headers={"origin": ORIGIN},
        )
        assert patched.status == 401
        deleted = await call_app(
            schedules_app,
            "DELETE",
            f"/api/v1/schedules/{uuid.uuid4()}",
            headers={"origin": ORIGIN},
        )
        assert deleted.status == 401

    async def test_reads_without_session_are_401(self, schedules_app: FastAPI) -> None:
        listed = await call_app(schedules_app, "GET", "/api/v1/schedules")
        assert listed.status == 401
        read = await call_app(schedules_app, "GET", f"/api/v1/schedules/{uuid.uuid4()}")
        assert read.status == 401
        fires = await call_app(
            schedules_app, "GET", f"/api/v1/schedules/{uuid.uuid4()}/fires"
        )
        assert fires.status == 401

    async def test_origin_and_csrf_layers_in_order(
        self, schedules_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        # Layer 2 without layer 3: a valid session from a non-allowlisted
        # origin is origin-rejected before any CSRF comparison.
        evil = await call_app(
            schedules_app,
            "POST",
            "/api/v1/schedules",
            json_body=_schedule_body(server_a, "*/7 * * * * *"),
            headers={"origin": "https://attacker.example"},
            cookies=jar,
        )
        assert evil.status == 403
        assert _error(evil)["code"] == "origin_rejected"
        # Layer 3: allowlisted origin but no double-submit token.
        no_csrf = await call_app(
            schedules_app,
            "POST",
            "/api/v1/schedules",
            json_body=_schedule_body(server_a, "*/7 * * * * *"),
            headers={"origin": ORIGIN},
            cookies=jar,
        )
        assert no_csrf.status == 403
        assert _error(no_csrf)["code"] == "csrf_rejected"

    async def test_revoked_session_is_denied_on_every_route(
        self, schedules_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        logout = await call_app(
            schedules_app,
            "POST",
            "/api/v1/auth/logout",
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert logout.status in (200, 204), logout.body
        for method, path, body in (
            ("GET", "/api/v1/schedules", None),
            ("GET", f"/api/v1/schedules/{uuid.uuid4()}", None),
            ("GET", f"/api/v1/schedules/{uuid.uuid4()}/fires", None),
            ("POST", "/api/v1/schedules", _schedule_body(server_a, "*/7 * * * * *")),
            ("PATCH", f"/api/v1/schedules/{uuid.uuid4()}", {"enabled": False}),
            ("DELETE", f"/api/v1/schedules/{uuid.uuid4()}", None),
        ):
            response = await call_app(
                schedules_app,
                method,
                path,
                json_body=body,
                headers=_mutation_headers(jar),
                cookies=jar,
            )
            assert response.status == 401, (method, path, response.body)
            assert _error(response)["code"] == "unauthenticated"


# ---------------------------------------------------------------------------
# create: frozen fields, target binding, strict schema (acceptance 1 + 2)
# ---------------------------------------------------------------------------


class TestCreate:
    async def test_create_persists_timezone_misfire_and_server_binding(
        self,
        schedules_app: FastAPI,
        bootstrapped_url: str,
        jar: dict[str, str],
        server_a: uuid.UUID,
    ) -> None:
        """Acceptance 1: create persists timezone/misfire + server binding."""
        response = await _create_schedule(
            schedules_app,
            jar,
            _schedule_body(
                server_a,
                "*/7 * * * * *",
                cron_timezone="Europe/Berlin",
                journal=Path("/tmp/j"),
                marker="create-pin",
            ),
        )
        assert response.status == 201, response.body
        view = response.json()
        schedule_id = view["schedule_id"]
        assert uuid.UUID(schedule_id)  # opaque stable identity (C01 rule 1)
        assert view["server_id"] == str(server_a)  # server binding
        assert view["cron_timezone"] == "Europe/Berlin"  # explicit zone
        assert view["misfire_policy"] == "skip_missed"  # frozen policy
        assert view["enabled"] is True
        assert view["run_spec"] == {
            "handler_name": "schedule.echo",
            "payload": {"marker": "create-pin", "journal": "/tmp/j"},
        }
        assert view["created_at"].endswith("Z")
        assert view["updated_at"].endswith("Z")
        # Read-back is the persisted row, not the request echo.
        read = await call_app(
            schedules_app,
            "GET",
            f"/api/v1/schedules/{schedule_id}",
            cookies=jar,
        )
        assert read.status == 200
        assert read.json() == view
        # The engine trigger is derived from the row (one DBOS schedule
        # named after the domain ID — facade contract, wired for real).
        assert (
            _pg_scalar(
                bootstrapped_url,
                "SELECT count(*) FROM dbos.workflow_schedules "
                "WHERE schedule_name = %(s)s",
                {"s": schedule_id},
            )
            == 1
        )

    async def test_absent_timezone_applies_frozen_utc_default(
        self, schedules_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        response = await _create_schedule(
            schedules_app, jar, _schedule_body(server_a, "*/7 * * * * *")
        )
        assert response.status == 201, response.body
        assert response.json()["cron_timezone"] == "UTC"

    async def test_unknown_or_unadopted_target_is_denied(
        self, schedules_app: FastAPI, jar: dict[str, str], bootstrapped_url: str
    ) -> None:
        """Acceptance 2: an unauthorized target is denied BEFORE any row."""
        response = await _create_schedule(
            schedules_app,
            jar,
            _schedule_body(uuid.uuid4(), "*/7 * * * * *"),
        )
        assert response.status == 404, response.body
        assert _error(response)["code"] == "not_found"
        assert (
            _pg_scalar(bootstrapped_url, "SELECT count(*) FROM job_schedules", {}) == 0
        )

    async def test_strict_schema_rejects_grant_vocabulary_and_drift(
        self,
        schedules_app: FastAPI,
        jar: dict[str, str],
        server_a: uuid.UUID,
        bootstrapped_url: str,
    ) -> None:
        """No grant rider can enter: the surface has no grant vocabulary.

        A schedule never expands grants for its later job (acceptance 2):
        any grant-shaped key is a structured 422, never a stored field.
        """
        bad_bodies = [
            # Explicit grant-expansion attempts — all rejected.
            _schedule_body(
                server_a, "*/7 * * * * *", extra={"grants": ["fixture.read"]}
            ),
            _schedule_body(
                server_a,
                "*/7 * * * * *",
                extra={"requested_grants": ["fixture.read"]},
            ),
            _schedule_body(
                server_a, "*/7 * * * * *", extra={"capability_ids": ["fixture.read"]}
            ),
            _schedule_body(
                server_a,
                "*/7 * * * * *",
                extra={"misfire_policy": "catchup"},  # policy is not a wire field
            ),
            # Definition drift is rejected at create, too.
            _schedule_body(server_a, "not a cron"),
            _schedule_body(server_a, "*/7 * * * * *", cron_timezone="Mars/Olympus"),
            _schedule_body(server_a, "*/7 * * * * *", extra={"enabled": False}),
            {**_schedule_body(server_a, "*/7 * * * * *"), "server_id": "not-a-uuid"},
            {
                **_schedule_body(server_a, "*/7 * * * * *"),
                "run_spec": {
                    "handler_name": "schedule.echo",
                    "payload": {},
                    "grants": ["fixture.read"],  # rider inside run_spec
                },
            },
            {
                **_schedule_body(server_a, "*/7 * * * * *"),
                "run_spec": {"handler_name": "NOT A SLUG", "payload": {}},
            },
        ]
        for body in bad_bodies:
            response = await _create_schedule(schedules_app, jar, body)
            assert response.status == 422, (body, response.body)
            error = _error(response)
            assert error["code"] == "validation_failed"
            assert error["fields"], body
        # Nothing persisted by any rejected body (value-free all-or-nothing).
        assert (
            _pg_scalar(bootstrapped_url, "SELECT count(*) FROM job_schedules", {}) == 0
        )


# ---------------------------------------------------------------------------
# update = pause/resume ONLY (definition frozen; conflicting update denied)
# ---------------------------------------------------------------------------


class TestUpdate:
    async def test_pause_and_resume_flips_persisted_enabled(
        self, schedules_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        created = await _create_schedule(
            schedules_app, jar, _schedule_body(server_a, "*/7 * * * * *")
        )
        schedule_id = created.json()["schedule_id"]
        paused = await call_app(
            schedules_app,
            "PATCH",
            f"/api/v1/schedules/{schedule_id}",
            json_body={"enabled": False},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert paused.status == 200, paused.body
        assert paused.json()["enabled"] is False
        resumed = await call_app(
            schedules_app,
            "PATCH",
            f"/api/v1/schedules/{schedule_id}",
            json_body={"enabled": True},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert resumed.status == 200, resumed.body
        assert resumed.json()["enabled"] is True

    async def test_conflicting_definition_update_is_denied(
        self, schedules_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        """The definition is frozen: mutation attempts are structured 422s.

        The facade exposes no definition mutation (delete + re-create is
        the path), so a conflicting update can never half-apply.
        """
        created = await _create_schedule(
            schedules_app,
            jar,
            _schedule_body(server_a, "*/7 * * * * *", cron_timezone="UTC"),
        )
        schedule_id = created.json()["schedule_id"]
        for body in (
            {"enabled": True, "cron": "*/9 * * * * *"},  # rider on a legal key
            {"cron": "*/9 * * * * *"},
            {"cron_timezone": "Asia/Tokyo"},
            {"run_spec": {"handler_name": "schedule.other", "payload": {}}},
            {},  # not a pause/resume either
        ):
            response = await call_app(
                schedules_app,
                "PATCH",
                f"/api/v1/schedules/{schedule_id}",
                json_body=body,
                headers=_mutation_headers(jar),
                cookies=jar,
            )
            assert response.status == 422, (body, response.body)
            assert _error(response)["code"] == "validation_failed"
        # The stored definition is exactly the created one.
        read = await call_app(
            schedules_app, "GET", f"/api/v1/schedules/{schedule_id}", cookies=jar
        )
        assert read.json()["cron"] == "*/7 * * * * *"
        assert read.json()["cron_timezone"] == "UTC"

    async def test_update_of_unknown_schedule_is_404(
        self, schedules_app: FastAPI, jar: dict[str, str]
    ) -> None:
        response = await call_app(
            schedules_app,
            "PATCH",
            f"/api/v1/schedules/{uuid.uuid4()}",
            json_body={"enabled": False},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert response.status == 404
        assert _error(response)["code"] == "not_found"


# ---------------------------------------------------------------------------
# delete: row + fires cascade, then the trigger; unknown is 404
# ---------------------------------------------------------------------------


class TestDelete:
    async def test_delete_removes_record_and_engine_trigger(
        self,
        schedules_app: FastAPI,
        bootstrapped_url: str,
        jar: dict[str, str],
        server_a: uuid.UUID,
    ) -> None:
        created = await _create_schedule(
            schedules_app, jar, _schedule_body(server_a, "*/7 * * * * *")
        )
        schedule_id = created.json()["schedule_id"]
        deleted = await call_app(
            schedules_app,
            "DELETE",
            f"/api/v1/schedules/{schedule_id}",
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert deleted.status == 204, deleted.body
        read = await call_app(
            schedules_app, "GET", f"/api/v1/schedules/{schedule_id}", cookies=jar
        )
        assert read.status == 404
        assert (
            _pg_scalar(
                bootstrapped_url,
                "SELECT count(*) FROM dbos.workflow_schedules "
                "WHERE schedule_name = %(s)s",
                {"s": schedule_id},
            )
            == 0
        )
        again = await call_app(
            schedules_app,
            "DELETE",
            f"/api/v1/schedules/{schedule_id}",
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert again.status == 404

    async def test_delete_of_unknown_schedule_is_404(
        self, schedules_app: FastAPI, jar: dict[str, str]
    ) -> None:
        response = await call_app(
            schedules_app,
            "DELETE",
            f"/api/v1/schedules/{uuid.uuid4()}",
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert response.status == 404


# ---------------------------------------------------------------------------
# list/get: bounded pages, explicit scope, opaque identity validation
# ---------------------------------------------------------------------------


class TestListAndGet:
    async def _seed(
        self,
        schedules_app: FastAPI,
        jar: dict[str, str],
        server: uuid.UUID,
        count: int,
        cron: str,
    ) -> list[str]:
        ids = []
        for _ in range(count):
            created = await _create_schedule(
                schedules_app, jar, _schedule_body(server, cron)
            )
            assert created.status == 201, created.body
            ids.append(created.json()["schedule_id"])
        return ids

    async def test_list_is_bounded_scoped_and_ordered(
        self,
        schedules_app: FastAPI,
        jar: dict[str, str],
        server_a: uuid.UUID,
        server_b: uuid.UUID,
    ) -> None:
        ids_a = await self._seed(schedules_app, jar, server_a, 3, "*/7 * * * * *")
        ids_b = await self._seed(schedules_app, jar, server_b, 1, "*/9 * * * * *")
        full = await call_app(schedules_app, "GET", "/api/v1/schedules", cookies=jar)
        assert full.status == 200
        page = full.json()
        assert page["total"] == 4
        assert len(page["schedules"]) == 4
        assert {item["schedule_id"] for item in page["schedules"]} == (
            set(ids_a) | set(ids_b)
        )
        # Bounded page: limit + offset travel as explicit bounds.
        paged = await call_app(
            schedules_app, "GET", "/api/v1/schedules?limit=2&offset=1", cookies=jar
        )
        assert paged.json()["total"] == 4
        assert [item["schedule_id"] for item in paged.json()["schedules"]] == (
            [item["schedule_id"] for item in page["schedules"][1:3]]
        )
        # Explicit server scope (C01 rule 7): only that server's schedules.
        scoped = await call_app(
            schedules_app,
            "GET",
            f"/api/v1/schedules?server_id={server_a}",
            cookies=jar,
        )
        scoped_page = scoped.json()
        assert scoped_page["total"] == 3
        assert {item["schedule_id"] for item in scoped_page["schedules"]} == set(ids_a)
        assert all(
            item["server_id"] == str(server_a) for item in scoped_page["schedules"]
        )

    async def test_list_bounds_are_enforced(
        self, schedules_app: FastAPI, jar: dict[str, str]
    ) -> None:
        zero = await call_app(
            schedules_app, "GET", "/api/v1/schedules?limit=0", cookies=jar
        )
        assert zero.status == 422
        huge = await call_app(
            schedules_app, "GET", "/api/v1/schedules?limit=201", cookies=jar
        )
        assert huge.status == 422

    async def test_get_malformed_identity_is_validation_not_route_miss(
        self, schedules_app: FastAPI, jar: dict[str, str]
    ) -> None:
        response = await call_app(
            schedules_app, "GET", "/api/v1/schedules/not-a-uuid", cookies=jar
        )
        assert response.status == 422
        error = _error(response)
        assert error["code"] == "validation_failed"
        assert any("schedule_id" in field for field in error["fields"])


# ---------------------------------------------------------------------------
# fire ledger page (C05 rule 8 read surface)
# ---------------------------------------------------------------------------


class TestFiresBoundary:
    async def test_fires_of_unknown_schedule_are_404(
        self, schedules_app: FastAPI, jar: dict[str, str]
    ) -> None:
        response = await call_app(
            schedules_app,
            "GET",
            f"/api/v1/schedules/{uuid.uuid4()}/fires",
            cookies=jar,
        )
        assert response.status == 404
        assert _error(response)["code"] == "not_found"

    async def test_fires_of_malformed_identity_are_422(
        self, schedules_app: FastAPI, jar: dict[str, str]
    ) -> None:
        response = await call_app(
            schedules_app, "GET", "/api/v1/schedules/not-a-uuid/fires", cookies=jar
        )
        assert response.status == 422


# ---------------------------------------------------------------------------
# OpenAPI contract (AC3: real registration, generated schema only)
# ---------------------------------------------------------------------------


class TestOpenApiContract:
    def test_schedules_routes_are_registered_through_the_real_app(
        self, bootstrapped_url: str, tmp_path: Path
    ) -> None:
        app = _build_app(bootstrapped_url, tmp_path / "data-root")
        api_routes = [
            route
            for included in app.routes
            for route in (
                getattr(included, "original_router", included).routes
                if getattr(included, "original_router", None) is not None
                else [included]
            )
        ]
        registered = {route.path: route for route in api_routes}
        collection = registered.get("/api/v1/schedules")
        assert collection is not None
        item = registered.get("/api/v1/schedules/{schedule_id}")
        assert item is not None
        fires = registered.get("/api/v1/schedules/{schedule_id}/fires")
        assert fires is not None
        # No public docs/OpenAPI listeners (G1).
        api_paths = {route.path for route in api_routes}
        assert "/docs" not in api_paths and "/openapi.json" not in api_paths

    def test_openapi_declares_the_frozen_schedules_contract(
        self, bootstrapped_url: str, tmp_path: Path
    ) -> None:
        schema = _build_app(bootstrapped_url, tmp_path / "data-root").openapi()
        paths = schema["paths"]
        assert set(paths["/api/v1/schedules"]) == {"get", "post"}
        assert set(paths["/api/v1/schedules/{schedule_id}"]) == {
            "get",
            "patch",
            "delete",
        }
        assert set(paths["/api/v1/schedules/{schedule_id}/fires"]) == {"get"}
        # Mutation layers are documented on the mutating operations.
        for path, method in (
            ("/api/v1/schedules", "post"),
            ("/api/v1/schedules/{schedule_id}", "patch"),
            ("/api/v1/schedules/{schedule_id}", "delete"),
        ):
            operation = paths[path][method]
            documented = {str(code) for code in (operation.get("responses") or {})}
            assert "401" in documented, (path, method)
            assert "403" in documented, (path, method)
        # The generated component schemas exist (no handwritten types).
        components = schema["components"]["schemas"]
        for name in (
            "ScheduleCreateBody",
            "ScheduleUpdateBody",
            "ScheduleView",
            "SchedulePage",
            "FireView",
            "FirePage",
        ):
            assert name in components, name
        # Strict bodies: no additional properties anywhere on inputs.
        create = components["ScheduleCreateBody"]
        assert create.get("additionalProperties") is False


# ---------------------------------------------------------------------------
# live scenario (writer half of the registered P05 coverage; assembly is
# 05-verify): pause across due time → resume under the frozen misfire
# policy → exactly one resulting intent + revocation denial
# ---------------------------------------------------------------------------


class TestLiveScenario:
    async def test_pause_across_due_resumes_to_exactly_one_intent(
        self,
        schedules_app: FastAPI,
        bootstrapped_url: str,
        jar: dict[str, str],
        server_a: uuid.UUID,
        scenario: Path,
    ) -> None:
        # Near-term benign job: three fire seconds 20s apart; the first
        # two come due while the schedule is PAUSED via the API.
        first_second = (datetime.now(timezone.utc) + timedelta(seconds=15)).second
        seconds = {
            first_second,
            (first_second + 20) % 60,
            (first_second + 40) % 60,
        }
        created = await _create_schedule(
            schedules_app,
            jar,
            _schedule_body(
                server_a,
                _cron_for_seconds(seconds),
                journal=_journal(scenario),
                marker="api-resumed-fire",
            ),
        )
        assert created.status == 201, created.body
        view = created.json()
        schedule_id = view["schedule_id"]

        missed_one = _next_fire(seconds, datetime.now(timezone.utc))
        missed_two = _next_fire(seconds, missed_one)
        resume_fire = _next_fire(seconds, missed_two)

        # PAUSE via the API before the first fire is due.
        paused = await call_app(
            schedules_app,
            "PATCH",
            f"/api/v1/schedules/{schedule_id}",
            json_body={"enabled": False},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert paused.status == 200 and paused.json()["enabled"] is False

        # The two misses actually pass while paused: the engine trigger
        # is paused AND the domain flag is off — no ledger row, no run
        # (skip missed fires, resume from now, no catch-up — C05 rule 8).
        _wait_until(
            lambda: datetime.now(timezone.utc) >= missed_two + timedelta(seconds=2),
            FIRE_TIMEOUT,
            "the two missed fire times to pass while paused",
        )
        while_paused = await call_app(
            schedules_app,
            "GET",
            f"/api/v1/schedules/{schedule_id}/fires",
            cookies=jar,
        )
        assert while_paused.status == 200
        assert while_paused.json()["total"] == 0

        # RESUME via the API under the selected (frozen) misfire policy,
        # then start the ONE real worker: the post-resume fire dispatches.
        resumed = await call_app(
            schedules_app,
            "PATCH",
            f"/api/v1/schedules/{schedule_id}",
            json_body={"enabled": True},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert resumed.status == 200 and resumed.json()["enabled"] is True
        worker = _spawn_worker(bootstrapped_url, scenario / "handlers", scenario, "w1")
        try:
            _wait_until(
                lambda: _journal_count(scenario, "api-resumed-fire") >= 1,
                FIRE_TIMEOUT,
                "the post-resume fire to dispatch",
            )
        finally:
            _stop(worker)

        # ONE resulting intent, verified through the API surface AND the
        # durable layers behind it.
        assert _journal_count(scenario, "api-resumed-fire") == 1  # effect once
        fires = await call_app(
            schedules_app,
            "GET",
            f"/api/v1/schedules/{schedule_id}/fires",
            cookies=jar,
        )
        assert fires.status == 200
        page = fires.json()
        assert page["total"] == 1  # no duplicate dispatch
        (fire,) = page["fires"]
        assert fire["schedule_id"] == schedule_id
        assert fire["planned_fire_time"].startswith(
            resume_fire.strftime("%Y-%m-%dT%H:%M:%S")
        )
        assert fire["run_id"] is None  # ledger link open (run-row owner pending)
        expected_identity = planned_run_id(schedule_id, resume_fire)
        assert fire["planned_run_id"] == expected_identity
        assert (
            _engine_run_row(bootstrapped_url, expected_identity, "SUCCESS") == 1
        )  # the fire's run intent, exactly once
        assert (
            _pg_scalar(
                bootstrapped_url,
                "SELECT count(*) FROM dbos.workflow_status WHERE name = "
                "'schedule_fire_workflow' AND workflow_uuid LIKE %(prefix)s "
                "AND status = 'SUCCESS'",
                {"prefix": f"sched-{schedule_id}-%"},
            )
            == 1
        )  # one fire workflow for the resumed fire

        # Revocation denial: the revoked session is denied everywhere —
        # even the read of the intent it just created.
        logout = await call_app(
            schedules_app,
            "POST",
            "/api/v1/auth/logout",
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert logout.status in (200, 204), logout.body
        denied = await call_app(
            schedules_app,
            "GET",
            f"/api/v1/schedules/{schedule_id}/fires",
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert denied.status == 401
        assert _error(denied)["code"] == "unauthenticated"
