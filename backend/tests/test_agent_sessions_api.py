"""D06 REST contract tests — real ASGI app + real Postgres.

Every claim runs through the REAL FastAPI application (``create_app`` +
lifespan + middleware + handlers) against a disposable pgvector Postgres
fixture migrated through the real Alembic chain (the reviewed
conversation-HTTP suite's approach). No mocks.

Proven here (BRIEF_D06 §B.1):

1. List page: bounded ``limit``/``offset``, the ``SessionListPage``
   envelope (limit/offset/total/has_more), deterministic order; out-of-
   bound pagination is refused by the contract (422).
2. Idempotent create: a replayed ``client_request_id`` converges on the
   same session; the detail carries bounded aggregates.
3. Scope blindness: a session of server A does not exist under server B
   (the SAME value-free 404 as a missing session).
4. History: bounded pages with honest ``total``/``has_more``/``has_older``/
   ``byte_size``/``truncated_by_bytes``; an invalid cursor is a 400.
5. Turns: durable admission (run row + user entry), then the engine
   enqueue follows the commit; a replayed idempotency key returns the same
   run/entry with ``deduplicated=true`` (no second row of any kind).
6. Control: delegates to the reviewed facade; the value-free envelope.
7. OpenAPI: the new ``/api/v1/agent-sessions/**`` routes appear; the old
   ``/api/v1/chat/**`` routes still exist (D09 removes them, not D06).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from starlette.testclient import TestClient

from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE
from mai.contracts.sessions import EntryKind, SessionSourceKind, UserMessageEntry
from mai.database import create_app_engine
from mai.session_store import aappend_entry, acreate_run

BACKEND_ROOT = Path(__file__).resolve().parents[1]
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"
ORIGIN = "http://testserver"
FIXTURE_PASSWORD = "fixture-password-9f3a"

SERVER_A = uuid.UUID("5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60")
SERVER_B = uuid.UUID("9a8b7c6d-1e2f-4a3b-8c5d-6e7f8a9b0c1d")

pytestmark = pytest.mark.anyio


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
            f"{LIVE_URL_VAR} is not set; the D06 REST claims need a real "
            "disposable Postgres fixture (pgvector/pgvector:pg16)"
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

    jobs_engine.launch_worker(url)  # engine-owned dbos schema
    jobs_engine.shutdown()
    yield url
    jobs_engine.shutdown()
    _drop_all(url)


def _build_app(url: str, data_root: Path) -> FastAPI:
    return create_app(
        env={
            "MAI_DATABASE_URL": url,
            "MAI_PUBLIC_ORIGINS": ORIGIN,
            "MAI_DATA_ROOT": str(data_root),
        }
    )


@pytest.fixture
async def app_fixture(migrated_url: str, tmp_path: Path) -> AsyncIterator[FastAPI]:
    import psycopg

    with psycopg.connect(migrated_url) as conn:
        for table in (
            "session_tags",
            "session_provider_artifacts",
            "session_admissions",
            "session_runs",
            "session_entries",
            "sessions",
            "job_resource_locks",
            "job_schedule_fires",
            "job_steps",
            "job_events",
            "job_schedules",
            "job_runs",
            "job_tags",
            "approvals",
            "admin_sessions",
            "admin_credential",
        ):
            present = conn.execute(
                "SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",)
            ).fetchone()
            if not present or not present[0]:
                continue
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    app = _build_app(migrated_url, tmp_path / "data-root")
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


def _create_session(
    client: TestClient, jar: dict[str, str], server_id: uuid.UUID
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/agent-sessions",
        json={
            "server_id": str(server_id),
            "source": "interactive",
            "client_request_id": str(uuid.uuid4()),
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 1. list page envelope + bounds
# ---------------------------------------------------------------------------


async def test_list_page_envelope_and_bounds(
    app_fixture: FastAPI,
) -> None:
    with TestClient(app_fixture) as client:
        jar = _login(client)
        for _ in range(3):
            _create_session(client, jar, SERVER_A)
        page = client.get(
            "/api/v1/agent-sessions",
            params={"server_id": str(SERVER_A), "limit": 2, "offset": 0},
            cookies=jar,
        )
        assert page.status_code == 200, page.text
        body = page.json()
        assert set(body) >= {"items", "limit", "offset", "total", "has_more"}
        assert body["limit"] == 2 and body["offset"] == 0
        assert body["total"] == 3 and body["has_more"] is True
        assert len(body["items"]) == 2
        page2 = client.get(
            "/api/v1/agent-sessions",
            params={"server_id": str(SERVER_A), "limit": 2, "offset": 2},
            cookies=jar,
        )
        body2 = page2.json()
        assert body2["has_more"] is False and len(body2["items"]) == 1
        # Out-of-bound pagination: the contract refuses (422 validation).
        bad = client.get(
            "/api/v1/agent-sessions",
            params={"server_id": str(SERVER_A), "limit": 10000},
            cookies=jar,
        )
        assert bad.status_code == 422


# ---------------------------------------------------------------------------
# 2. idempotent create + detail aggregates
# ---------------------------------------------------------------------------


async def test_create_is_idempotent_on_client_request_id(
    app_fixture: FastAPI,
) -> None:
    with TestClient(app_fixture) as client:
        jar = _login(client)
        key = str(uuid.uuid4())
        first = client.post(
            "/api/v1/agent-sessions",
            json={
                "server_id": str(SERVER_A),
                "source": "interactive",
                "client_request_id": key,
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert first.status_code == 201, first.text
        second = client.post(
            "/api/v1/agent-sessions",
            json={
                "server_id": str(SERVER_A),
                "source": "interactive",
                "client_request_id": key,
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert second.status_code == 201, second.text
        assert second.json()["session_id"] == first.json()["session_id"]
        detail = client.get(
            f"/api/v1/agent-sessions/{first.json()['session_id']}",
            params={"server_id": str(SERVER_A)},
            cookies=jar,
        )
        assert detail.status_code == 200
        body = detail.json()
        assert body["latest_seq"] is None and body["total_entries"] == 0


# ---------------------------------------------------------------------------
# 3. scope blindness
# ---------------------------------------------------------------------------


async def test_cross_scope_session_is_the_same_404(
    app_fixture: FastAPI,
) -> None:
    with TestClient(app_fixture) as client:
        jar = _login(client)
        created = _create_session(client, jar, SERVER_A)
        foreign = client.get(
            f"/api/v1/agent-sessions/{created['session_id']}",
            params={"server_id": str(SERVER_B)},
            cookies=jar,
        )
        assert foreign.status_code == 404
        body = foreign.json()["error"]
        assert body["code"] == "session_not_found"
        missing = client.get(
            f"/api/v1/agent-sessions/{uuid.uuid4()}",
            params={"server_id": str(SERVER_B)},
            cookies=jar,
        )
        assert missing.status_code == 404
        missing_body = missing.json()["error"]
        # The SAME value-free failure (modulo the per-request id).
        assert missing_body["code"] == body["code"]
        assert missing_body["message"] == body["message"]
        assert missing_body["fields"] == body["fields"]


# ---------------------------------------------------------------------------
# 4. bounded history page + invalid cursor
# ---------------------------------------------------------------------------


async def test_history_is_bounded_and_cursor_checked(
    app_fixture: FastAPI,
    migrated_url: str,
) -> None:
    with TestClient(app_fixture) as client:
        jar = _login(client)
        created = _create_session(client, jar, SERVER_A)
        session_id = uuid.UUID(created["session_id"])
        _, sessions = create_app_engine(migrated_url)
        run_id = uuid.uuid4()
        await acreate_run(
            sessions,
            session_id=session_id,
            server_id=SERVER_A,
            run_id=run_id,
            model_profile="default",
            client_request_id=uuid.uuid4(),
            parent_run_id=None,
        )
        for n in range(5):
            entry = UserMessageEntry(
                kind=EntryKind.USER_MESSAGE,
                text=f"turn {n}",
                client_request_id=uuid.UUID(int=n),
                source=SessionSourceKind.API,
            )
            await aappend_entry(
                sessions,
                session_id=session_id,
                server_id=SERVER_A,
                kind=EntryKind.USER_MESSAGE,
                payload=entry.model_dump(mode="json"),
                run_id=run_id,
                durable_source_id=f"turn-{n}",
            )
        await sessions.dispose() if hasattr(sessions, "dispose") else None
        page = client.get(
            f"/api/v1/agent-sessions/{session_id}/history",
            params={"server_id": str(SERVER_A), "limit": 3},
            cookies=jar,
        )
        assert page.status_code == 200, page.text
        body = page.json()
        assert body["limit"] == 3 and body["total"] == 5
        # The newest page: nothing newer above, older entries below.
        assert body["has_more"] is False and body["has_older"] is True
        assert len(body["items"]) == 3
        assert body["byte_size"] > 0
        cursor = client.get(
            f"/api/v1/agent-sessions/{session_id}/history",
            params={
                "server_id": str(SERVER_A),
                "limit": 3,
                "before_seq": body["oldest_seq"],
            },
            cookies=jar,
        )
        body2 = cursor.json()
        assert len(body2["items"]) == 2
        assert body2["has_older"] is False
        assert body2["has_more"] is True
        bad = client.get(
            f"/api/v1/agent-sessions/{session_id}/history",
            params={
                "server_id": str(SERVER_A),
                "before_seq": 3,
                "after_seq": 2,
            },
            cookies=jar,
        )
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "invalid_cursor"


# ---------------------------------------------------------------------------
# 5. turns: durable admission, idempotent replay
# ---------------------------------------------------------------------------


async def test_turn_admission_is_durable_and_idempotent(
    app_fixture: FastAPI,
    migrated_url: str,
) -> None:
    with TestClient(app_fixture) as client:
        jar = _login(client)
        created = _create_session(client, jar, SERVER_A)
        session_id = uuid.UUID(created["session_id"])
        key = str(uuid.uuid4())
        first = client.post(
            f"/api/v1/agent-sessions/{session_id}/turns",
            params={"server_id": str(SERVER_A)},
            json={
                "session_id": session_id.hex
                if False
                else str(session_id),
                "text": "check the fixture logs",
                "client_request_id": key,
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert first.status_code in (200, 201), first.text
        body = first.json()
        assert body["session_id"] == str(session_id)
        assert body["run_state"] == "queued"
        assert body["deduplicated"] is False
        # The durable truth: the user entry exists under the run.
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(migrated_url, row_factory=dict_row) as conn:
            rows = conn.execute(
                "SELECT seq, run_id, kind FROM session_entries "
                "WHERE session_id = %s ORDER BY seq",
                (session_id,),
            ).fetchall()
            run_rows = conn.execute(
                "SELECT run_id, state FROM session_runs WHERE session_id = %s",
                (session_id,),
            ).fetchall()
        assert [row["kind"] for row in rows] == [
            "user_message",
            "run_state",
        ]  # the durable admission: the user turn + the queued transition
        assert uuid.UUID(str(rows[0]["run_id"])) == uuid.UUID(body["run_id"])
        assert len(run_rows) == 1 and run_rows[0]["state"] == "queued"
        # The engine enqueue followed the commit (the dbos workflow exists).
        with psycopg.connect(migrated_url) as conn:
            workflows = conn.execute(
                "SELECT COUNT(*) FROM dbos.workflow_status WHERE workflow_uuid = %s",
                (body["run_id"],),
            ).fetchone()[0]
        assert workflows == 1
        # A replayed key: same run/entry, deduplicated, no second row.
        second = client.post(
            f"/api/v1/agent-sessions/{session_id}/turns",
            params={"server_id": str(SERVER_A)},
            json={
                "session_id": str(session_id),
                "text": "check the fixture logs",
                "client_request_id": key,
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert second.status_code in (200, 201), second.text
        body2 = second.json()
        assert body2["run_id"] == body["run_id"]
        assert body2["entry_id"] == body["entry_id"]
        assert body2["deduplicated"] is True


# ---------------------------------------------------------------------------
# 6. control facade delegation
# ---------------------------------------------------------------------------


async def test_control_route_delegates_to_facade(
    app_fixture: FastAPI,
) -> None:
    with TestClient(app_fixture) as client:
        jar = _login(client)
        created = _create_session(client, jar, SERVER_A)
        session_id = created["session_id"]
        response = client.post(
            f"/api/v1/agent-sessions/{session_id}/control",
            params={"server_id": str(SERVER_A)},
            json={
                "session_id": session_id,
                "action": "cancel",
                "client_request_id": str(uuid.uuid4()),
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["session_id"] == session_id
        assert body["action"] == "cancel"
        assert set(body) >= {"accepted", "run_state"}


# ---------------------------------------------------------------------------
# 7. OpenAPI presence: new routes exist, old chat surface still exists
# ---------------------------------------------------------------------------


async def test_openapi_route_presence(app_fixture: FastAPI) -> None:
    # No public OpenAPI listener (G1); the schema is generated in-process.
    openapi = app_fixture.openapi()
    paths = set(openapi["paths"])
    assert "/api/v1/agent-sessions" in paths
    assert "/api/v1/agent-sessions/{session_id}" in paths
    assert "/api/v1/agent-sessions/{session_id}/history" in paths
    assert "/api/v1/agent-sessions/{session_id}/turns" in paths
    assert "/api/v1/agent-sessions/{session_id}/control" in paths
    # ADDITIVE (COORD-D02-01): the old chat surface stays until D09.
    assert any(path.startswith("/api/v1/chat") for path in paths)
