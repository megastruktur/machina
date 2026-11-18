"""ask_26: the scoped knowledge HTTP API tests (real ASGI + fixture DB).

Real ``create_app`` + the NEW ``register_service_knowledge`` router over
the disposable fixture PostgreSQL: authenticated pages, strict same-server
scope, C01 envelopes, operator retraction under the coordinator lease and
authorized retry of unacknowledged preservation operations.

Composition note (recorded limitation): the API root wiring of the router
lands with the integration slice — the tests mount the exported
``register_service_knowledge`` on the REAL ``create_app`` instance
themselves, exercising the exact production seam.
"""

from __future__ import annotations

import json
import os
import pathlib
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE
from mai.applications.knowledge_write import KnowledgeWriteStore
from mai.applications.learning_operations import LearningOperationStore
from mai.database import create_app_engine
from mai.api.service_knowledge import register_service_knowledge

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"
ORIGIN = "http://testserver"
FIXTURE_PASSWORD = "fixture-password-ask-26"

# The suites run async fixtures through the anyio pytest plugin (repo convention).
pytestmark = pytest.mark.anyio

SERVER_A = uuid.UUID("5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60")
SERVER_B = uuid.UUID("9a8b7c6d-1e2f-4a3b-8c5d-6e7f8a9b0c1d")
APP_MAIN_ID = uuid.UUID("0aaaaaaa-0000-4000-8000-000000000001")

_LEARN_REF = "00000000-0000-4000-8000-000000000001"

_FACT = {
    "category": "deployment",
    "key": "health-path",
    "statement": "health lives at /healthz",
    "source_kind": "tool_result",
    "source_refs": [_LEARN_REF],
}


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(str(BACKEND_ROOT / "alembic.ini"))


def _drop_all(url: str) -> None:
    import psycopg

    with psycopg.connect(url) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


@pytest.fixture(scope="module")
def migrated_url() -> Iterator[str]:
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the knowledge HTTP claims need a "
            "real disposable Postgres fixture"
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
    from mai.jobs import engine as jobs_engine

    # Bootstrap launch ONLY: creates the engine-owned ``dbos`` schema so
    # the learn enqueue seam can submit from the API process. The engine
    # itself never runs in the test process.
    jobs_engine.launch_worker(url)
    jobs_engine.shutdown()
    yield url
    jobs_engine.shutdown()
    _drop_all(url)


@pytest.fixture
async def app_fixture(
    migrated_url: str, tmp_path: pathlib.Path
) -> AsyncIterator[FastAPI]:
    import psycopg

    with psycopg.connect(migrated_url, autocommit=True) as conn:
        for table in (
            "service_learned_fact_revision",
            "service_learned_fact",
            "service_learning_operation",
            "service_knowledge_identity",
            "session_entries",
            "session_runs",
            "sessions",
            "job_resource_locks",
        ):
            if conn.execute(
                "SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",)
            ).fetchone()[0]:
                conn.execute(f"TRUNCATE TABLE {table} CASCADE")
    app = create_app(
        env={
            "MAI_DATABASE_URL": migrated_url,
            "MAI_PUBLIC_ORIGINS": ORIGIN,
            "MAI_DATA_ROOT": str(tmp_path / "data-root"),
        }
    )
    register_service_knowledge(app)
    with TestClient(app) as client:
        yield client.app  # type: ignore[misc]


def _login(client: TestClient) -> dict[str, str]:
    bootstrap = client.post(
        "/api/v1/auth/bootstrap",
        json={"password": FIXTURE_PASSWORD},
        headers={"origin": ORIGIN},
    )
    assert bootstrap.status_code in (201, 409)
    login = client.post(
        "/api/v1/auth/login",
        json={"password": FIXTURE_PASSWORD},
        headers={"origin": ORIGIN},
    )
    assert login.status_code == 204, login.text
    jar = {name: value for name, value in login.cookies.items()}
    assert SESSION_COOKIE in jar and CSRF_COOKIE in jar
    return jar


def _mutation_headers(jar: dict[str, str]) -> dict[str, str]:
    return {"origin": ORIGIN, "x-csrf-token": jar[CSRF_COOKIE]}


def _seed_foreground_run(
    url: str,
    factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
) -> uuid.UUID:
    """One real foreground session run + a successful tool-result entry
    (the provenance a learn fact must reference)."""
    from mai.session_store import acreate_session
    from mai.contracts.sessions import SessionSourceKind

    session_row = _run(
        acreate_session(factory, server_id=SERVER_A, source=SessionSourceKind.INTERACTIVE)
    )
    ref = uuid.uuid4()
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO session_runs (run_id, session_id, server_id, "
            "state, model_profile, client_request_id) VALUES "
            "(%s, %s, %s, 'running', 'test-profile', %s)",
            (
                str(run_id),
                str(session_row.session_id),
                str(SERVER_A),
                str(uuid.uuid4()),
            ),
        )
        conn.execute(
            "INSERT INTO session_entries (entry_id, session_id, run_id, "
            "seq, kind, payload, durable_source_id) VALUES "
            "(%s, %s, %s, 1, 'tool_result', %s::jsonb, %s)",
            (
                str(ref),
                str(session_row.session_id),
                str(run_id),
                json.dumps({"status": "executed", "output": "ok"}),
                str(uuid.uuid4()),
            ),
        )
    return ref


def _apply_fact(
    factory: async_sessionmaker[AsyncSession],
    url: str,
    *,
    application_id: uuid.UUID = APP_MAIN_ID,
    key: str | None = None,
    statement: str | None = None,
    expected_generation: int = 1,
) -> uuid.UUID:
    """Apply one learned fact through the REAL write seam (identity
    creation + provenance validation included). Returns the fact id."""
    from mai.applications.knowledge_contracts import (
        LearnRequest,
        ProposedFactDelta,
        TrustedSourceContext,
    )

    run_id = uuid.uuid4()
    ref = _seed_foreground_run(url, factory, run_id)
    trusted = TrustedSourceContext(
        server_id=SERVER_A,
        run_id=run_id,
        session_id=None,
        tool_call_id=str(uuid.uuid4()),
        actor="operator",
        grants=["app.learn_service"],
        context_at=datetime.now(tz=timezone.utc),
    )
    statement = statement or str(_FACT["statement"])
    request = LearnRequest(
        facts=[
            ProposedFactDelta(
                category=str(_FACT["category"]),
                key=key or str(_FACT["key"]),
                statement=statement,
                source_kind="tool_result",
                source_refs=[str(ref)],
            )
        ],
        expected_generation=expected_generation,
    )
    write = KnowledgeWriteStore(factory)
    result = _run(
        write.apply_facts(
            server_id=SERVER_A,
            application_id=application_id,
            request=request,
            trusted=trusted,
            expected_generation=expected_generation,
        )
    )
    assert result.status.value == "applied", (
        result.status,
        result.error_code,
        result.error_detail,
    )
    fact_id = result.applied_fact_ids[0]
    return fact_id


def _run(coro: Any) -> Any:
    import asyncio
    import threading

    global _LOOP  # noqa: PLW0603
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        threading.Thread(target=_LOOP.run_forever, daemon=True).start()
    return asyncio.run_coroutine_threadsafe(coro, _LOOP).result()


_LOOP: Any = None


class _DeferredEngine:
    """Enqueue seam fake that records WITHOUT acknowledging (raises):
    the admitted row stays honestly ``queued``."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, workflow_id: str) -> None:
        self.calls.append(workflow_id)
        raise RuntimeError("simulated outage: enqueue never acknowledged")


def _admit_queued(
    app: FastAPI,
    *,
    application_id: uuid.UUID = APP_MAIN_ID,
) -> tuple[uuid.UUID, uuid.UUID]:

    from mai.applications.knowledge_contracts import (
        LearnRequest,
        ProposedFactDelta,
        TrustedSourceContext,
    )

    operations: LearningOperationStore = app.state.learning_operations
    run_id = uuid.uuid4()
    tool_call_id = str(uuid.uuid4())
    trusted = TrustedSourceContext(
        server_id=SERVER_A,
        run_id=run_id,
        session_id=None,
        tool_call_id=tool_call_id,
        actor="principal",
        grants=["app.learn_service"],
        context_at=datetime.now(tz=timezone.utc),
    )
    request = LearnRequest(
        facts=[
            ProposedFactDelta(
                category=str(_FACT["category"]),
                key=str(_FACT["key"]),
                statement=str(_FACT["statement"]),
                source_kind="tool_result",
                source_refs=[_LEARN_REF],
            )
        ],
        expected_generation=1,
    )
    operation_id = uuid.uuid5(
        uuid.NAMESPACE_URL, f"mai:learning-operation:{run_id}:{tool_call_id}"
    )

    from mai.applications.learning_operations import OperationAdmissionError

    async def _admit() -> None:
        await operations.admit(
            request=request,
            trusted=trusted,
            application_id=application_id,
            engine_enqueue=_DeferredEngine(),
        )

    try:
        _run(_admit())
    except OperationAdmissionError as error:
        # The deferred seam never acknowledges: the row stays queued.
        assert error.code == "enqueue_failed", error.code
    return operation_id, run_id


# ---------------------------------------------------------------------------
# 1. GET '' — current knowledge page, scalars, strict scope
# ---------------------------------------------------------------------------


async def test_read_page_scalars_and_strict_scope(
    app_fixture: FastAPI, migrated_url: str, tmp_path: pathlib.Path
) -> None:
    engine, factory = create_app_engine(migrated_url)
    try:
        _apply_fact(factory, migrated_url)
    finally:
        _run(engine.dispose())

    with TestClient(app_fixture) as client:
        response = client.get(
            f"/api/v1/servers/{SERVER_A}/applications/{APP_MAIN_ID}/knowledge",
            params={"limit": 5, "offset": 0},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["server_id"] == str(SERVER_A)
        assert body["application_id"] == str(APP_MAIN_ID)
        assert body["generation"] == 1
        assert body["facts"]["limit"] == 5
        assert body["facts"]["offset"] == 0
        assert body["facts"]["total"] == 1
        assert body["facts"]["items"][0]["statement"] == str(_FACT["statement"])
        assert len(json.dumps(body).encode()) < 65536

        # Strict same-server scope: unknown application and cross-server
        # path identities both answer 404 not_found (no existence leak).
        unknown = client.get(
            f"/api/v1/servers/{SERVER_A}/applications/{uuid.uuid4()}/knowledge"
        )
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "not_found"

        cross = client.get(
            f"/api/v1/servers/{SERVER_B}/applications/{APP_MAIN_ID}/knowledge"
        )
        assert cross.status_code == 404
        assert cross.json()["error"]["code"] == "not_found"

    _run(engine.dispose())


async def test_read_page_pagination_window(
    app_fixture: FastAPI, migrated_url: str, tmp_path: pathlib.Path
) -> None:
    engine, factory = create_app_engine(migrated_url)
    try:
        for index in range(3):
            _apply_fact(
                factory,
                migrated_url,
                key=f"key-{index}",
                statement=f"fact number {index}",
                expected_generation=1,
            )
    finally:
        _run(engine.dispose())

    with TestClient(app_fixture) as client:
        page = client.get(
            f"/api/v1/servers/{SERVER_A}/applications/{APP_MAIN_ID}/knowledge",
            params={"limit": 1, "offset": 1},
        )
        assert page.status_code == 200
        body = page.json()
        assert body["facts"]["total"] == 3
        assert body["facts"]["limit"] == 1
        assert body["facts"]["offset"] == 1
        assert len(body["facts"]["items"]) == 1

        beyond = client.get(
            f"/api/v1/servers/{SERVER_A}/applications/{APP_MAIN_ID}/knowledge",
            params={"limit": 5, "offset": 9},
        )
        assert beyond.status_code == 200
        assert beyond.json()["facts"]["total"] == 3
        assert beyond.json()["facts"]["items"] == []

    _run(engine.dispose())


# ---------------------------------------------------------------------------
# 2. GET /history — paginated revisions with fact_id filter
# ---------------------------------------------------------------------------


async def test_history_page_and_fact_filter(
    app_fixture: FastAPI, migrated_url: str, tmp_path: pathlib.Path
) -> None:
    engine, factory = create_app_engine(migrated_url)
    try:
        fact_one = _apply_fact(factory, migrated_url)
        fact_two = _apply_fact(
            factory, migrated_url, key="second-key", statement="second distinct fact"
        )
    finally:
        _run(engine.dispose())

    with TestClient(app_fixture) as client:
        page = client.get(
            f"/api/v1/servers/{SERVER_A}/applications/{APP_MAIN_ID}/knowledge/history"
        )
        assert page.status_code == 200
        body = page.json()
        assert body["total"] == 2
        assert len(body["items"]) == 2

        filtered = client.get(
            f"/api/v1/servers/{SERVER_A}/applications/{APP_MAIN_ID}/knowledge/history",
            params={"fact_id": str(fact_one)},
        )
        assert filtered.status_code == 200
        filtered_body = filtered.json()
        assert filtered_body["total"] == 1
        assert filtered_body["items"][0]["fact_id"] == str(fact_one)
        assert filtered_body["items"][0]["fact_id"] != str(fact_two)

        # Strict scope on the collection read as well.
        cross = client.get(
            f"/api/v1/servers/{SERVER_B}/applications/{APP_MAIN_ID}/knowledge/history"
        )
        assert cross.status_code == 404

    _run(engine.dispose())


# ---------------------------------------------------------------------------
# 3. GET /operations — paginated receipts with status filter
# ---------------------------------------------------------------------------


async def test_operations_page_and_status_filter(
    app_fixture: FastAPI, migrated_url: str, tmp_path: pathlib.Path
) -> None:
    operation_id, _run_id = _admit_queued(app_fixture)

    with TestClient(app_fixture) as client:
        page = client.get(
            f"/api/v1/servers/{SERVER_A}/applications/{APP_MAIN_ID}/knowledge/operations"
        )
        assert page.status_code == 200
        body = page.json()
        assert body["total"] == 1
        assert body["items"][0]["operation_id"] == str(operation_id)
        assert body["items"][0]["status"] == "queued"

        queued = client.get(
            f"/api/v1/servers/{SERVER_A}/applications/{APP_MAIN_ID}/knowledge/operations",
            params={"status": "queued"},
        )
        assert queued.json()["total"] == 1

        applied = client.get(
            f"/api/v1/servers/{SERVER_A}/applications/{APP_MAIN_ID}/knowledge/operations",
            params={"status": "applied"},
        )
        assert applied.json()["total"] == 0

        # Strict scope: the (server, application) predicates ARE the
        # scope — an unknown application owns no receipts (honest empty
        # page), and receipts never leak across applications.
        other = client.get(
            f"/api/v1/servers/{SERVER_A}/applications/{uuid.uuid4()}/knowledge/operations"
        )
        assert other.status_code == 200
        assert other.json()["total"] == 0


# ---------------------------------------------------------------------------
# 4. POST retract — operator CAS under the coordinator lease
# ---------------------------------------------------------------------------


async def test_retract_auth_boundary(
    app_fixture: FastAPI, migrated_url: str, tmp_path: pathlib.Path
) -> None:
    engine, factory = create_app_engine(migrated_url)
    try:
        fact_id = _apply_fact(factory, migrated_url)
    finally:
        _run(engine.dispose())

    base = (
        f"/api/v1/servers/{SERVER_A}/applications/{APP_MAIN_ID}"
        f"/knowledge/facts/{fact_id}/retract"
    )
    body = {"expected_revision": 1, "expected_generation": 1}

    with TestClient(app_fixture) as client:
        no_session = client.post(base, json=body)
        assert no_session.status_code == 401
        assert no_session.json()["error"]["code"] == "unauthenticated"

        jar = _login(client)
        # Origin allowlisted but CSRF token missing → layer 3 rejects.
        no_csrf = client.post(
            base, json=body, headers={"origin": ORIGIN}, cookies=jar
        )
        assert no_csrf.status_code == 403
        assert no_csrf.json()["error"]["code"] == "csrf_rejected"

        bad_origin = client.post(
            base,
            json=body,
            headers={"origin": "http://evil.example"},
            cookies=jar,
        )
        assert bad_origin.status_code == 403
        assert bad_origin.json()["error"]["code"] == "origin_rejected"

    _run(engine.dispose())


async def test_retract_applies_and_stale_revision_conflicts(
    app_fixture: FastAPI, migrated_url: str, tmp_path: pathlib.Path
) -> None:
    engine, factory = create_app_engine(migrated_url)
    try:
        fact_id = _apply_fact(factory, migrated_url)
    finally:
        _run(engine.dispose())

    base = (
        f"/api/v1/servers/{SERVER_A}/applications/{APP_MAIN_ID}"
        f"/knowledge/facts/{fact_id}/retract"
    )

    with TestClient(app_fixture) as client:
        jar = _login(client)
        headers = _mutation_headers(jar)

        stale = client.post(
            base,
            json={"expected_revision": 9, "expected_generation": 1},
            headers=headers,
            cookies=jar,
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["error"]["code"] == "revision_conflict"

        ok = client.post(
            base,
            json={
                "expected_revision": 1,
                "expected_generation": 1,
                "reason": "operator verified the statement is wrong",
            },
            headers=headers,
            cookies=jar,
        )
        assert ok.status_code == 200, ok.text
        result = ok.json()
        assert result["status"] == "applied"
        assert result["new_revision"] == 2
        assert uuid.UUID(result["fact_id"]) == fact_id

        # The durable truth: the fact is RETRACTED (never deleted) and
        # one retracted history row was appended.
        history = client.get(
            f"/api/v1/servers/{SERVER_A}/applications/{APP_MAIN_ID}/knowledge/history",
            params={"fact_id": str(fact_id)},
        )
        rows = history.json()["items"]
        assert rows[0]["status"] == "retracted"
        assert rows[0]["revision"] == 2

        # A re-retract with the CURRENT revision is a fresh CAS the seam
        # honors (status stays retracted, revision bumps again).
        again = client.post(
            base,
            json={"expected_revision": 2, "expected_generation": 1},
            headers=headers,
            cookies=jar,
        )
        assert again.status_code == 200, again.text
        assert again.json()["new_revision"] == 3

    _run(engine.dispose())


# ---------------------------------------------------------------------------
# 5. POST retry — authorized retry of unacknowledged operations
# ---------------------------------------------------------------------------


async def test_retry_lifecycle_and_refusals(
    app_fixture: FastAPI, migrated_url: str, tmp_path: pathlib.Path
) -> None:
    operation_id, _run_id = _admit_queued(app_fixture)
    base = (
        f"/api/v1/servers/{SERVER_A}/applications/{APP_MAIN_ID}"
        f"/knowledge/operations/{operation_id}/retry"
    )

    with TestClient(app_fixture) as client:
        jar = _login(client)
        headers = _mutation_headers(jar)

        # Unauthenticated retry is refused at the boundary.
        no_session = client.post(base)
        assert no_session.status_code == 401

        retry = client.post(base, headers=headers, cookies=jar)
        assert retry.status_code == 200, retry.text
        receipt = retry.json()
        assert receipt["status"] == "accepted"
        assert receipt["operation_id"] == str(operation_id)

        # The REAL queue client recorded the deterministic workflow.
        assert app_fixture.state.learn_engine_client.run_state(
            str(operation_id)
        ) is not None

        # A second retry hits an ACCEPTED (in-flight) row: refused.
        again = client.post(base, headers=headers, cookies=jar)
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "operation_not_runnable"


async def test_retry_refuses_cancelled_unknown_and_cross_scope(
    app_fixture: FastAPI, migrated_url: str, tmp_path: pathlib.Path
) -> None:
    cancelled_id, _run_a = _admit_queued(app_fixture)
    unknown_effect_id, _run_b = _admit_queued(app_fixture)
    unknown_op = uuid.uuid4()

    with psycopg.connect(migrated_url, autocommit=True) as conn:
        # Cancelled: the frozen vocabulary has no 'cancelled' status —
        # the store stamps failed + the source_cancelled code.
        conn.execute(
            "UPDATE service_learning_operation SET status = 'failed', "
            "error_code = 'source_cancelled' WHERE operation_id = %s",
            (str(cancelled_id),),
        )
        # Unknown-effect: reconcile-never-retry (C5).
        conn.execute(
            "UPDATE service_learning_operation SET sub_effects = "
            "'{\"facts\": \"unknown\"}'::jsonb WHERE operation_id = %s",
            (str(unknown_effect_id),),
        )

    with TestClient(app_fixture) as client:
        jar = _login(client)
        headers = _mutation_headers(jar)

        cancelled = client.post(
            f"/api/v1/servers/{SERVER_A}/applications/{APP_MAIN_ID}"
            f"/knowledge/operations/{cancelled_id}/retry",
            headers=headers,
            cookies=jar,
        )
        assert cancelled.status_code == 409
        assert cancelled.json()["error"]["code"] == "source_cancelled"

        unknown_effect = client.post(
            f"/api/v1/servers/{SERVER_A}/applications/{APP_MAIN_ID}"
            f"/knowledge/operations/{unknown_effect_id}/retry",
            headers=headers,
            cookies=jar,
        )
        assert unknown_effect.status_code == 409
        assert unknown_effect.json()["error"]["code"] == "unknown_effect"

        unknown = client.post(
            f"/api/v1/servers/{SERVER_A}/applications/{APP_MAIN_ID}"
            f"/knowledge/operations/{unknown_op}/retry",
            headers=headers,
            cookies=jar,
        )
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "not_found"

        # Cross-scope retry: the operation exists but under a DIFFERENT
        # application than the path → 404, never a substituted retry.
        other_app = uuid.uuid4()
        outcome = _admit_queued(app_fixture, application_id=other_app)
        cross = client.post(
            f"/api/v1/servers/{SERVER_A}/applications/{APP_MAIN_ID}"
            f"/knowledge/operations/{outcome[0]}/retry",
            headers=headers,
            cookies=jar,
        )
        assert cross.status_code == 404
        assert cross.json()["error"]["code"] == "not_found"
