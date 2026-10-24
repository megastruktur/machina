"""Focused tests for the canonical conversation history surface (08).

Card ``08-history-api`` against act_CONTRACTS **§1–§4** (conversation
list/detail/transcript/events), the AGENTS.md mandatory-pagination
invariant (``limit|offset|total|has_more`` on every collection, same-filter
``COUNT(*)``, deterministic ordering) and the runtime scenarios:

- **C2** — a fresh authenticated session restores the true server
  transcript through bounded deterministic pages, and the events route's
  monotonic ``seq`` cursor replays strictly cursor-exclusive frames, so
  a reconnect deduplicates already-seen events without gaps or
  duplicates.
- **C7** — a legacy ``partial`` conversation surfaces its gaps
  explicitly (missing sequence range + bounded proven-but-unrecoverable
  submission count derived ONLY from app-owned ``job_events``/``job_runs``
  records), the transcript maps ONLY proved rows (never a placeholder or
  fabricated turn), and a mutation request without explicit user scope
  confirmation fails closed ``409 history_reconstruction_incomplete``.
- **C8** — cross-server reads fail closed with the scope-blind
  ``conversation_not_found`` 404 and zero identity leakage; the generic
  ``/api/v1/jobs/*`` list stays transcript-body-free (C05).

Boundary discipline (test policy): every HTTP claim runs through the
real ASGI app (``create_app`` + lifespan + middleware + handlers)
against a real disposable Postgres (``MAI_TEST_DATABASE_URL``) through
the real linear Alembic chain. Seeding rides the real conversation-store
producers (submit/producer-append/run-link/steering) plus the real
``chat.submitted`` record producer for the legacy gap proof. No worker
and no model is involved: this surface is read-only.

Registered limitations: no live provider, no browser (the UI journey is
09/16's typed-client work); SSE-style streaming is deliberately not
exercised — the canonical events contract is a bounded resumable page.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from sqlalchemy import text

from mai.agents.conversation_store import (
    aappend_producer_message,
    aattach_run,
    acreate_conversation,
    aenqueue_steering,
    asubmit_turn,
)
from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE
from mai.api.chat import _record_chat_submission
from mai.contracts.conversations import (
    HistoryCompleteness,
    MessageRange,
    MessageRole,
    MessageSourceKind,
    SteerAction,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.anyio

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: Synthetic fixture credential (NOT a secret; disposable fixture DB only).
FIXTURE_PASSWORD = "fixture-password-08-history-api-only"
ORIGIN = "http://127.0.0.1:8443"  # loopback allowlist entry

#: Opaque server identities for the cross-server (C8) proof.
SERVER_B = uuid.uuid4()

#: A transcript marker that must NEVER appear in generic job payloads.
SECRET_MARKERS = (
    "openssl private key material alpha",
    "continue with the deploy beta",
)


# ---------------------------------------------------------------------------
# ASGI harness (in-process; the proven 06 shape, trimmed to read paths)
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
        import json

        return json.loads(self.body)


def _scope(method: str, path: str, headers: dict[str, str], body: bytes) -> dict:
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
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> HttpResponse:
    """Run one bounded HTTP request through the real ASGI callable."""
    flat_headers = dict(headers or {})
    if cookies:
        flat_headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    body = b""
    if json_body is not None:
        import json

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


def _mutation_headers(jar: dict[str, str]) -> dict[str, str]:
    return {"origin": ORIGIN, "x-csrf-token": jar[CSRF_COOKIE]}


def _write_model_root(root: Path) -> None:
    """A bounded model selection so the canonical submit path can pin a
    profile (the C7 gate fires AFTER profile resolution)."""
    import json

    from mai.agents.model_store import ModelConfigStore

    store = ModelConfigStore(root)
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
# disposable database + app fixtures
# ---------------------------------------------------------------------------

FIXTURE_ROOT = Path("/tmp/mai-fxt-08-history-api")


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(os.path.join(BACKEND_ROOT, "alembic.ini"))


def _drop_app_tables(url: str) -> None:
    """Reset the fixture database to pre-bootstrap state (engine schema too)."""
    import psycopg

    with psycopg.connect(url) as conn:
        conn.execute(
            "DROP TABLE IF EXISTS conversation_steering, conversation_runs, "
            "conversation_messages, conversations, "
            "job_resource_locks, job_schedule_fires, "
            "job_schedules, job_events, job_steps, notification_alerts, "
            "observation_findings, server_observations, job_runs, approvals, "
            "application_projection, application_projection_journal, "
            "knowledge_doc_chunks, knowledge_documents, "
            "profile_projection_journal, profile_projection, admin_sessions, "
            "admin_credential, alembic_version"
        )
        conn.execute("DROP SEQUENCE IF EXISTS server_observations_seq")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``head``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the conversation history surface "
            "claims need a real disposable Postgres fixture"
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


@pytest.fixture(scope="session")
def bootstrapped_url(migrated_url: str) -> str:
    """Create the engine-owned ``dbos`` schema the way production does."""
    from mai.jobs import engine as jobs_engine

    jobs_engine.launch_worker(migrated_url)
    jobs_engine.shutdown()
    return migrated_url


async def _truncate(url: str) -> None:
    """Reset the facade tables between tests (the session-scoped DB stays)."""
    from mai.database import create_app_engine

    engine, _sessions = create_app_engine(url)
    try:
        async with engine.begin() as conn:
            for table in (
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
                # Engine/service-owned tables are absent until first use.
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
async def history_app(bootstrapped_url: str, tmp_path: Path):
    """App on the real fixture DB with a fresh confined data root."""
    await _truncate(bootstrapped_url)
    app = _build_app(bootstrapped_url, tmp_path / "data-root")
    async with _running(app):
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()


@pytest.fixture
async def jar(history_app: FastAPI) -> dict[str, str]:
    return await login_session(history_app)


@pytest.fixture
async def server_a(history_app: FastAPI, jar: dict[str, str]) -> uuid.UUID:
    return await create_profile(history_app, jar, "history-fixture-a")


# ---------------------------------------------------------------------------
# store-level seeding through the real producers
# ---------------------------------------------------------------------------


async def _seed_dialogue(
    app: FastAPI,
    conversation_id: uuid.UUID,
    server_id: uuid.UUID,
    turns: list[tuple[str, str]],
) -> None:
    """Append alternating user/assistant rows through the real producers."""
    factory = app.state.session_factory
    for role, content in turns:
        if role == "user":
            await asubmit_turn(
                factory,
                conversation_id=conversation_id,
                server_id=server_id,
                client_request_id=uuid.uuid4(),
                message=content,
                source_kind=MessageSourceKind.API,
            )
        else:
            await aappend_producer_message(
                factory,
                conversation_id=conversation_id,
                server_id=server_id,
                role=MessageRole.ASSISTANT,
                source_kind=MessageSourceKind.API,
                content=content,
            )


@dataclass(slots=True)
class SeededConversation:
    conversation_id: uuid.UUID
    server_id: uuid.UUID
    run_id: uuid.UUID | None


async def _seed_canonical_conversation(
    app: FastAPI,
    server_id: uuid.UUID,
    *,
    title: str | None = None,
    turns: list[tuple[str, str]] | None = None,
    attach_run: bool = False,
    steer_pending: bool = False,
) -> SeededConversation:
    """One complete canonical conversation: identity, dialogue, lease."""
    factory = app.state.session_factory
    conversation = await acreate_conversation(factory, server_id=server_id, title=title)
    run_id: uuid.UUID | None = None
    if attach_run:
        run_id = uuid.uuid4()
        await aattach_run(
            factory,
            conversation_id=conversation.conversation_id,
            server_id=server_id,
            run_id=run_id,
        )
    if turns:
        await _seed_dialogue(app, conversation.conversation_id, server_id, turns)
    if steer_pending:
        await aenqueue_steering(
            factory,
            conversation_id=conversation.conversation_id,
            server_id=server_id,
            action=SteerAction.STEER,
            client_request_id=uuid.uuid4(),
            content="steer toward the staging host",
        )
    return SeededConversation(
        conversation_id=conversation.conversation_id,
        server_id=server_id,
        run_id=run_id,
    )


async def _seed_legacy_conversation(
    app: FastAPI, server_id: uuid.UUID
) -> SeededConversation:
    """One legacy ``partial`` conversation with honest proven records (C7).

    The conversations row declares sequences 1-4 missing; exactly THREE
    rows were recovered at their PROVED original positions (sequences
    5-7, inserted by the simulated legacy-recovery producer with
    explicit ``legacy_import`` provenance). One ``chat.submitted``
    job-event record proves a submission whose canonical message row
    does not exist: the bounded unrecoverable-submission gap unit.
    """
    from mai.contracts.conversations import content_fingerprint

    factory = app.state.session_factory
    conversation = await acreate_conversation(
        factory,
        server_id=server_id,
        title="legacy import",
        history_completeness=HistoryCompleteness.PARTIAL,
        missing_range=MessageRange(from_sequence=1, to_sequence=4),
    )
    recovered = [
        (5, MessageRole.USER, "legacy recovered question"),
        (6, MessageRole.ASSISTANT, "legacy recovered answer"),
        (7, MessageRole.USER, "legacy follow-up question"),
    ]
    async with factory() as session, session.begin():
        for seq, role, content in recovered:
            safe, redacted = content, False
            await session.execute(
                text(
                    """
                    INSERT INTO conversation_messages
                        (message_id, conversation_id, seq, role, source_kind,
                         content, content_fingerprint, redacted, content_ref,
                         client_request_id)
                    VALUES (:mid, :cid, :seq, :role, 'legacy_import',
                            :content, :fingerprint, :redacted, NULL, NULL)
                    """
                ),
                {
                    "mid": uuid.uuid4(),
                    "cid": conversation.conversation_id,
                    "seq": seq,
                    "role": role.value,
                    "content": safe,
                    "fingerprint": content_fingerprint(safe),
                    "redacted": redacted,
                },
            )
    # The proved-but-unrecoverable submission: a real job_runs row plus
    # the real chat.submitted record producer, carrying identity binding
    # to a message_id that has NO canonical row.
    run_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO job_runs (run_id, server_id, depth, "
                "profile_revision, prompt_revision, model_profile, "
                "requested_grants) VALUES (:rid, :sid, 0, 1, 1, "
                "'legacy-import', '[]')"
            ),
            {"rid": run_id, "sid": server_id},
        )
    async with factory() as session, session.begin():
        await _record_chat_submission(
            session,
            run_id=str(run_id),
            digest="legacy-digest",
            handler="legacy",
            attachments=[],
            child_count=0,
            service_tags=[],
            now=datetime.now(timezone.utc),
            conversation_id=conversation.conversation_id,
            message_id=uuid.uuid4(),  # no canonical row for this identity
        )
    return SeededConversation(
        conversation_id=conversation.conversation_id,
        server_id=server_id,
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# C2: authenticated, scoped, paginated reads
# ---------------------------------------------------------------------------


async def test_list_requires_session_and_explicit_scope(
    history_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
) -> None:
    anonymous = await call_app(
        history_app, "GET", f"/api/v1/chat/conversations?server_id={server_a}"
    )
    assert anonymous.status == 401
    envelope = _error(anonymous)
    assert envelope["code"] == "unauthenticated"

    missing_scope = await call_app(
        history_app, "GET", "/api/v1/chat/conversations", cookies=jar
    )
    assert missing_scope.status == 422
    assert _error(missing_scope)["code"] == "validation_failed"

    bounded = await call_app(
        history_app,
        "GET",
        "/api/v1/chat/conversations?server_id=not-a-uuid",
        cookies=jar,
    )
    assert bounded.status == 422

    page = await call_app(
        history_app,
        "GET",
        f"/api/v1/chat/conversations?server_id={server_a}",
        cookies=jar,
    )
    assert page.status == 200
    document = page.json()
    assert set(document) == {"limit", "offset", "total", "has_more", "items"}
    assert document == {
        "limit": 50,
        "offset": 0,
        "total": 0,
        "has_more": False,
        "items": [],
    }

    over_limit = await call_app(
        history_app,
        "GET",
        f"/api/v1/chat/conversations?server_id={server_a}&limit=201",
        cookies=jar,
    )
    assert over_limit.status == 422
    zero_limit = await call_app(
        history_app,
        "GET",
        f"/api/v1/chat/conversations?server_id={server_a}&limit=0",
        cookies=jar,
    )
    assert zero_limit.status == 422
    negative_offset = await call_app(
        history_app,
        "GET",
        f"/api/v1/chat/conversations?server_id={server_a}&offset=-1",
        cookies=jar,
    )
    assert negative_offset.status == 422


async def test_list_scoped_paginated_deterministic(
    history_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
) -> None:
    older = await _seed_canonical_conversation(
        history_app,
        server_a,
        title="older",
        turns=[("user", SECRET_MARKERS[0])],
    )
    newer = await _seed_canonical_conversation(
        history_app,
        server_a,
        title="newer",
        turns=[("user", SECRET_MARKERS[1])],
    )
    other = await _seed_canonical_conversation(
        history_app,
        SERVER_B,
        title="other server",
        turns=[("user", "cross server body")],
    )

    page_one = await call_app(
        history_app,
        "GET",
        f"/api/v1/chat/conversations?server_id={server_a}&limit=1&offset=0",
        cookies=jar,
    )
    assert page_one.status == 200
    document = page_one.json()
    assert document["limit"] == 1
    assert document["offset"] == 0
    assert document["total"] == 2
    assert document["has_more"] is True
    assert [item["conversation_id"] for item in document["items"]] == [
        str(newer.conversation_id)
    ]
    summary = document["items"][0]
    assert set(summary) == {
        "conversation_id",
        "server_id",
        "title",
        "created_at",
        "updated_at",
        "message_count",
        "active_task_id",
        "active_run_id",
    }
    assert summary["server_id"] == str(server_a)
    assert summary["message_count"] == 1

    page_two = await call_app(
        history_app,
        "GET",
        f"/api/v1/chat/conversations?server_id={server_a}&limit=1&offset=1",
        cookies=jar,
    )
    assert page_two.status == 200
    tail = page_two.json()
    assert [item["conversation_id"] for item in tail["items"]] == [
        str(older.conversation_id)
    ]
    assert tail["has_more"] is False
    assert tail["total"] == 2

    # Transcript content never rides a list page (C01 rule 5).
    assert SECRET_MARKERS[0] not in page_one.body.decode()
    assert SECRET_MARKERS[1] not in page_one.body.decode()

    # The other server's conversation is invisible from this scope (C8).
    assert str(other.conversation_id) not in page_one.body.decode()
    other_page = await call_app(
        history_app,
        "GET",
        f"/api/v1/chat/conversations?server_id={SERVER_B}",
        cookies=jar,
    )
    assert other_page.status == 200
    assert other_page.json()["total"] == 1
    assert other_page.json()["items"][0]["server_id"] == str(SERVER_B)


async def test_detail_complete_and_active_references(
    history_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
) -> None:
    seeded = await _seed_canonical_conversation(
        history_app,
        server_a,
        title="canonical",
        attach_run=True,
        turns=[
            ("user", "inspect the fixture host"),
            ("assistant", "inspection report"),
        ],
    )
    response = await call_app(
        history_app,
        "GET",
        f"/api/v1/chat/conversations/{seeded.conversation_id}?server_id={server_a}",
        cookies=jar,
    )
    assert response.status == 200
    detail = response.json()
    assert detail["conversation_id"] == str(seeded.conversation_id)
    assert detail["server_id"] == str(server_a)
    assert detail["title"] == "canonical"
    assert detail["history_completeness"] == "complete"
    assert detail["missing_range"] is None
    assert detail["legacy_gaps"] == []
    assert detail["active_run_id"] == str(seeded.run_id)
    assert detail["message_count"] == 2

    anonymous = await call_app(
        history_app,
        "GET",
        f"/api/v1/chat/conversations/{seeded.conversation_id}?server_id={server_a}",
    )
    assert anonymous.status == 401

    missing = await call_app(
        history_app,
        "GET",
        f"/api/v1/chat/conversations/{uuid.uuid4()}?server_id={server_a}",
        cookies=jar,
    )
    assert missing.status == 404
    assert _error(missing)["code"] == "conversation_not_found"


# ---------------------------------------------------------------------------
# C2: transcript pagination and role preservation
# ---------------------------------------------------------------------------


async def test_messages_pagination_role_preserving(
    history_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
) -> None:
    seeded = await _seed_canonical_conversation(
        history_app,
        server_a,
        turns=[
            ("user", "turn one"),
            ("assistant", "reply one"),
            ("user", "turn two"),
            ("assistant", "reply two"),
            ("user", "turn three"),
            ("assistant", "reply three"),
            ("user", "turn four"),
        ],
    )
    base = f"/api/v1/chat/conversations/{seeded.conversation_id}/messages"

    first = await call_app(
        history_app, "GET", f"{base}?server_id={server_a}&limit=3&offset=0", cookies=jar
    )
    assert first.status == 200
    page = first.json()
    assert page["conversation_id"] == str(seeded.conversation_id)
    assert page["history_completeness"] == "complete"
    assert (page["limit"], page["offset"], page["total"], page["has_more"]) == (
        3,
        0,
        7,
        True,
    )
    assert [row["sequence"] for row in page["items"]] == [1, 2, 3]
    assert [row["role"] for row in page["items"]] == [
        "user",
        "assistant",
        "user",
    ]
    assert page["items"][0]["content"] == "turn one"
    assert page["items"][0]["source_kind"] == "api"

    middle = await call_app(
        history_app, "GET", f"{base}?server_id={server_a}&limit=3&offset=3", cookies=jar
    )
    middle_page = middle.json()
    assert [row["sequence"] for row in middle_page["items"]] == [4, 5, 6]
    assert middle_page["has_more"] is True

    last = await call_app(
        history_app, "GET", f"{base}?server_id={server_a}&limit=3&offset=6", cookies=jar
    )
    last_page = last.json()
    assert [row["sequence"] for row in last_page["items"]] == [7]
    assert last_page["has_more"] is False
    assert last_page["total"] == 7

    beyond = await call_app(
        history_app, "GET", f"{base}?server_id={server_a}&limit=3&offset=9", cookies=jar
    )
    beyond_page = beyond.json()
    assert beyond_page["items"] == []
    assert beyond_page["total"] == 7
    assert beyond_page["has_more"] is False

    anonymous = await call_app(history_app, "GET", f"{base}?server_id={server_a}")
    assert anonymous.status == 401
    cross = await call_app(
        history_app, "GET", f"{base}?server_id={SERVER_B}", cookies=jar
    )
    assert cross.status == 404
    assert _error(cross)["code"] == "conversation_not_found"


# ---------------------------------------------------------------------------
# C8: cross-server fails closed with zero leakage; jobs stay body-free
# ---------------------------------------------------------------------------


async def test_cross_server_fails_closed_without_leakage(
    history_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
) -> None:
    foreign = await _seed_canonical_conversation(
        history_app,
        SERVER_B,
        turns=[("user", SECRET_MARKERS[0]), ("assistant", SECRET_MARKERS[1])],
        attach_run=True,
    )
    foreign_id = foreign.conversation_id
    for suffix in ("", "/messages", "/events"):
        response = await call_app(
            history_app,
            "GET",
            f"/api/v1/chat/conversations/{foreign_id}{suffix}?server_id={server_a}",
            cookies=jar,
        )
        assert response.status == 404, (suffix, response.status, response.body)
        envelope = _error(response)
        assert envelope["code"] == "conversation_not_found"
        body = response.body.decode()
        assert str(foreign_id) not in body
        assert str(SERVER_B) not in body
        assert str(foreign.run_id) not in body

    # The generic jobs surface stays transcript-body-free (C05).
    jobs_page = await call_app(
        history_app,
        "GET",
        f"/api/v1/jobs/runs?server_id={SERVER_B}&limit=200&offset=0",
        cookies=jar,
    )
    assert jobs_page.status == 200
    jobs_body = jobs_page.body.decode()
    assert SECRET_MARKERS[0] not in jobs_body
    assert SECRET_MARKERS[1] not in jobs_body


# ---------------------------------------------------------------------------
# C2: cursor-deduplicated metadata events
# ---------------------------------------------------------------------------


async def test_events_cursor_dedup(
    history_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
) -> None:
    seeded = await _seed_canonical_conversation(
        history_app,
        server_a,
        attach_run=True,
        steer_pending=True,
        turns=[
            ("user", "first question"),
            ("assistant", "first answer"),
            ("user", "second question"),
            ("assistant", "second answer"),
            ("user", "third question"),
        ],
    )
    base = f"/api/v1/chat/conversations/{seeded.conversation_id}/events"

    full = await call_app(
        history_app, "GET", f"{base}?server_id={server_a}&after_seq=0", cookies=jar
    )
    assert full.status == 200
    page = full.json()
    assert set(page) == {
        "conversation_id",
        "limit",
        "offset",
        "total",
        "has_more",
        "after_seq",
        "events",
        "current",
    }
    assert page["conversation_id"] == str(seeded.conversation_id)
    assert page["after_seq"] == 0
    assert page["offset"] == 0
    assert page["total"] == 5
    assert page["has_more"] is False
    assert [event["seq"] for event in page["events"]] == [1, 2, 3, 4, 5]
    for event in page["events"]:
        # Metadata identities only: NO content field exists to leak (C05).
        assert set(event) == {
            "seq",
            "kind",
            "message_id",
            "role",
            "source_kind",
            "redacted",
            "has_content_ref",
            "created_at",
        }
        assert event["kind"] == "transcript.appended"
    assert page["events"][0]["role"] == "user"
    assert page["events"][1]["role"] == "assistant"
    assert page["current"]["latest_seq"] == 5
    assert page["current"]["history_completeness"] == "complete"
    assert page["current"]["active_run_id"] == str(seeded.run_id)
    assert page["current"]["pending_steering"] == 1
    body = full.body.decode()
    assert "first question" not in body
    assert "first answer" not in body

    # Bounded replay: small pages walk the SAME cursor space without
    # gaps or duplicates (the reconnect contract, C2).
    seen: list[int] = []
    cursor = 0
    while True:
        window = await call_app(
            history_app,
            "GET",
            f"{base}?server_id={server_a}&after_seq={cursor}&limit=2",
            cookies=jar,
        )
        assert window.status == 200
        window_page = window.json()
        assert window_page["after_seq"] == cursor
        assert window_page["total"] == 5 - cursor
        assert window_page["has_more"] == (cursor + len(window_page["events"]) < 5)
        seqs = [event["seq"] for event in window_page["events"]]
        assert seqs == sorted(seqs)
        assert all(seq > cursor for seq in seqs)
        seen.extend(seqs)
        if not window_page["has_more"]:
            break
        cursor = seqs[-1]
    assert seen == [1, 2, 3, 4, 5]

    # Reconnect at the tip: strictly cursor-exclusive, no duplicates.
    tip = await call_app(
        history_app, "GET", f"{base}?server_id={server_a}&after_seq=5", cookies=jar
    )
    tip_page = tip.json()
    assert tip_page["events"] == []
    assert tip_page["total"] == 0
    assert tip_page["has_more"] is False

    # New transcript appends produce exactly the NEW events.
    await _seed_dialogue(
        history_app,
        seeded.conversation_id,
        server_a,
        [("assistant", "third answer"), ("user", "fourth question")],
    )
    reconnect = await call_app(
        history_app, "GET", f"{base}?server_id={server_a}&after_seq=5", cookies=jar
    )
    reconnect_page = reconnect.json()
    assert [event["seq"] for event in reconnect_page["events"]] == [6, 7]
    assert reconnect_page["total"] == 2
    assert reconnect_page["current"]["latest_seq"] == 7

    anonymous = await call_app(history_app, "GET", f"{base}?server_id={server_a}")
    assert anonymous.status == 401
    cross = await call_app(
        history_app, "GET", f"{base}?server_id={SERVER_B}", cookies=jar
    )
    assert cross.status == 404
    assert _error(cross)["code"] == "conversation_not_found"
    negative = await call_app(
        history_app,
        "GET",
        f"{base}?server_id={server_a}&after_seq=-1",
        cookies=jar,
    )
    assert negative.status == 422


# ---------------------------------------------------------------------------
# C7: legacy gaps labelled, never fabricated; mutation gated
# ---------------------------------------------------------------------------


async def test_legacy_gaps_labeled_not_fabricated(
    history_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
) -> None:
    legacy = await _seed_legacy_conversation(history_app, server_a)

    detail = await call_app(
        history_app,
        "GET",
        f"/api/v1/chat/conversations/{legacy.conversation_id}?server_id={server_a}",
        cookies=jar,
    )
    assert detail.status == 200
    header = detail.json()
    assert header["history_completeness"] == "partial"
    assert header["missing_range"] == {"from_sequence": 1, "to_sequence": 4}
    gaps = header["legacy_gaps"]
    assert {gap["kind"] for gap in gaps} == {
        "missing_range",
        "unrecoverable_submissions",
    }
    range_gap = next(gap for gap in gaps if gap["kind"] == "missing_range")
    assert range_gap["from_sequence"] == 1
    assert range_gap["to_sequence"] == 4
    unrecoverable = next(
        gap for gap in gaps if gap["kind"] == "unrecoverable_submissions"
    )
    assert unrecoverable["count"] == 1

    # The list page surfaces the same honest marker (safe metadata).
    listing = await call_app(
        history_app,
        "GET",
        f"/api/v1/chat/conversations?server_id={server_a}",
        cookies=jar,
    )
    assert listing.status == 200
    listed = listing.json()["items"]
    assert [item["conversation_id"] for item in listed] == [str(legacy.conversation_id)]
    assert listed[0]["message_count"] == 3

    # The transcript maps ONLY proved rows: exactly the recovered
    # sequences 5-7, no placeholder rows for the missing range.
    messages = await call_app(
        history_app,
        "GET",
        f"/api/v1/chat/conversations/{legacy.conversation_id}/messages"
        f"?server_id={server_a}",
        cookies=jar,
    )
    assert messages.status == 200
    transcript = messages.json()
    assert transcript["history_completeness"] == "partial"
    assert transcript["total"] == 3
    assert [row["sequence"] for row in transcript["items"]] == [5, 6, 7]
    assert all(row["source_kind"] == "legacy_import" for row in transcript["items"])
    body = messages.body.decode()
    assert "missing" not in body  # no placeholder text was invented

    # A mutation without explicit user scope confirmation fails closed.
    # The submit path pins a model profile BEFORE the history gate, so a
    # bounded selection is stored in the app's data root first.
    _write_model_root(Path(history_app.state.env["MAI_DATA_ROOT"]))
    submit = await call_app(
        history_app,
        "POST",
        f"/api/v1/chat/conversations/{legacy.conversation_id}/messages",
        json_body={
            "server_id": str(server_a),
            "client_request_id": str(uuid.uuid4()),
            "message": "continue anyway",
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert submit.status == 409
    envelope = _error(submit)
    assert envelope["code"] == "history_reconstruction_incomplete"

    anonymous = await call_app(
        history_app,
        "GET",
        f"/api/v1/chat/conversations/{legacy.conversation_id}?server_id={server_a}",
    )
    assert anonymous.status == 401
