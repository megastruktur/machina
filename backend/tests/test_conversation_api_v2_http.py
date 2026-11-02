"""Focused HTTP/SSE tests for the runtime-v2 conversation/objective API.

Card ``conversation-api`` (campaign runtime-v2) against
`docs/architecture/runtime-v2.md` **§3/§5/§8/§9/§10/§11** and the task
AC1–AC6. Every claim runs through the REAL ASGI app (``create_app`` +
lifespan + middleware) against a real disposable PostgreSQL
(``MAI_TEST_DATABASE_URL``) through the real Alembic chain. The live
halves spawn REAL worker subprocesses composing the REAL agent-run/v2
executors with a deterministic FunctionModel fake and the real
dispatcher/policy/approval machinery — no mocked feature-under-test;
the only external dependency is the disposable database.

Journeys (task §4):
1. v2 submit + idempotency dedup + dual legacy/v2 ownership prevention.
2. objective creation, CAS scope update (operator-only), journal &
   checklist endpoints, multi-page activity pagination.
3. SSE live stream + disconnect/reconnect cursor-exclusive replay with
   redaction/truncation verification.
4. state-specific steer, pause/resume, cancel, approval-bound resume
   (payload mismatch rejection), invalid resume rejection (including
   ``reconciling``/``outcome_unknown`` blind-resume refusal).
5. unauthenticated (401) and cross-origin/missing-CSRF (403) rejection.
6. legacy conversation read compatibility and drain-status inventory.

Registered limitations: no live provider (FunctionModel fake proves MAI
plumbing per C06 rule 6), no real SSH target, no browser.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from psycopg.rows import dict_row
from fastapi import FastAPI

from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE
from mai.api.chat import _record_chat_submission
from mai.api.jobs import _record_submit_intent
from mai.contracts.jobs import TERMINAL_RUN_STATES, RunState
from mai.database import create_app_engine
from mai.jobs.lifecycle import RunLifecycle

BACKEND_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.anyio

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: Synthetic fixture credential (NOT a secret; disposable fixture DB only).
FIXTURE_PASSWORD = "fixture-password-rv-conversation-api-only"
ORIGIN = "http://127.0.0.1:8443"  # loopback allowlist entry
EVIL_ORIGIN = "https://attacker.example"

READ_CAP = "fixture.read"
PRIV_CAP = "fixture.priv"

#: A fake secret-shaped token for redaction proofs (NOT a real credential).
FAKE_SECRET = "sk-rvfixtureredaction0123456789abcdef"
SECRET_MARKER = "[REDACTED]"

HANDLER_MODULE_NAME = "conversation_api_v2_handlers"


# ---------------------------------------------------------------------------
# persistent test loop + ASGI harness (the proven chat-suite shape)
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
        if not call._request_consumed:
            call._request_consumed = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()  # connected until close() cancels
        return {"type": "http.disconnect"}  # pragma: no cover

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
    assert bootstrap.status in (201, 409), bootstrap.body
    login = await call_app(
        app,
        "POST",
        "/api/v1/auth/login",
        json_body={"password": FIXTURE_PASSWORD},
        headers={"origin": ORIGIN},
    )
    assert login.status == 204, login.body
    jar: dict[str, str] = {}
    for key, value in login.headers:
        if key.decode("ascii").lower() == "set-cookie":
            cookie_name, _, rest = value.decode("ascii").partition("=")
            jar[cookie_name] = rest.split(";", 1)[0]
    assert SESSION_COOKIE in jar and CSRF_COOKIE in jar
    return jar


def _mutation_headers(jar: dict[str, str], origin: str = ORIGIN) -> dict[str, str]:
    return {"origin": origin, "x-csrf-token": jar[CSRF_COOKIE]}


async def create_profile(
    app: FastAPI,
    jar: dict[str, str],
    name: str,
    *,
    capabilities: list[str] | None = None,
) -> uuid.UUID:
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
            "capabilities": capabilities if capabilities is not None else [],
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert response.status == 201, response.body
    return uuid.UUID(response.json()["server_id"])


async def put_grants(
    app: FastAPI, jar: dict[str, str], server: uuid.UUID, revision: int = 0
) -> None:
    """Grant the fixture capabilities through the REAL grants surface."""
    response = await call_app(
        app,
        "PUT",
        "/api/v1/grants",
        json_body={
            "expected_revision": revision,
            "denied_capabilities": [],
            "grants": [
                {"capability_id": READ_CAP, "server_ids": [str(server)]},
                {"capability_id": PRIV_CAP, "server_ids": [str(server)]},
            ],
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert response.status == 200, response.body


# ---------------------------------------------------------------------------
# the worker-side composition (real executors, scripted fake model)
# ---------------------------------------------------------------------------

HANDLER_MODULE_SOURCE = '''
"""Worker-side composition for conversation-api v2 live scenarios: real
dispatcher/policy/approval/model-store machinery, journaled fixture
effects (gate + unknown-status behaviors), deterministic FunctionModel
fake, and the runtime-v2 handler registration."""

import asyncio
import json
import os
import time
import uuid

from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import mai.agents.run as agent_run
from mai.agents.model_store import ModelConfigStore
from mai.capabilities.dispatch import CapabilityDispatcher, ExecutorResult
from mai.credentials.env import EnvResolver
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
                "required": ["status"],
                "properties": {"status": {"type": "string"}},
            },
            "execution_environments": ["linux_ssh"],
            "effect_class": effect_class,
            "resource_scope": [] if effect_class == "read" else ["target.exec"],
            "required_grants": [],
        },
        source=f"<{capability_id} descriptor>",
    )


class FixtureExecutor:
    """Journaled disposable target: effect markers make exactly-once
    observable; behaviors keyed ``capability:marker`` then ``capability``:
    ``ok`` | ``gate`` | ``unknown_status``. Every output carries a fake
    secret-shaped token plus oversized padding so redaction/truncation is
    observable end to end."""

    def __init__(self):
        self.journal = os.environ["MAI_FX_JOURNAL"]
        self.gate_dir = os.environ["MAI_FX_GATE_DIR"]
        self.behaviors = json.loads(os.environ.get("MAI_FX_BEHAVIOR", "{}"))

    def _mark(self, marker):
        with open(self.journal, "a", encoding="utf-8") as handle:
            handle.write(marker + "\\n")

    def _behavior(self, capability_id, marker):
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
            "stdout": (
                "leak token sk-rvfixtureredaction0123456789abcdef end "
                + "x" * 5000
            ),
            "stderr": "",
        }
        return ExecutorResult(
            ok=status == "succeeded", output=json.dumps(document, sort_keys=True)
        )


def _script_model():
    script = json.loads(os.environ["MAI_FX_MODEL_SCRIPT"])
    journal = os.environ["MAI_FX_JOURNAL"]

    def _mark(line):
        with open(journal, "a", encoding="utf-8") as handle:
            handle.write(line + "\\n")

    def _entry_payload(entry):
        if entry.get("gate"):
            gate = os.path.join(os.environ["MAI_FX_GATE_DIR"], entry["gate"])
            deadline = time.time() + 300
            while not os.path.exists(gate):
                if time.time() > deadline:
                    raise RuntimeError("model gate timeout")
                time.sleep(0.1)
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
        for message in reversed(messages):
            if message.kind == "request":
                for part in message.parts:
                    if getattr(part, "part_kind", "") == "user-prompt":
                        text = getattr(part, "content", "")
                        for marker in ("aspect-alpha", "aspect-beta"):
                            if marker in text:
                                _mark(f"user_marker:{marker}")
                break
        index = (len(messages) - 1) // 2
        if index >= len(script):
            raise AssertionError("model script exhausted")
        return _entry_payload(script[index])

    async def _stream_fn(messages, info):
        response = await asyncio.to_thread(_model_fn, messages, info)
        for index, part in enumerate(response.parts):
            if isinstance(part, ToolCallPart):
                yield {
                    index: DeltaToolCall(
                        name=part.tool_name,
                        json_args=part.args_as_json_str(),
                        tool_call_id=part.tool_call_id,
                    )
                }
            elif isinstance(part, TextPart):
                yield part.content

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
    agent_run.configure_agent_run(
        agent_run.AgentRunExecutor(
            sessions=sessions,
            dispatcher=dispatcher,
            registry=registry,
            model_store=model_store,
            data_root=os.environ["MAI_FX_DATA_ROOT"],
            locks=ResourceLockService(url),
            resolver=EnvResolver(os.environ["MAI_FX_ENV"]),
            model_builder=lambda **kwargs: fake_model,
        )
    )
    # The runtime-v2 handler registers itself through the ONE engine seam
    # at import and drives the SAME executor seam.
    import mai.agents.run_v2  # noqa: F401


_build()
'''


# ---------------------------------------------------------------------------
# disposable database + app fixtures
# ---------------------------------------------------------------------------

FIXTURE_ROOT = Path("/tmp/mai-fxt-rv-conversation-api")

_TRUNCATE_TABLES = (
    # v2 objective tables first: they reference job_runs (objective_runs)
    # and conversations; deleting them before the facade keeps the FK
    # graph acyclic in this list order.
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
)


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(os.path.join(BACKEND_ROOT, "alembic.ini"))


def _drop_app_tables(url: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the conversation-api v2 claims need "
            "a real disposable Postgres fixture"
        )
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url
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
    from mai.jobs import engine as jobs_engine

    jobs_engine.launch_worker(migrated_url)
    jobs_engine.shutdown()
    _seed_terminal_catalog(migrated_url)
    return migrated_url


async def _truncate(url: str) -> None:
    from sqlalchemy import text

    engine, _sessions = create_app_engine(url)
    try:
        async with engine.begin() as conn:
            for table in _TRUNCATE_TABLES:
                present = await conn.execute(
                    text("SELECT to_regclass(:t) IS NOT NULL"),
                    {"t": f"public.{table}"},
                )
                if not present.scalar():
                    continue
                await conn.execute(text(f"DELETE FROM {table}"))
    finally:
        await engine.dispose()


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
async def v2_app(bootstrapped_url: str, tmp_path: Path):
    await _truncate(bootstrapped_url)
    app = _build_app(bootstrapped_url, tmp_path / "data-root")
    # A bounded controller-side model selection so the canonical submit
    # path can pin a profile (the C06 gate fires after profile resolution).
    from mai.agents.model_store import ModelConfigStore

    store = ModelConfigStore(tmp_path / "data-root")
    connection: dict[str, object] = {
        "schema_version": 1,
        "name": "gateway",
        "base_url": "http://127.0.0.1:9/v1",
        "timeout_seconds": 5,
        "capabilities": {"tool_calls": True},
    }
    store.write_connection(
        "gateway", json.dumps(connection) + "\n", expected_revision=0
    )
    profile: dict[str, object] = {
        "schema_version": 1,
        "name": "fast",
        "connection": "gateway",
        "model": "fixture-model",
        "max_tokens": 256,
    }
    store.write_profile("fast", json.dumps(profile) + "\n", expected_revision=0)
    store.set_selection("gateway", "fast", expected_revision=0)
    async with _running(app):
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()


@pytest.fixture
async def jar(v2_app: FastAPI) -> dict[str, str]:
    return await login_session(v2_app)


@pytest.fixture
async def server_a(v2_app: FastAPI, jar: dict[str, str]) -> uuid.UUID:
    server = await create_profile(v2_app, jar, "v2-fixture-a")
    await put_grants(v2_app, jar, server)
    return server


SERVER_B = uuid.uuid4()


# ---------------------------------------------------------------------------
# worker + scenario helpers (per-test disposable composition)
# ---------------------------------------------------------------------------


def _wait_until(predicate: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


class Scenario:
    """One disposable worker composition (roots, policy, journal, gates)."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "scenario"
        (self.root / "gates").mkdir(parents=True)
        handlers = self.root / "handlers"
        handlers.mkdir()
        (handlers / f"{HANDLER_MODULE_NAME}.py").write_text(
            HANDLER_MODULE_SOURCE, encoding="utf-8"
        )
        self.worker: subprocess.Popen | None = None

    @property
    def journal(self) -> Path:
        return self.root / "journal.log"

    def lines(self) -> list[str]:
        try:
            return self.journal.read_text().splitlines()
        except FileNotFoundError:
            return []

    def count(self, marker: str) -> int:
        return sum(1 for line in self.lines() if line == marker)

    def write_policy(self, server: uuid.UUID) -> None:
        (self.root / "grants.json").write_text(
            json.dumps(
                [
                    {"capability_id": READ_CAP, "server_ids": [str(server)]},
                    {"capability_id": PRIV_CAP, "server_ids": [str(server)]},
                ]
            ),
            encoding="utf-8",
        )
        (self.root / "policy.json").write_text(
            json.dumps({"revision": 1, "denied": []}), encoding="utf-8"
        )

    def write_model_script(self, script: list[dict]) -> None:
        (self.root / "model-script.json").write_text(
            json.dumps(script), encoding="utf-8"
        )

    def set_behavior(self, behavior: dict[str, str]) -> None:
        (self.root / "behavior.json").write_text(
            json.dumps(behavior), encoding="utf-8"
        )

    def compose_roots(self, server: uuid.UUID) -> None:
        from mai.agents.model_store import ModelConfigStore
        from mai.profiles.files import ProfileFileStore

        model_root = self.root / "model-root"
        store = ModelConfigStore(model_root)
        connection: dict[str, object] = {
            "schema_version": 1,
            "name": "gateway",
            "base_url": "http://127.0.0.1:9/v1",
            "timeout_seconds": 5,
            "capabilities": {"tool_calls": True},
        }
        store.write_connection(
            "gateway", json.dumps(connection) + "\n", expected_revision=0
        )
        profile: dict[str, object] = {
            "schema_version": 1,
            "name": "fast",
            "connection": "gateway",
            "model": "fixture-model",
            "max_tokens": 256,
        }
        store.write_profile("fast", json.dumps(profile) + "\n", expected_revision=0)
        store.set_selection("gateway", "fast", expected_revision=0)
        data_root = self.root / "data-root"
        profile_store = ProfileFileStore(data_root)
        profile_text = (
            f'schema_version: 1\nserver_id: "{server}"\nname: "v2-fixture"\n'
            "prompt_revision: 3\nenvironment: linux_ssh\nssh:\n"
            '  host: "198.51.100.10"\n  port: 22\n  user: "deploy"\n'
            '  host_key: "SHA256:6VhOyUC6fvXW0aQ9WNe7Kx3nZZf+zxgupEWLoZXoTMU"\n'
            '  credential_ref: "MAI_SSH_KEY_FIXTURE"\nprivilege: none\n'
            "capabilities:\n  - fixture.read\n"
        )
        profile_store.write(server, profile_text, expected_revision=0)
        prompt_dir = data_root / "servers" / str(server)
        prompt_dir.mkdir(parents=True, exist_ok=True)
        prompt_dir.chmod(0o700)
        prompt_path = prompt_dir / "prompt.md"
        prompt_path.write_text("Answer briefly.\n", encoding="utf-8")
        os.chmod(prompt_path, 0o600)
        (data_root / "fixture.env").write_text(
            "MAI_SSH_KEY_FIXTURE=fixture-key-value-not-a-secret\n", encoding="utf-8"
        )
        os.chmod(data_root / "fixture.env", 0o600)
        self.write_policy(server)

    def spawn_worker(self, url: str) -> subprocess.Popen:
        env = dict(os.environ)
        env.pop(LIVE_URL_VAR, None)
        env["MAI_DATABASE_URL"] = url
        env["MAI_WORKER_HANDLERS"] = HANDLER_MODULE_NAME
        env["MAI_FX_JOURNAL"] = str(self.journal)
        env["MAI_FX_GATE_DIR"] = str(self.root / "gates")
        env["MAI_FX_GRANTS"] = str(self.root / "grants.json")
        env["MAI_FX_POLICY"] = str(self.root / "policy.json")
        env["MAI_FX_ENV"] = str(self.root / "data-root" / "fixture.env")
        env["MAI_FX_MODEL_ROOT"] = str(self.root / "model-root")
        env["MAI_FX_DATA_ROOT"] = str(self.root / "data-root")
        behavior = self.root / "behavior.json"
        env.pop("MAI_FX_BEHAVIOR", None)
        if behavior.exists():
            env["MAI_FX_BEHAVIOR"] = behavior.read_text(encoding="utf-8")
        script = self.root / "model-script.json"
        if script.exists():
            env["MAI_FX_MODEL_SCRIPT"] = script.read_text(encoding="utf-8")
        env["PYTHONPATH"] = os.pathsep.join(
            [str(self.root / "handlers"), *(p for p in [env.get("PYTHONPATH")] if p)]
        )
        log = open(self.root / "worker.log", "w")  # noqa: SIM115
        self.worker = subprocess.Popen(
            [sys.executable, "-m", "mai.jobs.worker", "--concurrency", "2"],
            cwd=str(BACKEND_ROOT),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        return self.worker

    def release_gate(self, capability: str, marker: str) -> None:
        (self.root / "gates" / f"gate-{capability}-{marker}").write_text("go")

    def stop(self) -> int:
        if self.worker is None or self.worker.poll() is not None:
            return 0
        self.worker.send_signal(signal.SIGTERM)
        try:
            return self.worker.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.worker.kill()
            return self.worker.wait(timeout=30)


@pytest.fixture
def scenario(tmp_path: Path) -> AsyncIterator[Scenario]:
    box = Scenario(tmp_path)
    yield box
    box.stop()


# ---------------------------------------------------------------------------
# HTTP journey helpers
# ---------------------------------------------------------------------------


async def create_objective(
    app: FastAPI,
    jar: dict[str, str],
    server: uuid.UUID,
    *,
    scope: list[uuid.UUID] | None = None,
    client_request_id: uuid.UUID | None = None,
    original_request: str = "Deploy the fixture service and verify health.",
    constraints: list[str] | None = None,
    **extra: Any,
) -> HttpResponse:
    body: dict[str, Any] = {
        "server_id": str(server),
        "original_request": original_request,
        "constraints": constraints or [],
        "scope_server_ids": [str(s) for s in (scope or [server])],
    }
    if client_request_id is not None:
        body["client_request_id"] = str(client_request_id)
    body.update(extra)
    return await call_app(
        app,
        "POST",
        "/api/v1/objectives",
        json_body=body,
        headers=_mutation_headers(jar),
        cookies=jar,
    )


async def submit_v2_conversation(
    app: FastAPI,
    jar: dict[str, str],
    server: uuid.UUID,
    objective_id: uuid.UUID,
    *,
    client_request_id: uuid.UUID | None = None,
    message: str = "Check the fixture service and report.",
    mode: str | None = None,
    profile: str = "fast",
) -> HttpResponse:
    body: dict[str, Any] = {
        "server_id": str(server),
        "client_request_id": str(client_request_id or uuid.uuid4()),
        "message": message,
        "objective_id": str(objective_id),
        "profile": profile,
    }
    if mode is not None:
        body["mode"] = mode
    return await call_app(
        app,
        "POST",
        "/api/v1/chat/conversations",
        json_body=body,
        headers=_mutation_headers(jar),
        cookies=jar,
    )


async def seed_legacy_conversation(
    url: str,
    server: uuid.UUID,
    *,
    client_request_id: uuid.UUID,
    message: str = "Legacy conversation turn.",
    profile: str = "fast",
    enqueue: bool = False,
    allowed_capabilities: tuple[str, ...] = ("fixture.read",),
) -> str:
    """Seed ONE historical legacy conversation through the INTERNAL
    store seams (runtime-v2 §10: history predating the direct-agent
    default was never created by today's canonical surface, so fixtures
    seed it the same way — there is deliberately NO public legacy
    selector).

    Real store writes only: the conversation row, the canonical message
    (``asubmit_turn``), the facade intent + ``agent.run`` submission
    record, the run link — and, with ``enqueue=True``, the frozen legacy
    payload through the ONE engine adapter so the REAL ``agent.run``
    worker can replay it. Returns the run id.
    """
    from mai.agents import run as agent_run
    from mai.agents.conversation_store import (
        MessageSourceKind,
        aattach_run,
        acreate_conversation,
        asubmit_turn,
    )
    from mai.api.chat import _canonical, _modern_turn_document
    from mai.jobs.engine import EngineClient
    from mai.policy.evaluate import ExecutionContext, PolicyMode

    engine, sessions = create_app_engine(url)
    try:
        await acreate_conversation(
            sessions, server_id=server, conversation_id=client_request_id
        )
        turn = await asubmit_turn(
            sessions,
            conversation_id=client_request_id,
            server_id=server,
            client_request_id=client_request_id,
            message=message,
            source_kind=MessageSourceKind.API,
        )
        now = datetime.now(timezone.utc)
        context = ExecutionContext(
            principal="seed-legacy-fixture-not-a-secret",
            server_id=server,
            run_id=client_request_id,
            parent_scope=None,
            profile_revision="1",
            prompt_revision="1",
            credential_references=(),
            allowed_capabilities=list(allowed_capabilities),
            mode=PolicyMode.DELEGATED,
        )
        engine_document = _modern_turn_document(
            run_id=str(client_request_id),
            server_id=str(server),
            context=context,
            profile=profile,
            task=turn.message.content,
        )
        agent_run.parse_agent_run_intent(_canonical(engine_document))
        payload_json = _canonical(engine_document)
        digest = hashlib.sha256(payload_json.encode("ascii")).hexdigest()
        async with sessions() as session, session.begin():
            await _record_submit_intent(
                session,
                run_id=str(client_request_id),
                server_id=str(server),
                profile_revision=1,
                prompt_revision=1,
                model_profile=profile,
                requested_grants=sorted(allowed_capabilities),
                digest=digest,
                label=next(iter(sorted(allowed_capabilities)), profile),
                now=now,
            )
            await _record_chat_submission(
                session,
                run_id=str(client_request_id),
                digest=digest,
                handler="agent.run",
                attachments=[],
                child_count=0,
                service_tags=[],
                now=now,
                conversation_id=client_request_id,
                message_id=turn.message.message_id,
            )
        await aattach_run(
            sessions,
            conversation_id=client_request_id,
            server_id=server,
            run_id=client_request_id,
        )
    finally:
        await engine.dispose()
    if enqueue:
        client = EngineClient(url)
        try:
            await asyncio.to_thread(
                client.enqueue_run,
                str(client_request_id),
                "agent.run",
                payload_json,
            )
        finally:
            client.close()
    return str(client_request_id)


def _wait_state(url: str, run_id: uuid.UUID, states: set[str], timeout: float) -> str:
    async def _state() -> str | None:
        engine, sessions = create_app_engine(url)
        try:
            value = await RunLifecycle(sessions).current_state(str(run_id))
            return None if value is None else value.value
        finally:
            await engine.dispose()

    def _check() -> bool:
        return _run(_state()) in states

    _wait_until(_check, timeout, f"run state in {states}")
    result = _run(_state())
    assert result is not None
    return result


def _read_step_row(url: str, run_id: uuid.UUID, seq: int) -> dict | None:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT step_id, seq, capability_id, state, receipt FROM job_steps "
            "WHERE run_id = %s ORDER BY seq",
            (run_id,),
        ).fetchall()
    for row in rows:
        if row["seq"] == seq:
            return row
    return None


def _run_event_types(url: str, run_id: uuid.UUID) -> list[str]:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT type FROM job_events WHERE run_id = %s ORDER BY seq",
            (run_id,),
        ).fetchall()
    return [row["type"] for row in rows]


def _run_event_data(url: str, run_id: uuid.UUID, event_type: str) -> dict:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT data FROM job_events WHERE run_id = %s AND type = %s "
            "ORDER BY seq DESC LIMIT 1",
            (run_id, event_type),
        ).fetchone()
    assert row is not None, f"missing event {event_type}"
    return row["data"]


def _objective_events(url: str, objective_id: uuid.UUID) -> list[dict]:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT seq, type, actor_kind, data FROM objective_events "
            "WHERE objective_id = %s ORDER BY seq",
            (objective_id,),
        ).fetchall()


def _wait_journal(scenario: Scenario, marker: str, timeout: float) -> None:
    """Wait until the fixture worker journaled one marker (effect in flight)."""
    _wait_until(lambda: scenario.count(marker) >= 1, timeout, marker)


def _conversation_runs(url: str, conversation_id: uuid.UUID) -> list[dict]:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT run_id, released_at FROM conversation_runs "
            "WHERE conversation_id = %s ORDER BY created_at, run_id",
            (conversation_id,),
        ).fetchall()


def _cookies(jar: dict[str, str]) -> dict[str, str]:
    return {"cookie": "; ".join(f"{k}={v}" for k, v in jar.items())}


# ---------------------------------------------------------------------------
# journey 2: objective surface (CAS authority, journal, checklist, pages)
# ---------------------------------------------------------------------------


class TestObjectiveSurface:
    async def test_create_idempotent_dedup_and_conflict(
        self, v2_app, jar, server_a
    ) -> None:
        key = uuid.uuid4()
        created = await create_objective(v2_app, jar, server_a, client_request_id=key)
        assert created.status == 201, created.body
        detail = created.json()
        assert detail["status"] == "active"
        assert detail["revision"] == 1
        assert detail["server_id"] == str(server_a)
        assert [s["server_id"] for s in detail["scope"]] == [str(server_a)]
        assert detail["scope"][0]["added_by_actor_kind"] == "operator"

        dedup = await create_objective(v2_app, jar, server_a, client_request_id=key)
        assert dedup.status == 200, dedup.body
        assert dedup.json()["objective_id"] == detail["objective_id"]

        conflict = await create_objective(
            v2_app,
            jar,
            server_a,
            client_request_id=key,
            original_request="A different verbatim request entirely.",
        )
        assert conflict.status == 409
        assert _error(conflict)["code"] == "client_request_conflict"

    async def test_scope_cas_operator_authority_and_conflict(
        self, v2_app, jar, server_a, bootstrapped_url
    ) -> None:
        created = await create_objective(v2_app, jar, server_a)
        oid = uuid.UUID(created.json()["objective_id"])

        expanded = await call_app(
            v2_app,
            "POST",
            f"/api/v1/objectives/{oid}/scope",
            json_body={
                "server_id": str(server_a),
                "expected_revision": 1,
                "add_server_ids": [str(SERVER_B)],
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert expanded.status == 200, expanded.body
        body = expanded.json()
        assert body["revision"] == 2
        assert {s["server_id"] for s in body["scope"]} == {
            str(server_a),
            str(SERVER_B),
        }
        events = _objective_events(bootstrapped_url, oid)
        scope_events = [e for e in events if e["type"] == "objective.scope_changed"]
        assert len(scope_events) == 1
        assert scope_events[0]["actor_kind"] == "operator"
        assert scope_events[0]["data"]["added_server_ids"] == [str(SERVER_B)]

        stale = await call_app(
            v2_app,
            "POST",
            f"/api/v1/objectives/{oid}/scope",
            json_body={
                "server_id": str(server_a),
                "expected_revision": 1,
                "add_server_ids": [str(uuid.uuid4())],
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert stale.status == 409
        assert _error(stale)["code"] == "revision_conflict"

        home_removal = await call_app(
            v2_app,
            "POST",
            f"/api/v1/objectives/{oid}/scope",
            json_body={
                "server_id": str(server_a),
                "expected_revision": 2,
                "remove_server_ids": [str(server_a)],
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert home_removal.status == 409

    async def test_journal_checklist_and_event_pagination(
        self, v2_app, jar, server_a
    ) -> None:
        created = await create_objective(
            v2_app,
            jar,
            server_a,
            constraints=["Only touch the fixture host."],
        )
        oid = uuid.UUID(created.json()["objective_id"])
        scope_query = f"?server_id={server_a}"

        journal = await call_app(
            v2_app,
            "GET",
            f"/api/v1/objectives/{oid}/journal{scope_query}",
            headers=_cookies(jar),
        )
        assert journal.status == 200
        doc = journal.json()
        # The journal is a projection: the verbatim goal/constraints live
        # on the objective row and are only REFERENCED here (§8.1).
        assert doc["objective_id"] == str(oid)
        assert doc["derived_seq"] >= 1
        assert doc["status"] == "active"
        detail = await call_app(
            v2_app,
            "GET",
            f"/api/v1/objectives/{oid}{scope_query}",
            headers=_cookies(jar),
        )
        assert detail.status == 200
        assert "deploy the fixture service" in detail.json()["original_request"].lower()
        assert detail.json()["constraints"] == ["Only touch the fixture host."]

        absent = await call_app(
            v2_app,
            "GET",
            f"/api/v1/objectives/{oid}/checklist{scope_query}",
            headers=_cookies(jar),
        )
        assert absent.status == 404
        assert _error(absent)["code"] == "checklist_absent"

        put = await call_app(
            v2_app,
            "PUT",
            f"/api/v1/objectives/{oid}/checklist",
            json_body={
                "server_id": str(server_a),
                "expected_revision": 0,
                "items": [{"text": "inspect unit", "done": False}],
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert put.status == 200, put.body
        assert put.json()["revision"] == 1

        stale = await call_app(
            v2_app,
            "PUT",
            f"/api/v1/objectives/{oid}/checklist",
            json_body={
                "server_id": str(server_a),
                "expected_revision": 0,
                "items": [{"text": "stale write", "done": True}],
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert stale.status == 409
        assert _error(stale)["code"] == "revision_conflict"

        # Multi-page activity pagination over the objective ledger:
        # 1 created + 1 + 6 checklist events = 8 total.
        for index in range(6):
            tick = await call_app(
                v2_app,
                "PUT",
                f"/api/v1/objectives/{oid}/checklist",
                json_body={
                    "server_id": str(server_a),
                    "expected_revision": index + 1,
                    "items": [{"text": f"step {index}", "done": False}],
                },
                headers=_mutation_headers(jar),
                cookies=jar,
            )
            assert tick.status == 200

        page1 = await call_app(
            v2_app,
            "GET",
            f"/api/v1/objectives/{oid}/events?server_id={server_a}&limit=3&offset=0",
            headers=_cookies(jar),
        )
        assert page1.status == 200
        doc1 = page1.json()
        assert doc1["total"] == 8
        assert doc1["has_more"] is True
        assert [i["sequence"] for i in doc1["items"]] == [1, 2, 3]

        page2 = await call_app(
            v2_app,
            "GET",
            f"/api/v1/objectives/{oid}/events?server_id={server_a}&limit=3&offset=3",
            headers=_cookies(jar),
        )
        doc2 = page2.json()
        assert [i["sequence"] for i in doc2["items"]] == [4, 5, 6]

        page3 = await call_app(
            v2_app,
            "GET",
            f"/api/v1/objectives/{oid}/events?server_id={server_a}&limit=3&offset=6",
            headers=_cookies(jar),
        )
        doc3 = page3.json()
        assert [i["sequence"] for i in doc3["items"]] == [7, 8]
        assert doc3["has_more"] is False

        cursor_page = await call_app(
            v2_app,
            "GET",
            f"/api/v1/objectives/{oid}/events?server_id={server_a}&after_seq=7",
            headers=_cookies(jar),
        )
        doc_cursor = cursor_page.json()
        assert doc_cursor["total"] == 1
        assert [i["sequence"] for i in doc_cursor["items"]] == [8]

        oversized = await call_app(
            v2_app,
            "GET",
            f"/api/v1/objectives/{oid}/events?server_id={server_a}&limit=201",
            headers=_cookies(jar),
        )
        assert oversized.status == 422

    async def test_scope_blind_objective_read_is_404(
        self, v2_app, jar, server_a
    ) -> None:
        created = await create_objective(v2_app, jar, server_a)
        oid = uuid.UUID(created.json()["objective_id"])
        foreign = await call_app(
            v2_app,
            "GET",
            f"/api/v1/objectives/{oid}?server_id={SERVER_B}",
            headers=_cookies(jar),
        )
        assert foreign.status == 404
        assert _error(foreign)["code"] == "objective_not_found"


# ---------------------------------------------------------------------------
# journey 1 live: v2 submission, dedup, dual-ownership prevention
# ---------------------------------------------------------------------------


class TestV2SubmissionJourneys:
    async def test_submit_dedup_and_dual_ownership_prevention(
        self, v2_app, jar, server_a, bootstrapped_url, scenario
    ) -> None:
        objective = await create_objective(v2_app, jar, server_a)
        oid = uuid.UUID(objective.json()["objective_id"])

        scenario.compose_roots(server_a)
        scenario.write_model_script(
            [
                {"tool_call": {"name": "fixture_read", "args": {"marker": "m1"}}},
                {"text": "investigation complete"},
            ]
        )
        scenario.set_behavior({"fixture.read:m1": "gate"})
        scenario.spawn_worker(bootstrapped_url)

        key = uuid.uuid4()
        submitted = await submit_v2_conversation(
            v2_app, jar, server_a, oid, client_request_id=key
        )
        assert submitted.status == 201, submitted.body
        view = submitted.json()
        run_id = uuid.UUID(view["run_id"])
        assert view["disposition"] == "created"
        assert view["conversation_id"] == str(key)

        # The durable intent selects agent.run_v2 explicitly (AC1).
        submission = _run_event_data(bootstrapped_url, run_id, "chat.submitted")
        assert submission["handler"] == "agent.run_v2"

        # The run goes live and parks on the fixture gate, holding the lease.
        _wait_state(bootstrapped_url, run_id, {"running"}, 60)
        links = _conversation_runs(bootstrapped_url, key)
        assert [str(link["run_id"]) for link in links] == [str(run_id)]
        assert all(link["released_at"] is None for link in links)

        # Dual ownership: a follow-up turn while the v2 run holds the
        # lease is a queued follow-up, never a second concurrent executor.
        followup = await call_app(
            v2_app,
            "POST",
            f"/api/v1/chat/conversations/{key}/messages",
            json_body={
                "server_id": str(server_a),
                "client_request_id": str(uuid.uuid4()),
                "message": "while the run holds the lease",
                "profile": "fast",
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert followup.status == 202, followup.body
        assert followup.json()["disposition"] == "queued_followup"
        assert followup.json()["run_id"] is None

        # A legacy-surface submission chained to this conversation steers
        # the SAME run instead of creating a competing legacy owner.
        legacy = await call_app(
            v2_app,
            "POST",
            "/api/v1/chat/messages",
            json_body={
                "run_id": str(uuid.uuid4()),
                "server_id": str(server_a),
                "message": "legacy surface follow-up",
                "profile": "fast",
                "session_run_id": str(key),
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert legacy.status == 200, legacy.body
        assert legacy.json()["conversation"]["run_id"] == str(run_id)
        links = _conversation_runs(bootstrapped_url, key)
        assert [str(link["run_id"]) for link in links] == [str(run_id)]

        # Same-key retransmission deduplicates; changed content conflicts.
        dup = await submit_v2_conversation(
            v2_app, jar, server_a, oid, client_request_id=key
        )
        assert dup.status == 200
        assert dup.json()["disposition"] == "deduplicated"
        assert dup.json()["run_id"] == str(run_id)
        conflict = await submit_v2_conversation(
            v2_app,
            jar,
            server_a,
            oid,
            client_request_id=key,
            message="a different message under the same idempotency key",
        )
        assert conflict.status == 409
        assert _error(conflict)["code"] == "client_request_conflict"

        # The v2 run binds the objective durably (the worker-side §3.3 seam).
        scenario.release_gate("fixture.read", "m1")
        _wait_state(bootstrapped_url, run_id, {"succeeded"}, 120)
        with psycopg.connect(bootstrapped_url, row_factory=dict_row) as conn:
            binding = conn.execute(
                "SELECT objective_id, handler, actor_kind, initiating_server_id "
                "FROM objective_runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()
        assert binding is not None
        assert str(binding["objective_id"]) == str(oid)
        assert binding["handler"] == "agent.run_v2"

        # The objective runs page reports the binding (AC1 read surface).
        runs_page = await call_app(
            v2_app,
            "GET",
            f"/api/v1/objectives/{oid}/runs?server_id={server_a}",
            headers=_cookies(jar),
        )
        assert runs_page.status == 200
        runs_doc = runs_page.json()
        assert runs_doc["total"] == 1
        assert runs_doc["items"][0]["run_id"] == str(run_id)
        assert runs_doc["items"][0]["handler"] == "agent.run_v2"

    async def test_foreign_scope_submission_fails_closed(
        self, v2_app, jar, server_a
    ) -> None:
        objective = await create_objective(v2_app, jar, server_a)
        oid = uuid.UUID(objective.json()["objective_id"])
        foreign = await call_app(
            v2_app,
            "POST",
            "/api/v1/chat/conversations",
            json_body={
                "server_id": str(SERVER_B),
                "client_request_id": str(uuid.uuid4()),
                "message": "cross-scope v2 submit",
                "objective_id": str(oid),
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        # Scope-blind fail-closed: either the unknown-server profile 404 or
        # the objective-scope 404 — never an identity leak (C8).
        assert foreign.status == 404

    async def test_v2_conversation_stays_v2_on_followup(
        self, v2_app, jar, server_a, bootstrapped_url, scenario
    ) -> None:
        objective = await create_objective(v2_app, jar, server_a)
        oid = uuid.UUID(objective.json()["objective_id"])
        scenario.compose_roots(server_a)
        # The follow-up run continues the SAME scripted conversation (the
        # model index derives from the durable transcript length), so the
        # script carries entries for both rounds.
        scenario.write_model_script(
            [{"text": "done"}, {"text": "continued done"}, {"text": "slack"}]
        )
        scenario.spawn_worker(bootstrapped_url)

        key = uuid.uuid4()
        first = await submit_v2_conversation(
            v2_app, jar, server_a, oid, client_request_id=key
        )
        assert first.status == 201
        _wait_state(
            bootstrapped_url,
            uuid.UUID(first.json()["run_id"]),
            {"succeeded"},
            120,
        )

        followup = await call_app(
            v2_app,
            "POST",
            f"/api/v1/chat/conversations/{key}/messages",
            json_body={
                "server_id": str(server_a),
                "client_request_id": str(uuid.uuid4()),
                "message": "continue the objective",
                "profile": "fast",
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert followup.status == 201, followup.body
        second_run = uuid.UUID(followup.json()["run_id"])
        submission = _run_event_data(bootstrapped_url, second_run, "chat.submitted")
        assert submission["handler"] == "agent.run_v2"
        _wait_state(bootstrapped_url, second_run, {"succeeded"}, 120)
        # No explicit objective_id was sent: the binding probe kept the
        # conversation on its objective episode.
        with psycopg.connect(bootstrapped_url, row_factory=dict_row) as conn:
            bindings = conn.execute(
                "SELECT objective_id FROM objective_runs WHERE run_id = %s",
                (second_run,),
            ).fetchall()
        assert [str(b["objective_id"]) for b in bindings] == [str(oid)]


# ---------------------------------------------------------------------------
# journey 3: objective activity SSE, cursor-exclusive reconnect, redaction
# ---------------------------------------------------------------------------


class TestObjectiveActivityStream:
    async def test_sse_replay_cursor_and_redaction(
        self, v2_app, jar, server_a, bootstrapped_url, scenario
    ) -> None:
        objective = await create_objective(v2_app, jar, server_a)
        oid = uuid.UUID(objective.json()["objective_id"])

        # The agent's OWN advisory checklist carries a secret-shaped token
        # and an oversized item: the projection redacts BEFORE persistence
        # and bounds at the frozen item length (§5.3; honest truncation).
        fenced_checklist = (
            "```machina-checklist\n"
            + json.dumps(
                {
                    "items": [
                        {
                            "text": f"token {FAKE_SECRET} end " + "y" * 800,
                            "done": False,
                        }
                    ]
                }
            )
            + "\n```"
        )
        scenario.compose_roots(server_a)
        scenario.write_model_script(
            [
                {"tool_call": {"name": "fixture_read", "args": {"marker": "m1"}}},
                {"text": f"{fenced_checklist}\nverification done"},
            ]
        )
        scenario.spawn_worker(bootstrapped_url)

        submitted = await submit_v2_conversation(
            v2_app, jar, server_a, oid, client_request_id=uuid.uuid4()
        )
        assert submitted.status == 201
        run_id = uuid.UUID(submitted.json()["run_id"])
        _wait_state(bootstrapped_url, run_id, {"succeeded"}, 120)

        # The agent-maintained checklist landed redacted and bounded.
        checklist = await call_app(
            v2_app,
            "GET",
            f"/api/v1/objectives/{oid}/checklist?server_id={server_a}",
            headers=_cookies(jar),
        )
        assert checklist.status == 200
        stored_item = checklist.json()["items"][0]["text"]
        assert FAKE_SECRET not in stored_item
        assert SECRET_MARKER in stored_item
        assert len(stored_item) <= 500

        stream_path = (
            f"/api/v1/objectives/{oid}/events/stream?server_id={server_a}"
        )
        stream = await open_stream(v2_app, stream_path, cookies=jar)
        try:
            await stream.wait_until(
                lambda body: "objective.checklist_updated" in body,
                60,
                "the agent-maintained checklist event",
            )
            frames = sse_frames(stream.body)
            assert len(frames) >= 3
            sequences = [int(frame["id"]) for frame in frames]
            assert sequences == sorted(sequences)
            assert len(set(sequences)) == len(sequences)
            # The secret-shaped token NEVER rides the ledger surface.
            assert FAKE_SECRET not in stream.body
            journal_frames = [
                f for f in frames if f["data"]["type"] == "objective.journal_updated"
            ]
            assert journal_frames, "the v2 run journaled its effects"
            cursor = sequences[-1]
        finally:
            await stream.close()

        # A fresh ledger event after the disconnect...
        expanded = await call_app(
            v2_app,
            "POST",
            f"/api/v1/objectives/{oid}/scope",
            json_body={
                "server_id": str(server_a),
                "expected_revision": 1,
                "add_server_ids": [str(SERVER_B)],
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert expanded.status == 200

        # ...replays cursor-EXCLUSIVELY on reconnect (Last-Event-ID).
        replay = await open_stream(
            v2_app,
            stream_path,
            headers={"last-event-id": str(cursor)},
            cookies=jar,
        )
        try:
            await replay.wait_until(
                lambda body: "objective.scope_changed" in body,
                30,
                "the post-disconnect scope event",
            )
            replay_frames = sse_frames(replay.body)
            replay_sequences = [int(f["id"]) for f in replay_frames]
            assert all(seq > cursor for seq in replay_sequences)
        finally:
            await replay.close()

        # The ``after_seq`` query cursor behaves identically (AC3).
        page = await call_app(
            v2_app,
            "GET",
            f"/api/v1/objectives/{oid}/events?server_id={server_a}&after_seq={cursor}",
            headers=_cookies(jar),
        )
        assert page.status == 200
        assert all(
            item["sequence"] > cursor for item in page.json()["items"]
        )


# ---------------------------------------------------------------------------
# journey 4: state-specific steer / pause / cancel / approval / resume
# ---------------------------------------------------------------------------


class TestStateSpecificControls:
    async def test_steer_reaches_live_v2_run(
        self, v2_app, jar, server_a, bootstrapped_url, scenario
    ) -> None:
        objective = await create_objective(v2_app, jar, server_a)
        oid = uuid.UUID(objective.json()["objective_id"])
        scenario.compose_roots(server_a)
        scenario.write_model_script(
            [
                {"tool_call": {"name": "fixture_read", "args": {"marker": "m1"}}},
                {"text": "steered investigation complete"},
            ]
        )
        scenario.set_behavior({"fixture.read:m1": "gate"})
        scenario.spawn_worker(bootstrapped_url)

        key = uuid.uuid4()
        submitted = await submit_v2_conversation(
            v2_app, jar, server_a, oid, client_request_id=key
        )
        run_id = uuid.UUID(submitted.json()["run_id"])
        _wait_state(bootstrapped_url, run_id, {"running"}, 60)
        # The gated effect is IN FLIGHT: the steering lands mid-run and is
        # consumed at the next safe boundary, never mid-effect.
        _wait_journal(scenario, "begin:fixture.read:m1", 60)

        steered = await call_app(
            v2_app,
            "POST",
            f"/api/v1/chat/conversations/{key}/steer",
            json_body={
                "conversation_id": str(key),
                "server_id": str(server_a),
                "action": "steer",
                "message": "focus on aspect-alpha please",
                "client_request_id": str(uuid.uuid4()),
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert steered.status == 201, steered.body
        assert steered.json()["delivery_status"] == "pending"

        scenario.release_gate("fixture.read", "m1")
        _wait_state(bootstrapped_url, run_id, {"succeeded"}, 120)
        # The steer is consumed exactly once at the safe boundary and the
        # NEXT model round sees it as user input (the journal marker).
        _wait_until(
            lambda: scenario.count("user_marker:aspect-alpha") == 1,
            60,
            "the model seeing the steering message",
        )
        assert scenario.count("user_marker:aspect-alpha") == 1

    async def test_pause_stops_at_boundary_and_resume_continues_checkpoint(
        self, v2_app, jar, server_a, bootstrapped_url, scenario
    ) -> None:
        objective = await create_objective(v2_app, jar, server_a)
        oid = uuid.UUID(objective.json()["objective_id"])
        scenario.compose_roots(server_a)
        scenario.write_model_script(
            [
                {"tool_call": {"name": "fixture_read", "args": {"marker": "m1"}}},
                {"tool_call": {"name": "fixture_read", "args": {"marker": "m2"}}},
                {"text": "paused point"},
                {"text": "resumed complete"},
                {"text": "slack entry"},
            ]
        )
        scenario.set_behavior({"fixture.read:m1": "gate"})
        scenario.spawn_worker(bootstrapped_url)

        key = uuid.uuid4()
        submitted = await submit_v2_conversation(
            v2_app, jar, server_a, oid, client_request_id=key
        )
        run_id = uuid.UUID(submitted.json()["run_id"])
        _wait_state(bootstrapped_url, run_id, {"running"}, 60)
        _wait_journal(scenario, "begin:fixture.read:m1", 60)

        paused = await call_app(
            v2_app,
            "POST",
            f"/api/v1/chat/conversations/{key}/steer",
            json_body={
                "conversation_id": str(key),
                "server_id": str(server_a),
                "action": "pause",
                "client_request_id": str(uuid.uuid4()),
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert paused.status == 201, paused.body

        scenario.release_gate("fixture.read", "m1")
        _wait_state(bootstrapped_url, run_id, {"succeeded"}, 120)
        # The pause won its boundary: the NEXT tool effect never began.
        assert scenario.count("begin:fixture.read:m1") == 1
        assert scenario.count("begin:fixture.read:m2") == 0
        summary = _run_event_data(bootstrapped_url, run_id, "agent.completed")
        assert summary.get("stopped_by") == "pause"

        # A no-content resume names the honest next action.
        empty_resume = await call_app(
            v2_app,
            "POST",
            f"/api/v1/chat/conversations/{key}/resume",
            json_body={"server_id": str(server_a)},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert empty_resume.status == 409
        assert _error(empty_resume)["code"] == "resume_not_applicable"

        # The operator resume FROM the last settled checkpoint is the
        # continuation turn: a NEW v2 run on the SAME objective episode.
        resumed = await call_app(
            v2_app,
            "POST",
            f"/api/v1/chat/conversations/{key}/resume",
            json_body={
                "server_id": str(server_a),
                "message": "continue from where the run paused",
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert resumed.status == 201, resumed.body
        resumed_run = uuid.UUID(resumed.json()["run_id"])
        assert resumed_run != run_id
        submission = _run_event_data(bootstrapped_url, resumed_run, "chat.submitted")
        assert submission["handler"] == "agent.run_v2"
        _wait_state(bootstrapped_url, resumed_run, {"succeeded"}, 120)
        with psycopg.connect(bootstrapped_url, row_factory=dict_row) as conn:
            bindings = conn.execute(
                "SELECT objective_id FROM objective_runs WHERE run_id = %s",
                (resumed_run,),
            ).fetchall()
        assert [str(b["objective_id"]) for b in bindings] == [str(oid)]

    async def test_cancel_stops_parked_v2_run(
        self, v2_app, jar, server_a, bootstrapped_url, scenario
    ) -> None:
        objective = await create_objective(v2_app, jar, server_a)
        oid = uuid.UUID(objective.json()["objective_id"])
        scenario.compose_roots(server_a)
        scenario.write_model_script(
            [
                {"tool_call": {"name": "fixture_priv", "args": {"marker": "m1"}}},
                {"text": "never reached"},
            ]
        )
        scenario.spawn_worker(bootstrapped_url)

        key = uuid.uuid4()
        submitted = await submit_v2_conversation(
            v2_app,
            jar,
            server_a,
            oid,
            client_request_id=key,
            mode="ask",
        )
        assert submitted.status == 201
        run_id = uuid.UUID(submitted.json()["run_id"])
        _wait_state(bootstrapped_url, run_id, {"waiting_approval"}, 120)
        # The refused pre-approval attempt never effected anything.
        assert scenario.count("effect:fixture.priv:m1") == 0

        cancelled = await call_app(
            v2_app,
            "POST",
            f"/api/v1/chat/runs/{run_id}/cancel",
            json_body={"cancel_children": True},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert cancelled.status in (200, 202), cancelled.body
        _wait_state(bootstrapped_url, run_id, {"cancelled"}, 120)
        # The parked deferral was never executed (exactly-once honesty).
        assert scenario.count("effect:fixture.priv:m1") == 0

    async def test_approval_bound_resume_and_payload_mismatch_rejection(
        self, v2_app, jar, server_a, bootstrapped_url, scenario
    ) -> None:
        from mai.capabilities.dispatch import canonical_payload_hash

        objective = await create_objective(v2_app, jar, server_a)
        oid = uuid.UUID(objective.json()["objective_id"])
        scenario.compose_roots(server_a)
        scenario.write_model_script(
            [
                {"tool_call": {"name": "fixture_priv", "args": {"marker": "m1"}}},
                {"text": "approved operation complete"},
            ]
        )
        scenario.spawn_worker(bootstrapped_url)

        key = uuid.uuid4()
        submitted = await submit_v2_conversation(
            v2_app,
            jar,
            server_a,
            oid,
            client_request_id=key,
            mode="ask",
        )
        assert submitted.status == 201, submitted.body
        run_id = uuid.UUID(submitted.json()["run_id"])

        # The refused pre-approval attempt parks the run durably.
        _wait_state(bootstrapped_url, run_id, {"waiting_approval"}, 120)
        step = _read_step_row(bootstrapped_url, run_id, 1)
        assert step is not None
        assert step["state"] == "failed"
        assert step["receipt"]["reason"] == "approval_required_but_absent"
        approval_id = uuid.UUID(step["receipt"]["approval_id"])
        payload = {"marker": "m1"}
        payload_hash = canonical_payload_hash(payload)
        assert step["receipt"]["payload_hash"] == payload_hash
        assert "agent.awaiting_approval" in _run_event_types(
            bootstrapped_url, run_id
        )
        assert scenario.count("effect:fixture.priv:m1") == 0

        preview = await call_app(
            v2_app,
            "GET",
            f"/api/v1/approvals/{approval_id}",
            headers=_cookies(jar),
        )
        assert preview.status == 200
        assert preview.json()["status"] == "pending"

        # The approval CANNOT retarget its payload: a decision whose
        # binding mismatches is a structured rejection, row untouched.
        mismatch = await call_app(
            v2_app,
            "POST",
            f"/api/v1/approvals/{approval_id}/decision",
            json_body={
                "capability_id": PRIV_CAP,
                "server_id": str(server_a),
                "payload_hash": canonical_payload_hash({"marker": "OTHER"}),
                "policy_revision": 1,
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert mismatch.status == 409
        assert _error(mismatch)["code"] == "payload_mismatch"

        # While parked, the ONLY resume is the payload-bound decision.
        blind_resume = await call_app(
            v2_app,
            "POST",
            f"/api/v1/chat/conversations/{key}/resume",
            json_body={
                "server_id": str(server_a),
                "message": "generic resume attempt",
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert blind_resume.status == 409
        assert _error(blind_resume)["code"] == "approval_decision_required"

        decided = await call_app(
            v2_app,
            "POST",
            f"/api/v1/approvals/{approval_id}/decision",
            json_body={
                "capability_id": PRIV_CAP,
                "server_id": str(server_a),
                "payload_hash": payload_hash,
                "policy_revision": 1,
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert decided.status == 200, decided.body
        # The wake path leaves the spend to the worker-side
        # dispatch_once: the view reports the pending row + the wake.
        assert decided.json()["status"] == "pending"
        assert decided.json()["resume_wake_sent"] is True

        # The worker resumes ONLY the payload-bound operation through
        # dispatch_once(approval_id=...): exactly one effect, ever.
        _wait_state(bootstrapped_url, run_id, {"succeeded"}, 120)
        assert scenario.count("effect:fixture.priv:m1") == 1
        resume_row = _read_step_row(
            bootstrapped_url, run_id, 1025
        )
        assert resume_row is not None
        assert resume_row["state"] == "succeeded"
        assert resume_row["receipt"]["approval_spent"] is True

    async def test_reconciling_blind_resume_refused_then_classification(
        self, v2_app, jar, server_a, bootstrapped_url, scenario
    ) -> None:
        objective = await create_objective(v2_app, jar, server_a)
        oid = uuid.UUID(objective.json()["objective_id"])
        scenario.compose_roots(server_a)
        scenario.write_model_script(
            [
                {"tool_call": {"name": "fixture_read", "args": {"marker": "m1"}}},
                {"text": "after reconciliation"},
            ]
        )
        scenario.set_behavior({"fixture.read:m1": "unknown_status"})
        scenario.spawn_worker(bootstrapped_url)

        key = uuid.uuid4()
        submitted = await submit_v2_conversation(
            v2_app, jar, server_a, oid, client_request_id=key
        )
        run_id = uuid.UUID(submitted.json()["run_id"])

        # The unproven effect is journaled FIRST, then the run parks in
        # the durable reconciliation wait (never auto-resumed).
        _wait_state(bootstrapped_url, run_id, {"reconciling"}, 120)
        row = _read_step_row(bootstrapped_url, run_id, 1)
        assert row is not None
        assert row["state"] == "outcome_unknown"
        assert scenario.count("effect:fixture.read:m1") == 1

        # A blind resume is refused: classification comes FIRST.
        blind = await call_app(
            v2_app,
            "POST",
            f"/api/v1/chat/conversations/{key}/resume",
            json_body={
                "server_id": str(server_a),
                "message": "just continue",
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert blind.status == 409
        assert _error(blind)["code"] == "classification_required"

        resolved = await call_app(
            v2_app,
            "POST",
            f"/api/v1/chat/conversations/{key}/reconcile",
            json_body={
                "server_id": str(server_a),
                "classifications": [{"seq": 1, "verdict": "confirm_applied"}],
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert resolved.status == 200, resolved.body
        body = resolved.json()
        assert body["run_id"] == str(run_id)
        assert body["results"][0]["seq"] == 1
        assert body["results"][0]["state"] == "succeeded"
        assert body["results"][0]["verdict"] == "final_succeeded"

        # The parked workflow wakes and continues to completion; the
        # uncertain effect was NEVER re-dispatched (exactly-once).
        _wait_state(bootstrapped_url, run_id, {"succeeded"}, 120)
        assert scenario.count("effect:fixture.read:m1") == 1

    async def test_waiting_children_force_resume_rejected(
        self, v2_app, jar, server_a, bootstrapped_url
    ) -> None:
        objective = await create_objective(v2_app, jar, server_a)
        oid = uuid.UUID(objective.json()["objective_id"])
        key = uuid.uuid4()
        submitted = await submit_v2_conversation(
            v2_app, jar, server_a, oid, client_request_id=key
        )
        assert submitted.status == 201
        run_id = uuid.UUID(submitted.json()["run_id"])

        # Drive the REAL facade onto the delegation park via the ONE
        # lifecycle writer (the legal running -> waiting_children edge).
        engine, sessions = create_app_engine(bootstrapped_url)
        try:
            lifecycle = RunLifecycle(sessions)

            async def _drive() -> None:
                await lifecycle.record_transition(str(run_id), RunState.RUNNING)
                await lifecycle.record_transition(
                    str(run_id), RunState.WAITING_CHILDREN
                )

            _run(_drive())
        finally:
            await engine.dispose()
        assert _wait_state(bootstrapped_url, run_id, {"waiting_children"}, 30)

        force = await call_app(
            v2_app,
            "POST",
            f"/api/v1/chat/conversations/{key}/resume",
            json_body={
                "server_id": str(server_a),
                "message": "manual force resume",
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert force.status == 409
        assert _error(force)["code"] == "resume_rejected"

        reconcile = await call_app(
            v2_app,
            "POST",
            f"/api/v1/chat/conversations/{key}/reconcile",
            json_body={
                "server_id": str(server_a),
                "classifications": [{"seq": 1, "verdict": "cancel"}],
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert reconcile.status == 409
        assert _error(reconcile)["code"] == "classification_not_applicable"

    async def test_reconcile_without_active_run_rejected(
        self, v2_app, jar, server_a
    ) -> None:
        objective = await create_objective(v2_app, jar, server_a)
        oid = uuid.UUID(objective.json()["objective_id"])
        key = uuid.uuid4()
        created = await submit_v2_conversation(
            v2_app, jar, server_a, oid, client_request_id=key
        )
        assert created.status == 201
        reconcile = await call_app(
            v2_app,
            "POST",
            f"/api/v1/chat/conversations/{key}/reconcile",
            json_body={
                "server_id": str(server_a),
                "classifications": [{"seq": 1, "verdict": "confirm_applied"}],
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert reconcile.status == 409
        assert _error(reconcile)["code"] in (
            "no_active_run",
            "classification_not_applicable",
        )


# ---------------------------------------------------------------------------
# journey 5: unauthenticated / cross-origin / missing-CSRF rejection
# ---------------------------------------------------------------------------


class TestAuthBoundary:
    NEW_GET_ROUTES = (
        "/api/v1/objectives/drain-status",
    )

    async def test_unauthenticated_reads_are_401(
        self, v2_app, jar, server_a
    ) -> None:
        objective = await create_objective(v2_app, jar, server_a)
        oid = uuid.UUID(objective.json()["objective_id"])
        bare = {}
        for path in (
            *self.NEW_GET_ROUTES,
            f"/api/v1/objectives?server_id={server_a}",
            f"/api/v1/objectives/{oid}?server_id={server_a}",
            f"/api/v1/objectives/{oid}/journal?server_id={server_a}",
            f"/api/v1/objectives/{oid}/events?server_id={server_a}",
            f"/api/v1/objectives/{oid}/runs?server_id={server_a}",
            f"/api/v1/objectives/{oid}/checklist?server_id={server_a}",
        ):
            response = await call_app(v2_app, "GET", path, headers=bare)
            assert response.status == 401, (path, response.status)
            assert _error(response)["code"] == "unauthenticated"

    async def test_unauthenticated_mutations_are_401(
        self, v2_app, jar, server_a
    ) -> None:
        objective = await create_objective(v2_app, jar, server_a)
        oid = uuid.UUID(objective.json()["objective_id"])
        conversation_key = uuid.uuid4()
        bodies: tuple[tuple[str, str, dict[str, Any]], ...] = (
            (
                "POST",
                "/api/v1/objectives",
                {
                    "server_id": str(server_a),
                    "original_request": "x",
                    "scope_server_ids": [str(server_a)],
                },
            ),
            (
                "POST",
                f"/api/v1/objectives/{oid}/scope",
                {"server_id": str(server_a), "expected_revision": 1},
            ),
            (
                "PUT",
                f"/api/v1/objectives/{oid}/checklist",
                {
                    "server_id": str(server_a),
                    "expected_revision": 0,
                    "items": [{"text": "x"}],
                },
            ),
            (
                "POST",
                f"/api/v1/chat/conversations/{conversation_key}/resume",
                {"server_id": str(server_a)},
            ),
            (
                "POST",
                f"/api/v1/chat/conversations/{conversation_key}/reconcile",
                {
                    "server_id": str(server_a),
                    "classifications": [{"seq": 1, "verdict": "cancel"}],
                },
            ),
        )
        for method, path, body in bodies:
            response = await call_app(v2_app, method, path, json_body=body)
            assert response.status == 401, (path, response.status)

    async def test_cross_origin_and_missing_csrf_are_403(
        self, v2_app, jar, server_a
    ) -> None:
        objective = await create_objective(v2_app, jar, server_a)
        oid = uuid.UUID(objective.json()["objective_id"])
        body = {
            "server_id": str(server_a),
            "original_request": "x",
            "scope_server_ids": [str(server_a)],
        }
        evil = await call_app(
            v2_app,
            "POST",
            "/api/v1/objectives",
            json_body=body,
            headers={"origin": EVIL_ORIGIN, "x-csrf-token": jar[CSRF_COOKIE]},
            cookies=jar,
        )
        assert evil.status == 403
        assert _error(evil)["code"] == "origin_rejected"

        csrfless = await call_app(
            v2_app,
            "POST",
            "/api/v1/objectives",
            json_body=body,
            headers={"origin": ORIGIN},
            cookies=jar,
        )
        assert csrfless.status == 403
        assert _error(csrfless)["code"] == "csrf_rejected"

        scope_evil = await call_app(
            v2_app,
            "POST",
            f"/api/v1/objectives/{oid}/scope",
            json_body={"server_id": str(server_a), "expected_revision": 1},
            headers={"origin": EVIL_ORIGIN, "x-csrf-token": jar[CSRF_COOKIE]},
            cookies=jar,
        )
        assert scope_evil.status == 403

        # The objective activity SSE rejects a cross-site Origin before
        # any stream byte (C08 rule 7).
        sse_call = await call_app(
            v2_app,
            "GET",
            f"/api/v1/objectives/{oid}/events/stream?server_id={server_a}",
            headers={"origin": EVIL_ORIGIN, "accept": "text/event-stream"},
            cookies=jar,
        )
        assert sse_call.status == 403


# ---------------------------------------------------------------------------
# journey 6: legacy read compatibility + drain inventory
# ---------------------------------------------------------------------------


class TestLegacyReadAndDrain:
    async def test_truthful_runtime_markers_and_drain_status(
        self, v2_app, jar, server_a, bootstrapped_url
    ) -> None:
        # A v2 conversation bound to an objective (no worker yet: queued).
        objective = await create_objective(v2_app, jar, server_a)
        oid = uuid.UUID(objective.json()["objective_id"])
        v2_key = uuid.uuid4()
        v2 = await submit_v2_conversation(
            v2_app, jar, server_a, oid, client_request_id=v2_key
        )
        assert v2.status == 201

        # A historical legacy conversation, seeded through the internal
        # store seams (pre-default runtime: no public legacy selector).
        legacy_key = uuid.uuid4()
        legacy_run = await seed_legacy_conversation(
            bootstrapped_url,
            server_a,
            client_request_id=legacy_key,
        )
        assert legacy_run == str(legacy_key)

        v2_detail = await call_app(
            v2_app,
            "GET",
            f"/api/v1/chat/conversations/{v2_key}?server_id={server_a}",
            headers=_cookies(jar),
        )
        assert v2_detail.status == 200
        v2_doc = v2_detail.json()
        assert v2_doc["is_legacy"] is False
        assert v2_doc["handler_name"] == "agent.run_v2"
        assert v2_doc["objective_ids"] == [str(oid)]

        legacy_detail = await call_app(
            v2_app,
            "GET",
            f"/api/v1/chat/conversations/{legacy_key}?server_id={server_a}",
            headers=_cookies(jar),
        )
        assert legacy_detail.status == 200
        legacy_doc = legacy_detail.json()
        assert legacy_doc["is_legacy"] is True
        assert legacy_doc["handler_name"] == "agent.run"
        assert legacy_doc["objective_ids"] == []

        # The drain inventory reports the unsettled legacy run honestly.
        drain = await call_app(
            v2_app,
            "GET",
            "/api/v1/objectives/drain-status",
            headers=_cookies(jar),
        )
        assert drain.status == 200
        doc = drain.json()
        assert doc["legacy"]["handler"] == "agent.run"
        assert doc["v2"]["handler"] == "agent.run_v2"
        assert doc["legacy"]["unsettled"] >= 1
        assert doc["v2"]["unsettled"] >= 1
        assert doc["ready_to_retire_legacy"] is False

        # Settle both queued runs through the ONE facade lifecycle writer
        # (the legal queued -> cancelled edge): the legacy drain window
        # empties and the readiness flips honestly (RV2-DEPLOY-DRAIN).
        engine, sessions = create_app_engine(bootstrapped_url)
        try:
            lifecycle = RunLifecycle(sessions)

            async def _settle() -> None:
                for run in (v2.json()["run_id"], legacy_run):
                    await lifecycle.record_transition(run, RunState.CANCELLED)

            _run(_settle())
        finally:
            await engine.dispose()
        drained = await call_app(
            v2_app,
            "GET",
            "/api/v1/objectives/drain-status",
            headers=_cookies(jar),
        )
        assert drained.status == 200
        doc = drained.json()
        assert doc["legacy"]["unsettled"] == 0
        assert doc["legacy"]["total"] >= 1
        assert doc["v2"]["total"] >= 1
        assert doc["ready_to_retire_legacy"] is True

