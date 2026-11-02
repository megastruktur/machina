"""Focused tests for the chat HTTP API (card 06-chat-api).

Card ``06-chat-api`` against contracts.md **C06** (agent conversation
runs, explicit context references, delegation), **C05** (durable runs,
submit dedup, cancel semantics, the frozen event envelope), **C08**
(session/origin boundary, SSE + ``Last-Event-ID`` replay cursor), **C01**
(envelope, opaque identity, explicit server scope).

Boundary discipline (test policy): every HTTP claim runs through the
real ASGI app (``create_app`` + lifespan + middleware + handlers)
against a real disposable Postgres (fixture ``migrated_url``) through
the real linear Alembic chain. The live halves spawn REAL worker
subprocesses composing the REAL agent-run + delegation executors with a
deterministic FunctionModel fake (C06 rule 6: fake LLMs prove MAI
plumbing only) and the real dispatcher/policy/approval/model-store
machinery — exactly the ``06-agent-run``/``06-child-runs`` harness
shape. The artifact store fixture is written by the REAL
``write_evidence_artifact`` producer, so attachment resolution is
exercised against the actual on-disk layout.

Scenario coverage owned here (writer half; assembly is ``06-verify``):
authenticated real HTTP submits a message with an allowed artifact
reference (and rejects forbidden path-shaped references), observes
parent/child events, reconnects from the cursor without duplicates, and
the journaled model view proves only the scoped conversation content
reached the model.

Registered limitations: no live provider, no real SSH, no browser (the
UI journey is ``06-web-chat``; the assembled scenario is the P06 gate's).
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

import mai.api.chat as chat_surface
from mai.agents.budgets import (
    BUDGET_CHARGED_EVENT_TYPE,
    BUDGET_PAYLOAD_VERSION,
    BUDGET_RELEASED_EVENT_TYPE,
    BUDGET_RESERVED_EVENT_TYPE,
)
from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE
from mai.capabilities.history_tags import TAGS_CAPABILITY_IDS
from mai.capabilities.internal_api import INTERNAL_API_CAPABILITY_IDS
from mai.capabilities.logs import write_evidence_artifact
from mai.contracts.jobs import TERMINAL_RUN_STATES, RunState, parse_job_event

BACKEND_ROOT = Path(__file__).resolve().parents[1]

#: The suites run async tests through the anyio pytest plugin (the repo's
#: async test convention); sync helpers stay unmarked and pass through.
pytestmark = pytest.mark.anyio

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: Synthetic fixture credential (NOT a secret; disposable fixture DB only).
FIXTURE_PASSWORD = "fixture-password-06-chat-api-only"
ORIGIN = "http://127.0.0.1:8443"  # loopback allowlist entry
EVIL_ORIGIN = "https://attacker.example"

#: Opaque server identities (C01 rule 1) — never display names.
SERVER_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")

READ_CAP = "fixture.read"
PRIV_CAP = "fixture.priv"

HANDLER_MODULE_NAME = "chat_http_scenario_handlers"


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
        # delivered once; afterwards the channel BLOCKS until a disconnect.
        if not call._request_consumed:
            call._request_consumed = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()  # connected until close() cancels
        return {"type": "http.disconnect"}  # pragma: no cover - unreachable

    async def runner() -> None:
        await app(_scope("GET", path, flat_headers, b""), receive, call._on_message)

    call.task = asyncio.get_running_loop().create_task(runner())
    await call.wait_started()
    return call


def sse_frames(body: str) -> list[dict[str, Any]]:
    """Parse COMPLETE SSE frames into ``{id, event, data}`` records."""
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


async def create_profile(
    app: FastAPI,
    jar: dict[str, str],
    name: str,
    capabilities: list[str] | None = None,
) -> uuid.UUID:
    """Create one canonical server profile through the real HTTP surface.

    The identity is MINTED server-side (C01 rule 1: opaque identities are
    never client-chosen); the minted UUID is the fixture's server scope.
    """
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
            "capabilities": capabilities if capabilities is not None else [],
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert response.status == 201, response.body
    return uuid.UUID(response.json()["server_id"])


# ---------------------------------------------------------------------------
# the worker-side composition (real executors, scripted fake model)
# ---------------------------------------------------------------------------

HANDLER_MODULE_SOURCE = '''
"""Worker-side composition for 06-chat-api live scenarios: real
dispatcher/policy/approval/model-store machinery, journaled fixture
effects, deterministic FunctionModel fake (C06 rule 6)."""

import json
import os
import time
import uuid

from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import mai.agents.run as agent_run
import mai.agents.delegate as agent_delegate
from mai.agents.budgets import BudgetLedger
from mai.agents.model_store import ModelConfigStore
from mai.capabilities.dispatch import CapabilityDispatcher, ExecutorResult
from mai.credentials.env import EnvResolver
from mai.jobs.engine import EngineClient
from mai.jobs.resource_locks import ResourceLockService
from mai.plugins.registry import CapabilityRegistry, parse_descriptor
from mai.policy.approvals import ApprovalStore
from mai.policy.evaluate import Policy

READ_CAP = "fixture.read"
PRIV_CAP = "fixture.priv"


def _descriptor(capability_id, effect_class):
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
            "resource_scope": [] if effect_class == "read" else ["target.exec"],
            "required_grants": [],
        },
        source=f"<{capability_id} descriptor>",
    )


class FixtureExecutor:
    """Journaled capability executor (effect markers make exactly-once
    observable)."""

    def __init__(self):
        self.journal = os.environ["MAI_FX_JOURNAL"]

    def _mark(self, marker):
        with open(self.journal, "a", encoding="utf-8") as handle:
            handle.write(marker + "\\n")

    async def __call__(self, call):
        capability_id = call.descriptor.identifier
        marker = call.payload.get("marker", "?")
        self._mark(f"begin:{capability_id}:{marker}")
        self._mark(f"effect:{capability_id}:{marker}")
        document = {"status": "succeeded", "exit_code": 0, "stdout": "", "stderr": ""}
        return ExecutorResult(ok=True, output=json.dumps(document, sort_keys=True))


def _script_model():
    script = json.loads(os.environ["MAI_FX_MODEL_SCRIPT"])
    journal = os.environ["MAI_FX_JOURNAL"]

    def _mark(line):
        with open(journal, "a", encoding="utf-8") as handle:
            handle.write(line + "\\n")

    def _entry_payload(entry):
        if "tool_call" in entry:
            call = entry["tool_call"]
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=call["name"],
                        args=call.get("args", {"marker": "m1"}),
                        tool_call_id=call.get("id", "c1"),
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content=entry.get("text", "done"))])

    def _model_fn(messages, info):
        _mark("model_call")
        # Journal what the model actually received as the task line, so
        # the scoped-context claim is observable at the model boundary:
        # the latest request's user prompt carries exactly ONE task
        # marker (its own conversation's), never a sibling's.
        for message in reversed(messages):
            if message.kind == "request":
                for part in message.parts:
                    if getattr(part, "part_kind", "") == "user-prompt":
                        text = getattr(part, "content", "")
                        for marker in ("aspect-alpha", "aspect-beta",
                                       "canary-artifact"):
                            if marker in text:
                                _mark(f"user_marker:{marker}")
                        break
                break
        index = (len(messages) - 1) // 2
        if index >= len(script):
            raise AssertionError("model script exhausted")
        return _entry_payload(script[index])

    async def _stream_fn(messages, info):
        # The real observers always consume streamed requests; replay the
        # exact same deterministic script as ONE complete chunk per
        # response (a valid FunctionModel stream shape): the full text for
        # text entries, or one DeltaToolCalls mapping for tool calls.
        response = _model_fn(messages, info)
        for part in response.parts:
            if isinstance(part, TextPart):
                yield part.content
            else:
                yield {
                    0: DeltaToolCall(
                        name=part.tool_name,
                        json_args=json.dumps(part.args_as_dict()),
                        tool_call_id=part.tool_call_id,
                    )
                }

    return FunctionModel(_model_fn, stream_function=_stream_fn)


def _build():
    url = os.environ["MAI_DATABASE_URL"]
    app_engine = create_async_engine(url, poolclass=NullPool)
    sessions = async_sessionmaker(app_engine, expire_on_commit=False)
    with open(os.environ["MAI_FX_GRANTS"], encoding="utf-8") as handle:
        grants_doc = json.load(handle)
    with open(os.environ["MAI_FX_POLICY"], encoding="utf-8") as handle:
        policy_doc = json.load(handle)
    grants = {
        entry["capability_id"]: frozenset(uuid.UUID(v) for v in entry["server_ids"])
        for entry in grants_doc
    }
    registry = CapabilityRegistry()
    registry.register(_descriptor(READ_CAP, "read"))
    registry.register(_descriptor(PRIV_CAP, "privileged"))
    dispatcher = CapabilityDispatcher(
        registry=registry,
        approvals=ApprovalStore(sessions),
        grants=lambda: grants,
        policy=lambda: Policy(
            revision=int(policy_doc["revision"]),
            denied_capabilities=frozenset(policy_doc.get("denied", [])),
        ),
        adopted=lambda server_id: True,
        credentials=lambda: EnvResolver(os.environ["MAI_FX_ENV"]),
        executor=FixtureExecutor(),
    )
    model_store = ModelConfigStore(os.environ["MAI_FX_MODEL_ROOT"])
    fake_model = _script_model()
    executor = agent_run.AgentRunExecutor(
        sessions=sessions,
        dispatcher=dispatcher,
        registry=registry,
        model_store=model_store,
        data_root=os.environ["MAI_FX_DATA_ROOT"],
        locks=ResourceLockService(url),
        resolver=EnvResolver(os.environ["MAI_FX_ENV"]),
        model_builder=lambda **kwargs: fake_model,
    )
    agent_run.configure_agent_run(executor)
    agent_delegate.configure_delegation(
        agent_delegate.DelegationExecutor(
            sessions=sessions,
            executor=executor,
            client=EngineClient(url),
            ledger=BudgetLedger(sessions),
        )
    )


_build()
'''


# ---------------------------------------------------------------------------
# live fixtures: real disposable Postgres + the real engine shape
# ---------------------------------------------------------------------------

FIXTURE_ROOT = Path("/tmp/mai-fxt-06-chat-api")


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(os.path.join(BACKEND_ROOT, "alembic.ini"))


def _drop_app_tables(url: str) -> None:
    """Reset the fixture database to pre-bootstrap state (whole schema)."""
    with psycopg.connect(url) as conn:
        # Whole-schema reset (the applications suite's approach): the
        # enumerated-drop list went stale across migrations 0005–0009
        # (e.g. agent_tasks now holds an FK into conversations).
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``head``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the chat HTTP surface claims need a "
            "real disposable Postgres fixture"
        )
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
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


def _seed_terminal_catalog(url: str) -> None:
    """Seed the terminal catalog tags the workers' transitions swap into.

    A terminal ``record_transition`` atomically swaps the reserved
    ``unclassified`` assignment for a catalog tag named after the target
    state (W5B1); a fresh 0011 catalog only seeds ``unclassified``, so
    the live scenarios seed the terminal rows (idempotent) — the same
    harness contract the jobs suite's live scenarios use.
    """
    with psycopg.connect(url) as conn:
        for state in TERMINAL_RUN_STATES:
            conn.execute(
                "INSERT INTO tags (tag_id, name, description) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (uuid.uuid4(), state.value, "terminal run classification"),
            )
        conn.commit()


@pytest.fixture(scope="session")
def bootstrapped_url(migrated_url: str) -> str:
    """Create the engine-owned ``dbos`` schema the way production does."""
    from mai.jobs import engine as jobs_engine

    jobs_engine.launch_worker(migrated_url)
    jobs_engine.shutdown()
    _seed_terminal_catalog(migrated_url)
    return migrated_url


def _build_app(url: str, data_root: Path, artifacts_root: Path) -> FastAPI:
    return create_app(
        env={
            "MAI_DATABASE_URL": url,
            "MAI_PUBLIC_ORIGINS": ORIGIN,
            "MAI_DATA_ROOT": str(data_root),
            "MAI_ARTIFACTS_ROOT": str(artifacts_root),
        }
    )


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[FastAPI]:
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
def scenario(tmp_path: Path) -> Path:
    """One fresh scratch scenario namespace (journal, handlers, roots)."""
    directory = tmp_path / "scenario"
    (directory / "gates").mkdir(parents=True)
    handlers = directory / "handlers"
    handlers.mkdir()
    (handlers / f"{HANDLER_MODULE_NAME}.py").write_text(
        HANDLER_MODULE_SOURCE, encoding="utf-8"
    )
    return directory


@pytest.fixture
async def chat_app(bootstrapped_url: str, tmp_path: Path):
    """App on the real fixture DB with fresh confined roots."""
    await _truncate(bootstrapped_url)
    app = _build_app(
        bootstrapped_url, tmp_path / "data-root", tmp_path / "artifacts"
    )
    async with _running(app):
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()


async def _truncate(url: str) -> None:
    """Reset the facade tables between tests (the session-scoped DB stays)."""
    from sqlalchemy import text

    from mai.database import create_app_engine

    engine, _sessions = create_app_engine(url)
    try:
        async with engine.begin() as conn:
            for table in (
                # v2 objective tables first: they reference job_runs and
                # conversations (the same FK-safe order as the v2 suite).
                "objective_checklists",
                "objective_journal",
                "objective_events",
                "objective_runs",
                "objective_servers",
                "objectives",
                "job_resource_locks",
                "job_schedule_fires",
                "job_steps",
                "job_events",
                "job_schedules",
                "job_runs",
                "approvals",
                "profile_projection_journal",
                "profile_projection",
                "admin_sessions",
                "admin_credential",
                "conversation_steering",
                "conversation_runs",
                "conversation_messages",
                "conversations",
            ):
                # ``job_resource_locks`` is engine/service-owned (like the
                # dbos schema): absent until its first real use, so the
                # reset observes existence instead of assuming it.
                present = await conn.execute(
                    text("SELECT to_regclass(:t) IS NOT NULL"),
                    {"t": f"public.{table}"},
                )
                if not present.scalar():
                    continue
                await conn.execute(text(f"DELETE FROM {table}"))
    finally:
        await engine.dispose()


@pytest.fixture
async def jar(chat_app: FastAPI) -> dict[str, str]:
    return await login_session(chat_app)


@pytest.fixture
async def server_a(chat_app: FastAPI, jar: dict[str, str]) -> uuid.UUID:
    return await create_profile(chat_app, jar, "fixture-a")


def _write_worker_policy(scenario: Path, server: uuid.UUID) -> None:
    document = [
        {"capability_id": READ_CAP, "server_ids": [str(server)]},
        {"capability_id": PRIV_CAP, "server_ids": []},
    ]
    (scenario / "grants.json").write_text(json.dumps(document), encoding="utf-8")
    (scenario / "policy.json").write_text(
        json.dumps({"revision": 1, "denied": []}), encoding="utf-8"
    )


def _write_model_root(root: Path) -> None:
    from mai.agents.models import SCHEMA_VERSION as MODEL_SCHEMA_VERSION
    from mai.agents.model_store import ModelConfigStore

    store = ModelConfigStore(root)
    connection: dict[str, object] = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "name": "gateway",
        "base_url": "http://127.0.0.1:9/v1",
        "timeout_seconds": 5,
        "capabilities": {"tool_calls": True},
    }
    store.write_connection("gateway", json.dumps(connection) + "\n", expected_revision=0)
    profile = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "name": "fast",
        "connection": "gateway",
        "model": "fixture-model",
        "max_tokens": 256,
    }
    store.write_profile("fast", json.dumps(profile) + "\n", expected_revision=0)
    store.set_selection("gateway", "fast", expected_revision=0)


def _compose_worker_roots(scenario: Path, server: uuid.UUID) -> None:
    """Write the worker-side model/data roots + fixture env once."""
    _write_model_root(scenario / "model-root")
    from mai.profiles.files import ProfileFileStore

    store = ProfileFileStore(scenario / "data-root")
    profile_text = (
        f"schema_version: 1\n"
        f'server_id: "{server}"\n'
        'name: "chat-fixture"\n'
        "prompt_revision: 3\n"
        "environment: linux_ssh\n"
        "ssh:\n"
        '  host: "198.51.100.10"\n'
        "  port: 22\n"
        '  user: "deploy"\n'
        '  host_key: "SHA256:6VhOyUC6fvXW0aQ9WNe7Kx3nZZf+zxgupEWLoZXoTMU"\n'
        '  credential_ref: "MAI_SSH_KEY_FIXTURE"\n'
        "privilege: none\n"
        "capabilities:\n"
        "  - fixture.read\n"
    )
    store.write(server, profile_text, expected_revision=0)
    prompt_dir = scenario / "data-root" / "servers" / str(server)
    prompt_dir.chmod(0o700)
    prompt_path = prompt_dir / "prompt.md"
    prompt_path.write_text("Answer briefly.\n", encoding="utf-8")
    os.chmod(prompt_path, 0o600)
    (scenario / "fixture.env").write_text(
        "MAI_SSH_KEY_FIXTURE=fixture-key-value-not-a-secret\n", encoding="utf-8"
    )
    os.chmod(scenario / "fixture.env", 0o600)
    _write_worker_policy(scenario, server)


def _write_model_script(scenario: Path, script: list[dict]) -> None:
    (scenario / "model-script.json").write_text(json.dumps(script), encoding="utf-8")


def _journal_path(scenario: Path) -> Path:
    return scenario / "journal.log"


def _journal_lines(scenario: Path) -> list[str]:
    try:
        return _journal_path(scenario).read_text().splitlines()
    except FileNotFoundError:
        return []


def _journal_count(scenario: Path, marker: str) -> int:
    return sum(1 for line in _journal_lines(scenario) if line == marker)


def _wait_until(predicate, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _spawn_worker(
    url: str, handler_dir: Path, scenario: Path, tag: str
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
    env["MAI_FX_ENV"] = str(scenario / "fixture.env")
    env["MAI_FX_MODEL_ROOT"] = str(scenario / "model-root")
    env["MAI_FX_DATA_ROOT"] = str(scenario / "data-root")
    script_path = scenario / "model-script.json"
    if script_path.exists():
        env["MAI_FX_MODEL_SCRIPT"] = script_path.read_text(encoding="utf-8")
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
# chat-specific helpers
# ---------------------------------------------------------------------------


def _write_artifact(root: Path, server: uuid.UUID, op_id: uuid.UUID,
                    content: bytes) -> str:
    """Persist one fixture artifact through the REAL producer layout."""
    reference = write_evidence_artifact(
        root,
        server,
        op_id,
        content,
        {"capability": "host.logs", "source": "journal", "service": "fixture"},
    )
    return reference.sha256


async def submit_chat(
    app: FastAPI,
    jar: dict[str, str],
    run_id: uuid.UUID,
    server: uuid.UUID,
    *,
    message: str = "investigate the fixture service",
    profile: str = "fast",
    allowed_capabilities: list[str] | None = None,
    children: list[dict] | None = None,
    attachments: list[dict] | None = None,
    **extra: Any,
) -> HttpResponse:
    body: dict[str, Any] = {
        "run_id": str(run_id),
        "server_id": str(server),
        "message": message,
        "profile": profile,
    }
    if allowed_capabilities is not None:
        body["allowed_capabilities"] = allowed_capabilities
    if children is not None:
        body["children"] = children
    if attachments is not None:
        body["attachments"] = attachments
    body.update(extra)
    return await call_app(
        app,
        "POST",
        "/api/v1/chat/messages",
        json_body=body,
        headers=_mutation_headers(jar),
        cookies=jar,
    )


async def read_events_direct(url: str, run_id: uuid.UUID) -> list:
    """The run's persisted events via direct typed reads (assertion aid)."""
    with psycopg.connect(url, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT seq, ts, type, v, data FROM job_events "
            "WHERE run_id = %s ORDER BY seq",
            (run_id,),
        ).fetchall()
    return [
        parse_job_event(
            {
                "seq": row["seq"],
                "run_id": str(run_id),
                "ts": row["ts"].isoformat(),
                "type": row["type"],
                "v": row["v"],
                "data": row["data"],
            }
        )
        for row in rows
    ]


async def _current_state(url: str, run_id: uuid.UUID) -> str | None:
    from mai.database import create_app_engine
    from mai.jobs.lifecycle import RunLifecycle

    _engine, sessions = create_app_engine(url)
    try:
        state = await RunLifecycle(sessions).current_state(str(run_id))
        return None if state is None else state.value
    finally:
        await _engine.dispose()


# ---------------------------------------------------------------------------
# pure units (run everywhere; no fixture DB required)
# ---------------------------------------------------------------------------


class TestArtifactsRootResolution:
    def test_explicit_env_value_wins_with_expansion(self) -> None:
        resolved = chat_surface.resolve_artifacts_root(
            {"MAI_ARTIFACTS_ROOT": "~/evidence-store", "MAI_DATA_ROOT": "/data"}
        )
        assert resolved == os.path.expanduser("~/evidence-store")

    def test_default_lives_under_the_explicit_data_root(self) -> None:
        resolved = chat_surface.resolve_artifacts_root({"MAI_DATA_ROOT": "/data"})
        assert resolved == "/data/artifacts"

    def test_default_falls_back_to_the_contract_data_root(self) -> None:
        resolved = chat_surface.resolve_artifacts_root({})
        assert resolved.endswith("/.mai/artifacts")


class TestSubmitBodyShape:
    def test_rename_body_bounds_and_extra_forbid(self) -> None:
        from pydantic import ValidationError

        parsed = chat_surface.ChatRenameBody(title="My Production Audit")
        assert parsed.title == "My Production Audit"
        with pytest.raises(ValidationError):
            chat_surface.ChatRenameBody(title="")
        with pytest.raises(ValidationError):
            chat_surface.ChatRenameBody(title="x" * 121)
        with pytest.raises(ValidationError):
            chat_surface.ChatRenameBody(title="ok", extra_field="bad")  # type: ignore[call-arg]

    def test_unknown_fields_are_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            chat_surface.ChatSubmitBody(
                run_id=str(uuid.uuid4()),
                server_id=str(uuid.uuid4()),
                message="hello",
                profile="fast",
                path="/etc/passwd",  # <- invented vocabulary must not exist
            )

    def test_rounds_bound_matches_the_producer_budget(self) -> None:
        from pydantic import ValidationError

        base = {
            "run_id": str(uuid.uuid4()),
            "server_id": str(uuid.uuid4()),
            "message": "hello",
            "profile": "fast",
        }
        with pytest.raises(ValidationError):
            chat_surface.ChatSubmitBody(**base, rounds=0)
        with pytest.raises(ValidationError):
            chat_surface.ChatSubmitBody(**base, rounds=33)
        parsed = chat_surface.ChatSubmitBody(**base, rounds=16)
        assert parsed.rounds == 16

    def test_attachment_bodies_carry_identity_only(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            chat_surface.AttachmentBody(
                server_id=str(uuid.uuid4()),
                op_id=str(uuid.uuid4()),
                path="/etc/passwd",  # <- no path field may exist
            )

    def test_children_carry_no_target_field(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            chat_surface.ChildBody(task="x", server_id=str(SERVER_B))

    def test_root_budget_bounds_mirror_the_producer_vocabulary(self) -> None:
        from pydantic import ValidationError

        base = {
            "max_depth": 2,
            "max_children": 2,
            "max_active": 3,
            "max_steps": 50,
            "max_total_tokens": 5_000,
        }
        # Every cap is REQUIRED (no silent ultra-tight defaults); ``v``
        # is stamped with the frozen version and the doc round-trips.
        budget = chat_surface.RootBudgetBody(**base)
        assert budget.v == BUDGET_PAYLOAD_VERSION
        assert budget.model_dump() == {"v": BUDGET_PAYLOAD_VERSION, **base}
        with pytest.raises(ValidationError):
            chat_surface.RootBudgetBody(max_depth=2, max_children=2)
        with pytest.raises(ValidationError):
            chat_surface.RootBudgetBody(**{**base, "max_depth": 0})
        with pytest.raises(ValidationError):
            chat_surface.RootBudgetBody(
                **{**base, "max_depth": chat_surface.agent_delegate.MAX_DELEGATION_DEPTH + 1}
            )
        with pytest.raises(ValidationError):
            chat_surface.RootBudgetBody(**base, unknown=1)
        # The omitted budget is the explicit unbudgeted root.
        submit = chat_surface.ChatSubmitBody(
            run_id=str(uuid.uuid4()),
            server_id=str(uuid.uuid4()),
            message="hello",
            profile="fast",
        )
        assert submit.root_budget is None


# ---------------------------------------------------------------------------
# live HTTP boundary (fixture DB required)
# ---------------------------------------------------------------------------


async def test_submit_creates_durable_agent_job_with_recorded_references(
    chat_app, jar, server_a, bootstrapped_url, tmp_path
) -> None:
    """AC1: the message becomes a durable agent run; the allowed artifact
    reference resolves, is integrity-checked and is recorded value-free."""
    artifacts_root = tmp_path / "artifacts"
    op_id = uuid.uuid4()
    canary = b"fixture log line with canary-artifact body\n"
    sha = _write_artifact(artifacts_root, server_a, op_id, canary)
    run_id = uuid.uuid4()

    response = await submit_chat(
        chat_app,
        jar,
        run_id,
        server_a,
        allowed_capabilities=[READ_CAP],
        attachments=[{"server_id": str(server_a), "op_id": str(op_id)}],
    )
    assert response.status == 201, response.body
    created = response.json()
    assert created["deduplicated"] is False
    view = created["conversation"]
    assert view["run_id"] == str(run_id)
    assert view["server_id"] == str(server_a)
    assert view["depth"] == 0
    assert view["model_profile"] == "fast"
    # An explicit request rides with the internal-API tool surface and the
    # web research tools (the documented explicit-path union).
    assert view["requested_grants"] == sorted(
        {
            READ_CAP,
            *INTERNAL_API_CAPABILITY_IDS,
            "web.search",
            "web.read",
        }
    )
    assert view["attachments"] == [
        {
            "server_id": str(server_a),
            "op_id": str(op_id),
            "sha256": sha,
            "bytes": len(canary),
        }
    ]

    # The facade row + both intent records exist; the engine HOLDS the
    # workflow (durably enqueued even with no worker running).
    with psycopg.connect(bootstrapped_url, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT run_id, server_id, depth, model_profile, requested_grants "
            "FROM job_runs WHERE run_id = %s",
            (run_id,),
        ).fetchone()
        assert row is not None
        assert row["model_profile"] == "fast"
    from mai.jobs.engine import EngineClient

    client = EngineClient(bootstrapped_url)
    try:
        assert client.run_state(str(run_id)) is RunState.QUEUED
    finally:
        client.close()
    events = await read_events_direct(bootstrapped_url, run_id)
    types = [event.type for event in events]
    assert "run.submitted" in types and "chat.submitted" in types
    submitted = next(e for e in events if e.type == "chat.submitted")
    assert submitted.data["attachments"] == view["attachments"]
    assert submitted.data["handler"] == "agent.run"
    assert submitted.data["children"] == 0
    # The transition edge anchors the replay at queued.
    assert await _current_state(bootstrapped_url, run_id) == "queued"

    # The durable intent is a valid agent-run payload (the ONE producer
    # accepted it at submit; the digest record covers the whole submission).
    assert submitted.data["submission_sha256"]


async def test_submit_view_carries_the_mandatory_tag_preview(
    chat_app, jar, server_a, bootstrapped_url
) -> None:
    """The RunView ``tag_preview`` is required on the REAL submit path
    (HT-W6A fix1): the response carries the actual reserved
    classification preview — never a fabricated empty projection
    (``total=0`` is contract-invalid)."""
    response = await submit_chat(chat_app, jar, uuid.uuid4(), server_a)
    assert response.status == 201, response.body
    view = response.json()["conversation"]
    assert view["tag_preview"] == {"preview": ["unclassified"], "total": 1}


async def test_submit_auto_resolves_default_capabilities(
    chat_app, jar, server_a, bootstrapped_url
) -> None:
    """Omitted ``allowed_capabilities`` runs EVERY capability granted for
    the target: grant-store entries, server-profile capabilities (the
    worker's ``host_shell`` → ``host.shell`` mapping), enabled plugin
    bindings and the built-in defaults — under the delegated mode."""
    created = await submit_chat(chat_app, jar, uuid.uuid4(), server_a)
    assert created.status == 201, created.body
    view = created.json()["conversation"]
    # No grant config stored, empty profile capabilities, no plugins:
    # the built-in surface alone (fixture scope: server_a has no grants).
    assert view["requested_grants"] == sorted(
        {*INTERNAL_API_CAPABILITY_IDS, *chat_surface.BUILTIN_CHAT_CAPABILITIES}
    )

    # The SAME omitted request on General Consult resolves the same
    # controller-side defaults without any host profile.
    consult = await submit_chat(
        chat_app, jar, uuid.uuid4(), uuid.UUID(int=0)
    )
    assert consult.status == 201, consult.body
    assert consult.json()["conversation"]["requested_grants"] == (
        view["requested_grants"]
    )

    # Server-profile capabilities join through the exact mapping the
    # worker's grant bootstrap applies (raw slug + normalized + specials).
    host_server = await create_profile(
        chat_app, jar, "fixture-host", capabilities=["host_shell", "fixture.read"]
    )
    hosted = await submit_chat(chat_app, jar, uuid.uuid4(), host_server)
    assert hosted.status == 201, hosted.body
    hosted_grants = set(hosted.json()["conversation"]["requested_grants"])
    assert {"host_shell", "host.shell", "fixture.read"} <= hosted_grants

    # The durable row carries the auto-resolved grant set.
    with psycopg.connect(bootstrapped_url, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT requested_grants FROM job_runs WHERE run_id = %s",
            (uuid.UUID(hosted.json()["conversation"]["run_id"]),),
        ).fetchone()
    assert row is not None
    assert set(row["requested_grants"]) == hosted_grants


async def test_submit_auto_resolution_includes_grant_store_entries(
    chat_app, jar, server_a
) -> None:
    """Grant-store entries scoped to the target join the auto-resolved
    set; entries scoped to another server do not."""
    chat_app.state.grants.write(
        {
            "policy": {"revision": 1, "denied_capabilities": []},
            "grants": [
                {
                    "capability_id": "fixture.granted",
                    "server_ids": [str(server_a)],
                },
                {
                    "capability_id": "fixture.elsewhere",
                    "server_ids": [str(SERVER_B)],
                },
            ],
        },
        expected_revision=0,
    )
    created = await submit_chat(chat_app, jar, uuid.uuid4(), server_a)
    assert created.status == 201, created.body
    grants = set(created.json()["conversation"]["requested_grants"])
    assert "fixture.granted" in grants
    assert "fixture.elsewhere" not in grants


async def test_submit_read_only_strips_mutation_capabilities(
    chat_app, jar, server_a
) -> None:
    """``read_only=True`` strips the non-read vocabulary and pins the
    read fallbacks; the flag participates in the whole-submission
    digest (a flipped flag for the same key is a DIFFERENT submission)."""
    tag_mutations = ["tags.assign", "tags.remove", "tags.create", "tags.delete"]
    tag_reads = [
        "tags.list_for",
        "tags.list_catalog",
        "tags.search_jobs",
        "tags.search_conversations",
        "tags.read_conversation",
        "tags.read_job",
    ]
    run_id = uuid.uuid4()
    created = await submit_chat(
        chat_app,
        jar,
        run_id,
        server_a,
        allowed_capabilities=[
            READ_CAP,
            PRIV_CAP,
            "host.shell",
            *TAGS_CAPABILITY_IDS,
        ],
        read_only=True,
    )
    assert created.status == 201, created.body
    grants = set(created.json()["conversation"]["requested_grants"])
    # The static mutation vocabulary is stripped even without a registry.
    assert "host.shell" not in grants
    assert "app.adopt" not in grants
    assert "app.unadopt" not in grants
    assert "app.upsert_map_entry" not in grants
    assert "app.delete_map_entry" not in grants
    assert "secret.store" not in grants
    # The four tags mutations are stripped; the six tags reads survive.
    assert all(mutation not in grants for mutation in tag_mutations)
    assert all(read in grants for read in tag_reads)
    # Read capabilities (explicit and internal) survive; the read
    # fallbacks are always ensured so the context tuple is non-empty.
    assert READ_CAP in grants
    assert PRIV_CAP in grants  # no registry on the API process: unseen
    assert {"chat.read_session", "docs.retrieve"} <= grants

    # Identical read-only resubmission attaches; flipping the flag is a
    # new submission (409 for the same idempotency key).
    duplicate = await submit_chat(
        chat_app,
        jar,
        run_id,
        server_a,
        allowed_capabilities=[
            READ_CAP,
            PRIV_CAP,
            "host.shell",
            *TAGS_CAPABILITY_IDS,
        ],
        read_only=True,
    )
    assert duplicate.status == 200, duplicate.body
    assert duplicate.json()["deduplicated"] is True
    writable = await submit_chat(
        chat_app,
        jar,
        run_id,
        server_a,
        allowed_capabilities=[
            READ_CAP,
            PRIV_CAP,
            "host.shell",
            *TAGS_CAPABILITY_IDS,
        ],
        read_only=False,
    )
    assert writable.status == 409
    assert _error(writable)["code"] == "conflict"


async def test_submit_explicit_tag_read_subset_is_not_broadened(
    chat_app, jar, server_a
) -> None:
    """An explicit ``allowed_capabilities`` subset carries exactly the
    requested ``tags.*`` reads: no built-in vocabulary joins and no
    ``tags.*`` mutation is silently added."""
    created = await submit_chat(
        chat_app,
        jar,
        uuid.uuid4(),
        server_a,
        allowed_capabilities=["tags.list_for", "tags.read_job"],
    )
    assert created.status == 201, created.body
    grants = set(created.json()["conversation"]["requested_grants"])
    # The explicit subset is preserved: the two requested reads are in
    # and every other ``tags.*`` id (mutations included) stays out; the
    # existing internal/web/plugins union is NOT tags-broadened.
    requested = {"tags.list_for", "tags.read_job"}
    assert requested <= grants
    assert (set(TAGS_CAPABILITY_IDS) - requested).isdisjoint(grants)


async def test_submit_read_only_filters_registry_effect_classes(
    chat_app, jar, server_a
) -> None:
    """Registry descriptors with ``effect_class != "read"`` are stripped
    from a read-only conversation's resolved set."""
    from types import SimpleNamespace

    chat_app.state.registry = SimpleNamespace(
        list_capabilities=lambda: (
            SimpleNamespace(identifier=READ_CAP, effect_class="read"),
            SimpleNamespace(identifier=PRIV_CAP, effect_class="privileged"),
        )
    )
    try:
        created = await submit_chat(
            chat_app,
            jar,
            uuid.uuid4(),
            server_a,
            allowed_capabilities=[READ_CAP, PRIV_CAP],
            read_only=True,
        )
        assert created.status == 201, created.body
        grants = set(created.json()["conversation"]["requested_grants"])
        assert READ_CAP in grants
        assert PRIV_CAP not in grants
        assert {"chat.read_session", "docs.retrieve"} <= grants
    finally:
        chat_app.state.registry = None


def test_read_only_instruction_is_pinned_onto_the_task() -> None:
    """The read-only suffix is the frozen wording, appended to the task
    and bounded to the producer's task limit."""
    from pydantic import ValidationError

    body = chat_surface.ChatSubmitBody(
        run_id=str(uuid.uuid4()),
        message="inspect the service",
        profile="fast",
        read_only=True,
    )
    context = chat_surface.ExecutionContext(
        principal="principal",
        server_id=uuid.UUID(body.server_id),
        run_id=uuid.UUID(body.run_id),
        parent_scope=None,
        profile_revision="1",
        prompt_revision="1",
        credential_references=(),
        allowed_capabilities=("chat.read_session",),
        mode=chat_surface.PolicyMode.DELEGATED,
    )
    document = chat_surface._agent_document(body, body.run_id, body.server_id, context)
    assert document["task"].startswith("inspect the service")
    assert document["task"].endswith(chat_surface.READ_ONLY_INSTRUCTION)

    # A message at the producer's cap yields its tail so the enriched
    # task still fits (never a validation rejection).
    huge = chat_surface.ChatSubmitBody(
        run_id=str(uuid.uuid4()),
        message="x" * chat_surface.agent_run.MAX_TASK_CHARS,
        profile="fast",
        read_only=True,
    )
    with pytest.raises(ValidationError):
        chat_surface.ChatSubmitBody(
            run_id=str(uuid.uuid4()),
            message="x" * (chat_surface.agent_run.MAX_TASK_CHARS + 1),
            profile="fast",
        )
    huge_document = chat_surface._agent_document(
        huge, huge.run_id, huge.server_id, context
    )
    assert len(huge_document["task"]) <= chat_surface.agent_run.MAX_TASK_CHARS
    assert huge_document["task"].endswith(chat_surface.READ_ONLY_INSTRUCTION)


async def test_duplicate_submit_attaches_and_conflicting_submit_rejects(
    chat_app, jar, server_a, bootstrapped_url
) -> None:
    """C05 rule 5 at chat granularity: identical submission attaches to the
    SAME run (the intent-level resume); any difference is a 409."""
    run_id = uuid.uuid4()
    first = await submit_chat(chat_app, jar, run_id, server_a)
    assert first.status == 201, first.body
    duplicate = await submit_chat(chat_app, jar, run_id, server_a)
    assert duplicate.status == 200, duplicate.body
    assert duplicate.json()["deduplicated"] is True
    assert duplicate.json()["conversation"]["run_id"] == str(run_id)

    different_message = await submit_chat(
        chat_app, jar, run_id, server_a, message="a different ask entirely"
    )
    assert different_message.status == 409
    assert _error(different_message)["code"] == "conflict"

    # A different attachment set for the same key is also a NEW submission.
    op_id = uuid.uuid4()
    _write_artifact(
        chat_app_state_artifacts(chat_app), server_a, op_id, b"body\n"
    )
    different_attachments = await submit_chat(
        chat_app,
        jar,
        run_id,
        server_a,
        attachments=[{"server_id": str(server_a), "op_id": str(op_id)}],
    )
    assert different_attachments.status == 409


async def test_submit_root_budget_persists_and_participates_in_dedup(
    chat_app, jar, server_a, bootstrapped_url
) -> None:
    """C05 rule 7 at the submit seam: the budget document freezes on the
    run row exactly as submitted, the omitted budget stays the explicit
    unbudgeted root (backward compatible), and the budget participates
    in the whole-submission dedup digest."""
    budget = {
        "max_depth": 2,
        "max_children": 2,
        "max_active": 3,
        "max_steps": 50,
        "max_total_tokens": 5_000,
    }
    run_id = uuid.uuid4()
    created = await submit_chat(
        chat_app, jar, run_id, server_a, root_budget=budget
    )
    assert created.status == 201, created.body

    unbudgeted_id = uuid.uuid4()
    plain = await submit_chat(chat_app, jar, unbudgeted_id, server_a)
    assert plain.status == 201, plain.body

    with psycopg.connect(bootstrapped_url, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT run_id, root_budget FROM job_runs "
            "WHERE run_id = ANY(%s) ORDER BY run_id",
            ([run_id, unbudgeted_id],),
        ).fetchall()
    by_run = {row["run_id"]: row["root_budget"] for row in rows}
    # The stored document is the submitted one, v stamped by the wire type.
    assert by_run[run_id] == {"v": BUDGET_PAYLOAD_VERSION, **budget}
    assert by_run[unbudgeted_id] is None

    # The SAME budget for the same key attaches (deduplicated).
    duplicate = await submit_chat(chat_app, jar, run_id, server_a, root_budget=budget)
    assert duplicate.status == 200
    assert duplicate.json()["deduplicated"] is True
    # A different budget for the same key is a DIFFERENT submission (409).
    conflicting = await submit_chat(
        chat_app, jar, run_id, server_a, root_budget={**budget, "max_steps": 51}
    )
    assert conflicting.status == 409
    assert _error(conflicting)["code"] == "conflict"


async def test_submit_rejects_budgets_that_fail_the_producer_or_fanout(
    chat_app, jar, server_a
) -> None:
    """Fail closed at the seam: a document the ONE producer rejects and a
    delegation that could never fit the budget's fan-out are 422s —
    never a durable intent, never a failed run."""
    run_id = uuid.uuid4()
    foreign_version = await submit_chat(
        chat_app,
        jar,
        run_id,
        server_a,
        root_budget={
            "v": 99,
            "max_depth": 2,
            "max_children": 2,
            "max_active": 3,
            "max_steps": 50,
            "max_total_tokens": 5_000,
        },
    )
    assert foreign_version.status == 422
    assert _error(foreign_version)["code"] == "validation_failed"
    assert "root_budget" in foreign_version.body.decode()

    out_of_range = await submit_chat(
        chat_app,
        jar,
        uuid.uuid4(),
        server_a,
        root_budget={
            "max_depth": 99,
            "max_children": 2,
            "max_active": 3,
            "max_steps": 50,
            "max_total_tokens": 5_000,
        },
    )
    assert out_of_range.status == 422

    over_fanout = await submit_chat(
        chat_app,
        jar,
        uuid.uuid4(),
        server_a,
        root_budget={
            "max_depth": 4,
            "max_children": 1,
            "max_active": 3,
            "max_steps": 50,
            "max_total_tokens": 5_000,
        },
        children=[
            {"task": "child aspect-alpha review", "capabilities": [READ_CAP]},
            {"task": "child aspect-beta review", "capabilities": [READ_CAP]},
        ],
    )
    assert over_fanout.status == 422
    assert "root_budget" in over_fanout.body.decode()
    # A budget WITHIN the fan-out accepts the same delegation.
    within = await submit_chat(
        chat_app,
        jar,
        uuid.uuid4(),
        server_a,
        root_budget={
            "max_depth": 4,
            "max_children": 2,
            "max_active": 3,
            "max_steps": 50,
            "max_total_tokens": 5_000,
        },
        children=[
            {"task": "child aspect-alpha review", "capabilities": [READ_CAP]},
            {"task": "child aspect-beta review", "capabilities": [READ_CAP]},
        ],
    )
    assert within.status == 201, within.body


async def test_forbidden_path_references_never_resolve(
    chat_app, jar, server_a
) -> None:
    """AC2: no arbitrary local-file read is expressible. Path-shaped
    references fail validation (canonical UUID); unknown/drifted/cross-
    server references fail honestly; nothing is read outside the store."""
    run_id = uuid.uuid4()
    traversal = await submit_chat(
        chat_app,
        jar,
        run_id,
        server_a,
        attachments=[{"server_id": str(server_a), "op_id": "../../../etc/passwd"}],
    )
    assert traversal.status == 422
    envelope = _error(traversal)
    assert envelope["code"] == "validation_failed"
    assert any(key.startswith("attachments") for key in envelope["fields"])

    unknown = await submit_chat(
        chat_app,
        jar,
        uuid.uuid4(),
        server_a,
        attachments=[{"server_id": str(server_a), "op_id": str(uuid.uuid4())}],
    )
    assert unknown.status == 404
    assert _error(unknown)["code"] == "not_found"

    cross_server = await submit_chat(
        chat_app,
        jar,
        uuid.uuid4(),
        server_a,
        attachments=[{"server_id": str(SERVER_B), "op_id": str(uuid.uuid4())}],
    )
    assert cross_server.status == 422
    assert "another server" in _error(cross_server)["fields"]["attachments[0].server_id"]

    # A stored artifact whose bytes no longer match their sidecar is a
    # conflict, never a silent accept.
    drifted_op = uuid.uuid4()
    _write_artifact(chat_app_state_artifacts(chat_app), server_a, drifted_op, b"original\n")
    artifact_path = (
        chat_app_state_artifacts(chat_app) / str(server_a) / f"{drifted_op}.log"
    )
    artifact_path.write_bytes(b"tampered\n")
    drifted = await submit_chat(
        chat_app,
        jar,
        uuid.uuid4(),
        server_a,
        attachments=[{"server_id": str(server_a), "op_id": str(drifted_op)}],
    )
    assert drifted.status == 409
    assert _error(drifted)["code"] == "conflict"


def chat_app_state_artifacts(app: FastAPI) -> Path:
    return Path(app.state.env["MAI_ARTIFACTS_ROOT"])


async def test_submit_boundary_session_origin_csrf(
    chat_app, jar, server_a
) -> None:
    """AUTH-S3 on the chat surface: every mutation is session+origin+CSRF
    gated; the event stream is session+origin gated before any byte."""
    run_id = uuid.uuid4()
    unauthenticated = await call_app(
        chat_app,
        "POST",
        "/api/v1/chat/messages",
        json_body={
            "run_id": str(run_id),
            "server_id": str(server_a),
            "message": "hi",
            "profile": "fast",
        },
        headers={"origin": ORIGIN, "x-csrf-token": "whatever"},
    )
    assert unauthenticated.status == 401
    assert _error(unauthenticated)["code"] == "unauthenticated"

    evil_origin = await call_app(
        chat_app,
        "POST",
        "/api/v1/chat/messages",
        json_body={
            "run_id": str(run_id),
            "server_id": str(server_a),
            "message": "hi",
            "profile": "fast",
        },
        headers={"origin": EVIL_ORIGIN, "x-csrf-token": jar[CSRF_COOKIE]},
        cookies=jar,
    )
    assert evil_origin.status == 403
    assert _error(evil_origin)["code"] == "origin_rejected"

    missing_csrf = await call_app(
        chat_app,
        "POST",
        "/api/v1/chat/messages",
        json_body={
            "run_id": str(run_id),
            "server_id": str(server_a),
            "message": "hi",
            "profile": "fast",
        },
        headers={"origin": ORIGIN},
        cookies=jar,
    )
    assert missing_csrf.status == 403
    assert _error(missing_csrf)["code"] == "csrf_rejected"

    # Safe methods: session required; stream origin-checked.
    no_session_list = await call_app(chat_app, "GET", f"/api/v1/chat/runs?server_id={server_a}")
    assert no_session_list.status == 401

    # The run exists only after a successful submit; submit one now.
    created = await submit_chat(chat_app, jar, run_id, server_a)
    assert created.status == 201, created.body

    evil_stream = await call_app(
        chat_app,
        "GET",
        f"/api/v1/chat/runs/{run_id}/events",
        headers={"accept": "text/event-stream", "origin": EVIL_ORIGIN},
        cookies=jar,
    )
    assert evil_stream.status == 403
    assert _error(evil_stream)["code"] == "origin_rejected"
    assert "text/event-stream" not in (evil_stream.header("content-type") or "")

    anonymous_stream = await call_app(
        chat_app,
        "GET",
        f"/api/v1/chat/runs/{run_id}/events",
        headers={"accept": "text/event-stream"},
    )
    assert anonymous_stream.status == 401
    assert _error(anonymous_stream)["code"] == "unauthenticated"
    assert not anonymous_stream.body.startswith(b"id:")


async def test_stream_unknown_run_and_bad_cursor(chat_app, jar, server_a) -> None:
    unknown = await call_app(
        chat_app,
        "GET",
        f"/api/v1/chat/runs/{uuid.uuid4()}/events",
        headers={"accept": "text/event-stream"},
        cookies=jar,
    )
    assert unknown.status == 404
    assert _error(unknown)["code"] == "not_found"

    run_id = uuid.uuid4()
    created = await submit_chat(chat_app, jar, run_id, server_a)
    assert created.status == 201, created.body

    for bad_cursor in ("not-a-cursor", "-3"):
        rejected = await call_app(
            chat_app,
            "GET",
            f"/api/v1/chat/runs/{run_id}/events",
            headers={"accept": "text/event-stream", "last-event-id": bad_cursor},
            cookies=jar,
        )
        assert rejected.status == 422
        assert _error(rejected)["code"] == "validation_failed"

    unknown_cancel = await call_app(
        chat_app,
        "POST",
        f"/api/v1/chat/runs/{uuid.uuid4()}/cancel",
        json_body={},
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert unknown_cancel.status == 404
    assert _error(unknown_cancel)["code"] == "not_found"


async def test_value_free_views_and_events(chat_app, jar, server_a, bootstrapped_url) -> None:
    """C05 rule 4/C01: the secret canary of the MESSAGE text never appears
    in any view or event payload; the submission record carries handles."""
    secret_marker = "TOP-SECRET-FIXTURE-PAYLOAD"
    run_id = uuid.uuid4()
    created = await submit_chat(
        chat_app, jar, run_id, server_a, message=f"please leak {secret_marker}"
    )
    assert created.status == 201, created.body
    assert secret_marker not in created.body.decode()
    listing = await call_app(
        chat_app,
        "GET",
        f"/api/v1/chat/runs?server_id={server_a}",
        cookies=jar,
    )
    assert listing.status == 200
    assert secret_marker not in listing.body.decode()
    assert listing.json()["total"] == 1
    for event in await read_events_direct(bootstrapped_url, run_id):
        assert secret_marker not in json.dumps(event.data)


async def test_live_conversation_executes_streams_and_reconnects(
    chat_app, jar, server_a, bootstrapped_url, scenario
) -> None:
    """The writer half of the gate scenario, conversation half: real HTTP
    submit → REAL worker (scripted fake model + journaled fixture effect)
    → parent events observed over SSE → reconnect from the cursor
    deduplicates → the model view journals carry NO attachment canary
    (only scoped context reached the model)."""
    _compose_worker_roots(scenario, server_a)
    _write_model_script(
        scenario,
        [
            {"tool_call": {"name": "fixture_read", "args": {"marker": "m1"}}},
            {"text": "done"},
        ],
    )
    artifacts_root = chat_app_state_artifacts(chat_app)
    op_id = uuid.uuid4()
    canary = b"canary-artifact must never reach the model in this slice\n"
    _write_artifact(artifacts_root, server_a, op_id, canary)

    run_id = uuid.uuid4()
    created = await submit_chat(
        chat_app,
        jar,
        run_id,
        server_a,
        message="check the fixture service with aspect-alpha focus",
        allowed_capabilities=[READ_CAP],
        attachments=[{"server_id": str(server_a), "op_id": str(op_id)}],
    )
    assert created.status == 201, created.body

    worker = _spawn_worker(
        bootstrapped_url, scenario / "handlers", scenario, "w1"
    )
    try:
        _wait_until(
            lambda: _journal_count(scenario, "effect:fixture.read:m1") == 1,
            120,
            "the journaled effect to happen exactly once",
        )
        _wait_until(
            lambda: _journal_count(scenario, "model_call") >= 2,
            120,
            "the scripted second round",
        )
        _wait_until(
            lambda: _current_state_sync(bootstrapped_url, run_id) == "succeeded",
            120,
            "the conversation to succeed",
        )
    finally:
        _stop(worker)

    journal = _journal_lines(scenario)
    assert journal.count("begin:fixture.read:m1") == 1
    assert journal.count("effect:fixture.read:m1") == 1
    assert journal.count("model_call") == 2
    assert "user_marker:aspect-alpha" in journal
    assert "user_marker:canary-artifact" not in journal

    # SSE replay from zero: every frame is a frozen-envelope event; the
    # full durable sequence includes the agent + transition vocabulary;
    # message/attachment CONTENT never appears.
    stream = await open_stream(
        chat_app, f"/api/v1/chat/runs/{run_id}/events", cookies=jar
    )
    await stream.wait_until(
        lambda body: "agent.completed" in body and stream.task.done(),
        30,
        "the drained terminal stream to close itself",
    )
    frames = sse_frames(stream.body)
    assert [int(frame["id"]) for frame in frames] == sorted(
        int(frame["id"]) for frame in frames
    )
    parsed = [parse_job_event(frame["data"]) for frame in frames]
    types = [event.type for event in parsed]
    assert types[0] == "run.submitted"
    assert "chat.submitted" in types
    assert "agent.started" in types
    assert "agent.context_assembled" in types
    assert "agent.model_round" in types
    # The terminal translation lands running→succeeded BEFORE the safe
    # completed summary event (the observed producer order).
    finished = [e for e in parsed if e.type == "run.transition"][-1]
    assert finished.data["to"] == "succeeded"
    assert types[-1] == "agent.completed"
    context_event = next(e for e in parsed if e.type == "agent.context_assembled")
    assert context_event.data["evidence_items"] == 0
    whole = json.dumps([frame["data"] for frame in frames])
    assert "canary-artifact" not in whole
    assert "aspect-alpha" not in whole  # task text stays out of the stream
    last_cursor = frames[-1]["id"]
    await stream.close()

    # Reconnect from the cursor: strictly cursor-exclusive replay.
    reconnect = await open_stream(
        chat_app,
        f"/api/v1/chat/runs/{run_id}/events",
        headers={"last-event-id": last_cursor},
        cookies=jar,
    )
    await reconnect.wait_until(
        lambda body: reconnect.task.done(), 15, "the empty replay to end"
    )
    assert sse_frames(reconnect.body) == []
    await reconnect.close()

    # The conversation detail: replayed terminal state + recorded refs.
    detail = await call_app(
        chat_app, "GET", f"/api/v1/chat/runs/{run_id}", cookies=jar
    )
    assert detail.status == 200
    view = detail.json()
    assert view["state"] == "succeeded"
    # The mandatory preview rides the detail view: the terminal swap left
    # exactly the succeeded state tag (total=0 is contract-invalid).
    assert view["tag_preview"] == {"preview": ["succeeded"], "total": 1}
    assert len(view["attachments"]) == 1
    assert view["children"] == []


def _current_state_sync(url: str, run_id: uuid.UUID) -> str | None:
    return _run(_current_state(url, run_id))


async def test_live_delegation_tree_parent_and_child_events(
    chat_app, jar, server_a, bootstrapped_url, scenario
) -> None:
    """The writer half of the gate scenario, delegation half: submit with
    two explicit children → the parent delegates (real engine child
    edges), both children run their OWN scoped tasks, parent AND child
    events stream with replay cursors, and the detail view shows the
    persisted child rows."""
    _compose_worker_roots(scenario, server_a)
    _write_model_script(
        scenario,
        [
            {"tool_call": {"name": "fixture_read", "args": {"marker": "m1"}}},
            {"text": "done"},
        ],
    )

    run_id = uuid.uuid4()
    created = await submit_chat(
        chat_app,
        jar,
        run_id,
        server_a,
        message="coordinate the fixture review",
        allowed_capabilities=[READ_CAP],
        children=[
            {"task": "child aspect-alpha review", "capabilities": [READ_CAP]},
            {"task": "child aspect-beta review", "capabilities": [READ_CAP]},
        ],
    )
    assert created.status == 201, created.body
    assert created.json()["conversation"]["children"] == []

    worker = _spawn_worker(
        bootstrapped_url, scenario / "handlers", scenario, "w1"
    )
    try:
        _wait_until(
            lambda: _journal_count(scenario, "effect:fixture.read:m1") == 2,
            120,
            "each child's effect to happen exactly once",
        )
        _wait_until(
            lambda: _current_state_sync(bootstrapped_url, run_id) == "succeeded",
            120,
            "the parent tree to succeed",
        )
    finally:
        _stop(worker)

    journal = _journal_lines(scenario)
    assert "user_marker:aspect-alpha" in journal
    assert "user_marker:aspect-beta" in journal
    # Per-child scoping at the model boundary: each child's OWN prompt is
    # the only task source — the marker is journaled once per child (the
    # first round's user prompt; later rounds carry tool returns), and a
    # child's conversation can never contain its sibling's marker.
    assert journal.count("user_marker:aspect-alpha") == 1
    assert journal.count("user_marker:aspect-beta") == 1

    # Parent stream: delegation plan with the child identities + wake.
    parent_events = await read_events_direct(bootstrapped_url, run_id)
    planned = next(
        (e for e in parent_events if e.type == "agent.delegation_planned"), None
    )
    assert planned is not None
    child_ids = [str(item) for item in planned.data["children"]]
    assert len(child_ids) == 2
    assert "agent.children_resolved" in [e.type for e in parent_events]

    detail = await call_app(
        chat_app, "GET", f"/api/v1/chat/runs/{run_id}", cookies=jar
    )
    assert detail.status == 200
    view = detail.json()
    assert view["state"] == "succeeded"
    # The batched mandatory previews cover parent AND children: the
    # terminal swap left exactly the succeeded state tag on each (the
    # preview batch is resolved per parent server scope, total ≥ 1).
    assert view["tag_preview"] == {"preview": ["succeeded"], "total": 1}
    assert sorted(child["run_id"] for child in view["children"]) == sorted(child_ids)
    assert all(child["state"] == "succeeded" for child in view["children"])
    assert all(
        child["tag_preview"] == {"preview": ["succeeded"], "total": 1}
        for child in view["children"]
    )
    assert all(child["depth"] == 1 for child in view["children"])
    assert all(child["parent_run_id"] == str(run_id) for child in view["children"])

    # Child streams subscribe through the SAME route and replay the full
    # child sequence (agent.started → completed), cursor-exclusive.
    for child_id in child_ids:
        stream = await open_stream(
            chat_app, f"/api/v1/chat/runs/{child_id}/events", cookies=jar
        )
        await stream.wait_until(
            lambda body: stream.task.done(), 15, "the terminal child stream to close"
        )
        child_types = [
            frame["data"]["type"] for frame in sse_frames(stream.body)
        ]
        # A child row is inserted by the delegation executor (no submit
        # owner): its stream anchors at the observed submit edge, then
        # carries the full agent sequence.
        assert child_types[0] == "run.transition"
        assert "run.submitted" not in child_types
        assert "agent.started" in child_types
        assert "agent.completed" in child_types
        await stream.close()

    # The conversation list: the delegation parent is one root entry.
    listing = await call_app(
        chat_app, "GET", f"/api/v1/chat/runs?server_id={server_a}", cookies=jar
    )
    assert listing.status == 200
    page = listing.json()
    assert page["total"] == 1
    assert page["conversations"][0]["run_id"] == str(run_id)


async def test_live_budgeted_delegation_enforces_the_root_budget(
    chat_app, jar, server_a, bootstrapped_url, scenario
) -> None:
    """The submit-to-ledger journey: a root budget submitted through the
    REAL chat surface freezes on the run row and the REAL workers'
    delegation reserves the batch, charges each child's durable rounds
    and releases it on the root's stream (C05 rule 7 live)."""
    _compose_worker_roots(scenario, server_a)
    _write_model_script(
        scenario,
        [
            {"tool_call": {"name": "fixture_read", "args": {"marker": "m1"}}},
            {"text": "done"},
        ],
    )
    budget = {
        "max_depth": 4,
        "max_children": 4,
        "max_active": 3,
        "max_steps": 50,
        "max_total_tokens": 5_000,
    }
    run_id = uuid.uuid4()
    created = await submit_chat(
        chat_app,
        jar,
        run_id,
        server_a,
        message="coordinate the budgeted fixture review",
        allowed_capabilities=[READ_CAP],
        children=[
            {"task": "child aspect-alpha review", "capabilities": [READ_CAP]},
            {"task": "child aspect-beta review", "capabilities": [READ_CAP]},
        ],
        root_budget=budget,
    )
    assert created.status == 201, created.body

    worker = _spawn_worker(bootstrapped_url, scenario / "handlers", scenario, "w1")
    try:
        _wait_until(
            lambda: _current_state_sync(bootstrapped_url, run_id) == "succeeded",
            120,
            "the budgeted delegation tree to succeed",
        )
    finally:
        _stop(worker)

    with psycopg.connect(bootstrapped_url, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT root_budget FROM job_runs WHERE run_id = %s", (run_id,)
        ).fetchone()
        assert row is not None
        assert row["root_budget"] == {"v": BUDGET_PAYLOAD_VERSION, **budget}
        child_rows = conn.execute(
            "SELECT run_id, root_budget FROM job_runs WHERE parent_run_id = %s "
            "ORDER BY run_id",
            (run_id,),
        ).fetchall()
    assert len(child_rows) == 2
    for child_row in child_rows:
        assert child_row["root_budget"] == {"v": BUDGET_PAYLOAD_VERSION, **budget}
    child_ids = sorted(str(child_row["run_id"]) for child_row in child_rows)

    events = await read_events_direct(bootstrapped_url, run_id)
    types = [event.type for event in events]
    reserved = next(
        event for event in events if event.type == BUDGET_RESERVED_EVENT_TYPE
    )
    assert reserved.data["owner"] == str(run_id)
    assert sorted(reserved.data["children"]) == child_ids
    charged = [event for event in events if event.type == BUDGET_CHARGED_EVENT_TYPE]
    assert sorted(str(event.data["run"]) for event in charged) == child_ids
    assert all(event.data["steps"] == 2 for event in charged)
    released = next(
        event for event in events if event.type == BUDGET_RELEASED_EVENT_TYPE
    )
    assert released.data["owner"] == str(run_id)
    assert types.index(BUDGET_RELEASED_EVENT_TYPE) < types.index("agent.delegation_completed")


async def test_cancel_records_request_and_stops_the_conversation(
    chat_app, jar, server_a, bootstrapped_url
) -> None:
    """Cancel composes the reviewed safe-cancel facade: the request is
    recorded once and a KNOWN terminal outcome is reported (an enqueued
    conversation that never started lands cancelled deterministically)."""
    run_id = uuid.uuid4()
    created = await submit_chat(
        chat_app, jar, run_id, server_a, allowed_capabilities=[READ_CAP]
    )
    assert created.status == 201, created.body

    cancel = await call_app(
        chat_app,
        "POST",
        f"/api/v1/chat/runs/{run_id}/cancel",
        json_body={},
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert cancel.status == 200, cancel.body
    verdict = cancel.json()
    assert verdict["run_id"] == str(run_id)
    assert verdict["outcome"] == "cancelled"
    assert verdict["requested_now"] is True
    assert await _current_state(bootstrapped_url, run_id) == "cancelled"

    # Re-entry is idempotent and honest.
    again = await call_app(
        chat_app,
        "POST",
        f"/api/v1/chat/runs/{run_id}/cancel",
        json_body={},
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert again.status == 200
    assert again.json()["requested_now"] is False


async def test_session_continuation_and_steering_keep_single_history_entry(
    chat_app, jar, server_a, bootstrapped_url
) -> None:
    """Steering an active run keeps the same run_id and records chat.steered;
    continuing a finished run via session_run_id supersedes the old run in
    list_conversations so Chat History shows one entry per session."""
    first_id = uuid.uuid4()
    created = await submit_chat(
        chat_app, jar, first_id, server_a, message="initial session turn"
    )
    assert created.status == 201, created.body

    # While first_id is still queued/non-terminal, submitting with
    # session_run_id=first_id steers the existing run in place (200 OK).
    steer_id = uuid.uuid4()
    steered = await call_app(
        chat_app,
        "POST",
        "/api/v1/chat/messages",
        json_body={
            "run_id": str(steer_id),
            "server_id": str(server_a),
            "session_run_id": str(first_id),
            "message": "steer the active session",
            "profile": "fast",
            "allowed_capabilities": [READ_CAP],
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert steered.status == 200, steered.body
    steered_body = steered.json()
    assert steered_body["conversation"]["run_id"] == str(first_id)
    events = await read_events_direct(bootstrapped_url, first_id)
    assert any(e.type == "chat.steered" for e in events)

    # Cancel first_id so it becomes terminal, then submit a follow-up turn
    # referencing session_run_id=first_id.
    cancel = await call_app(
        chat_app,
        "POST",
        f"/api/v1/chat/runs/{first_id}/cancel",
        json_body={},
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert cancel.status == 200

    followup_id = uuid.uuid4()
    followup = await call_app(
        chat_app,
        "POST",
        "/api/v1/chat/messages",
        json_body={
            "run_id": str(followup_id),
            "server_id": str(server_a),
            "session_run_id": str(first_id),
            "message": "follow up after terminal",
            "profile": "fast",
            "allowed_capabilities": [READ_CAP],
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert followup.status == 201, followup.body
    assert followup.json()["conversation"]["run_id"] == str(followup_id)
    assert followup.json()["conversation"]["session_run_id"] == str(first_id)

    # Listing conversations returns only followup_id for this session, not first_id.
    listed = await call_app(
        chat_app,
        "GET",
        f"/api/v1/chat/runs?server_id={server_a}",
        cookies=jar,
    )
    assert listed.status == 200
    listed_ids = [row["run_id"] for row in listed.json()["conversations"]]
    assert str(followup_id) in listed_ids
    assert str(first_id) not in listed_ids


async def test_rename_conversation_persists_and_carries_across_followup(
    chat_app, jar, server_a
) -> None:
    """PATCH /api/v1/chat/runs/{run_id} stores a durable title on the run
    and carries it forward when a terminal session is continued."""
    run_id = uuid.uuid4()
    created = await submit_chat(
        chat_app, jar, run_id, server_a, message="initial turn to rename"
    )
    assert created.status == 201, created.body
    assert created.json()["conversation"].get("title") is None

    renamed = await call_app(
        chat_app,
        "PATCH",
        f"/api/v1/chat/runs/{run_id}",
        json_body={"title": "  Production Nginx Audit  "},
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert renamed.status == 200, renamed.body
    assert renamed.json()["title"] == "Production Nginx Audit"

    # Read and list reflect the new title.
    read_back = await call_app(
        chat_app, "GET", f"/api/v1/chat/runs/{run_id}", cookies=jar
    )
    assert read_back.status == 200
    assert read_back.json()["title"] == "Production Nginx Audit"

    # Cancel and continue the session; the follow-up run inherits the title.
    await call_app(
        chat_app,
        "POST",
        f"/api/v1/chat/runs/{run_id}/cancel",
        json_body={},
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    followup_id = uuid.uuid4()
    followup = await call_app(
        chat_app,
        "POST",
        "/api/v1/chat/messages",
        json_body={
            "run_id": str(followup_id),
            "server_id": str(server_a),
            "session_run_id": str(run_id),
            "message": "continue the renamed session",
            "profile": "fast",
            "allowed_capabilities": [READ_CAP],
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert followup.status == 201, followup.body
    assert followup.json()["conversation"]["title"] == "Production Nginx Audit"


# ---------------------------------------------------------------------------
# modern canonical turn submission (05-turn-submit; C1/C4/C6/C8 regressions)
# ---------------------------------------------------------------------------


async def submit_turn(
    app: FastAPI,
    jar: dict[str, str],
    client_request_id: uuid.UUID,
    server: uuid.UUID,
    *,
    message: str = "investigate the fixture service",
    profile: str | None = "fast",
    conversation_id: uuid.UUID | None = None,
    **extra: Any,
) -> HttpResponse:
    """One modern §3 submit (create or follow-up) through the real ASGI app."""
    body: dict[str, Any] = {
        "server_id": str(server),
        "client_request_id": str(client_request_id),
        "message": message,
    }
    if profile is not None:
        body["profile"] = profile
    body.update(extra)
    path = (
        "/api/v1/chat/conversations"
        if conversation_id is None
        else f"/api/v1/chat/conversations/{conversation_id}/messages"
    )
    return await call_app(
        app,
        "POST",
        path,
        json_body=body,
        headers=_mutation_headers(jar),
        cookies=jar,
    )


def _conversation_rows(url: str, conversation_id: uuid.UUID) -> list[dict]:
    """The canonical transcript rows of one conversation (assertion aid)."""
    with psycopg.connect(url, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT message_id, conversation_id, seq, role, source_kind, content, "
            "redacted, content_fingerprint, client_request_id "
            "FROM conversation_messages WHERE conversation_id = %s ORDER BY seq",
            (conversation_id,),
        ).fetchall()


def _conversation_links(url: str, conversation_id: uuid.UUID) -> list[dict]:
    """The conversation's run links (assertion aid)."""
    with psycopg.connect(url, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT run_id, released_at FROM conversation_runs "
            "WHERE conversation_id = %s ORDER BY created_at, run_id",
            (conversation_id,),
        ).fetchall()


def _conversation_row(url: str, conversation_id: uuid.UUID) -> dict | None:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT conversation_id, server_id, active_run_id, active_task_id, "
            "history_completeness FROM conversations WHERE conversation_id = %s",
            (conversation_id,),
        ).fetchone()


async def test_modern_create_admits_first_turn_atomically(
    chat_app, jar, server_a, bootstrapped_url
) -> None:
    """C1/AC1: the first turn records the canonical conversation, the user
    message row and the enqueue-intent ownership with ONE identity (the
    client_request_id), then durably enqueues through the real engine."""
    crid = uuid.uuid4()
    created = await submit_turn(
        chat_app,
        jar,
        crid,
        server_a,
        message="Plan only; do not change files",
    )
    assert created.status == 201, created.body
    body = created.json()
    assert body["conversation_id"] == str(crid)
    assert body["run_id"] == str(crid)
    assert body["task_id"] is None
    assert body["revision"] == 1
    assert body["disposition"] == "created"
    message_id = uuid.UUID(body["message_id"])

    conversation = _conversation_row(bootstrapped_url, crid)
    assert conversation is not None
    assert conversation["server_id"] == server_a
    assert conversation["history_completeness"] == "complete"
    assert conversation["active_run_id"] == crid
    assert conversation["active_task_id"] is None

    rows = _conversation_rows(bootstrapped_url, crid)
    assert len(rows) == 1
    stored = rows[0]
    assert stored["message_id"] == message_id
    assert stored["seq"] == 1
    assert stored["role"] == "user"
    assert stored["source_kind"] == "api"
    assert stored["content"] == "Plan only; do not change files"
    assert stored["redacted"] is False
    assert stored["client_request_id"] == crid

    links = _conversation_links(bootstrapped_url, crid)
    assert [(link["run_id"], link["released_at"] is None) for link in links] == [
        (crid, True)
    ]

    with psycopg.connect(bootstrapped_url, row_factory=dict_row) as conn:
        run = conn.execute(
            "SELECT model_profile, server_id FROM job_runs WHERE run_id = %s",
            (crid,),
        ).fetchone()
    assert run is not None
    assert run["model_profile"] == "fast"
    assert run["server_id"] == server_a

    events = await read_events_direct(bootstrapped_url, crid)
    types = [event.type for event in events]
    assert "run.submitted" in types and "chat.submitted" in types
    submitted = next(e for e in events if e.type == "chat.submitted")
    assert submitted.data["conversation_id"] == str(crid)
    assert submitted.data["message_id"] == str(message_id)
    # Direct-agent default: the canonical create runs agent.run_v2 with
    # its own objective episode, bound before the engine enqueue.
    assert submitted.data["handler"] == "agent.run_v2"
    with psycopg.connect(bootstrapped_url, row_factory=dict_row) as conn:
        episode = conn.execute(
            "SELECT o.objective_id, o.server_id, o.status, "
            "o.original_request FROM objective_runs r "
            "JOIN objectives o ON o.objective_id = r.objective_id "
            "WHERE r.run_id = %s",
            (crid,),
        ).fetchone()
    assert episode is not None
    assert episode["server_id"] == server_a
    assert episode["status"] == "active"
    assert episode["original_request"] == "Plan only; do not change files"
    # The engine enqueue followed the commit: the engine HOLDS the run.
    assert await _current_state(bootstrapped_url, crid) == "queued"


async def test_modern_create_pins_missing_profile_from_selection(
    chat_app, jar, server_a, bootstrapped_url, tmp_path
) -> None:
    """An omitted profile pins the controller's stored model selection at
    submit time; with no selection stored the seam fails closed (503)."""
    missing = await submit_turn(
        chat_app, jar, uuid.uuid4(), server_a, profile=None
    )
    assert missing.status == 503, missing.body
    assert _error(missing)["code"] == "unavailable"
    assert _conversation_row(bootstrapped_url, uuid.uuid4()) is None

    # Store a real selection under the app's data root (the same confined
    # store the lifespan built), then the omitted profile resolves to it.
    from mai.agents.models import SCHEMA_VERSION as MODEL_SCHEMA_VERSION
    from mai.agents.model_store import ModelConfigStore

    store = ModelConfigStore(tmp_path / "data-root")
    connection = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "name": "gateway",
        "base_url": "http://127.0.0.1:9/v1",
        "timeout_seconds": 5,
        "capabilities": {"tool_calls": True},
    }
    store.write_connection(
        "gateway", json.dumps(connection) + "\n", expected_revision=0
    )
    profile = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "name": "selected",
        "connection": "gateway",
        "model": "fixture-model",
        "max_tokens": 256,
    }
    store.write_profile("selected", json.dumps(profile) + "\n", expected_revision=0)
    store.set_selection("gateway", "selected", expected_revision=0)

    crid = uuid.uuid4()
    created = await submit_turn(
        chat_app, jar, crid, server_a, message="ask with the selected model",
        profile=None,
    )
    assert created.status == 201, created.body
    with psycopg.connect(bootstrapped_url, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT model_profile FROM job_runs WHERE run_id = %s", (crid,)
        ).fetchone()
    assert row is not None and row["model_profile"] == "selected"


async def test_modern_retry_deduplicates_and_conflict_fails_closed(
    chat_app, jar, server_a, bootstrapped_url
) -> None:
    """C4/AC1: an identical retransmission under the same
    client_request_id attaches to the SAME message and run (200
    deduplicated, no second row); any changed payload under the same key
    fails closed with the stable client_request_conflict."""
    crid = uuid.uuid4()
    first = await submit_turn(chat_app, jar, crid, server_a, message="original ask")
    assert first.status == 201, first.body
    original_message_id = first.json()["message_id"]

    retry = await submit_turn(chat_app, jar, crid, server_a, message="original ask")
    assert retry.status == 200, retry.body
    assert retry.json()["disposition"] == "deduplicated"
    assert retry.json()["run_id"] == str(crid)
    assert retry.json()["message_id"] == original_message_id

    conflicting = await submit_turn(
        chat_app, jar, crid, server_a, message="a different ask entirely"
    )
    assert conflicting.status == 409, conflicting.body
    assert _error(conflicting)["code"] == "client_request_conflict"

    changed_profile = await submit_turn(
        chat_app, jar, crid, server_a, message="original ask", profile="other"
    )
    assert changed_profile.status == 409, changed_profile.body
    assert _error(changed_profile)["code"] == "client_request_conflict"

    # Exactly ONE message row and ONE run survived every attempt.
    rows = _conversation_rows(bootstrapped_url, crid)
    assert len(rows) == 1 and rows[0]["content"] == "original ask"
    with psycopg.connect(bootstrapped_url, row_factory=dict_row) as conn:
        runs = conn.execute(
            "SELECT COUNT(*) AS n FROM job_runs WHERE run_id = %s", (crid,)
        ).fetchone()
    assert runs is not None and runs["n"] == 1
    assert await _current_state(bootstrapped_url, crid) == "queued"


async def test_modern_followup_reuses_conversation_and_records_only_new_text(
    chat_app, jar, server_a, bootstrapped_url
) -> None:
    """C1/AC2: a finished follow-up reuses the canonical conversation_id,
    records ONLY the actual new user text (no browser-stitched history)
    and chains the legacy session_run_id so the history stays ONE entry."""
    crid = uuid.uuid4()
    created = await submit_turn(
        chat_app,
        jar,
        crid,
        server_a,
        message="Plan only; do not change files",
    )
    assert created.status == 201, created.body

    # Finish the first run (writer lease released on the next admission).
    cancel = await call_app(
        chat_app,
        "POST",
        f"/api/v1/chat/runs/{crid}/cancel",
        json_body={},
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert cancel.status == 200, cancel.body

    followup_id = uuid.uuid4()
    followup = await submit_turn(
        chat_app,
        jar,
        followup_id,
        server_a,
        message="continue",
        conversation_id=crid,
    )
    assert followup.status == 201, followup.body
    body = followup.json()
    assert body["conversation_id"] == str(crid)
    assert body["run_id"] == str(followup_id)
    assert body["disposition"] == "created"

    rows = _conversation_rows(bootstrapped_url, crid)
    assert [row["content"] for row in rows] == [
        "Plan only; do not change files",
        "continue",
    ]
    assert [row["seq"] for row in rows] == [1, 2]
    assert rows[1]["client_request_id"] == followup_id

    # The follow-up run chains to the conversation's first run for the
    # legacy surface: one entry, the follow-up listed, the root superseded.
    events = await read_events_direct(bootstrapped_url, followup_id)
    submitted = next(e for e in events if e.type == "chat.submitted")
    assert submitted.data["session_run_id"] == str(crid)
    assert submitted.data["conversation_id"] == str(crid)
    listed = await call_app(
        chat_app,
        "GET",
        f"/api/v1/chat/runs?server_id={server_a}",
        cookies=jar,
    )
    assert listed.status == 200
    listed_ids = [row["run_id"] for row in listed.json()["conversations"]]
    assert str(followup_id) in listed_ids
    assert str(crid) not in listed_ids


async def test_modern_cross_server_fails_closed(
    chat_app, jar, server_a, bootstrapped_url
) -> None:
    """C8/AC2: every modern submission against another server's
    conversation identity fails closed with the scope-blind
    conversation_not_found and zero identity leakage."""
    server_b = await create_profile(chat_app, jar, "fixture-b")
    crid = uuid.uuid4()
    created = await submit_turn(chat_app, jar, crid, server_a, message="mine")
    assert created.status == 201, created.body

    foreign = await submit_turn(
        chat_app,
        jar,
        uuid.uuid4(),
        server_b,
        message="stolen continuation",
        conversation_id=crid,
    )
    assert foreign.status == 404, foreign.body
    error = _error(foreign)
    assert error["code"] == "conversation_not_found"
    assert str(server_a) not in json.dumps(error)

    # The create route: an idempotency key already bound to another
    # server's conversation scope is a scope-blind 404 as well.
    stolen = await submit_turn(chat_app, jar, crid, server_b, message="stolen create")
    assert stolen.status == 404, stolen.body
    assert _error(stolen)["code"] == "conversation_not_found"

    # Nothing was written for the rejected attempts.
    assert len(_conversation_rows(bootstrapped_url, crid)) == 1
    assert [link["run_id"] for link in _conversation_links(bootstrapped_url, crid)] == [
        crid
    ]
    with psycopg.connect(bootstrapped_url, row_factory=dict_row) as conn:
        foreign_runs = conn.execute(
            "SELECT COUNT(*) AS n FROM job_runs WHERE server_id = %s",
            (server_b,),
        ).fetchone()
    assert foreign_runs is not None and foreign_runs["n"] == 0


async def test_modern_concurrent_submissions_serialize(
    chat_app, jar, server_a, bootstrapped_url
) -> None:
    """C6/AC1: concurrent submissions against one conversation serialize
    deterministically — the first turn wins the writer lease, the second
    is honestly queued (202) against the active writer; identical
    concurrent retransmissions collapse onto one row."""
    crid = uuid.uuid4()
    created = await submit_turn(chat_app, jar, crid, server_a, message="first turn")
    assert created.status == 201, created.body

    # Finish the first run so its writer lease is stale bookkeeping: the
    # winner of the race below reconciles it away and takes the lease.
    cancel = await call_app(
        chat_app,
        "POST",
        f"/api/v1/chat/runs/{crid}/cancel",
        json_body={},
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert cancel.status == 200, cancel.body

    second_id, third_id = uuid.uuid4(), uuid.uuid4()
    both = await asyncio.gather(
        submit_turn(
            chat_app, jar, second_id, server_a,
            message="second turn", conversation_id=crid,
        ),
        submit_turn(
            chat_app, jar, third_id, server_a,
            message="third turn", conversation_id=crid,
        ),
    )
    statuses = sorted(response.status for response in both)
    assert statuses == [201, 202]
    winner = next(r for r in both if r.status == 201)
    queued = next(r for r in both if r.status == 202)
    assert queued.json()["disposition"] == "queued_followup"
    assert queued.json()["run_id"] is None
    assert queued.json()["conversation_id"] == str(crid)
    winner_id = uuid.UUID(winner.json()["run_id"])
    assert winner_id in (second_id, third_id)

    # Ordered turns, exactly one new run, no duplicate active runs.
    rows = _conversation_rows(bootstrapped_url, crid)
    assert [row["seq"] for row in rows] == [1, 2, 3]
    assert len({row["client_request_id"] for row in rows}) == 3
    links = _conversation_links(bootstrapped_url, crid)
    assert sorted(str(link["run_id"]) for link in links) == sorted(
        [str(crid), str(winner_id)]
    )
    conversation = _conversation_row(bootstrapped_url, crid)
    assert conversation is not None
    assert conversation["active_run_id"] == winner_id

    # Concurrent IDENTICAL creates collapse onto one conversation/row/run.
    twin = uuid.uuid4()
    pair = await asyncio.gather(
        submit_turn(chat_app, jar, twin, server_a, message="twin turn"),
        submit_turn(chat_app, jar, twin, server_a, message="twin turn"),
    )
    assert sorted(response.status for response in pair) == [200, 201]
    assert len({response.json()["message_id"] for response in pair}) == 1
    assert len(_conversation_rows(bootstrapped_url, twin)) == 1


async def test_modern_followup_under_live_lease_queues(
    chat_app, jar, server_a, bootstrapped_url
) -> None:
    """A message follow-up while a run holds the writer lease is admitted,
    recorded in order and honestly reported queued (202) — no competing
    executor starts (steering is the 07 seam)."""
    crid = uuid.uuid4()
    created = await submit_turn(chat_app, jar, crid, server_a, message="holds lease")
    assert created.status == 201, created.body

    followup_id = uuid.uuid4()
    queued = await submit_turn(
        chat_app,
        jar,
        followup_id,
        server_a,
        message="arrives mid-run",
        conversation_id=crid,
    )
    assert queued.status == 202, queued.body
    body = queued.json()
    assert body["disposition"] == "queued_followup"
    assert body["run_id"] is None
    assert body["conversation_id"] == str(crid)

    # The turn is durably recorded (ordered), but no run attached.
    rows = _conversation_rows(bootstrapped_url, crid)
    assert [row["content"] for row in rows] == ["holds lease", "arrives mid-run"]
    assert [link["run_id"] for link in _conversation_links(bootstrapped_url, crid)] == [
        crid
    ]
    with psycopg.connect(bootstrapped_url, row_factory=dict_row) as conn:
        runs = conn.execute(
            "SELECT COUNT(*) AS n FROM job_runs WHERE run_id = %s", (followup_id,)
        ).fetchone()
    assert runs is not None and runs["n"] == 0


async def test_modern_new_task_conflicts_under_live_lease(
    chat_app, jar, server_a, bootstrapped_url
) -> None:
    """§1 one-writer lease: an exclusive new episode cannot start while a
    run holds the conversation lease (structured conversation_busy)."""
    crid = uuid.uuid4()
    created = await submit_turn(chat_app, jar, crid, server_a, message="hold the lease")
    assert created.status == 201, created.body

    rejected = await submit_turn(
        chat_app,
        jar,
        uuid.uuid4(),
        server_a,
        message="switch goal mid-flight",
        conversation_id=crid,
        task_action="new_task",
    )
    assert rejected.status == 409, rejected.body
    assert _error(rejected)["code"] == "conversation_busy"
    assert len(_conversation_rows(bootstrapped_url, crid)) == 1


async def test_modern_expected_revision_cas_fails_closed(
    chat_app, jar, server_a, bootstrapped_url
) -> None:
    """A stale expected_revision is a structured 409 BEFORE any store
    write; the initial revision pins admit the turn."""
    crid = uuid.uuid4()
    stale = await submit_turn(
        chat_app, jar, crid, server_a, message="stale pin", expected_revision=2
    )
    assert stale.status == 409, stale.body
    assert _error(stale)["code"] == "revision_conflict"
    assert _conversation_row(bootstrapped_url, crid) is None

    pinned = await submit_turn(
        chat_app, jar, crid, server_a, message="current pin", expected_revision=1
    )
    assert pinned.status == 201, pinned.body
    assert pinned.json()["revision"] == 1


async def test_modern_submit_boundary_session_origin_csrf(
    chat_app, jar, server_a
) -> None:
    """The modern seam sits behind the same session → origin → CSRF
    boundary as the legacy route (ADR §5 order), before any effect."""
    unauthenticated = await call_app(
        chat_app,
        "POST",
        "/api/v1/chat/conversations",
        json_body={
            "server_id": str(server_a),
            "client_request_id": str(uuid.uuid4()),
            "message": "no session",
            "profile": "fast",
        },
    )
    assert unauthenticated.status == 401

    evil = await call_app(
        chat_app,
        "POST",
        "/api/v1/chat/conversations",
        json_body={
            "server_id": str(server_a),
            "client_request_id": str(uuid.uuid4()),
            "message": "evil origin",
            "profile": "fast",
        },
        headers={"origin": EVIL_ORIGIN},
        cookies=jar,
    )
    assert evil.status == 403

    no_csrf = await call_app(
        chat_app,
        "POST",
        "/api/v1/chat/conversations",
        json_body={
            "server_id": str(server_a),
            "client_request_id": str(uuid.uuid4()),
            "message": "missing csrf",
            "profile": "fast",
        },
        headers={"origin": ORIGIN},
        cookies=jar,
    )
    assert no_csrf.status == 403
