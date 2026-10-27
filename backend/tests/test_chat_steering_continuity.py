"""Focused tests for durable active-run steering continuity (07-steering).

Acceptance (BRIEF, act_CONTRACTS §2–§3, act_RUNTIME C4/C5/C6):

1. **Durable append before delivery & exact-once safe-boundary
   consumption (C4/C5)** — active-run steering and queued follow-up
   turns persist the durable ``conversation_steering`` delivery row
   BEFORE the caller is told anything (and with no competing
   continuation executor: a lease-holding conversation answers
   ``202 queued_followup``, never a second run); the active run's
   executor poll/ack seam consumes each row exactly once, idempotently
   under duplicate signals and step replays.
2. **Deterministic pause/cancel (C5)** — pause/cancel are recorded
   without any intake model call and win their safe boundary;
   natural-language rows claimed at the same boundary stay pending for
   the next runner (never dropped, never double-applied); a terminal
   run releases the conversation writer lease so follow-up turns never
   stay stuck behind a stale ``active_run_id``.
3. **Honest boundary bookkeeping (C6)** — the pure boundary decision
   collects steering text for the next ``UserPromptPart`` injection, a
   stop acks ONLY its own row, and deferred calls a boundary never
   dispatched carry explicit ``not_executed`` result documents.

Boundary discipline (test policy): storage/HTTP/executor claims run
against a real disposable PostgreSQL fixture (``MAI_TEST_DATABASE_URL``)
with the real linear Alembic chain, in-process — the real ASGI app plus
the REAL composed ``AgentRunExecutor`` and no DBOS worker subprocess
harness (the full durable-worker loop through real workers is the
00-runtime fixture's C4/C5/C6 proof at the committed SHA). Fixtures
carry only fixed opaque UUIDs and synthetic text — never secret values.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents import run as agent_run
from mai.agents.conversation_store import (
    SteeringRow,
    aactive_run,
    aattach_run,
    acreate_conversation,
    aenqueue_steering,
    alist_messages,
    arelease_run,
)
from mai.agents.model_store import ModelConfigStore
from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE
from mai.capabilities.dispatch import CapabilityDispatcher
from mai.contracts.conversations import SteerAction, TurnDisposition
from mai.credentials.env import EnvResolver
from mai.database import create_app_engine
from mai.jobs import engine as jobs_engine
from mai.jobs.resource_locks import ResourceLockService
from mai.plugins.registry import CapabilityRegistry, parse_descriptor
from mai.policy.approvals import ApprovalStore
from mai.policy.evaluate import ExecutionContext, Policy, PolicyMode

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: The suite runs async tests through the anyio pytest plugin (the repo's
#: async test convention); every test below is async, so the marker is
#: applied module-wide.
pytestmark = pytest.mark.anyio

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"
ALEMBIC_INI = str(BACKEND_ROOT / "alembic.ini")

#: Synthetic fixture credential (NOT a secret; disposable fixture DB only).
FIXTURE_PASSWORD = "fixture-password-07-steering-only"
ORIGIN = "http://127.0.0.1:8443"  # loopback allowlist entry

SERVER_ID = "6a91c2e4-1b3d-4f5a-8c7e-2d3b4a5f6e70"
SERVER_UUID = uuid.UUID(SERVER_ID)
SERVER_B_ID = "00000000-0000-0000-0000-0000000000b2"
SERVER_B_UUID = uuid.UUID(SERVER_B_ID)

READ_CAP = "fixture.read"

STEER_TEXT = "also check the journalctl unit status"
FOLLOWUP_TEXT = "and then summarize what changed"


# ---------------------------------------------------------------------------
# ASGI harness (the repo's scope-call convention; every test is async)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HttpResponse:
    status: int | None = None
    headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    body: bytes = b""

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
) -> HttpResponse:
    """Run one bounded HTTP request through the real ASGI callable."""
    flat_headers = dict(headers or {})
    if cookies:
        flat_headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    body = b""
    if json_body is not None:
        body = json.dumps(json_body).encode()
        flat_headers["content-type"] = "application/json"
    scope_headers = [(b"host", b"fixture")]
    scope_headers.extend(
        (key.encode("ascii").lower(), value.encode("ascii"))
        for key, value in flat_headers.items()
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
    response = HttpResponse()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            response.status = message["status"]
            response.headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            response.body += message.get("body", b"")

    await app(scope, receive, send)
    return response


def _cookies(response: HttpResponse) -> dict[str, str]:
    """Parse the session/CSRF cookies off the Set-Cookie headers."""
    jar: dict[str, str] = {}
    for _, value in response.headers:
        text_value = value.decode("latin-1")
        for name in (SESSION_COOKIE, CSRF_COOKIE):
            if text_value.startswith(f"{name}="):
                jar[name] = text_value.split(";", 1)[0].partition("=")[2]
    return jar


def _mutation_headers(jar: Mapping[str, str]) -> dict[str, str]:
    return {"origin": ORIGIN, "x-csrf-token": jar[CSRF_COOKIE]}


def _error(response: HttpResponse) -> dict[str, Any]:
    envelope = response.json()
    assert set(envelope) == {"error"}
    body = envelope["error"]
    assert set(body) == {"code", "message", "request_id", "fields"}
    return body


# ---------------------------------------------------------------------------
# disposable database fixtures (real linear Alembic chain, in-process)
# ---------------------------------------------------------------------------


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


def _drop_all(url: str) -> None:
    with psycopg.connect(url) as conn:
        # Full app set at the chain tip — the 09-agent-task tables (which
        # hold the fk_agent_tasks_conversation dependency on
        # conversations) are listed beside the conversation/job domains
        # (chain-tip reconciliation, the ``test_agent_task_run``
        # DOMAIN_TABLES convention) so the zero-to-head fixture stays
        # order-independent after sibling suites ran on the same shared
        # database.
        conn.execute(
            "DROP TABLE IF EXISTS conversation_steering, conversation_runs, "
            "conversation_messages, conversations, agent_task_completions, "
            "agent_task_revisions, agent_task_evidence, "
            "agent_task_criteria, agent_task_constraints, "
            "agent_task_steps, agent_tasks, job_resource_locks, "
            "job_schedule_fires, job_schedules, job_events, job_steps, "
            "job_runs, approvals, profile_projection_journal, "
            "profile_projection, admin_sessions, admin_credential, "
            "alembic_version, notification_alerts, observation_findings, "
            "server_observations, knowledge_doc_chunks, knowledge_documents, "
            "application_projection_journal, application_projection"
        )
        conn.execute("DROP SEQUENCE IF EXISTS server_observations_seq")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


_RESET_TABLES = (
    # children before parents (FK-safe per-test reset): task children,
    # then the task, then the conversation domain, then jobs/approvals.
    "agent_task_completions",
    "agent_task_revisions",
    "agent_task_evidence",
    "agent_task_criteria",
    "agent_task_constraints",
    "agent_task_steps",
    "agent_tasks",
    "conversation_steering",
    "conversation_runs",
    "conversation_messages",
    "conversations",
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
)


async def _truncate(url: str) -> None:
    """Reset the facade tables between tests (the migrated schema stays)."""
    engine, _factory = create_app_engine(url)
    try:
        async with engine.begin() as conn:
            for table in _RESET_TABLES:
                present = await conn.execute(
                    text("SELECT to_regclass(:t) IS NOT NULL"),
                    {"t": f"public.{table}"},
                )
                if not present.scalar():
                    continue  # engine-owned tables may not exist pre-launch
                await conn.execute(text(f"DELETE FROM {table}"))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through the head."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the steering-continuity claims need "
            "a real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        _drop_all(url)
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    return url


@pytest.fixture(scope="session")
def bootstrapped_url(migrated_url: str) -> str:
    """Create the engine-owned ``dbos`` schema the way production does."""
    jobs_engine.launch_worker(migrated_url)
    jobs_engine.shutdown()
    return migrated_url


@pytest.fixture
async def sessions(migrated_url: str):
    """NullPool session factory; empty conversation+jobs domain per test."""
    engine, factory = create_app_engine(migrated_url)
    yield factory
    await _truncate(migrated_url)
    await engine.dispose()


@pytest.fixture
async def chat_app(bootstrapped_url: str, tmp_path: pathlib.Path):
    """The real ASGI app on the fixture DB with fresh confined roots."""
    await _truncate(bootstrapped_url)
    app = create_app(
        env={
            "MAI_DATABASE_URL": bootstrapped_url,
            "MAI_PUBLIC_ORIGINS": ORIGIN,
            "MAI_DATA_ROOT": str(tmp_path / "data-root"),
            "MAI_ARTIFACTS_ROOT": str(tmp_path / "artifacts"),
        }
    )
    async with app.router.lifespan_context(app):
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()


@pytest.fixture
async def jar(chat_app: FastAPI) -> dict[str, str]:
    """Bootstrap once and return the authenticated cookie jar."""
    bootstrap = await call_app(
        chat_app,
        "POST",
        "/api/v1/auth/bootstrap",
        json_body={"password": FIXTURE_PASSWORD},
        headers={"origin": ORIGIN},
    )
    assert bootstrap.status == 201, bootstrap.body
    login = await call_app(
        chat_app,
        "POST",
        "/api/v1/auth/login",
        json_body={"password": FIXTURE_PASSWORD},
        headers={"origin": ORIGIN},
    )
    assert login.status == 204, login.body
    jar = _cookies(login)
    assert SESSION_COOKIE in jar and CSRF_COOKIE in jar
    return jar


@pytest.fixture
async def server_a(chat_app: FastAPI, jar: dict[str, str]) -> uuid.UUID:
    """One canonical server profile through the real HTTP surface."""
    response = await call_app(
        chat_app,
        "POST",
        "/api/v1/servers",
        json_body={
            "name": "fixture-a",
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
# the REAL composed executor (in-process; no model layer needed here)
# ---------------------------------------------------------------------------


def _fixture_descriptor(capability_id: str, effect_class: str) -> object:
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
            "output_schema": {"type": "object"},
            "execution_environments": ["linux_ssh"],
            "effect_class": effect_class,
            "resource_scope": [] if effect_class == "read" else ["target.exec"],
            "required_grants": [],
        },
        source=f"<{capability_id} descriptor>",
    )


def _real_registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register(_fixture_descriptor(READ_CAP, "read"))
    return registry


def _refusing_executor():
    async def _execute(call) -> object:
        raise AssertionError(f"executor invoked for a denied call: {call}")

    return _execute


@pytest.fixture
async def executor(
    sessions, tmp_path: pathlib.Path
) -> AsyncIterator[agent_run.AgentRunExecutor]:
    """The REAL composed executor over the REAL session factory.

    The steering poll/ack/lease seams run before any model call, so the
    model layer stays uncomposed; consumption exercises the real store,
    the real event stream and the real lifecycle machinery.
    """
    yield agent_run.AgentRunExecutor(
        sessions=sessions,
        dispatcher=CapabilityDispatcher(
            registry=_real_registry(),
            approvals=ApprovalStore(sessions),
            grants=lambda: {READ_CAP: frozenset({SERVER_UUID})},
            policy=lambda: Policy(revision=1, denied_capabilities=frozenset()),
            adopted=lambda server_id: True,
            credentials=lambda: EnvResolver(
                str(BACKEND_ROOT / ".." / "absent-fixture.env")
            ),
            executor=_refusing_executor(),
        ),
        registry=_real_registry(),
        model_store=ModelConfigStore(tmp_path / "model-root"),
        data_root=str(tmp_path / "data-root"),
        locks=ResourceLockService(
            "postgresql://unused:unused@127.0.0.1:1/unused"
        ),
    )


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _intent(run_id: str, task: str, server: uuid.UUID = SERVER_UUID) -> agent_run.AgentRunIntent:
    context = ExecutionContext(
        principal="hash:fixture-principal",
        server_id=server,
        run_id=uuid.UUID(run_id),
        parent_scope=None,
        profile_revision="profile-1",
        prompt_revision="prompt-1",
        credential_references=(),
        allowed_capabilities=(READ_CAP,),
        mode=PolicyMode("delegated"),
    )
    document = {
        "v": agent_run.RUN_PAYLOAD_VERSION,
        "run_id": run_id,
        "server_id": str(server),
        "context": context.to_json(),
        "role": "operator assistant",
        "task": task,
        "connection": "gateway",
        "profile": "fast",
    }
    return agent_run.parse_agent_run_intent(_canonical(document))


async def _insert_run_row(
    sessions: async_sessionmaker[AsyncSession], run_id: str
) -> None:
    """One ``job_runs`` row (the event stream's FK parent)."""
    async with sessions() as session:
        await session.execute(
            text(
                """
                INSERT INTO job_runs
                    (run_id, server_id, depth, profile_revision,
                     prompt_revision, model_profile, requested_grants,
                     submitted_at, updated_at)
                VALUES
                    (:run_id, :server_id, 0, 3, 7, 'fixture-fast',
                     :grants, now(), now())
                """
            ),
            {
                "run_id": uuid.UUID(run_id),
                "server_id": SERVER_UUID,
                "grants": json.dumps([READ_CAP]),
            },
        )
        await session.commit()


async def _steering_rows(
    sessions: async_sessionmaker[AsyncSession], conversation_id: uuid.UUID
) -> list[SteeringRow]:
    """Every steering row of one conversation, deterministic order."""
    async with sessions() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT steering_id, conversation_id, run_id,
                           client_request_id, action, content,
                           delivery_status, created_at, applied_at
                    FROM conversation_steering
                    WHERE conversation_id = :cid
                    ORDER BY created_at, steering_id
                    """
                ),
                {"cid": conversation_id},
            )
        ).mappings().all()
    return [
        SteeringRow(
            steering_id=row["steering_id"],
            conversation_id=row["conversation_id"],
            run_id=row["run_id"],
            client_request_id=row["client_request_id"],
            action=SteerAction(str(row["action"])),
            content=row["content"],
            delivery_status=str(row["delivery_status"]),
            created_at=row["created_at"],
            applied_at=row["applied_at"],
        )
        for row in rows
    ]


async def _conversation_with_active_run(
    sessions: async_sessionmaker[AsyncSession], *, run_id: uuid.UUID | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    """One conversation whose writer lease is held by an attached run."""
    run = run_id or uuid.uuid4()
    conversation = await acreate_conversation(sessions, server_id=SERVER_UUID)
    await _insert_run_row(sessions, str(run))
    await aattach_run(
        sessions,
        conversation_id=conversation.conversation_id,
        server_id=SERVER_UUID,
        run_id=run,
    )
    return conversation.conversation_id, run


# ---------------------------------------------------------------------------
# part A — durable append BEFORE delivery (the canonical HTTP seam)
# ---------------------------------------------------------------------------


async def _create_conversation(
    chat_app: FastAPI, jar: dict[str, str], server_id: uuid.UUID, message: str
) -> dict[str, Any]:
    response = await call_app(
        chat_app,
        "POST",
        "/api/v1/chat/conversations",
        json_body={
            "server_id": str(server_id),
            "client_request_id": str(uuid.uuid4()),
            "message": message,
            "profile": "fixture-fast",
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert response.status == 201, response.body
    return response.json()


async def _steer(
    chat_app: FastAPI,
    jar: dict[str, str],
    conversation_id: uuid.UUID,
    server_id: uuid.UUID,
    *,
    action: SteerAction,
    message: str | None = None,
    client_request_id: uuid.UUID | None = None,
) -> HttpResponse:
    body: dict[str, Any] = {
        "conversation_id": str(conversation_id),
        "server_id": str(server_id),
        "action": action.value,
    }
    if message is not None:
        body["message"] = message
    if client_request_id is not None:
        body["client_request_id"] = str(client_request_id)
    return await call_app(
        chat_app,
        "POST",
        f"/api/v1/chat/conversations/{conversation_id}/steer",
        json_body=body,
        headers=_mutation_headers(jar),
        cookies=jar,
    )


class TestDurableDeliveryBeforeSignal:
    async def test_steer_row_is_durable_before_response_and_dedups(
        self, chat_app, jar, sessions, server_a
    ) -> None:
        """Active-run steering persists BEFORE the caller is told (C4/C5).

        The response carries the durable row identity with
        ``delivery_status=pending``; a retransmitted idempotency key
        replays onto the SAME row (``200 deduplicated``), never a
        second control.
        """
        created = await _create_conversation(
            chat_app, jar, server_a, "Plan only; do not change files"
        )
        cid = uuid.UUID(created["conversation_id"])
        holder = created["run_id"]
        assert holder is not None  # the creating run holds the lease
        key = uuid.uuid4()
        response = await _steer(
            chat_app,
            jar,
            cid,
            server_a,
            action=SteerAction.STEER,
            message=STEER_TEXT,
            client_request_id=key,
        )
        assert response.status == 201, response.body
        view = response.json()
        assert view["action"] == "steer"
        assert view["run_id"] == holder
        assert view["disposition"] == TurnDisposition.CREATED.value
        assert view["delivery_status"] == "pending"
        rows = await _steering_rows(sessions, cid)
        assert len(rows) == 1
        assert rows[0].steering_id == uuid.UUID(view["steering_id"])
        assert rows[0].run_id == uuid.UUID(holder)
        assert rows[0].content == STEER_TEXT
        assert rows[0].delivery_status == "pending"
        # Duplicate signal: same key replays onto the same durable row.
        replay = await _steer(
            chat_app,
            jar,
            cid,
            server_a,
            action=SteerAction.STEER,
            message=STEER_TEXT,
            client_request_id=key,
        )
        assert replay.status == 200, replay.body
        assert replay.json()["disposition"] == TurnDisposition.DEDUPLICATED.value
        assert len(await _steering_rows(sessions, cid)) == 1

    async def test_steer_without_active_run_conflicts(
        self, chat_app, jar, sessions, server_a
    ) -> None:
        """Steering targets an ACTIVE run; a free lease conflicts (409)."""
        created = await _create_conversation(
            chat_app, jar, server_a, "Plan only; do not change files"
        )
        cid = uuid.UUID(created["conversation_id"])
        await arelease_run(
            sessions,
            conversation_id=cid,
            server_id=server_a,
            run_id=uuid.UUID(created["run_id"]),
        )
        response = await _steer(
            chat_app,
            jar,
            cid,
            server_a,
            action=SteerAction.PAUSE,
        )
        assert response.status == 409, response.body
        envelope = _error(response)
        assert envelope["code"] == "no_active_run"
        assert await _steering_rows(sessions, cid) == []

    async def test_steer_scope_mismatch_fails_closed(
        self, chat_app, jar, sessions, server_a
    ) -> None:
        """Cross-server steering fails CLOSED with the scope-blind 404 (C8)."""
        created = await _create_conversation(
            chat_app, jar, server_a, "Plan only; do not change files"
        )
        cid = uuid.UUID(created["conversation_id"])
        response = await _steer(
            chat_app,
            jar,
            cid,
            SERVER_B_UUID,
            action=SteerAction.PAUSE,
        )
        assert response.status == 404, response.body
        assert _error(response)["code"] == "conversation_not_found"
        assert await _steering_rows(sessions, cid) == []

    async def test_pause_and_cancel_record_deterministically(
        self, chat_app, jar, sessions, server_a
    ) -> None:
        """Pause/cancel carry no message and no intake model call (C5)."""
        created = await _create_conversation(
            chat_app, jar, server_a, "Plan only; do not change files"
        )
        cid = uuid.UUID(created["conversation_id"])
        pause = await _steer(chat_app, jar, cid, server_a, action=SteerAction.PAUSE)
        assert pause.status == 201, pause.body
        assert pause.json()["action"] == "pause"
        await asyncio.sleep(0.02)  # deterministic created_at ordering
        cancel = await _steer(chat_app, jar, cid, server_a, action=SteerAction.CANCEL)
        assert cancel.status == 201, cancel.body
        rows = await _steering_rows(sessions, cid)
        assert [(row.action, row.content) for row in rows] == [
            (SteerAction.PAUSE, None),
            (SteerAction.CANCEL, None),
        ]
        # A steer control without its message is a 422, never a guess.
        broken = await _steer(chat_app, jar, cid, server_a, action=SteerAction.STEER)
        assert broken.status == 422, broken.body

    async def test_queued_followup_persists_message_and_delivery_row(
        self, chat_app, jar, sessions, server_a
    ) -> None:
        """Follow-up under a live lease: message + delivery row, no run (C4).

        The turn is admitted (``202 queued_followup``), the canonical
        message row is durable, and the SAME idempotency key carries
        the durable delivery row the lease-holding run consumes at its
        next safe boundary — a retransmission deduplicates BOTH rows.
        """
        created = await _create_conversation(
            chat_app, jar, server_a, "Plan only; do not change files"
        )
        cid = uuid.UUID(created["conversation_id"])
        holder = created["run_id"]
        key = uuid.uuid4()
        first = await call_app(
            chat_app,
            "POST",
            f"/api/v1/chat/conversations/{cid}/messages",
            json_body={
                "server_id": str(server_a),
                "client_request_id": str(key),
                "message": FOLLOWUP_TEXT,
                "profile": "fixture-fast",
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert first.status == 202, first.body
        admitted = first.json()
        assert admitted["disposition"] == "queued_followup"
        assert admitted["run_id"] is None  # no competing continuation run
        # The canonical message row is durable BEFORE the response.
        transcript = await alist_messages(
            sessions, cid, server_id=server_a, limit=50
        )
        assert transcript.total == 2
        assert transcript.items[-1].content == FOLLOWUP_TEXT
        rows = await _steering_rows(sessions, cid)
        assert len(rows) == 1
        assert rows[0].action is SteerAction.STEER
        assert rows[0].content == FOLLOWUP_TEXT
        assert rows[0].run_id == uuid.UUID(holder)
        assert rows[0].client_request_id == key
        # Retransmission: one message, one delivery row (C4).
        replay = await call_app(
            chat_app,
            "POST",
            f"/api/v1/chat/conversations/{cid}/messages",
            json_body={
                "server_id": str(server_a),
                "client_request_id": str(key),
                "message": FOLLOWUP_TEXT,
                "profile": "fixture-fast",
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert replay.status == 202, replay.body
        # The 05 seam reports the lease-held disposition; the C4 dedup
        # observable is row-level: same message identity, one delivery row.
        assert replay.json()["disposition"] == "queued_followup"
        assert replay.json()["message_id"] == admitted["message_id"]
        assert len(await _steering_rows(sessions, cid)) == 1

    async def test_idle_followup_starts_run_without_steering_row(
        self, chat_app, jar, sessions, server_a
    ) -> None:
        """A free lease starts a REAL run: no delivery row is enqueued."""
        created = await _create_conversation(
            chat_app, jar, server_a, "Plan only; do not change files"
        )
        cid = uuid.UUID(created["conversation_id"])
        await arelease_run(
            sessions,
            conversation_id=cid,
            server_id=server_a,
            run_id=uuid.UUID(created["run_id"]),
        )
        followup = await call_app(
            chat_app,
            "POST",
            f"/api/v1/chat/conversations/{cid}/messages",
            json_body={
                "server_id": str(server_a),
                "client_request_id": str(uuid.uuid4()),
                "message": FOLLOWUP_TEXT,
                "profile": "fixture-fast",
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert followup.status == 201, followup.body
        assert followup.json()["disposition"] == "created"
        assert followup.json()["run_id"] is not None
        assert await _steering_rows(sessions, cid) == []


# ---------------------------------------------------------------------------
# part B — exact-once safe-boundary consumption (the executor seam)
# ---------------------------------------------------------------------------


class TestSafeBoundaryConsumption:
    async def test_poll_returns_pending_rows_readonly(
        self, sessions, executor
    ) -> None:
        """The poll claims nothing: rows stay pending until the ack (C4)."""
        cid, run = await _conversation_with_active_run(sessions)
        for action, content in (
            (SteerAction.STEER, "first steering note"),
            (SteerAction.PAUSE, None),
        ):
            await asyncio.sleep(0.02)  # deterministic created_at ordering
            await aenqueue_steering(
                sessions,
                conversation_id=cid,
                server_id=SERVER_UUID,
                action=action,
                client_request_id=uuid.uuid4(),
                content=content,
                run_id=run,
            )
        polled = executor.run(executor.apoll_steering(_intent(str(run), "task")))
        assert polled["conversation_linked"] is True
        pending = list(polled["pending"])
        assert [(row["action"], row["content"]) for row in pending] == [
            ("steer", "first steering note"),
            ("pause", None),
        ]
        # Read-only: nothing was consumed by the poll itself.
        rows = await _steering_rows(sessions, cid)
        assert all(row.delivery_status == "pending" for row in rows)

    async def test_ack_is_idempotent_and_consumed_rows_never_relist(
        self, sessions, executor
    ) -> None:
        """Ack marks applied once; replays/duplicates never reapply (C4/C5)."""
        cid, run = await _conversation_with_active_run(sessions)
        steering_id = uuid.uuid4()
        await aenqueue_steering(
            sessions,
            conversation_id=cid,
            server_id=SERVER_UUID,
            action=SteerAction.STEER,
            client_request_id=steering_id,
            content=STEER_TEXT,
            run_id=run,
        )
        intent = _intent(str(run), "task")
        polled = executor.run(executor.apoll_steering(intent))
        claimed = [dict(item) for item in polled["pending"]]
        acked = executor.run(executor.aack_steering(intent, claimed))
        assert acked["acked"] == [str(claimed[0]["steering_id"])]
        # Step replay / duplicate signal: the ack re-runs onto the same
        # durable row, the poll lists nothing new.
        replayed = executor.run(executor.aack_steering(intent, claimed))
        assert replayed["acked"] == [str(claimed[0]["steering_id"])]
        again = executor.run(executor.apoll_steering(intent))
        assert again["pending"] == []
        rows = await _steering_rows(sessions, cid)
        assert len(rows) == 1
        assert rows[0].delivery_status == "applied"
        assert rows[0].applied_at is not None
        # The durable enqueue of the SAME idempotency key stays deduplicated.
        _, disposition = await aenqueue_steering(
            sessions,
            conversation_id=cid,
            server_id=SERVER_UUID,
            action=SteerAction.STEER,
            client_request_id=steering_id,
            content=STEER_TEXT,
            run_id=run,
        )
        assert disposition is TurnDisposition.DEDUPLICATED
        assert len(await _steering_rows(sessions, cid)) == 1

    async def test_poll_fails_closed_on_scope_mismatch(
        self, sessions, executor
    ) -> None:
        """A run bound to another server scope polls nothing (C8)."""
        cid, run = await _conversation_with_active_run(sessions)
        await aenqueue_steering(
            sessions,
            conversation_id=cid,
            server_id=SERVER_UUID,
            action=SteerAction.STEER,
            client_request_id=uuid.uuid4(),
            content=STEER_TEXT,
            run_id=run,
        )
        stranger = executor.run(
            executor.apoll_steering(_intent(str(run), "task", SERVER_B_UUID))
        )
        assert stranger == {"conversation_linked": False, "pending": []}

    async def test_legacy_run_without_link_polls_nothing(
        self, sessions, executor
    ) -> None:
        """Legacy runs (no conversation link) keep the legacy behavior."""
        run = str(uuid.uuid4())
        await _insert_run_row(sessions, run)
        polled = executor.run(executor.apoll_steering(_intent(run, "task")))
        assert polled == {"conversation_linked": False, "pending": []}

    async def test_finish_releases_writer_lease_and_reports_stop(
        self, sessions, executor
    ) -> None:
        """Terminal run ⇒ lease released + honest stopped_by event (§1/C5)."""
        cid, run = await _conversation_with_active_run(sessions)
        intent = _intent(str(run), "task")
        executor.observe_run_started(intent)
        summary = {
            "rounds": 1,
            "tool_calls": [],
            "output_chars": 12,
            "assistant_recorded": False,
            "assistant_message_id": None,
            agent_run.STOPPED_BY_EVENT_KEY: SteerAction.PAUSE.value,
        }
        state = executor.run(executor.afinish_run(intent, summary))
        assert state == "succeeded"
        active = await aactive_run(sessions, cid, server_id=SERVER_UUID)
        assert active is None  # the stale lease never blocks a follow-up
        async with sessions() as session:
            events = (
                await session.execute(
                    text(
                        "SELECT type, data FROM job_events "
                        "WHERE run_id = :rid ORDER BY seq"
                    ),
                    {"rid": uuid.UUID(str(run))},
                )
            ).mappings().all()
        completed = [e for e in events if e["type"] == "agent.completed"]
        assert len(completed) == 1
        assert completed[0]["data"].get(agent_run.STOPPED_BY_EVENT_KEY) == "pause"
        # Idempotent: a replayed release observes the same free lease.
        released = executor.run(executor.arelease_conversation_lease(intent))
        assert released == {"conversation_linked": True, "lease_free": True}

    async def test_failure_path_lease_release_helper_releases(
        self, sessions, executor
    ) -> None:
        """The failure/cancel paths release the lease best-effort (§1)."""
        cid, run = await _conversation_with_active_run(sessions)
        intent = _intent(str(run), "task")
        executor.observe_run_started(intent)
        assert executor.fail_run(intent, "round_budget_exhausted")
        released = executor.run(executor.arelease_conversation_lease(intent))
        assert released == {"conversation_linked": True, "lease_free": True}
        assert await aactive_run(sessions, cid, server_id=SERVER_UUID) is None


# ---------------------------------------------------------------------------
# part C — the deterministic boundary decision (pure)
# ---------------------------------------------------------------------------


def _claimed(
    steering_id: str, action: str, content: str | None
) -> dict[str, object]:
    return {"steering_id": steering_id, "action": action, "content": content}


class TestBoundaryDecision:
    async def test_decision_collects_steering_and_acks_all(self) -> None:
        claimed = [
            _claimed("s1", "steer", "first note"),
            _claimed("s2", "steer", "second note"),
        ]
        texts, stop, ack = agent_run._decide_steering_boundary(claimed)
        assert texts == ["first note", "second note"]
        assert stop is None
        assert [row["steering_id"] for row in ack] == ["s1", "s2"]

    async def test_decision_stop_wins_and_leaves_text_pending(self) -> None:
        """A stop acks ONLY itself; claimed steering stays pending (C5)."""
        claimed = [
            _claimed("s1", "steer", "check the logs first"),
            _claimed("p1", "pause", None),
            _claimed("s2", "steer", "late note"),
        ]
        texts, stop, ack = agent_run._decide_steering_boundary(claimed)
        assert texts == []  # never injected into a stopped run
        assert stop == "pause"
        assert [row["steering_id"] for row in ack] == ["p1"]
        # s1/s2 were NOT acked: the next runner consumes them.

    async def test_decision_cancel_first_and_blank_content_dropped(self) -> None:
        texts, stop, ack = agent_run._decide_steering_boundary(
            [_claimed("c1", "cancel", None)]
        )
        assert (texts, stop) == ([], "cancel")
        assert [row["steering_id"] for row in ack] == ["c1"]
        texts, stop, ack = agent_run._decide_steering_boundary(
            [
                _claimed("s1", "steer", "   "),
                _claimed("s2", "steer", None),
                _claimed("s3", "steer", "real note"),
            ]
        )
        assert texts == ["real note"]
        assert stop is None
        assert len(ack) == 3  # blank-content rows are still consumed

    async def test_unexecuted_deferred_result_document_is_honest(self) -> None:
        document = json.loads(
            agent_run._steering_stop_result_text("steering_pending")
        )
        assert document["status"] == "not_executed"
        assert document["reason"] == "steering_pending"
        assert "no effect occurred" in document["detail"]
