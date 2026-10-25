"""Focused tests for the task read/correction/decision surface (15).

Card ``15-task-api`` against act_CONTRACTS **§6/§8**, the AGENTS.md
mandatory-pagination invariant and the runtime scenarios:

- **Criterion 1** — a user correction creates a SOURCE-LINKED revision
  (the ledger row records the cited user message and ``author=user``,
  never the model); a stale tab's outdated ``base_revision`` gets the
  structured ``409 revision_conflict`` instead of overwriting newer
  state (no lost update, C6); the items/constraints/evidence reads are
  paginated (``limit|offset|total|has_more`` with the same-filter
  ``COUNT(*)`` and deterministic ordering) and enforce the explicit
  ``server_id`` scope (cross-server fails closed with the scope-blind
  ``task_not_found`` 404 and zero identity leakage, C8).
- **Criterion 2** — plan review (``POST revisions``), phase selection
  and pause/cancel (``POST decisions``) are DISTINCT routes with
  DISTINCT effects; a metadata-only edit can never silently authorize a
  pending operation (the C03 approval stays ``pending``) nor
  terminate/cancel the target operations of the task (status stays
  ``in_progress``).

Boundary discipline (test policy, the proven 08 shape): every HTTP
claim runs through the real ASGI app (``create_app`` + lifespan +
middleware + handlers) against a real disposable Postgres
(``MAI_TEST_DATABASE_URL``) through the real linear Alembic chain.
Seeding rides the REAL producers (``acreate_conversation``/
``asubmit_turn``/``acreate_task`` and the real ``ApprovalStore``); no
worker and no model is involved.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from sqlalchemy import text

from mai.agents.conversation_store import acreate_conversation, asubmit_turn
from mai.agents.task_store import acreate_task
from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE
from mai.contracts.tasks import (
    ConstraintOrigin,
    ConstraintProposal,
    EvidenceKind,
    EvidenceRef,
    TaskPlanStep,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.anyio

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: Synthetic fixture credential (NOT a secret; disposable fixture DB only).
FIXTURE_PASSWORD = "fixture-password-15-task-api-only"
ORIGIN = "http://127.0.0.1:8443"  # loopback allowlist entry

#: Opaque server identities for the cross-server (C8) proof.
SERVER_A = uuid.uuid4()
SERVER_B = uuid.uuid4()


# ---------------------------------------------------------------------------
# ASGI harness (in-process; the proven 08 shape)
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


# ---------------------------------------------------------------------------
# disposable database + app fixtures
# ---------------------------------------------------------------------------

FIXTURE_ROOT = Path("/tmp/mai-fxt-15-task-api")


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(os.path.join(BACKEND_ROOT, "alembic.ini"))


def _drop_app_tables(url: str) -> None:
    """Reset the fixture database to pre-bootstrap state."""
    import psycopg

    with psycopg.connect(url) as conn:
        conn.execute(
            "DROP TABLE IF EXISTS agent_task_completions, "
            "agent_task_revisions, agent_task_evidence, "
            "agent_task_criteria, agent_task_constraints, "
            "agent_task_steps, agent_tasks, conversation_steering, "
            "conversation_runs, conversation_messages, conversations, "
            "notification_alerts, observation_findings, "
            "server_observations, application_projection_journal, "
            "application_projection, knowledge_doc_chunks, "
            "knowledge_documents, job_schedule_fires, job_schedules, "
            "job_events, job_steps, job_runs, approvals, "
            "job_resource_locks, profile_projection_journal, "
            "profile_projection, admin_sessions, admin_credential, "
            "alembic_version"
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
            f"{LIVE_URL_VAR} is not set; the task surface claims need a "
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


@pytest.fixture(scope="session")
def bootstrapped_url(migrated_url: str) -> str:
    """Create the engine-owned ``dbos`` schema the way production does."""
    from mai.jobs import engine as jobs_engine

    jobs_engine.launch_worker(migrated_url)
    jobs_engine.shutdown()
    return migrated_url


async def _truncate(url: str) -> None:
    """Reset the facade tables between tests (the session DB stays)."""
    from mai.database import create_app_engine

    engine, _sessions = create_app_engine(url)
    try:
        async with engine.begin() as conn:
            for table in (
                "agent_task_completions",
                "agent_task_revisions",
                "agent_task_evidence",
                "agent_task_criteria",
                "agent_task_constraints",
                "agent_task_steps",
                "agent_tasks",
                "approvals",
                "job_resource_locks",
                "job_events",
                "job_runs",
                "admin_sessions",
                "admin_credential",
                "conversation_steering",
                "conversation_runs",
                "conversation_messages",
                "conversations",
            ):
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
async def task_app(bootstrapped_url: str, tmp_path: Path):
    """App on the real fixture DB with a fresh confined data root."""
    await _truncate(bootstrapped_url)
    app = _build_app(bootstrapped_url, tmp_path / "data-root")
    async with _running(app):
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()


@pytest.fixture
async def jar(task_app: FastAPI) -> dict[str, str]:
    return await login_session(task_app)


# ---------------------------------------------------------------------------
# seeding through the real producers
# ---------------------------------------------------------------------------


async def _make_conversation(app: FastAPI, server: uuid.UUID) -> uuid.UUID:
    conv = await acreate_conversation(app.state.session_factory, server_id=server)
    return conv.conversation_id


async def _make_message(
    app: FastAPI,
    conversation_id: uuid.UUID,
    server: uuid.UUID = SERVER_A,
    body: str = "synthetic user turn",
) -> uuid.UUID:
    result = await asubmit_turn(
        app.state.session_factory,
        conversation_id=conversation_id,
        server_id=server,
        client_request_id=uuid.uuid4(),
        message=body,
    )
    return result.message.message_id


def _step(step_id: str, **overrides: object) -> TaskPlanStep:
    return TaskPlanStep(step_id=step_id, title=f"step {step_id}", **overrides)  # type: ignore[arg-type]


def _constraint(constraint_id: str) -> ConstraintProposal:
    return ConstraintProposal(
        constraint_id=constraint_id,
        category="safety",
        statement=f"synthetic constraint {constraint_id}",
        origin=ConstraintOrigin.USER,
        suggested="add",  # type: ignore[arg-type]
    )


async def _make_task(
    app: FastAPI,
    server: uuid.UUID = SERVER_A,
    conversation_id: uuid.UUID | None = None,
    steps: list[TaskPlanStep] | None = None,
    constraints: list[ConstraintProposal] | None = None,
):
    cid = conversation_id or await _make_conversation(app, server)
    result = await acreate_task(
        app.state.session_factory,
        server_id=server,
        conversation_id=cid,
        title="synthetic fixture task",
        summary="fixture objective",
        steps=steps or [_step("s1"), _step("s2", depends_on=["s1"]), _step("s3")],
        constraints=constraints or [],
    )
    return result.task


def _correction_body(
    server: uuid.UUID,
    base_revision: int,
    source_message_id: uuid.UUID,
    **patch_overrides: Any,
) -> dict[str, Any]:
    return {
        "server_id": str(server),
        "patch": {
            "schema_version": 1,
            "source_message_id": str(source_message_id),
            "base_revision": base_revision,
            "idempotency_key": str(uuid.uuid4()),
            **patch_overrides,
        },
    }


def _decision_body(
    server: uuid.UUID,
    expected_revision: int,
    **overrides: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "server_id": str(server),
        "expected_revision": expected_revision,
        "client_request_id": str(uuid.uuid4()),
    }
    body.update(overrides)
    return body


def _step_ids(payload: Any) -> list[str]:
    return [item["step_id"] for item in payload["items"]]


# ---------------------------------------------------------------------------
# C8 — the scope boundary fails closed with zero identity leakage
# ---------------------------------------------------------------------------


class TestScopeBoundary:
    async def test_reads_fail_closed_across_servers(
        self, task_app: FastAPI, jar: dict[str, str]
    ) -> None:
        conversation = await _make_conversation(task_app, SERVER_A)
        message = await _make_message(task_app, conversation)
        task = await _make_task(
            task_app,
            conversation_id=conversation,
            steps=[
                _step(
                    "s1",
                    evidence_refs=[
                        EvidenceRef(
                            kind=EvidenceKind.MESSAGE,
                            evidence_id=message,
                            note="synthetic",
                        )
                    ],
                )
            ],
        )
        foreign = (str(task.task_id), str(SERVER_A), str(conversation), "step s1")
        for path in (
            f"/api/v1/chat/conversations/{conversation}/task?server_id={SERVER_B}",
            f"/api/v1/chat/tasks/{task.task_id}/items?server_id={SERVER_B}",
            f"/api/v1/chat/tasks/{task.task_id}/constraints?server_id={SERVER_B}",
            f"/api/v1/chat/tasks/{task.task_id}/evidence?server_id={SERVER_B}",
            f"/api/v1/chat/tasks/{task.task_id}/revisions?server_id={SERVER_B}",
        ):
            response = await call_app(task_app, "GET", path, cookies=jar)
            assert response.status == 404, (path, response.body)
            body = _error(response)
            assert body["code"] == "task_not_found"
            assert body["fields"] == {}
            for marker in foreign:
                assert marker not in response.body.decode()

    async def test_reads_require_a_session(self, task_app: FastAPI) -> None:
        response = await call_app(
            task_app,
            "GET",
            f"/api/v1/chat/tasks/{uuid.uuid4()}/items?server_id={SERVER_A}",
        )
        assert response.status == 401
        assert _error(response)["code"] == "unauthenticated"

    async def test_mutations_require_origin_and_csrf(
        self, task_app: FastAPI, jar: dict[str, str]
    ) -> None:
        conversation = await _make_conversation(task_app, SERVER_A)
        message = await _make_message(task_app, conversation)
        task = await _make_task(task_app, conversation_id=conversation)
        body = _correction_body(SERVER_A, 1, message, title="renamed")
        response = await call_app(
            task_app,
            "POST",
            f"/api/v1/chat/tasks/{task.task_id}/revisions",
            json_body=body,
            cookies=jar,  # session cookie only: no origin, no CSRF token
        )
        assert response.status == 403
        assert _error(response)["code"] == "origin_rejected"


# ---------------------------------------------------------------------------
# Criterion 1 — source-linked revisions, stale conflict, paged scope
# ---------------------------------------------------------------------------


class TestCriterion1Corrections:
    async def test_user_correction_creates_source_linked_user_revision(
        self, task_app: FastAPI, jar: dict[str, str]
    ) -> None:
        conversation = await _make_conversation(task_app, SERVER_A)
        message = await _make_message(task_app, conversation)
        task = await _make_task(task_app, conversation_id=conversation)

        body = _correction_body(
            SERVER_A,
            1,
            message,
            title="user-corrected objective",
            operations=[
                {
                    "op": "add",
                    "step": {"step_id": "s9", "title": "step s9"},
                }
            ],
        )
        response = await call_app(
            task_app,
            "POST",
            f"/api/v1/chat/tasks/{task.task_id}/revisions",
            json_body=body,
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert response.status == 201, response.body
        outcome = response.json()
        assert outcome["disposition"] == "created"
        assert outcome["task"]["revision"] == 2
        assert outcome["task"]["title"] == "user-corrected objective"
        revision = outcome["revision"]
        # Source-linked and user-authored — never the model (§6/§8).
        assert revision["kind"] == "plan_patch"
        assert revision["author"] == "user"
        assert revision["source_message_id"] == str(message)

        ledger = await call_app(
            task_app,
            "GET",
            f"/api/v1/chat/tasks/{task.task_id}/revisions?server_id={SERVER_A}",
            cookies=jar,
        )
        assert ledger.status == 200
        page = ledger.json()
        assert page["total"] == 2
        assert [row["revision"] for row in page["items"]] == [2, 1]
        correction_row, create_row = page["items"]
        assert create_row["kind"] == "create"
        assert correction_row["author"] == "user"
        assert correction_row["source_message_id"] == str(message)

        items = await call_app(
            task_app,
            "GET",
            f"/api/v1/chat/tasks/{task.task_id}/items?server_id={SERVER_A}",
            cookies=jar,
        )
        assert _step_ids(items.json()) == ["s1", "s2", "s3", "s9"]

    async def test_retransmitted_correction_replays_onto_the_same_revision(
        self, task_app: FastAPI, jar: dict[str, str]
    ) -> None:
        conversation = await _make_conversation(task_app, SERVER_A)
        message = await _make_message(task_app, conversation)
        task = await _make_task(task_app, conversation_id=conversation)
        body = _correction_body(SERVER_A, 1, message, title="renamed once")
        path = f"/api/v1/chat/tasks/{task.task_id}/revisions"
        first = await call_app(
            task_app,
            "POST",
            path,
            json_body=body,
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert first.status == 201
        replay = await call_app(
            task_app,
            "POST",
            path,
            json_body=body,
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert replay.status == 200
        assert replay.json()["disposition"] == "deduplicated"
        assert replay.json()["task"]["revision"] == 2
        assert replay.json()["revision"]["revision"] == 2
        ledger = await call_app(
            task_app,
            "GET",
            f"/api/v1/chat/tasks/{task.task_id}/revisions?server_id={SERVER_A}",
            cookies=jar,
        )
        assert ledger.json()["total"] == 2  # create + ONE correction

    async def test_same_key_different_request_is_a_conflict(
        self, task_app: FastAPI, jar: dict[str, str]
    ) -> None:
        conversation = await _make_conversation(task_app, SERVER_A)
        message = await _make_message(task_app, conversation)
        task = await _make_task(task_app, conversation_id=conversation)
        body = _correction_body(SERVER_A, 1, message, title="renamed once")
        path = f"/api/v1/chat/tasks/{task.task_id}/revisions"
        first = await call_app(
            task_app,
            "POST",
            path,
            json_body=body,
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert first.status == 201
        reused_key = dict(body)
        reused_key["patch"] = dict(body["patch"], title="something else")
        conflict = await call_app(
            task_app,
            "POST",
            path,
            json_body=reused_key,
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert conflict.status == 422
        assert _error(conflict)["code"] == "invalid_task_patch"

    async def test_stale_tab_conflicts_instead_of_overwriting(
        self, task_app: FastAPI, jar: dict[str, str]
    ) -> None:
        conversation = await _make_conversation(task_app, SERVER_A)
        message = await _make_message(task_app, conversation)
        task = await _make_task(task_app, conversation_id=conversation)
        path = f"/api/v1/chat/tasks/{task.task_id}/revisions"

        fresh = _correction_body(
            SERVER_A,
            1,
            message,
            operations=[{"op": "add", "step": {"step_id": "s8", "title": "step s8"}}],
        )
        accepted = await call_app(
            task_app,
            "POST",
            path,
            json_body=fresh,
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert accepted.status == 201

        # The stale tab still holds revision 1 and submits its own delta.
        stale = _correction_body(
            SERVER_A,
            1,
            message,
            operations=[{"op": "add", "step": {"step_id": "s7", "title": "step s7"}}],
        )
        rejected = await call_app(
            task_app,
            "POST",
            path,
            json_body=stale,
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert rejected.status == 409, rejected.body
        error = _error(rejected)
        assert error["code"] == "revision_conflict"
        assert error["fields"] == {"base_revision": "revision_conflict"}

        items = await call_app(
            task_app,
            "GET",
            f"/api/v1/chat/tasks/{task.task_id}/items?server_id={SERVER_A}",
            cookies=jar,
        )
        # The accepted writer's state stands; the stale delta is absent.
        assert _step_ids(items.json()) == ["s1", "s2", "s3", "s8"]

    async def test_correction_requires_a_real_source_message(
        self, task_app: FastAPI, jar: dict[str, str]
    ) -> None:
        conversation = await _make_conversation(task_app, SERVER_A)
        task = await _make_task(task_app, conversation_id=conversation)
        body = _correction_body(SERVER_A, 1, uuid.uuid4(), title="hallucinated")
        response = await call_app(
            task_app,
            "POST",
            f"/api/v1/chat/tasks/{task.task_id}/revisions",
            json_body=body,
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert response.status == 422
        error = _error(response)
        assert error["code"] == "invalid_task_patch"
        assert error["fields"] == {"source_message_id": "invalid_task_patch"}

    async def test_collections_are_paginated_and_scoped(
        self, task_app: FastAPI, jar: dict[str, str]
    ) -> None:
        conversation = await _make_conversation(task_app, SERVER_A)
        task = await _make_task(
            task_app,
            conversation_id=conversation,
            constraints=[_constraint("c1"), _constraint("c2")],
            steps=[
                _step("s1"),
                _step("s2", depends_on=["s1"]),
                _step("s3"),
            ],
        )
        base = f"/api/v1/chat/tasks/{task.task_id}"

        items_first = await call_app(
            task_app,
            "GET",
            f"{base}/items?server_id={SERVER_A}&limit=2",
            cookies=jar,
        )
        assert items_first.status == 200
        page = items_first.json()
        assert (page["limit"], page["offset"], page["total"], page["has_more"]) == (
            2,
            0,
            3,
            True,
        )
        assert _step_ids(page) == ["s1", "s2"]
        items_second = await call_app(
            task_app,
            "GET",
            f"{base}/items?server_id={SERVER_A}&limit=2&offset=2",
            cookies=jar,
        )
        tail = items_second.json()
        assert (_step_ids(tail), tail["has_more"], tail["total"]) == (["s3"], False, 3)

        constraints_first = await call_app(
            task_app,
            "GET",
            f"{base}/constraints?server_id={SERVER_A}&limit=1",
            cookies=jar,
        )
        cpage = constraints_first.json()
        assert (cpage["total"], cpage["has_more"], len(cpage["items"])) == (2, True, 1)
        assert cpage["items"][0]["constraint_id"] == "c1"
        assert cpage["items"][0]["origin"] == "user"
        constraints_second = await call_app(
            task_app,
            "GET",
            f"{base}/constraints?server_id={SERVER_A}&limit=1&offset=1",
            cookies=jar,
        )
        ctail = constraints_second.json()
        assert (ctail["total"], ctail["has_more"]) == (2, False)
        assert ctail["items"][0]["constraint_id"] == "c2"

        header = await call_app(
            task_app,
            "GET",
            f"/api/v1/chat/conversations/{conversation}/task?server_id={SERVER_A}",
            cookies=jar,
        )
        assert header.status == 200
        view = header.json()
        assert view["task_id"] == str(task.task_id)
        assert view["conversation_id"] == str(conversation)
        assert view["revision"] == 1
        assert view["status"] == "in_progress"
        assert view["requested_phase"] == "plan"
        assert view["counts"] == {
            "steps_total": 3,
            "steps_executed": 0,
            "constraints_total": 2,
            "evidence_total": 0,
            "criteria_total": 0,
            "criteria_verified": 0,
        }

    async def test_conversation_without_a_task_is_the_same_scope_blind_404(
        self, task_app: FastAPI, jar: dict[str, str]
    ) -> None:
        conversation = await _make_conversation(task_app, SERVER_A)
        response = await call_app(
            task_app,
            "GET",
            f"/api/v1/chat/conversations/{conversation}/task?server_id={SERVER_A}",
            cookies=jar,
        )
        assert response.status == 404
        assert _error(response)["code"] == "task_not_found"


# ---------------------------------------------------------------------------
# Criterion 2 — distinct decisions; metadata cannot authorize or terminate
# ---------------------------------------------------------------------------


class TestCriterion2Decisions:
    async def test_phase_selection_is_distinct_from_plan_review(
        self, task_app: FastAPI, jar: dict[str, str]
    ) -> None:
        conversation = await _make_conversation(task_app, SERVER_A)
        message = await _make_message(task_app, conversation)
        task = await _make_task(task_app, conversation_id=conversation)
        base = f"/api/v1/chat/tasks/{task.task_id}"
        header_path = (
            f"/api/v1/chat/conversations/{conversation}/task?server_id={SERVER_A}"
        )

        before = (await call_app(task_app, "GET", header_path, cookies=jar)).json()
        assert before["requested_phase"] == "plan"

        decision = _decision_body(SERVER_A, 1, requested_phase="inspect")
        response = await call_app(
            task_app,
            "POST",
            f"{base}/decisions",
            json_body=decision,
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert response.status == 201, response.body
        outcome = response.json()
        assert outcome["disposition"] == "created"
        assert outcome["task"]["requested_phase"] == "inspect"
        assert outcome["task"]["status"] == "in_progress"  # phase never terminates
        assert outcome["revision"]["kind"] == "metadata"
        assert outcome["revision"]["author"] == "user"

        items_after = await call_app(
            task_app, "GET", f"{base}/items?server_id={SERVER_A}", cookies=jar
        )
        assert _step_ids(items_after.json()) == ["s1", "s2", "s3"]

        replay = await call_app(
            task_app,
            "POST",
            f"{base}/decisions",
            json_body=decision,
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert replay.status == 200
        assert replay.json()["disposition"] == "deduplicated"
        assert replay.json()["task"]["revision"] == 2

        stale = await call_app(
            task_app,
            "POST",
            f"{base}/decisions",
            json_body=_decision_body(SERVER_A, 1, requested_phase="act"),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert stale.status == 409
        assert _error(stale)["code"] == "revision_conflict"

        # Plan review is a DIFFERENT route with DIFFERENT effects: it never
        # moves the phase the decision route owns.
        correction = _correction_body(SERVER_A, 2, message, title="renamed")
        accepted = await call_app(
            task_app,
            "POST",
            f"{base}/revisions",
            json_body=correction,
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert accepted.status == 201
        assert accepted.json()["task"]["requested_phase"] == "inspect"
        assert accepted.json()["revision"]["kind"] == "plan_patch"
        after = (await call_app(task_app, "GET", header_path, cookies=jar)).json()
        assert after["requested_phase"] == "inspect"
        assert after["status"] == "in_progress"

    async def test_pause_and_cancel_are_explicit_terminal_aware_controls(
        self, task_app: FastAPI, jar: dict[str, str]
    ) -> None:
        conversation = await _make_conversation(task_app, SERVER_A)
        task = await _make_task(task_app, conversation_id=conversation)
        base = f"/api/v1/chat/tasks/{task.task_id}"
        header_path = (
            f"/api/v1/chat/conversations/{conversation}/task?server_id={SERVER_A}"
        )

        paused = await call_app(
            task_app,
            "POST",
            f"{base}/decisions",
            json_body=_decision_body(SERVER_A, 1, task_action="pause"),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert paused.status == 201
        assert paused.json()["task"]["status"] == "needs_input"
        assert paused.json()["task"]["revision"] == 2

        cancelled = await call_app(
            task_app,
            "POST",
            f"{base}/decisions",
            json_body=_decision_body(SERVER_A, 2, task_action="cancel"),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert cancelled.status == 201
        assert cancelled.json()["task"]["status"] == "cancelled"

        # Terminal close releases the conversation's active-task pointer…
        header = await call_app(task_app, "GET", header_path, cookies=jar)
        assert header.status == 404
        assert _error(header)["code"] == "task_not_found"
        # …while the recorded episode and its plan stay readable history.
        items = await call_app(
            task_app, "GET", f"{base}/items?server_id={SERVER_A}", cookies=jar
        )
        assert items.status == 200
        assert _step_ids(items.json()) == ["s1", "s2", "s3"]

    async def test_metadata_edit_cannot_authorize_or_terminate_operations(
        self, task_app: FastAPI, jar: dict[str, str]
    ) -> None:
        from mai.policy.approvals import ApprovalRequest

        conversation = await _make_conversation(task_app, SERVER_A)
        message = await _make_message(task_app, conversation)
        task = await _make_task(task_app, conversation_id=conversation)
        base = f"/api/v1/chat/tasks/{task.task_id}"

        approval = await task_app.state.approvals.create(
            ApprovalRequest(
                capability_id="host.shell",
                server_id=SERVER_A,
                payload_hash="a" * 64,
                policy_revision=1,
                created_by="fixture-principal",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        approval_path = f"/api/v1/approvals/{approval.approval_id}"
        pending = await call_app(task_app, "GET", approval_path, cookies=jar)
        assert pending.status == 200
        assert pending.json()["status"] == "pending"

        # A metadata-only edit: rename/re-describe with zero operations.
        rename = _correction_body(
            SERVER_A,
            1,
            message,
            title="renamed objective",
            summary="re-described objective",
        )
        accepted = await call_app(
            task_app,
            "POST",
            f"{base}/revisions",
            json_body=rename,
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert accepted.status == 201, accepted.body
        assert accepted.json()["task"]["title"] == "renamed objective"

        shown = await call_app(task_app, "GET", approval_path, cookies=jar)
        assert shown.status == 200
        assert shown.json()["status"] == "pending"
        assert shown.json()["consumed_at"] is None

        after = await call_app(
            task_app,
            "GET",
            f"/api/v1/chat/conversations/{conversation}/task?server_id={SERVER_A}",
            cookies=jar,
        )
        assert after.status == 200
        # The metadata edit never terminated the episode's operations…
        assert after.json()["status"] == "in_progress"
        assert after.json()["revision"] == 2
        # …and the operation approval still requires its own C03 decision.
        assert shown.json()["payload_hash"] == "a" * 64

        phase_decision = await call_app(
            task_app,
            "POST",
            f"{base}/decisions",
            json_body=_decision_body(SERVER_A, 2, requested_phase="act"),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert phase_decision.status == 201
        shown_after_decision = await call_app(
            task_app, "GET", approval_path, cookies=jar
        )
        assert shown_after_decision.json()["status"] == "pending"
