"""Focused tests for the jobs HTTP API (card 05-jobs-api).

Card ``05-jobs-api`` against contracts.md **C05** (durable runs, submit
dedup, cancel semantics, the frozen event envelope), **C08** (session/
origin boundary; the C08 rule 7 open transport item settles HERE: SSE +
``Last-Event-ID`` replay cursor), **C01** (envelope, opaque identity,
explicit server scope) and the SERVICES ``mai-api`` row.

Boundary discipline: every claim runs through the real ASGI app
(``create_app`` + lifespan + middleware + handlers) against a real
disposable Postgres (fixture ``migrated_url``) through the real linear
Alembic chain, and — for the live scenario halves — REAL worker
subprocesses executing real capability steps through the real dispatcher
(exactly the ``05-cancel-recovery`` harness shape). Mocks are used only
where no fixture can produce the condition.

Registered P05 scenario coverage owned here (writer half; assembly is
``05-verify``; JOB-SUBMIT + EVT-REPLAY + the transport half of AUTH-S5):
*authenticated HTTP creates job, idempotent duplicate submit, disconnect
leaves work running, reconnect from cursor deduplicates, cancel with
scope/boundary checks verifies DB result, unauthorized event subscription
denied.*

Secret hygiene: fixture credentials/principals are inert constants for
the disposable fixture DB; session and CSRF tokens are minted at runtime
and never printed into assertion messages; runs carry opaque server UUIDs
and slug-shaped capability ids only.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from psycopg.rows import dict_row
from fastapi import FastAPI

from mai.api import jobs as jobs_api
from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE
from mai.contracts.jobs import TERMINAL_RUN_STATES, RunState
from mai.jobs.engine import EngineClient
from mai.tags import RESERVED_UNCLASSIFIED_TAG_ID

BACKEND_ROOT = Path(__file__).resolve().parents[1]

#: The suites run async tests through the anyio pytest plugin (the repo's
#: async test convention); sync helpers stay unmarked and pass through.
pytestmark = pytest.mark.anyio

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: Synthetic fixture credential (NOT a secret; disposable fixture DB only).
FIXTURE_PASSWORD = "fixture-password-05-jobs-api-only"
ORIGIN = "http://127.0.0.1:8443"  # loopback allowlist entry
EVIL_ORIGIN = "https://attacker.example"

#: Opaque server identities (C01 rule 1) — never display names.
SERVER_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
SERVER_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")

#: Fixture capability slugs (C03 rule 1 shape).
READ_CAP = "fixture.read"
MUTATE_CAP = "fixture.mutate"

READY_TIMEOUT = 90.0
RUN_TIMEOUT = 120.0

HANDLER_MODULE_NAME = "jobs_http_scenario_handlers"


# ---------------------------------------------------------------------------
# the worker-side composition: real dispatcher steps (the producer half the
# HTTP surface intentionally does NOT run inline — acceptance 2)
# ---------------------------------------------------------------------------

HANDLER_MODULE_SOURCE = '''
"""Worker-side composition for 05-jobs-api live scenarios."""

import json
import os
import time
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import mai.jobs.steps as steps
from mai.capabilities.dispatch import CapabilityDispatcher, ExecutorResult
from mai.credentials.env import EnvResolver
from mai.jobs.resource_locks import ResourceLockService
from mai.plugins.registry import CapabilityRegistry, parse_descriptor
from mai.policy.approvals import ApprovalStore
from mai.policy.evaluate import Policy


def _descriptor(capability_id: str, effect_class: str):
    return parse_descriptor(
        {
            "identifier": capability_id,
            "version": "1.0.0",
            "config_schema": None,
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["marker"],
                "properties": {"marker": {"type": "string", "minLength": 1}},
            },
            "output_schema": {
                "type": "object",
                "required": ["status", "exit_code", "stdout", "stderr"],
                "properties": {
                    "status": {"type": "string"},
                    "exit_code": {"type": ["integer", "null"]},
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                },
            },
            "execution_environments": ["linux_ssh"],
            "effect_class": effect_class,
            "resource_scope": ["target.exec"],
            "required_grants": [capability_id],
        },
        source=f"<{capability_id} descriptor>",
    )


class FixtureExecutor:
    """Journaled capability executor (the 05-steps fixture shape)."""

    def __init__(self) -> None:
        self.journal = os.environ["MAI_FX_JOURNAL"]
        self.gate_dir = os.environ["MAI_FX_GATE_DIR"]
        self.behaviors = json.loads(os.environ.get("MAI_FX_BEHAVIOR", "{}"))

    def _mark(self, marker: str) -> None:
        with open(self.journal, "a", encoding="utf-8") as handle:
            handle.write(marker + "\\n")

    def _behavior(self, capability_id: str, marker: str) -> str:
        keyed = self.behaviors.get(f"{capability_id}:{marker}")
        if keyed is not None:
            return keyed
        return self.behaviors.get(capability_id, "ok")

    async def __call__(self, call):
        capability_id = call.descriptor.identifier
        marker = call.payload.get("marker", "?")
        behavior = self._behavior(capability_id, str(marker))
        self._mark(f"begin:{capability_id}:{marker}")
        if behavior == "gate":
            gate = os.path.join(self.gate_dir, f"gate-{capability_id}-{marker}")
            deadline = time.time() + 300
            while not os.path.exists(gate):
                if time.time() > deadline:
                    raise RuntimeError("gate timeout")
                time.sleep(0.1)
        self._mark(f"effect:{capability_id}:{marker}")
        status = "outcome_unknown" if behavior == "unknown_status" else "succeeded"
        document = {
            "status": status,
            "exit_code": 0 if status == "succeeded" else None,
            "stdout": "",
            "stderr": "",
        }
        return ExecutorResult(
            ok=status == "succeeded", output=json.dumps(document, sort_keys=True)
        )


def _build() -> None:
    url = os.environ["MAI_DATABASE_URL"]
    app_engine = create_async_engine(url, poolclass=NullPool)
    sessions = async_sessionmaker(app_engine, expire_on_commit=False)
    with open(os.environ["MAI_FX_GRANTS"], encoding="utf-8") as handle:
        grants_doc = json.load(handle)
    with open(os.environ["MAI_FX_POLICY"], encoding="utf-8") as handle:
        policy_doc = json.load(handle)
    grants = {
        entry["capability_id"]: frozenset(
            uuid.UUID(value) for value in entry["server_ids"]
        )
        for entry in grants_doc
    }
    registry = CapabilityRegistry()
    registry.register(_descriptor(READ_CAP, "read"))
    registry.register(_descriptor(MUTATE_CAP, "mutate"))
    dispatcher = CapabilityDispatcher(
        registry=registry,
        approvals=ApprovalStore(sessions),
        grants=lambda: grants,
        policy=lambda: Policy(
            revision=int(policy_doc["revision"]),
            denied_capabilities=frozenset(policy_doc.get("denied", [])),
        ),
        adopted=lambda server_id: True,
        credentials=lambda: EnvResolver(
            os.environ.get("MAI_FX_ENV", "/tmp/mai-fxt-05-jobs-api/absent.env")
        ),
        executor=FixtureExecutor(),
    )
    locks = ResourceLockService(url)
    steps.configure_steps(
        steps.StepExecutor(sessions=sessions, dispatcher=dispatcher, locks=locks)
    )


READ_CAP = "fixture.read"
MUTATE_CAP = "fixture.mutate"

_build()
'''


# ---------------------------------------------------------------------------
# helpers: persistent test loop + ASGI harness (no test-client dependency)
# ---------------------------------------------------------------------------

_TEST_LOOP: asyncio.AbstractEventLoop | None = None
_TEST_LOOP_LOCK = threading.Lock()


def _run(coro: object) -> object:
    """Run one coroutine on the persistent test loop; block for the result."""
    global _TEST_LOOP
    with _TEST_LOOP_LOCK:
        if _TEST_LOOP is None or _TEST_LOOP.is_closed():
            _TEST_LOOP = asyncio.new_event_loop()
            thread = threading.Thread(target=_TEST_LOOP.run_forever, daemon=True)
            thread.start()
    return asyncio.run_coroutine_threadsafe(coro, _TEST_LOOP).result()


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


def _scope(method: str, path: str, headers: Mapping[str, str], body: bytes) -> dict:
    scope_headers = [(b"host", b"fixture")]
    scope_headers.extend(
        (key.encode("ascii").lower(), value.encode("ascii"))
        for key, value in headers.items()
    )
    url, _, query = path.partition("?")
    scope: dict[str, Any] = {
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
    return scope


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


@dataclass(slots=True)
class StreamCall:
    """One open streaming request through the real ASGI callable."""

    task: asyncio.Task | None = None
    status: int | None = None
    headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    body: str = ""
    _wake: asyncio.Event = field(default_factory=asyncio.Event)
    _request_consumed: bool = False

    def header(self, name: str) -> str | None:
        for key, value in self.headers:
            if key.decode("ascii").lower() == name:
                return value.decode("ascii")
        return None

    async def _on_message(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"").decode("utf-8", "replace")
        self._wake.set()

    async def wait_started(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while self.status is None:
            if self.task is not None and self.task.done():
                raise AssertionError("stream closed before response start")
            await asyncio.sleep(0.02)
            if time.monotonic() > deadline:
                raise AssertionError("stream never started")
        if self.status != 200:
            raise AssertionError(f"stream started with {self.status}: {self.body!r}")

    async def wait_until(self, predicate, timeout: float, what: str) -> None:
        """Drain the live stream until ``predicate(body)`` or it ends."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(self.body):
                return
            if self.task is not None and self.task.done():
                break
            self._wake.clear()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=0.05)
        if not predicate(self.body):
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")

    async def close(self) -> None:
        """Simulate the client disconnecting (the browser closes the tab)."""
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task


async def open_stream(
    app: FastAPI,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
    cookies: Mapping[str, str] | None = None,
) -> StreamCall:
    """Open one streaming GET; returns immediately after response start."""
    flat_headers = {"accept": "text/event-stream"}
    flat_headers.update(headers or {})
    if cookies:
        flat_headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    call = StreamCall()

    async def receive() -> dict[str, Any]:
        # ASGI receive contract for a LIVE client: the request body is
        # delivered once; afterwards the channel BLOCKS until a disconnect
        # (it never re-delivers ``http.request``). Re-delivering it makes
        # BaseHTTPMiddleware's disconnect listener raise
        # "Unexpected message received" and kills the stream.
        if not call._request_consumed:
            call._request_consumed = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()  # connected until close() cancels
        return {"type": "http.disconnect"}  # pragma: no cover - unreachable

    async def runner() -> None:
        await app(
            _scope("GET", path, flat_headers, b""),
            receive,
            call._on_message,
        )

    call.task = asyncio.get_running_loop().create_task(runner())
    await call.wait_started()
    return call


def sse_frames(body: str) -> list[dict[str, Any]]:
    """Parse COMPLETE SSE frames into ``{id, event, data}`` records.

    A frame without its terminating blank line is still streaming: it is
    dropped here, never parsed (a partial ``data:`` line would raise a
    confusing JSONDecodeError mid-wait).
    """
    parts = body.split("\n\n")
    if body and not body.endswith("\n\n"):
        parts = parts[:-1]
    frames = []
    for raw in parts:
        if not raw.strip():
            continue
        record: dict[str, Any] = {"id": None, "event": None, "data": None}
        data_lines: list[str] = []
        for line in raw.split("\n"):
            if line.startswith(":"):
                continue
            field_name, _, value = line.partition(":")
            value = value.lstrip(" ")
            if field_name == "id":
                record["id"] = value
            elif field_name == "event":
                record["event"] = value
            elif field_name == "data":
                data_lines.append(value)
        if data_lines:
            record["data"] = json.loads("\n".join(data_lines))
        frames.append(record)
    return frames


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


async def _login(app: FastAPI) -> dict[str, str]:
    """Login against an already-bootstrapped credential; return the jar."""
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
    return await _login(app)


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


def _submit_body(
    run_id: uuid.UUID,
    server_id: uuid.UUID,
    *specs: tuple[str, str],
    model_profile: str = "build-agent",
    mode: str = "delegated",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One submit body: ``(capability_id, marker)`` per step."""
    body: dict[str, Any] = {
        "run_id": str(run_id),
        "server_id": str(server_id),
        "model_profile": model_profile,
        "mode": mode,
        "steps": [
            {"capability_id": capability, "payload": {"marker": marker}}
            for capability, marker in specs
        ],
    }
    if extra:
        body.update(extra)
    return body


# ---------------------------------------------------------------------------
# fixture database + real worker harness
# ---------------------------------------------------------------------------


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(str(BACKEND_ROOT / "alembic.ini"))


def _drop_app_tables(url: str) -> None:
    """Reset the fixture database to pre-bootstrap state.

    Domain tables AND the engine-owned ``dbos`` schema: an interrupted
    earlier attempt can leave PARKED workflows or ENQUEUED runs that would
    otherwise occupy a fresh worker's queue slots.
    """
    with psycopg.connect(url) as conn:
        conn.execute(
            "DROP TABLE IF EXISTS objective_journal, objective_checklists, "
            "objective_events, objective_runs, objective_servers, "
            "objectives, "
            "job_resource_locks, job_schedule_fires, "
            "job_schedules, job_events, job_steps, job_tags, job_runs, "
            "approvals, profile_projection_journal, profile_projection, "
            "admin_sessions, admin_credential, alembic_version, "
            "notification_alerts, observation_findings, server_observations, "
            "knowledge_doc_chunks, knowledge_documents, "
            "application_projection_journal, application_projection, "
            "conversation_steering, conversation_runs, conversation_tags, "
            "conversation_messages, conversations, "
            "agent_task_completions, agent_task_revisions, agent_task_evidence, "
            "agent_task_criteria, agent_task_constraints, agent_task_steps, "
            "agent_tasks, tags"
        )
        conn.execute("DROP SEQUENCE IF EXISTS server_observations_seq")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


def _truncate(url: str) -> None:
    """Per-test isolation: fresh auth + fresh jobs domain (engine untouched)."""
    with psycopg.connect(url) as conn:
        present = conn.execute(
            "SELECT to_regclass('public.job_resource_locks') IS NOT NULL"
        ).fetchone()
        tables = [
            "objective_journal",
            "objective_checklists",
            "objective_events",
            "objective_runs",
            "objective_servers",
            "objectives",
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
            f"{LIVE_URL_VAR} is not set; the jobs HTTP surface claims need a "
            "real disposable Postgres fixture"
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
    HTTP surface can enqueue exactly like the real API process does
    beside a running worker.
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
async def jobs_app(bootstrapped_url: str, tmp_path: Path):
    """App on the real fixture DB with a fresh confined data root."""
    _truncate(bootstrapped_url)
    app = _build_app(bootstrapped_url, tmp_path / "data-root")
    async with _running(app):
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()


@pytest.fixture
async def jar(jobs_app: FastAPI) -> dict[str, str]:
    return await login_session(jobs_app)


@pytest.fixture
async def server_a(jobs_app: FastAPI, jar: dict[str, str]) -> uuid.UUID:
    return await create_profile(jobs_app, jar, "fixture-a")


@pytest.fixture
def scenario(tmp_path: Path) -> Path:
    """Fresh scenario namespace: handler module + journal + policy files."""
    root = tmp_path / "scenario"
    (root / "handlers").mkdir(parents=True)
    (root / "gates").mkdir(parents=True)
    (root / "handlers" / f"{HANDLER_MODULE_NAME}.py").write_text(
        HANDLER_MODULE_SOURCE, encoding="utf-8"
    )
    _journal_path(root).touch()
    _write_policy_files(root, grants=[{READ_CAP: True, MUTATE_CAP: True}])
    return root


def _journal_path(scenario: Path) -> Path:
    return scenario / "journal.log"


def _journal_lines(scenario: Path) -> list[str]:
    try:
        return _journal_path(scenario).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _journal_count(scenario: Path, marker: str) -> int:
    return _journal_lines(scenario).count(marker)


def _wait_until(predicate, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _write_policy_files(
    scenario: Path,
    *,
    grants: list[dict],
    server: uuid.UUID = SERVER_A,
    denied: list[str] | None = None,
) -> None:
    """Write the worker-side policy files for ONE target server scope.

    ``server`` must be the MINTED canonical server id the runs actually
    target (the servers API mints identities server-side, C01 rule 1) —
    the dispatcher's grant scope check runs against exactly this set.
    """
    document = [
        {"capability_id": capability, "server_ids": [str(server)] if present else []}
        for entry in grants
        for capability, present in entry.items()
    ]
    (scenario / "grants.json").write_text(json.dumps(document), encoding="utf-8")
    (scenario / "policy.json").write_text(
        json.dumps({"revision": 1, "denied": denied or []}), encoding="utf-8"
    )


def _spawn_worker(
    url: str, handler_dir: Path, scenario: Path, tag: str, *, concurrency: int = 2
) -> subprocess.Popen:
    """Launch a REAL worker process against the fixture database."""
    env = dict(os.environ)
    env.pop(LIVE_URL_VAR, None)  # workers speak the production env contract
    env["MAI_DATABASE_URL"] = url
    env["MAI_WORKER_HANDLERS"] = HANDLER_MODULE_NAME
    env["MAI_FX_JOURNAL"] = str(_journal_path(scenario))
    env["MAI_FX_GATE_DIR"] = str(scenario / "gates")
    env["MAI_FX_GRANTS"] = str(scenario / "grants.json")
    env["MAI_FX_POLICY"] = str(scenario / "policy.json")
    behavior_path = scenario / "behavior.json"
    if behavior_path.exists():
        env["MAI_FX_BEHAVIOR"] = behavior_path.read_text(encoding="utf-8")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(handler_dir), *(p for p in [env.get("PYTHONPATH")] if p)]
    )
    log = open(scenario / f"{tag}.log", "w")  # noqa: SIM115 - closed with proc
    return subprocess.Popen(
        [sys.executable, "-m", "mai.jobs.worker", "--concurrency", str(concurrency)],
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
# submit: idempotent durable intent (C05 rule 5) + boundary
# ---------------------------------------------------------------------------


class TestSubmit:
    async def test_submit_creates_run_intent_and_enqueues(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run_id = uuid.uuid4()
        response = await call_app(
            jobs_app,
            "POST",
            "/api/v1/jobs/runs",
            json_body=_submit_body(run_id, server_a, (READ_CAP, "m1")),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert response.status == 201, response.body
        body = response.json()
        assert body["deduplicated"] is False
        run = body["run"]
        assert run["run_id"] == str(run_id)
        assert run["server_id"] == str(server_a)
        assert run["depth"] == 0
        assert run["parent_run_id"] is None
        assert run["profile_revision"] == 1  # bound from the canonical store
        assert run["prompt_revision"] == 7  # bound from the canonical store
        assert run["model_profile"] == "build-agent"
        assert run["requested_grants"] == [READ_CAP]
        assert run["state"] == "queued"  # the frozen submit edge
        assert run["cancel_requested_at"] is None

        def check() -> None:
            with psycopg.connect(
                os.environ[LIVE_URL_VAR], row_factory=dict_row
            ) as conn:
                row = conn.execute(
                    "SELECT server_id::text AS server_id, depth, "
                    "profile_revision, prompt_revision, model_profile, "
                    "requested_grants FROM job_runs WHERE run_id = %s",
                    (run_id,),
                ).fetchone()
                assert row is not None
                assert row["server_id"] == str(server_a)
                assert row["depth"] == 0
                assert row["profile_revision"] == 1
                assert row["prompt_revision"] == 7
                assert row["model_profile"] == "build-agent"
                assert row["requested_grants"] == [READ_CAP]
                events = conn.execute(
                    "SELECT seq, type, data FROM job_events "
                    "WHERE run_id = %s ORDER BY seq",
                    (run_id,),
                ).fetchall()
                assert [e["type"] for e in events] == [
                    "run.submitted",
                    "run.transition",
                ]
                assert events[0]["data"]["payload_sha256"]
                assert events[1]["data"] == {"from": None, "to": "queued"}

        check()

        # The engine holds the workflow under the SAME id (deterministic
        # workflow ID, C05 rule 5) in the non-terminal mapped state.
        with EngineClient(os.environ[LIVE_URL_VAR]) as client:
            assert client.run_state(str(run_id)) is not None

    async def test_duplicate_submit_attaches_without_second_intent(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run_id = uuid.uuid4()
        body = _submit_body(run_id, server_a, (READ_CAP, "m1"))
        first = await call_app(
            jobs_app,
            "POST",
            "/api/v1/jobs/runs",
            json_body=body,
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert first.status == 201, first.body
        second = await call_app(
            jobs_app,
            "POST",
            "/api/v1/jobs/runs",
            json_body=body,
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert second.status == 200, second.body
        payload = second.json()
        assert payload["deduplicated"] is True
        assert payload["run"]["run_id"] == str(run_id)
        with psycopg.connect(os.environ[LIVE_URL_VAR]) as conn:
            runs = conn.execute(
                "SELECT COUNT(*) FROM job_runs WHERE run_id = %s", (run_id,)
            ).fetchone()[0]
            submitted = conn.execute(
                "SELECT COUNT(*) FROM job_events WHERE run_id = %s "
                "AND type = 'run.submitted'",
                (run_id,),
            ).fetchone()[0]
            transitions = conn.execute(
                "SELECT COUNT(*) FROM job_events WHERE run_id = %s "
                "AND type = 'run.transition'",
                (run_id,),
            ).fetchone()[0]
        assert (runs, submitted, transitions) == (1, 1, 1)

    async def test_duplicate_submit_different_intent_conflicts(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run_id = uuid.uuid4()
        first = await call_app(
            jobs_app,
            "POST",
            "/api/v1/jobs/runs",
            json_body=_submit_body(run_id, server_a, (READ_CAP, "m1")),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert first.status == 201, first.body
        second = await call_app(
            jobs_app,
            "POST",
            "/api/v1/jobs/runs",
            json_body=_submit_body(run_id, server_a, (MUTATE_CAP, "other")),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert second.status == 409, second.body
        assert _error(second)["code"] == "conflict"
        with psycopg.connect(os.environ[LIVE_URL_VAR]) as conn:
            runs = conn.execute(
                "SELECT COUNT(*) FROM job_runs WHERE run_id = %s", (run_id,)
            ).fetchone()[0]
            events = conn.execute(
                "SELECT COUNT(*) FROM job_events WHERE run_id = %s", (run_id,)
            ).fetchone()[0]
        assert (runs, events) == (1, 2)  # a new intent never rewrites the run

    async def test_submit_unknown_target_is_not_found(
        self, jobs_app: FastAPI, jar: dict[str, str]
    ) -> None:
        response = await call_app(
            jobs_app,
            "POST",
            "/api/v1/jobs/runs",
            json_body=_submit_body(uuid.uuid4(), SERVER_B, (READ_CAP, "m1")),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert response.status == 404, response.body
        assert _error(response)["code"] == "not_found"

    async def test_submit_rejects_unknown_body_fields(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        response = await call_app(
            jobs_app,
            "POST",
            "/api/v1/jobs/runs",
            json_body=_submit_body(
                uuid.uuid4(), server_a, (READ_CAP, "m1"), extra={"admin": True}
            ),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert response.status == 422, response.body
        error = _error(response)
        assert error["code"] == "validation_failed"
        assert any(key.endswith("admin") for key in error["fields"])

    async def test_submit_rejects_invalid_capability_slug(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        response = await call_app(
            jobs_app,
            "POST",
            "/api/v1/jobs/runs",
            json_body=_submit_body(uuid.uuid4(), server_a, ("BAD CAP", "m1")),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert response.status == 422, response.body
        assert _error(response)["code"] == "validation_failed"

    async def test_submit_boundary_session_origin_csrf(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        body = _submit_body(uuid.uuid4(), server_a, (READ_CAP, "m1"))
        unauthenticated = await call_app(
            jobs_app, "POST", "/api/v1/jobs/runs", json_body=body
        )
        assert unauthenticated.status == 401
        assert _error(unauthenticated)["code"] == "unauthenticated"
        evil = await call_app(
            jobs_app,
            "POST",
            "/api/v1/jobs/runs",
            json_body=body,
            headers={"origin": EVIL_ORIGIN, "x-csrf-token": jar[CSRF_COOKIE]},
            cookies=jar,
        )
        assert evil.status == 403
        assert _error(evil)["code"] == "origin_rejected"
        no_csrf = await call_app(
            jobs_app,
            "POST",
            "/api/v1/jobs/runs",
            json_body=body,
            headers={"origin": ORIGIN},
            cookies=jar,
        )
        assert no_csrf.status == 403
        assert _error(no_csrf)["code"] == "csrf_rejected"
        with psycopg.connect(os.environ[LIVE_URL_VAR]) as conn:
            count = conn.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0]
        assert count == 0  # no rejected call left intent behind


# ---------------------------------------------------------------------------
# list + get: bounded, explicitly server-scoped reads (C01 rules 5/7)
# ---------------------------------------------------------------------------


class TestListAndGet:
    async def _seed(
        self, jobs_app: FastAPI, jar: dict[str, str], server: uuid.UUID, count: int
    ) -> list[uuid.UUID]:
        run_ids = []
        for index in range(count):
            run_id = uuid.uuid4()
            response = await call_app(
                jobs_app,
                "POST",
                "/api/v1/jobs/runs",
                json_body=_submit_body(run_id, server, (READ_CAP, f"m{index}")),
                headers=_mutation_headers(jar),
                cookies=jar,
            )
            assert response.status == 201, response.body
            run_ids.append(run_id)
            time.sleep(0.02)  # distinct submitted_at for deterministic order
        return run_ids

    async def test_list_requires_explicit_server_scope(
        self, jobs_app: FastAPI, jar: dict[str, str]
    ) -> None:
        response = await call_app(jobs_app, "GET", "/api/v1/jobs/runs", cookies=jar)
        assert response.status == 422, response.body
        assert _error(response)["code"] == "validation_failed"

    async def test_list_is_scoped_bounded_and_paginated(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        await self._seed(jobs_app, jar, server_a, 3)
        # A second server's runs never leak into A's scope (C01 rule 7).
        other = await create_profile(jobs_app, jar, "fixture-other")
        await self._seed(jobs_app, jar, other, 1)
        page = await call_app(
            jobs_app,
            "GET",
            f"/api/v1/jobs/runs?server_id={server_a}",
            cookies=jar,
        )
        assert page.status == 200, page.body
        body = page.json()
        assert body["total"] == 3
        assert len(body["runs"]) == 3
        assert all(run["server_id"] == str(server_a) for run in body["runs"])
        assert all(run["state"] == "queued" for run in body["runs"])
        bounded = await call_app(
            jobs_app,
            "GET",
            f"/api/v1/jobs/runs?server_id={server_a}&limit=2",
            cookies=jar,
        )
        bounded_body = bounded.json()
        assert bounded.status == 200
        assert len(bounded_body["runs"]) == 2
        assert bounded_body["total"] == 3
        shifted = await call_app(
            jobs_app,
            "GET",
            f"/api/v1/jobs/runs?server_id={server_a}&limit=2&offset=2",
            cookies=jar,
        )
        shifted_body = shifted.json()
        assert shifted.status == 200
        assert len(shifted_body["runs"]) == 1
        assert shifted_body["runs"][0]["run_id"] not in [
            run["run_id"] for run in bounded_body["runs"]
        ]
        # List never carries step rows; that is the detail view's job.
        assert "steps" not in bounded_body["runs"][0]

    async def test_list_rejects_out_of_bounds_pagination(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        for query in ("limit=0", "limit=201", "offset=-1"):
            response = await call_app(
                jobs_app,
                "GET",
                f"/api/v1/jobs/runs?server_id={server_a}&{query}",
                cookies=jar,
            )
            assert response.status == 422, (query, response.body)

    async def test_get_run_returns_binding_state_and_steps(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run_id = uuid.uuid4()
        created = await call_app(
            jobs_app,
            "POST",
            "/api/v1/jobs/runs",
            json_body=_submit_body(run_id, server_a, (READ_CAP, "m1")),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert created.status == 201
        detail = await call_app(
            jobs_app, "GET", f"/api/v1/jobs/runs/{run_id}", cookies=jar
        )
        assert detail.status == 200, detail.body
        body = detail.json()
        assert body["run_id"] == str(run_id)
        assert body["state"] == "queued"
        assert body["steps"] == []  # nothing executed yet: no step rows
        unknown = await call_app(
            jobs_app, "GET", f"/api/v1/jobs/runs/{uuid.uuid4()}", cookies=jar
        )
        assert unknown.status == 404
        assert _error(unknown)["code"] == "not_found"

    async def test_reads_require_session(
        self, jobs_app: FastAPI, server_a: uuid.UUID
    ) -> None:
        listed = await call_app(
            jobs_app, "GET", f"/api/v1/jobs/runs?server_id={server_a}"
        )
        assert listed.status == 401
        read = await call_app(jobs_app, "GET", f"/api/v1/jobs/runs/{uuid.uuid4()}")
        assert read.status == 401

    async def test_drifted_transition_log_fails_closed_value_free(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        """A tampered log is surfaced as recovery-needing, never a 500."""
        run_id = uuid.uuid4()
        created = await call_app(
            jobs_app,
            "POST",
            "/api/v1/jobs/runs",
            json_body=_submit_body(run_id, server_a, (READ_CAP, "m1")),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert created.status == 201
        with psycopg.connect(os.environ[LIVE_URL_VAR]) as conn:
            # Break the submit anchor: replay requires the first transition
            # to anchor from None (a value-free integrity violation).
            conn.execute(
                'UPDATE job_events SET data = \'{"from": "queued", '
                '"to": "failed"}\' '
                "WHERE run_id = %s AND type = 'run.transition'",
                (run_id,),
            )
            conn.commit()
        response = await call_app(
            jobs_app, "GET", f"/api/v1/jobs/runs/{run_id}", cookies=jar
        )
        assert response.status == 409, response.body
        error = _error(response)
        assert error["code"] == "conflict"
        assert "requires recovery" in error["message"]
        # The list replay fails closed the same way for the same run.
        listed = await call_app(
            jobs_app,
            "GET",
            f"/api/v1/jobs/runs?server_id={server_a}",
            cookies=jar,
        )
        assert listed.status == 409
        assert _error(listed)["code"] == "conflict"


# ---------------------------------------------------------------------------
# bounded tag previews (HT-W6A)
# ---------------------------------------------------------------------------


def _attach_job_tags(url: str, run_id: uuid.UUID, count: int) -> list[str]:
    """Attach ``count`` uniquely-named tags to one run through direct SQL.

    The reserved ``unclassified`` seed rides along from submit, so the
    honest full name set is the sorted ``count + 1`` names (hex prefixes
    sort before ``"unclassified"``, matching the store's name order).
    """
    prefix = uuid.uuid4().hex[:8]
    names = [f"{prefix}-{index}" for index in range(count)]
    with psycopg.connect(url) as conn:
        for name in names:
            tag_id = uuid.uuid4()
            conn.execute(
                "INSERT INTO tags (tag_id, name, description) VALUES (%s, %s, %s)",
                (tag_id, name, "HT-W6A fixture tag"),
            )
            conn.execute(
                "INSERT INTO job_tags (job_id, tag_id, assigned_by, "
                "assignment_source) VALUES (%s, %s, 'fixture', 'system')",
                (run_id, tag_id),
            )
        conn.commit()
    return sorted([*names, "unclassified"])


class TestTagPreviews:
    async def _submit(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> uuid.UUID:
        run_id = uuid.uuid4()
        response = await call_app(
            jobs_app,
            "POST",
            "/api/v1/jobs/runs",
            json_body=_submit_body(run_id, server_a, (READ_CAP, "m1")),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert response.status == 201, response.body
        return run_id

    async def test_submit_preview_is_the_reserved_classification(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run_id = await self._submit(jobs_app, jar, server_a)
        listed = await call_app(
            jobs_app,
            "GET",
            f"/api/v1/jobs/runs?server_id={server_a}",
            cookies=jar,
        )
        assert listed.status == 200, listed.body
        row = listed.json()["runs"][0]
        assert row["run_id"] == str(run_id)
        assert row["tag_preview"] == {"preview": ["unclassified"], "total": 1}

    async def test_list_run_tag_preview_is_bounded_and_honest(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run_id = await self._submit(jobs_app, jar, server_a)
        all_names = _attach_job_tags(os.environ[LIVE_URL_VAR], run_id, count=4)
        page = await call_app(
            jobs_app,
            "GET",
            f"/api/v1/jobs/runs?server_id={server_a}",
            cookies=jar,
        )
        assert page.status == 200, page.body
        row = next(
            run
            for run in page.json()["runs"]
            if run["run_id"] == str(run_id)
        )
        assert set(row["tag_preview"]) == {"preview", "total"}
        # Exactly the first 3 names in deterministic name order...
        assert row["tag_preview"]["preview"] == all_names[:3]
        assert len(row["tag_preview"]["preview"]) == 3
        # ...plus the honest total (4 assigned + the reserved seed).
        assert row["tag_preview"]["total"] == len(all_names)

    async def test_get_run_tag_preview_is_bounded_and_honest(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run_id = await self._submit(jobs_app, jar, server_a)
        all_names = _attach_job_tags(os.environ[LIVE_URL_VAR], run_id, count=4)
        detail = await call_app(
            jobs_app, "GET", f"/api/v1/jobs/runs/{run_id}", cookies=jar
        )
        assert detail.status == 200, detail.body
        body = detail.json()
        assert body["tag_preview"]["preview"] == all_names[:3]
        assert len(body["tag_preview"]["preview"]) == 3
        assert body["tag_preview"]["total"] == len(all_names)

    async def test_list_empty_page_stays_honest_without_preview_rows(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        """An offset beyond ``total`` is a clean empty page: no preview
        rows, no DB errors, and the envelope stays honest."""
        page = await call_app(
            jobs_app,
            "GET",
            f"/api/v1/jobs/runs?server_id={server_a}&offset=50",
            cookies=jar,
        )
        assert page.status == 200, page.body
        body = page.json()
        assert body["runs"] == []
        assert body["total"] == 0

    async def test_get_run_resolves_the_actual_mandatory_preview(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        """The singleton endpoint converts the REAL store preview: a
        freshly submitted run carries exactly the reserved unclassified
        seed (never a fabricated empty projection)."""
        run_id = await self._submit(jobs_app, jar, server_a)
        detail = await call_app(
            jobs_app, "GET", f"/api/v1/jobs/runs/{run_id}", cookies=jar
        )
        assert detail.status == 200, detail.body
        assert detail.json()["tag_preview"] == {
            "preview": ["unclassified"],
            "total": 1,
        }

    def test_missing_mandatory_preview_is_never_an_empty_projection(
        self,
    ) -> None:
        """A successfully scoped read without its mandatory preview is a
        bug: the projection helper raises instead of converting ``None``
        to ``{preview: [], total: 0}``."""
        with pytest.raises(RuntimeError, match="mandatory tag preview"):
            jobs_api._preview_view(None)


# ---------------------------------------------------------------------------
# run-tag routes + list tag filters (HT-W6B)
# ---------------------------------------------------------------------------


async def _create_catalog_tag(
    jobs_app: FastAPI, jar: dict[str, str], name: str
) -> uuid.UUID:
    """Create one catalog tag through the real HTTP surface."""
    response = await call_app(
        jobs_app,
        "POST",
        "/api/v1/tags",
        json_body={"name": name},
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert response.status == 201, response.body
    return uuid.UUID(response.json()["tag_id"])


async def _assign_run_tag(
    jobs_app: FastAPI,
    jar: dict[str, str],
    run_id: uuid.UUID,
    tag_id: uuid.UUID,
    server_id: uuid.UUID,
) -> HttpResponse:
    """POST one run-tag assignment through the real HTTP surface."""
    return await call_app(
        jobs_app,
        "POST",
        f"/api/v1/jobs/runs/{run_id}/tags",
        json_body={"tag_id": str(tag_id), "server_id": str(server_id)},
        headers=_mutation_headers(jar),
        cookies=jar,
    )


async def _submit_run_intent(
    jobs_app: FastAPI, jar: dict[str, str], server_id: uuid.UUID
) -> uuid.UUID:
    """One durable run intent through the real submit seam (stays QUEUED)."""
    run_id = uuid.uuid4()
    response = await call_app(
        jobs_app,
        "POST",
        "/api/v1/jobs/runs",
        json_body=_submit_body(run_id, server_id, (READ_CAP, "m1")),
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert response.status == 201, response.body
    return run_id


class TestRunTagRoutes:
    async def test_add_list_remove_cycle_records_the_user_actor(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run_id = await _submit_run_intent(jobs_app, jar, server_a)
        tag_id = await _create_catalog_tag(jobs_app, jar, "hotfix")
        path = f"/api/v1/jobs/runs/{run_id}/tags"

        assigned = await _assign_run_tag(jobs_app, jar, run_id, tag_id, server_a)
        assert assigned.status == 204
        assert assigned.body == b""

        listed = await call_app(
            jobs_app, "GET", f"{path}?server_id={server_a}", cookies=jar
        )
        assert listed.status == 200, listed.body
        body = listed.json()
        assert body["run_id"] == str(run_id)
        assert body["total"] == 2  # the reserved seed + the user assignment
        row = next(item for item in body["items"] if item["tag_id"] == str(tag_id))
        assert row["name"] == "hotfix"
        assert row["assignment_source"] == "user"
        # Bounded non-secret actor: the validated session's token hash.
        assert len(row["assigned_by"]) == 64
        assert all(c in "0123456789abcdef" for c in row["assigned_by"])

        removed = await call_app(
            jobs_app,
            "DELETE",
            f"{path}/{tag_id}?server_id={server_a}",
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert removed.status == 204
        relisted = await call_app(
            jobs_app, "GET", f"{path}?server_id={server_a}", cookies=jar
        )
        assert relisted.json()["total"] == 1

    async def test_assignment_is_idempotent(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run_id = await _submit_run_intent(jobs_app, jar, server_a)
        tag_id = await _create_catalog_tag(jobs_app, jar, "once")
        for _ in range(2):
            assigned = await _assign_run_tag(jobs_app, jar, run_id, tag_id, server_a)
            assert assigned.status == 204
        listed = await call_app(
            jobs_app,
            "GET",
            f"/api/v1/jobs/runs/{run_id}/tags?server_id={server_a}",
            cookies=jar,
        )
        assert listed.json()["total"] == 2

    async def test_unknown_tag_is_404_and_absent_association_is_a_no_op(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run_id = await _submit_run_intent(jobs_app, jar, server_a)
        path = f"/api/v1/jobs/runs/{run_id}/tags"

        unknown = await _assign_run_tag(
            jobs_app, jar, run_id, uuid.uuid4(), server_a
        )
        assert unknown.status == 404
        assert _error(unknown)["code"] == "tag_not_found"

        unassigned = await _create_catalog_tag(jobs_app, jar, "never-bound")
        no_op = await call_app(
            jobs_app,
            "DELETE",
            f"{path}/{unassigned}?server_id={server_a}",
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert no_op.status == 204  # absent association: idempotent no-op

    async def test_scope_blind_404_for_missing_and_foreign(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run_id = await _submit_run_intent(jobs_app, jar, server_a)
        path = f"/api/v1/jobs/runs/{run_id}/tags"

        foreign = await call_app(
            jobs_app, "GET", f"{path}?server_id={SERVER_B}", cookies=jar
        )
        missing = await call_app(
            jobs_app,
            "GET",
            f"/api/v1/jobs/runs/{uuid.uuid4()}/tags?server_id={SERVER_B}",
            cookies=jar,
        )
        assert foreign.status == 404 and missing.status == 404
        # Missing ≡ foreign: the SAME closed envelope, no cross-server leak.
        assert _scope_blind_envelope(foreign) == _scope_blind_envelope(missing)
        assert _scope_blind_envelope(foreign)["code"] == "not_found"

        tag_id = await _create_catalog_tag(jobs_app, jar, "foreign")
        foreign_assign = await _assign_run_tag(
            jobs_app, jar, run_id, tag_id, SERVER_B
        )
        assert foreign_assign.status == 404
        foreign_delete = await call_app(
            jobs_app,
            "DELETE",
            f"{path}/{tag_id}?server_id={SERVER_B}",
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert foreign_delete.status == 404

    async def test_zero_uuid_is_not_a_job_scope(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run_id = await _submit_run_intent(jobs_app, jar, server_a)
        zero = uuid.UUID(int=0)
        response = await call_app(
            jobs_app,
            "GET",
            f"/api/v1/jobs/runs/{run_id}/tags?server_id={zero}",
            cookies=jar,
        )
        assert response.status == 404
        assert _error(response)["code"] == "not_found"

    async def test_final_tag_removal_requires_replacement(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run_id = await _submit_run_intent(jobs_app, jar, server_a)
        path = f"/api/v1/jobs/runs/{run_id}/tags"
        reserved = uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID)

        refused = await call_app(
            jobs_app,
            "DELETE",
            f"{path}/{reserved}?server_id={server_a}",
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert refused.status == 409
        assert _error(refused)["code"] == "last_tag"

        replacement = await _create_catalog_tag(jobs_app, jar, "completed")
        swapped = await call_app(
            jobs_app,
            "DELETE",
            f"{path}/{reserved}?server_id={server_a}"
            f"&replacement_tag_id={replacement}",
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert swapped.status == 204
        listed = await call_app(
            jobs_app, "GET", f"{path}?server_id={server_a}", cookies=jar
        )
        body = listed.json()
        assert body["total"] == 1
        assert body["items"][0]["tag_id"] == str(replacement)

    async def test_routes_require_session(
        self, jobs_app: FastAPI, server_a: uuid.UUID
    ) -> None:
        response = await call_app(
            jobs_app,
            "GET",
            f"/api/v1/jobs/runs/{uuid.uuid4()}/tags?server_id={server_a}",
        )
        assert response.status == 401
        assert _error(response)["code"] == "unauthenticated"


def _scope_blind_envelope(response: HttpResponse) -> dict[str, str]:
    """The comparable scope-blind envelope: code + message, no request_id."""
    body = _error(response)
    return {"code": body["code"], "message": body["message"]}


# ---------------------------------------------------------------------------
# list filters: q text search + date range, applied BEFORE pagination (W1)
# ---------------------------------------------------------------------------


class TestListFilters:
    async def _submit(
        self,
        jobs_app: FastAPI,
        jar: dict[str, str],
        server: uuid.UUID,
        *,
        steps: list[dict[str, Any]],
        model_profile: str = "build-agent",
    ) -> uuid.UUID:
        run_id = uuid.uuid4()
        response = await call_app(
            jobs_app,
            "POST",
            "/api/v1/jobs/runs",
            json_body={
                "run_id": str(run_id),
                "server_id": str(server),
                "model_profile": model_profile,
                "mode": "delegated",
                "steps": steps,
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert response.status == 201, response.body
        time.sleep(0.02)  # distinct submitted_at for deterministic order
        return run_id

    async def _list(self, jobs_app: FastAPI, jar: dict[str, str], query: str):
        response = await call_app(
            jobs_app,
            "GET",
            f"/api/v1/jobs/runs?{query}",
            cookies=jar,
        )
        assert response.status == 200, response.body
        return response.json()

    async def test_label_falls_back_to_first_capability(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run_id = await self._submit(
            jobs_app,
            jar,
            server_a,
            steps=[
                {"capability_id": READ_CAP, "payload": {"marker": "m1"}},
                {"capability_id": MUTATE_CAP, "payload": {"marker": "m2"}},
            ],
        )
        page = await self._list(jobs_app, jar, f"server_id={server_a}")
        labels = {run["run_id"]: run["label"] for run in page["runs"]}
        assert labels[str(run_id)] == READ_CAP

    async def test_label_from_payload_and_q_searches_labels(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        labeled = await self._submit(
            jobs_app,
            jar,
            server_a,
            steps=[
                {"capability_id": READ_CAP, "payload": {"label": "Deploy API"}},
            ],
        )
        plain = await self._submit(
            jobs_app,
            jar,
            server_a,
            steps=[{"capability_id": MUTATE_CAP, "payload": {"marker": "m1"}}],
        )
        page = await self._list(jobs_app, jar, f"server_id={server_a}&q=deploy api")
        assert page["total"] == 1  # filter runs BEFORE the LIMIT/OFFSET slice
        assert [str(labeled)] == [run["run_id"] for run in page["runs"]]
        assert page["runs"][0]["label"] == "Deploy API"
        other = await self._list(jobs_app, jar, f"server_id={server_a}&q=fixture")
        assert other["total"] == 1
        assert [str(plain)] == [run["run_id"] for run in other["runs"]]

    async def test_q_matches_model_profile_and_run_id(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run_id = await self._submit(
            jobs_app,
            jar,
            server_a,
            steps=[{"capability_id": READ_CAP, "payload": {"marker": "m1"}}],
            model_profile="probe-model",
        )
        by_profile = await self._list(
            jobs_app, jar, f"server_id={server_a}&q=probe-model"
        )
        assert by_profile["total"] == 1
        assert [str(run_id)] == [run["run_id"] for run in by_profile["runs"]]
        by_id = await self._list(
            jobs_app, jar, f"server_id={server_a}&q={str(run_id)[:8]}"
        )
        assert by_id["total"] == 1
        assert [str(run_id)] == [run["run_id"] for run in by_id["runs"]]

    async def test_date_range_filters_submitted_at(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        await self._submit(
            jobs_app,
            jar,
            server_a,
            steps=[{"capability_id": READ_CAP, "payload": {"marker": "m1"}}],
        )
        future_only = await self._list(
            jobs_app,
            jar,
            f"server_id={server_a}&date_from=2099-01-01T00:00:00Z",
        )
        assert future_only["total"] == 0
        assert future_only["runs"] == []
        past_only = await self._list(
            jobs_app,
            jar,
            f"server_id={server_a}&date_to=1990-01-01T00:00:00Z",
        )
        assert past_only["total"] == 0
        spanning = await self._list(
            jobs_app,
            jar,
            f"server_id={server_a}"
            "&date_from=2000-01-01T00:00:00Z&date_to=2099-01-01T00:00:00Z",
        )
        assert spanning["total"] == 1
        invalid = await call_app(
            jobs_app,
            "GET",
            f"/api/v1/jobs/runs?server_id={server_a}&date_from=not-a-date",
            cookies=jar,
        )
        assert invalid.status == 422, invalid.body

    async def test_filters_apply_before_pagination(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        first = await self._submit(
            jobs_app,
            jar,
            server_a,
            steps=[{"capability_id": READ_CAP, "payload": {"label": "alpha one"}}],
        )
        await self._submit(
            jobs_app,
            jar,
            server_a,
            steps=[{"capability_id": READ_CAP, "payload": {"label": "alpha two"}}],
        )
        await self._submit(
            jobs_app,
            jar,
            server_a,
            steps=[{"capability_id": READ_CAP, "payload": {"label": "beta"}}],
        )
        page = await self._list(
            jobs_app, jar, f"server_id={server_a}&q=alpha&limit=1&offset=1"
        )
        assert page["total"] == 2  # the FILTERED total, not the scope total (3)
        assert page["limit"] == 1
        assert page["offset"] == 1
        # submitted_at DESC order: offset 1 lands on the OLDER alpha run.
        assert [str(first)] == [run["run_id"] for run in page["runs"]]

    async def _assign(
        self,
        jobs_app: FastAPI,
        jar: dict[str, str],
        run_id: uuid.UUID,
        tag_id: uuid.UUID,
        server: uuid.UUID,
    ) -> None:
        assigned = await _assign_run_tag(jobs_app, jar, run_id, tag_id, server)
        assert assigned.status == 204, assigned.body

    async def test_tag_filter_any_and_all(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        tagged = await self._submit(
            jobs_app, jar, server_a, steps=[{"capability_id": READ_CAP, "payload": {"marker": "m1"}}]
        )
        other = await self._submit(
            jobs_app, jar, server_a, steps=[{"capability_id": READ_CAP, "payload": {"marker": "m2"}}]
        )
        tag_a = await _create_catalog_tag(jobs_app, jar, "filter-alpha")
        tag_b = await _create_catalog_tag(jobs_app, jar, "filter-beta")
        tag_c = await _create_catalog_tag(jobs_app, jar, "filter-gamma")
        await self._assign(jobs_app, jar, tagged, tag_a, server_a)
        await self._assign(jobs_app, jar, tagged, tag_b, server_a)
        await self._assign(jobs_app, jar, other, tag_c, server_a)

        # ANY: at least one of the set.
        single = await self._list(
            jobs_app, jar, f"server_id={server_a}&tag_id={tag_a}"
        )
        assert single["total"] == 1
        assert [str(tagged)] == [run["run_id"] for run in single["runs"]]
        spread = await self._list(
            jobs_app,
            jar,
            f"server_id={server_a}&tag_id={tag_a}&tag_id={tag_c}&tag_match=any",
        )
        assert spread["total"] == 2

        # ALL: every id of the set must be present.
        both = await self._list(
            jobs_app,
            jar,
            f"server_id={server_a}&tag_id={tag_a}&tag_id={tag_b}&tag_match=all",
        )
        assert both["total"] == 1
        assert [str(tagged)] == [run["run_id"] for run in both["runs"]]
        impossible = await self._list(
            jobs_app,
            jar,
            f"server_id={server_a}&tag_id={tag_a}&tag_id={tag_c}&tag_match=all",
        )
        assert impossible["total"] == 0
        assert impossible["runs"] == []

    async def test_tag_filter_omitted_preserves_behavior(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run_id = await self._submit(
            jobs_app, jar, server_a, steps=[{"capability_id": READ_CAP, "payload": {"marker": "m1"}}]
        )
        plain = await self._list(jobs_app, jar, f"server_id={server_a}")
        match_default = await self._list(
            jobs_app, jar, f"server_id={server_a}&tag_match=all"
        )
        assert match_default == plain  # no tag_id: match mode is inert
        assert plain["total"] == 1
        assert [str(run_id)] == [run["run_id"] for run in plain["runs"]]

    async def test_tag_filter_applies_before_pagination(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        first = await self._submit(
            jobs_app, jar, server_a, steps=[{"capability_id": READ_CAP, "payload": {"marker": "m1"}}]
        )
        second = await self._submit(
            jobs_app, jar, server_a, steps=[{"capability_id": READ_CAP, "payload": {"marker": "m2"}}]
        )
        await self._submit(
            jobs_app, jar, server_a, steps=[{"capability_id": READ_CAP, "payload": {"marker": "m3"}}]
        )
        tag = await _create_catalog_tag(jobs_app, jar, "page-filter")
        await self._assign(jobs_app, jar, first, tag, server_a)
        await self._assign(jobs_app, jar, second, tag, server_a)

        page = await self._list(
            jobs_app,
            jar,
            f"server_id={server_a}&tag_id={tag}&limit=1&offset=1",
        )
        # The FILTERED total (2), not the scope total (3); submitted_at DESC
        # order puts the OLDER matching run at offset 1.
        assert page["total"] == 2
        assert page["limit"] == 1 and page["offset"] == 1
        assert [str(first)] == [run["run_id"] for run in page["runs"]]

    async def test_tag_filter_validates_malformed_and_oversized_sets(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        malformed = await call_app(
            jobs_app,
            "GET",
            f"/api/v1/jobs/runs?server_id={server_a}&tag_id=not-a-uuid",
            cookies=jar,
        )
        assert malformed.status == 422, malformed.body
        assert _error(malformed)["code"] == "validation_failed"

        bad_match = await call_app(
            jobs_app,
            "GET",
            f"/api/v1/jobs/runs?server_id={server_a}&tag_match=some",
            cookies=jar,
        )
        assert bad_match.status == 422

        fifty = "&".join(f"tag_id={uuid.uuid4()}" for _ in range(50))
        boundary = await call_app(
            jobs_app,
            "GET",
            f"/api/v1/jobs/runs?server_id={server_a}&{fifty}",
            cookies=jar,
        )
        assert boundary.status == 200, boundary.body  # the documented bound
        assert boundary.json()["total"] == 0

        fifty_one = "&".join(f"tag_id={uuid.uuid4()}" for _ in range(51))
        oversized = await call_app(
            jobs_app,
            "GET",
            f"/api/v1/jobs/runs?server_id={server_a}&{fifty_one}",
            cookies=jar,
        )
        assert oversized.status == 422, oversized.body

    async def test_tag_filter_is_same_server_only(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run_a = await self._submit(
            jobs_app, jar, server_a, steps=[{"capability_id": READ_CAP, "payload": {"marker": "m1"}}]
        )
        tag = await _create_catalog_tag(jobs_app, jar, "scoped-tag")
        await self._assign(jobs_app, jar, run_a, tag, server_a)
        page = await self._list(
            jobs_app, jar, f"server_id={SERVER_B}&tag_id={tag}"
        )
        # Other servers' runs are invisible; the tag itself never leaks rows.
        assert page["total"] == 0
        assert page["runs"] == []


# ---------------------------------------------------------------------------
# live scenario: stream replay + disconnect + cancel (JOB-SUBMIT/EVT-REPLAY)
# ---------------------------------------------------------------------------


def _seed_terminal_catalog(url: str) -> None:
    """Seed the terminal catalog tags the worker's transitions swap into.

    A terminal ``record_transition`` atomically swaps the reserved
    ``unclassified`` assignment for a catalog tag named after the target
    state (W5B1); a fresh 0011 catalog only seeds ``unclassified``, so
    the live engine scenarios seed the terminal rows (idempotent).
    """
    with psycopg.connect(url) as conn:
        for state in TERMINAL_RUN_STATES:
            conn.execute(
                "INSERT INTO tags (tag_id, name, description) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (uuid.uuid4(), state.value, "terminal run classification"),
            )
        conn.commit()


class TestLiveScenario:
    async def test_stream_replay_disconnect_and_completion(
        self,
        bootstrapped_url: str,
        jobs_app: FastAPI,
        jar: dict[str, str],
        server_a: uuid.UUID,
        scenario: Path,
        tmp_path: Path,
    ) -> None:
        """JOB-SUBMIT + EVT-REPLAY writer half, on a REAL worker.

        Authenticated HTTP creates the job; the stream delivers the
        durable events; the disconnect leaves the work running; the
        reconnect from the cursor deduplicates; the run completes with a
        verified step receipt in the database.
        """
        # The fixture step parks on a gate file, so the stream observes a
        # run that is genuinely in flight, not already finished.
        (scenario / "behavior.json").write_text(
            json.dumps({f"{READ_CAP}:m1": "gate"}), encoding="utf-8"
        )
        _seed_terminal_catalog(bootstrapped_url)
        # The worker's grant scope must name the MINTED server id.
        _write_policy_files(
            scenario, grants=[{READ_CAP: True, MUTATE_CAP: True}], server=server_a
        )
        run_id = uuid.uuid4()
        response = await call_app(
            jobs_app,
            "POST",
            "/api/v1/jobs/runs",
            json_body=_submit_body(run_id, server_a, (READ_CAP, "m1")),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert response.status == 201, response.body

        worker = _spawn_worker(
            bootstrapped_url, tmp_path / "scenario" / "handlers", scenario, "worker"
        )
        try:
            _wait_until(
                lambda: _journal_count(scenario, f"begin:{READ_CAP}:m1") == 1,
                READY_TIMEOUT,
                "worker to start the gated step",
            )

            stream = await open_stream(
                jobs_app,
                f"/api/v1/jobs/runs/{run_id}/events",
                cookies=jar,
            )
            try:
                await stream.wait_until(
                    lambda body: len(sse_frames(body)) >= 3,
                    RUN_TIMEOUT,
                    "first replay frames (submit edge, queued, step started)",
                )
                frames_before = sse_frames(stream.body)
                assert frames_before, "stream delivered no complete frames"
                cursor = int(frames_before[-1]["id"])
                assert cursor >= 2
                types_before = [f["data"]["type"] for f in frames_before if f["data"]]
                assert "run.transition" in types_before
                assert "step.started" in types_before
                seqs_before = [int(f["id"]) for f in frames_before if f["id"]]
                assert seqs_before == sorted(seqs_before)  # cursor order
            finally:
                await stream.close()  # the browser tab closes; work continues

            # The gated step is STILL waiting: the run survives the
            # disconnect (SERVICES mai-web row: closing a window cancels
            # nothing), and opening the gate completes it offline.
            assert _journal_count(scenario, f"effect:{READ_CAP}:m1") == 0
            gate = scenario / "gates" / f"gate-{READ_CAP}-m1"
            gate.write_text("open", encoding="utf-8")
            _wait_until(
                lambda: _journal_count(scenario, f"effect:{READ_CAP}:m1") == 1,
                RUN_TIMEOUT,
                "the gated effect to complete while no stream is attached",
            )

            # Reconnect FROM THE CURSOR: strictly newer events, no dupes.
            reconnect = await open_stream(
                jobs_app,
                f"/api/v1/jobs/runs/{run_id}/events",
                headers={"last-event-id": str(cursor)},
                cookies=jar,
            )
            try:
                await reconnect.wait_until(
                    lambda _body: reconnect.task.done(),
                    RUN_TIMEOUT,
                    "the terminal-drained stream to close itself",
                )
            finally:
                await reconnect.close()
            replay_frames = sse_frames(reconnect.body)
            seqs_after = [int(f["id"]) for f in replay_frames if f["id"]]
            assert seqs_after, "reconnect delivered no events"
            assert all(seq > cursor for seq in seqs_after)
            assert seqs_after == sorted(set(seqs_after))  # zero duplicates
            replay_types = [f["data"]["type"] for f in replay_frames if f["data"]]
            assert "step.completed" in replay_types
            assert "run.transition" in replay_types
            final_transition = [
                f["data"]
                for f in replay_frames
                if f["data"] and f["data"]["type"] == "run.transition"
            ][-1]
            assert final_transition["data"]["to"] == "succeeded"
        finally:
            _stop(worker)

        # DB/target result verified: the step row holds its safe receipt
        # and the facade replay reports the terminal state.
        def check_db() -> None:
            with psycopg.connect(
                os.environ[LIVE_URL_VAR], row_factory=dict_row
            ) as conn:
                step = conn.execute(
                    "SELECT state, receipt FROM job_steps "
                    "WHERE run_id = %s AND seq = 1",
                    (run_id,),
                ).fetchone()
                assert step is not None
                assert step["state"] == "succeeded"
                assert step["receipt"]["effect"] == "executed"

        await asyncio.get_running_loop().run_in_executor(None, check_db)
        detail = await call_app(
            jobs_app, "GET", f"/api/v1/jobs/runs/{run_id}", cookies=jar
        )
        body = detail.json()
        assert body["state"] == "succeeded"
        assert body["steps"][0]["state"] == "succeeded"
        assert body["steps"][0]["receipt"]["effect"] == "executed"

    async def test_cancel_before_claim_records_and_blocks_execution(
        self,
        bootstrapped_url: str,
        jobs_app: FastAPI,
        jar: dict[str, str],
        server_a: uuid.UUID,
        scenario: Path,
        tmp_path: Path,
    ) -> None:
        """Cancel records the request once; a verified outcome, then silence.

        The engine lands CANCELLED while the run is still queued; a worker
        started afterwards executes nothing; re-entry is idempotent and
        never moves the recorded timestamp.
        """
        run_id = uuid.uuid4()
        # The worker's grant scope must name the MINTED server id.
        _write_policy_files(
            scenario, grants=[{READ_CAP: True, MUTATE_CAP: True}], server=server_a
        )
        _seed_terminal_catalog(bootstrapped_url)
        response = await call_app(
            jobs_app,
            "POST",
            "/api/v1/jobs/runs",
            json_body=_submit_body(run_id, server_a, (READ_CAP, "m1")),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert response.status == 201, response.body

        cancel = await call_app(
            jobs_app,
            "POST",
            f"/api/v1/jobs/runs/{run_id}/cancel",
            json_body={},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert cancel.status == 200, cancel.body
        body = cancel.json()
        assert body["outcome"] == "cancelled"  # KNOWN engine outcome
        assert body["requested_now"] is True
        assert body["requested_at"]

        def check_db() -> dict:
            with psycopg.connect(
                os.environ[LIVE_URL_VAR], row_factory=dict_row
            ) as conn:
                row = conn.execute(
                    "SELECT cancel_requested_at FROM job_runs WHERE run_id = %s",
                    (run_id,),
                ).fetchone()
                assert row is not None
                # The reviewed producer records the request as the EVENT
                # (requested ≠ verified); the column stays NULL — the API
                # view derives the wire timestamp from the event instead.
                assert row["cancel_requested_at"] is None
                events = conn.execute(
                    "SELECT type, data FROM job_events WHERE run_id = %s ORDER BY seq",
                    (run_id,),
                ).fetchall()
                requested = [e for e in events if e["type"] == "run.cancel_requested"]
                assert len(requested) == 1
                transitions = [
                    e["data"]["to"] for e in events if e["type"] == "run.transition"
                ]
                assert transitions == ["queued", "cancelled"]
                return {"cancel_event": requested[0]}

        loop = asyncio.get_running_loop()
        recorded = await loop.run_in_executor(None, check_db)

        again = await call_app(
            jobs_app,
            "POST",
            f"/api/v1/jobs/runs/{run_id}/cancel",
            json_body={},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert again.status == 200, again.body
        body_again = again.json()
        assert body_again["outcome"] == "cancelled"
        assert body_again["requested_now"] is False
        assert body_again["requested_at"] == body["requested_at"]

        detail = await call_app(
            jobs_app, "GET", f"/api/v1/jobs/runs/{run_id}", cookies=jar
        )
        assert detail.json()["state"] == "cancelled"
        # The wire timestamp is derived from the recorded request event.
        assert detail.json()["cancel_requested_at"] == body["requested_at"]

        # A worker started AFTER the verified cancellation executes nothing.
        worker = _spawn_worker(
            bootstrapped_url, tmp_path / "scenario" / "handlers", scenario, "worker"
        )
        try:
            time.sleep(6.0)
            assert _journal_lines(scenario) == []
        finally:
            _stop(worker)
        detail = await call_app(
            jobs_app, "GET", f"/api/v1/jobs/runs/{run_id}", cookies=jar
        )
        assert detail.json()["state"] == "cancelled"  # never resurrected
        with EngineClient(bootstrapped_url) as client:
            assert client.run_state(str(run_id)) is RunState.CANCELLED
        assert recorded["cancel_event"]["data"]["cancel_children"] is False

    async def test_cancel_boundary_and_unknown_run(
        self, jobs_app: FastAPI, jar: dict[str, str]
    ) -> None:
        unknown_id = uuid.uuid4()
        unauthenticated = await call_app(
            jobs_app,
            "POST",
            f"/api/v1/jobs/runs/{unknown_id}/cancel",
            json_body={},
        )
        assert unauthenticated.status == 401
        evil = await call_app(
            jobs_app,
            "POST",
            f"/api/v1/jobs/runs/{unknown_id}/cancel",
            json_body={},
            headers={"origin": EVIL_ORIGIN, "x-csrf-token": jar[CSRF_COOKIE]},
            cookies=jar,
        )
        assert evil.status == 403
        assert _error(evil)["code"] == "origin_rejected"
        no_csrf = await call_app(
            jobs_app,
            "POST",
            f"/api/v1/jobs/runs/{unknown_id}/cancel",
            json_body={},
            headers={"origin": ORIGIN},
            cookies=jar,
        )
        assert no_csrf.status == 403
        assert _error(no_csrf)["code"] == "csrf_rejected"
        unknown = await call_app(
            jobs_app,
            "POST",
            f"/api/v1/jobs/runs/{unknown_id}/cancel",
            json_body={},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert unknown.status == 404
        assert _error(unknown)["code"] == "not_found"
        strict = await call_app(
            jobs_app,
            "POST",
            f"/api/v1/jobs/runs/{uuid.uuid4()}/cancel",
            json_body={"force": True},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert strict.status == 422
        assert _error(strict)["code"] == "validation_failed"


class TestEventStreamBoundary:
    async def test_unauthenticated_subscription_is_401_before_any_stream_byte(
        self, jobs_app: FastAPI, server_a: uuid.UUID, jar: dict[str, str]
    ) -> None:
        """AUTH-S5 transport half: 401 is an envelope, never stream bytes."""
        run_id = uuid.uuid4()
        created = await call_app(
            jobs_app,
            "POST",
            "/api/v1/jobs/runs",
            json_body=_submit_body(run_id, server_a, (READ_CAP, "m1")),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert created.status == 201
        response = await call_app(
            jobs_app,
            "GET",
            f"/api/v1/jobs/runs/{run_id}/events",
            headers={"accept": "text/event-stream"},
        )
        assert response.status == 401
        assert _error(response)["code"] == "unauthenticated"
        assert "text/event-stream" not in (response.header("content-type") or "")
        assert not response.body.startswith(b"id:")

    async def test_subscription_negative_controls(
        self, jobs_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
    ) -> None:
        run_id = uuid.uuid4()
        created = await call_app(
            jobs_app,
            "POST",
            "/api/v1/jobs/runs",
            json_body=_submit_body(run_id, server_a, (READ_CAP, "m1")),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert created.status == 201
        evil = await call_app(
            jobs_app,
            "GET",
            f"/api/v1/jobs/runs/{run_id}/events",
            headers={"origin": EVIL_ORIGIN},
            cookies=jar,
        )
        assert evil.status == 403
        assert _error(evil)["code"] == "origin_rejected"
        unknown = await call_app(
            jobs_app,
            "GET",
            f"/api/v1/jobs/runs/{uuid.uuid4()}/events",
            cookies=jar,
        )
        assert unknown.status == 404
        bad_cursor = await call_app(
            jobs_app,
            "GET",
            f"/api/v1/jobs/runs/{run_id}/events",
            headers={"last-event-id": "not-a-number"},
            cookies=jar,
        )
        assert bad_cursor.status == 422
        assert _error(bad_cursor)["code"] == "validation_failed"
        negative_cursor = await call_app(
            jobs_app,
            "GET",
            f"/api/v1/jobs/runs/{run_id}/events",
            headers={"last-event-id": "-3"},
            cookies=jar,
        )
        assert negative_cursor.status == 422


# ---------------------------------------------------------------------------
# the surface is really registered and the OpenAPI contract is current
# ---------------------------------------------------------------------------


class TestOpenApiContract:
    def test_jobs_routes_are_registered_through_the_real_app(
        self, bootstrapped_url: str, tmp_path: Path
    ) -> None:
        app = _build_app(bootstrapped_url, tmp_path / "data-root")
        schema = app.openapi()
        paths = schema["paths"]
        assert "/api/v1/jobs/runs" in paths
        assert set(paths["/api/v1/jobs/runs"]) == {"post", "get"}
        assert "/api/v1/jobs/runs/{run_id}" in paths
        assert set(paths["/api/v1/jobs/runs/{run_id}"]) == {"get"}
        assert "/api/v1/jobs/runs/{run_id}/cancel" in paths
        assert set(paths["/api/v1/jobs/runs/{run_id}/cancel"]) == {"post"}
        assert "/api/v1/jobs/runs/{run_id}/events" in paths
        assert set(paths["/api/v1/jobs/runs/{run_id}/events"]) == {"get"}
        submit = paths["/api/v1/jobs/runs"]["post"]
        assert set(submit["responses"]) >= {"200", "201", "409", "422"}
        cancel = paths["/api/v1/jobs/runs/{run_id}/cancel"]["post"]
        assert set(cancel["responses"]) >= {"200", "202", "409"}
        component_names = set(schema["components"]["schemas"])
        assert {
            "RunSubmitBody",
            "RunView",
            "RunDetail",
            "RunCreated",
            "RunPage",
            "CancelView",
            "StepView",
        } <= component_names
