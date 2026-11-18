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
   ``/api/v1/chat/**`` routes are absent (D09 removed them).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from pydantic_ai.messages import UserPromptPart
from starlette.requests import Request as StarletteRequest
from starlette.testclient import TestClient

from mai.agents.session_runner import parse_session_run_intent
from mai.agents.tools import CapabilityToolBridge, tool_name_for
from mai.api.agent_sessions import _profile_binding
from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE
from mai.api.capability_defaults import BUILTIN_DEFAULT_CAPABILITIES
from mai.capabilities.dispatch import (
    CapabilityCall,
    CapabilityDispatcher,
    ExecutorResult,
)
from mai.capabilities.history_tags import TAGS_CAPABILITY_IDS
from mai.capabilities.host_shell import descriptor as host_shell_descriptor
from mai.capabilities.internal_api import INTERNAL_API_CAPABILITY_IDS
from mai.contracts.sessions import (
    SERVICE_MAP_SECTION_HEADER,
    EntryKind,
    RunbookRef,
    SessionSourceKind,
    UserMessageEntry,
)
from mai.credentials.env import EnvResolver
from mai.database import create_app_engine
from mai.plugins.builtin_onepassword import (
    CONFIG_SCHEMA as ONEPASSWORD_CONFIG_SCHEMA,
    PLUGIN_NAME as ONEPASSWORD_PLUGIN,
    SECRET_GET_CAPABILITY,
    SECRET_LIST_CAPABILITY,
    SECRET_STORE_CAPABILITY,
)
from mai.plugins.registry import CapabilityRegistry, parse_descriptor
from mai.policy.approvals import ApprovalStore
from mai.policy.evaluate import (
    ExecutionContext,
    Policy,
    PolicyMode,
    effective_child_grants,
)
from mai.session_store import aappend_entry, acreate_run, acreate_session
from mai.contracts.server import SCHEMA_VERSION
from mai.tag_store import RESERVED_UNCLASSIFIED_TAG_ID

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


async def test_turn_admission_pins_ninety_rounds_on_the_enqueued_intent(
    app_fixture: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chat-turn seam enqueues a SessionRunIntent pinned to the
    90-round root budget: captured at the REAL ``enqueue_run`` seam (the
    payload the engine would receive), parsed through the strict shared
    parser — no DBOS payload storage is consulted or invented."""
    with TestClient(app_fixture) as client:
        recovery = client.app.state.job_recovery
        assert recovery is not None
        captured: list[str] = []
        original = recovery.client.enqueue_run

        def _capture(run_id: str, handler_name: str, payload_json: str) -> None:
            captured.append(payload_json)
            original(run_id, handler_name, payload_json)

        monkeypatch.setattr(recovery.client, "enqueue_run", _capture)
        jar = _login(client)
        created = _create_session(client, jar, SERVER_A)
        session_id = uuid.UUID(created["session_id"])
        first = client.post(
            f"/api/v1/agent-sessions/{session_id}/turns",
            params={"server_id": str(SERVER_A)},
            json={
                "session_id": str(session_id),
                "text": "pin the round budget",
                "client_request_id": str(uuid.uuid4()),
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert first.status_code in (200, 201), first.text
        body = first.json()
        assert len(captured) == 1
        intent = parse_session_run_intent(captured[0])
        assert intent.max_rounds == 90
        assert intent.source == SessionSourceKind.API
        assert intent.run_id == body["run_id"]


async def test_turn_admission_creates_execution_facade_with_real_binding(
    app_fixture: FastAPI, migrated_url: str
) -> None:
    """S2 (queued-chat hotfix): the admitted root run carries the retained
    execution facade atomically — job_runs depth 0 with the REAL profile
    binding (never fabricated 1/1) and exactly the RESOLVED admission set
    (CAP-1: grant-store grants + profile capabilities + internal API +
    built-in defaults, deduplicated) as requested_grants (no widening),
    plus the mandatory classification. Replay mutates nothing; a
    cross-server attempt writes nothing."""
    import psycopg
    from psycopg.rows import dict_row

    # A normal grant-store entry naming SERVER_A: the resolved admission
    # set must include it ahead of the profile capabilities.
    app_fixture.state.grants.write(
        {
            "policy": {"revision": 1, "denied_capabilities": []},
            "grants": [
                {
                    "capability_id": "host.shell",
                    "server_ids": [str(SERVER_A)],
                }
            ],
        },
        expected_revision=0,
    )

    # A real server profile: revision 2 (created, then updated),
    # prompt_revision 3, two capabilities — deliberately distinct values
    # so the facade proves caller passthrough.
    store = app_fixture.state.profiles
    assert store is not None, "the app fixture wires the profile file store"

    def profile_text(name: str) -> str:
        return f"""\
schema_version: {SCHEMA_VERSION}
server_id: "{SERVER_A}"
name: "{name}"
prompt_revision: 3
environment: linux_ssh
ssh:
  host: "198.51.100.10"
  port: 22
  user: "deploy"
  host_key: "SHA256:ZmXWe/xAeTz26CuAmJ1jHtg8ZPX0mMhVfrBPM+C/zS4"
  credential_ref: "MAI_SSH_KEY_BUILD_HOST"
privilege: none
capabilities:
  - shell.exec
  - files.read
"""

    store.write(SERVER_A, profile_text("s2-fixture"), expected_revision=0)
    stored = store.write(
        SERVER_A, profile_text("s2-fixture-renamed"), expected_revision=1
    )
    assert stored.revision == 2

    with TestClient(app_fixture) as client:
        jar = _login(client)
        created = _create_session(client, jar, SERVER_A)
        session_id = uuid.UUID(created["session_id"])
        key = str(uuid.uuid4())
        first = client.post(
            f"/api/v1/agent-sessions/{session_id}/turns",
            params={"server_id": str(SERVER_A)},
            json={
                "session_id": str(session_id),
                "text": "verify the execution facade",
                "client_request_id": key,
                "model_profile": "primary",
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert first.status_code in (200, 201), first.text
        body = first.json()
        assert body["run_state"] == "queued"
        run_id = uuid.UUID(body["run_id"])

        with psycopg.connect(migrated_url, row_factory=dict_row) as conn:
            run_row = conn.execute(
                "SELECT state, parent_run_id, model_profile, client_request_id "
                "FROM session_runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            facade = conn.execute(
                "SELECT server_id, depth, parent_run_id, profile_revision, "
                "prompt_revision, model_profile, requested_grants "
                "FROM job_runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            tags = conn.execute(
                "SELECT tag_id, assignment_source FROM job_tags "
                "WHERE job_id = %s",
                (run_id,),
            ).fetchall()
        assert run_row is not None and facade is not None
        assert run_row["state"] == "queued"
        assert run_row["parent_run_id"] is None
        assert run_row["model_profile"] == "primary"
        assert run_row["client_request_id"] == uuid.UUID(key)
        assert facade["server_id"] == SERVER_A
        assert facade["depth"] == 0  # a root, on the retained tree
        assert facade["parent_run_id"] is None
        assert facade["profile_revision"] == 2  # the stored profile revision
        assert facade["prompt_revision"] == 3  # the document's prompt revision
        assert facade["model_profile"] == "primary"
        # No widening: exactly the RESOLVED admission set. Documented
        # resolver dedup order: grant-store grants first (canonical
        # sorted order), then the profile capability slugs, then the
        # internal-API vocabulary, then the built-in defaults. The
        # facade persists the same set in the store's canonical sorted
        # form (json.dumps(sorted(...))).
        admitted = _profile_capabilities(app_fixture, SERVER_A)
        assert admitted == (
            "host.shell",
            "shell.exec",
            "files.read",
            *INTERNAL_API_CAPABILITY_IDS,
            *BUILTIN_DEFAULT_CAPABILITIES,
        )
        assert facade["requested_grants"] == sorted(admitted)
        assert len(tags) == 1  # the 0011 mandatory classification
        assert tags[0]["tag_id"] == uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID)
        assert tags[0]["assignment_source"] == "system"

        # Replay: same ids, no duplicates, facade metadata NOT mutated.
        second = client.post(
            f"/api/v1/agent-sessions/{session_id}/turns",
            params={"server_id": str(SERVER_A)},
            json={
                "session_id": str(session_id),
                "text": "verify the execution facade",
                "client_request_id": key,
                "model_profile": "primary",
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert second.status_code in (200, 201), second.text
        body2 = second.json()
        assert body2["run_id"] == body["run_id"]
        assert body2["entry_id"] == body["entry_id"]
        assert body2["deduplicated"] is True
        with psycopg.connect(migrated_url, row_factory=dict_row) as conn:
            counts = conn.execute(
                "SELECT (SELECT COUNT(*) FROM session_runs "
                "        WHERE run_id = %s) AS session_runs,"
                "       (SELECT COUNT(*) FROM job_runs "
                "        WHERE run_id = %s) AS job_runs,"
                "       (SELECT COUNT(*) FROM job_tags "
                "        WHERE job_id = %s) AS job_tags",
                (run_id, run_id, run_id),
            ).fetchone()
            facade2 = conn.execute(
                "SELECT profile_revision, prompt_revision, model_profile, "
                "requested_grants FROM job_runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()
        assert (counts["session_runs"], counts["job_runs"], counts["job_tags"]) == (
            1,
            1,
            1,
        )
        assert facade2["profile_revision"] == 2
        assert facade2["prompt_revision"] == 3
        assert facade2["model_profile"] == "primary"
        assert facade2["requested_grants"] == sorted(admitted)

        # Cross-server scope: the session stays value-free absent for
        # server B and NOTHING is admitted through the wrong scope.
        cross = client.post(
            f"/api/v1/agent-sessions/{session_id}/turns",
            params={"server_id": str(SERVER_B)},
            json={
                "session_id": str(session_id),
                "text": "must never be admitted",
                "client_request_id": str(uuid.uuid4()),
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert cross.status_code == 404
        assert cross.json()["error"]["code"] == "session_not_found"
        with psycopg.connect(migrated_url, row_factory=dict_row) as conn:
            after = conn.execute(
                "SELECT (SELECT COUNT(*) FROM session_runs "
                "        WHERE session_id = %s) AS session_runs,"
                "       (SELECT COUNT(*) FROM job_runs) AS job_runs_total,"
                "       (SELECT COUNT(*) FROM session_entries "
                "        WHERE session_id = %s) AS entries",
                (session_id, session_id),
            ).fetchone()
        assert after["session_runs"] == 1
        assert after["entries"] == 2  # user_message + queued run_state only
        assert after["job_runs_total"] == 1  # the one facade, nothing added


async def test_turn_admission_on_long_session_is_not_404(
    app_fixture: FastAPI, migrated_url: str
) -> None:
    """R06-fix1 regression (F1): a session with MORE entries than one
    ascending confirmation page (>50). The route builds its response from the
    append result itself — an ``after_seq=0`` page would return the OLDEST
    50 entries, miss the new turn and falsely report scope-blind 404 AFTER
    the durable commit. Admission AND idempotent replay both work here."""
    import json as _json

    from sqlalchemy import text as sql_text

    with TestClient(app_fixture) as client:
        jar = _login(client)
        created = _create_session(client, jar, SERVER_A)
        session_id = uuid.UUID(created["session_id"])
        # Seed 60 durable entries (seq 1..60) in ONE executemany transaction.
        _, sessions = create_app_engine(migrated_url)
        async with sessions() as db:
            async with db.begin():
                rows = [
                    {
                        "sid": session_id,
                        "seq": n,
                        "kind": "user_message",
                        "payload": _json.dumps(
                            {
                                "kind": "user_message",
                                "text": f"seed {n}",
                                "client_request_id": str(uuid.UUID(int=n)),
                                "source": "api",
                            }
                        ),
                        "src": f"seed-{n}",
                    }
                    for n in range(1, 61)
                ]
                await db.execute(
                    sql_text(
                        """
                        INSERT INTO session_entries
                            (entry_id, session_id, run_id, seq, kind,
                             payload, durable_source_id)
                        VALUES (gen_random_uuid(), :sid, NULL, :seq, :kind,
                                CAST(:payload AS jsonb), :src)
                        """
                    ),
                    rows,
                )
        key = str(uuid.uuid4())
        first = client.post(
            f"/api/v1/agent-sessions/{session_id}/turns",
            params={"server_id": str(SERVER_A)},
            json={
                "session_id": str(session_id),
                "text": "turn on a long session",
                "client_request_id": key,
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        # The reviewed contract: admission is 200 OK (201 also accepted).
        assert first.status_code in (200, 201), first.text
        body = first.json()
        assert body["session_id"] == str(session_id)
        # The turn entry appended AFTER the 60 seeded entries.
        assert body["seq"] == 61
        assert body["deduplicated"] is False
        # A replayed idempotency key on the SAME long session: the ORIGINAL
        # entry_id/run_id/seq — never a 404, never a second row.
        replay = client.post(
            f"/api/v1/agent-sessions/{session_id}/turns",
            params={"server_id": str(SERVER_A)},
            json={
                "session_id": str(session_id),
                "text": "turn on a long session",
                "client_request_id": key,
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert replay.status_code in (200, 201), replay.text
        replayed = replay.json()
        assert replayed["entry_id"] == body["entry_id"]
        assert replayed["run_id"] == body["run_id"]
        assert replayed["seq"] == 61
        assert replayed["deduplicated"] is True
        # Durable truth: 60 seed + user_message + run_state = 62 rows.
        import psycopg

        with psycopg.connect(migrated_url) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM session_entries WHERE session_id = %s",
                (session_id,),
            ).fetchone()[0]
        assert total == 62


async def test_turn_route_refuses_wrong_origin_and_missing_csrf(
    app_fixture: FastAPI,
) -> None:
    """R06-fix1 (F5): direct REST negatives on the NEW mutation route — the
    exact-origin allowlist and the CSRF double submit, both value-free 403s
    (no admission happens; the gates precede the store)."""
    with TestClient(app_fixture) as client:
        jar = _login(client)
        created = _create_session(client, jar, SERVER_A)
        session_id = uuid.UUID(created["session_id"])
        payload = {
            "session_id": str(session_id),
            "text": "x",
            "client_request_id": str(uuid.uuid4()),
        }
        url = f"/api/v1/agent-sessions/{session_id}/turns"
        wrong_origin = client.post(
            url,
            params={"server_id": str(SERVER_A)},
            json=payload,
            headers={
                "origin": "http://evil.example",
                "x-csrf-token": jar[CSRF_COOKIE],
            },
            cookies=jar,
        )
        assert wrong_origin.status_code == 403
        assert wrong_origin.json()["error"]["code"] == "origin_rejected"
        missing_csrf = client.post(
            url,
            params={"server_id": str(SERVER_A)},
            json=payload,
            headers={"origin": ORIGIN},  # no x-csrf-token header
            cookies=jar,
        )
        assert missing_csrf.status_code == 403
        assert missing_csrf.json()["error"]["code"] == "csrf_rejected"


# ---------------------------------------------------------------------------
# 5b. rename + tags surface (D07-fix1 F2/N6)
# ---------------------------------------------------------------------------


async def test_rename_route_updates_title_and_enforces_scope(
    app_fixture: FastAPI,
    migrated_url: str,
) -> None:
    """PATCH /{session_id}: the durable rename over the explicit (or the
    session's own stored) scope; cross-server stays the SAME 404; blank
    titles refuse 400; the CSRF gate precedes any effect."""
    import psycopg

    with TestClient(app_fixture) as client:
        jar = _login(client)
        created = _create_session(client, jar, SERVER_A)
        session_id = uuid.UUID(created["session_id"])
        url = f"/api/v1/agent-sessions/{session_id}"

        # Missing CSRF refuses before any effect (403 envelope).
        no_csrf = client.patch(
            url,
            params={"server_id": str(SERVER_A)},
            json={"title": "nope"},
            headers={"origin": ORIGIN},
            cookies=jar,
        )
        assert no_csrf.status_code == 403
        assert no_csrf.json()["error"]["code"] == "csrf_rejected"

        # The explicit-scope rename: 200 detail with the new title.
        renamed = client.patch(
            url,
            params={"server_id": str(SERVER_A)},
            json={"title": "Renamed A"},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["title"] == "Renamed A"

        # Durable truth: the stored row carries the new title.
        with psycopg.connect(migrated_url) as conn:
            stored = conn.execute(
                "SELECT title FROM sessions WHERE session_id = %s",
                (session_id,),
            ).fetchone()
        assert stored is not None and stored[0] == "Renamed A"

        # Scope-blind 404: a wrong server_id is the SAME refusal as a
        # missing session (no existence leak).
        cross = client.patch(
            url,
            params={"server_id": str(SERVER_B)},
            json={"title": "cross"},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert cross.status_code == 404
        assert cross.json()["error"]["code"] == "session_not_found"

        # A blank (whitespace-only) title refuses 400.
        blank = client.patch(
            url,
            json={"title": "   "},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert blank.status_code == 400
        assert blank.json()["error"]["code"] == "forbidden_field"

        # The stored-scope rename (no query pin) still converges.
        stored_scope = client.patch(
            url,
            json={"title": "Renamed B"},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert stored_scope.status_code == 200, stored_scope.text
        assert stored_scope.json()["title"] == "Renamed B"


async def test_session_tags_surface_is_bounded_and_honest(
    app_fixture: FastAPI,
    migrated_url: str,
) -> None:
    """GET/POST/DELETE /{session_id}/tags: idempotent user assignment,
    bounded pagination, tag_preview on the list + detail rows, and the
    mandatory-at-least-one-tag invariant (last_tag 409)."""
    import psycopg
    from psycopg.rows import dict_row

    with TestClient(app_fixture) as client:
        jar = _login(client)
        created = _create_session(client, jar, SERVER_A)
        session_id = uuid.UUID(created["session_id"])
        base = f"/api/v1/agent-sessions/{session_id}"

        # Create-if-missing assignment: the trimmed name mints the row.
        first = client.post(
            f"{base}/tags",
            json={
                "server_id": str(SERVER_A),
                "tag_id": "aaaa0000-0000-4000-8000-000000000001",
                "tag_name": "alpha",
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert first.status_code == 204, first.body

        # A replayed assignment (same body: name-first resolution) stays an
        # idempotent no-op on the SAME tag pair.
        replay = client.post(
            f"{base}/tags",
            json={
                "server_id": str(SERVER_A),
                "tag_id": "aaaa0000-0000-4000-8000-000000000001",
                "tag_name": "alpha",
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert replay.status_code == 204
        # An unknown catalog id WITHOUT a create name refuses tag_not_found.
        unknown = client.post(
            f"{base}/tags",
            json={
                "server_id": str(SERVER_A),
                "tag_id": "aaaa0000-0000-4000-8000-0000000000ff",
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "tag_not_found"

        # Seed two more catalog tags and attach them (existing ids win).
        tag_ids = [uuid.UUID("aaaa0000-0000-4000-8000-000000000001")]
        with psycopg.connect(migrated_url) as conn:
            for suffix in (2, 3):
                tag_id = uuid.UUID(f"aaaa0000-0000-4000-8000-{suffix:012d}")
                conn.execute(
                    "INSERT INTO tags (tag_id, name) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (tag_id, f"beta-{suffix}"),
                )
                tag_ids.append(tag_id)
            conn.commit()
        for tag_id in tag_ids[1:]:
            attached = client.post(
                f"{base}/tags",
                json={"server_id": str(SERVER_A), "tag_id": str(tag_id)},
                headers=_mutation_headers(jar),
                cookies=jar,
            )
            assert attached.status_code == 204

        # The bounded page: limit=2 -> honest total/has_more + the page 2.
        page1 = client.get(
            f"{base}/tags",
            params={"server_id": str(SERVER_A), "limit": 2},
            cookies=jar,
        )
        assert page1.status_code == 200, page1.text
        page = page1.json()
        assert page["session_id"] == str(session_id)
        assert page["server_id"] == str(SERVER_A)
        assert page["total"] == 3 and page["has_more"] is True
        assert len(page["items"]) == 2
        assert {item["name"] for item in page["items"]} <= {
            "alpha",
            "beta-2",
            "beta-3",
        }
        page2 = client.get(
            f"{base}/tags",
            params={"server_id": str(SERVER_A), "limit": 2, "offset": 2},
            cookies=jar,
        )
        assert page2.status_code == 200
        assert page2.json()["has_more"] is False
        assert len(page2.json()["items"]) == 1

        # tag_preview rides the list page AND the detail (N5).
        listing = client.get(
            "/api/v1/agent-sessions",
            params={"server_id": str(SERVER_A)},
            cookies=jar,
        )
        row = next(
            one
            for one in listing.json()["items"]
            if one["session_id"] == str(session_id)
        )
        assert row["tag_preview"]["total"] == 3
        assert len(row["tag_preview"]["preview"]) == 3
        detail = client.get(
            f"/api/v1/agent-sessions/{session_id}",
            params={"server_id": str(SERVER_A)},
            cookies=jar,
        )
        assert detail.json()["tag_preview"]["total"] == 3

        # Cross-server reads of the tags stay the scope-blind 404.
        foreign = client.get(
            f"{base}/tags", params={"server_id": str(SERVER_B)}, cookies=jar
        )
        assert foreign.status_code == 404

        # The mandatory-at-least-one-tag invariant: removing the last tag
        # without a replacement refuses 409 last_tag; with one it commits
        # in the same transaction (204), leaving the replacement attached.
        with psycopg.connect(migrated_url, row_factory=dict_row) as conn:
            remaining = conn.execute(
                "SELECT t.tag_id FROM session_tags st "
                "JOIN tags t ON t.tag_id = st.tag_id "
                "WHERE st.session_id = %s",
                (session_id,),
            ).fetchall()
        assert len(remaining) == 3
        for one in remaining[:-1]:
            removed = client.delete(
                f"{base}/tags/{one['tag_id']}",
                params={"server_id": str(SERVER_A)},
                headers=_mutation_headers(jar),
                cookies=jar,
            )
            assert removed.status_code == 204
        last_id = remaining[-1]["tag_id"]
        final = client.delete(
            f"{base}/tags/{last_id}",
            params={"server_id": str(SERVER_A)},
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert final.status_code == 409
        assert final.json()["error"]["code"] == "last_tag"

        replacement = uuid.UUID("aaaa0000-0000-4000-8000-000000000009")
        with psycopg.connect(migrated_url) as conn:
            conn.execute(
                "INSERT INTO tags (tag_id, name) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (replacement, "replacement"),
            )
            conn.commit()
        replaced = client.delete(
            f"{base}/tags/{last_id}",
            params={
                "server_id": str(SERVER_A),
                "replacement_tag_id": str(replacement),
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert replaced.status_code == 204
        after = client.get(
            f"{base}/tags", params={"server_id": str(SERVER_A)}, cookies=jar
        )
        after_page = after.json()
        assert after_page["total"] == 1
        assert after_page["items"][0]["tag_id"] == str(replacement)


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
# 7. OpenAPI presence: new routes exist, old chat surface absent
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
    # D09: the old chat surface is GONE — no /api/v1/chat/** route remains.
    assert not any(path.startswith("/api/v1/chat") for path in paths)


# ---------------------------------------------------------------------------
# 8. capability restoration (CAP-1): admission resolver parity + bridge
# ---------------------------------------------------------------------------

#: General Consult: the zero server UUID (the worker's pseudo-server).
GENERAL_SERVER = uuid.UUID(int=0)


def _profile_capabilities(
    app: FastAPI, server_id: uuid.UUID
) -> tuple[str, ...]:
    """The admission helper's resolved capability tuple for one scope,
    in the documented dedup order."""
    request = StarletteRequest({"type": "http", "app": app})
    _, _, capabilities = _profile_binding(request, server_id)
    return capabilities


def _write_onepassword_config(store: Any, *, enabled: bool) -> None:
    """Synthetic onepassword connection config (fixture vault names only,
    no production configuration and no secret values)."""
    store.write_config(
        ONEPASSWORD_PLUGIN,
        (
            "schema_version: 1\n"
            f'plugin: "{ONEPASSWORD_PLUGIN}"\n'
            f"enabled: {'true' if enabled else 'false'}\n"
            "credential_refs:\n"
            '  - "OP_SERVICE_ACCOUNT_TOKEN"\n'
            'config: {"default_vault": "fixture-vault", '
            '"allowed_vaults": ["fixture-vault"]}\n'
        ),
        expected_revision=0,
        config_schema=ONEPASSWORD_CONFIG_SCHEMA,
    )


def _bind_capabilities(
    store: Any, server_id: uuid.UUID, capabilities: tuple[str, ...]
) -> None:
    """One synthetic plugin grant binding for the fixture plugin."""
    caps_yaml = "\n".join(f'  - "{cap}"' for cap in capabilities)
    store.write_binding(
        ONEPASSWORD_PLUGIN,
        server_id,
        (
            "schema_version: 1\n"
            f'plugin: "{ONEPASSWORD_PLUGIN}"\n'
            f'server_id: "{server_id}"\n'
            "capabilities:\n"
            f"{caps_yaml}\n"
        ),
        expected_revision=0,
    )


def _grant(
    server_ids: list[uuid.UUID], capability_id: str
) -> dict[str, object]:
    return {
        "capability_id": capability_id,
        "server_ids": [str(sid) for sid in server_ids],
    }


def _write_grants(store: Any, entries: list[dict[str, object]]) -> None:
    """One synthetic grant-store revision below the fixture data root."""
    store.write(
        {
            "policy": {"revision": 1, "denied_capabilities": []},
            "grants": entries,
        },
        expected_revision=0,
    )


def _profile_yaml(
    server_id: uuid.UUID, name: str, capabilities: tuple[str, ...]
) -> str:
    caps_yaml = "\n".join(f"  - {cap}" for cap in capabilities)
    return f"""\
schema_version: {SCHEMA_VERSION}
server_id: "{server_id}"
name: "{name}"
prompt_revision: 3
environment: linux_ssh
ssh:
  host: "198.51.100.10"
  port: 22
  user: "deploy"
  host_key: "SHA256:ZmXWe/xAeTz26CuAmJ1jHtg8ZPX0mMhVfrBPM+C/zS4"
  credential_ref: "MAI_SSH_KEY_BUILD_HOST"
privilege: none
capabilities:
{caps_yaml}
"""


def _turn(
    client: TestClient,
    jar: dict[str, str],
    session_id: uuid.UUID,
    server_id: uuid.UUID,
) -> dict[str, Any]:
    """Admit one user turn through the REAL API route."""
    response = client.post(
        f"/api/v1/agent-sessions/{session_id}/turns",
        params={"server_id": str(server_id)},
        json={
            "session_id": str(session_id),
            "text": "exercise the capability seam",
            "client_request_id": str(uuid.uuid4()),
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


# SCOPE-1: typed nullable session service scope
# ---------------------------------------------------------------------------

GENERAL_SERVER = uuid.UUID(int=0)
SERVICE_ALPHA = uuid.UUID("3f9d2c4e-0001-4001-8001-00000000d001")
SERVICE_BETA = uuid.UUID("3f9d2c4e-0002-4002-8002-00000000d002")


def _map_service(
    app: FastAPI, server_id: uuid.UUID, service_id: uuid.UUID, name: str
) -> None:
    """Seed one real Service Map entry on the app's confined store."""
    store = app.state.service_map
    assert store is not None, "the app fixture wires the Service Map store"
    store.upsert_entry(server_id, name=name, application_id=service_id)


def _create_session_body(
    server_id: uuid.UUID,
    client_request_id: str | None = None,
    service_id: uuid.UUID | None = None,
) -> dict[str, str]:
    body = {
        "server_id": str(server_id),
        "source": "interactive",
        "client_request_id": client_request_id or str(uuid.uuid4()),
    }
    if service_id is not None:
        body["service_id"] = str(service_id)
    return body


async def test_create_with_service_scope_persists_and_projects(
    app_fixture: FastAPI,
) -> None:
    with TestClient(app_fixture) as client:
        jar = _login(client)
        _map_service(app_fixture, SERVER_A, SERVICE_ALPHA, "nginx-proxy")
        created = client.post(
            "/api/v1/agent-sessions",
            json=_create_session_body(SERVER_A, service_id=SERVICE_ALPHA),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["service_id"] == str(SERVICE_ALPHA)
        assert body["service_name"] == "nginx-proxy"

        listed = client.get(
            "/api/v1/agent-sessions",
            params={"server_id": str(SERVER_A)},
            cookies=jar,
        )
        assert listed.status_code == 200, listed.text
        item = listed.json()["items"][0]
        assert item["service_id"] == str(SERVICE_ALPHA)
        assert item["service_name"] == "nginx-proxy"

        # An unscoped chat carries explicit nulls (never a guessed label).
        plain = _create_session(client, jar, SERVER_A)
        assert plain["service_id"] is None
        assert plain["service_name"] is None


async def test_create_rejects_unknown_cross_server_and_general_service(
    app_fixture: FastAPI,
) -> None:
    with TestClient(app_fixture) as client:
        jar = _login(client)
        # Unknown identity on a mapped server.
        unknown = client.post(
            "/api/v1/agent-sessions",
            json=_create_session_body(SERVER_A, service_id=SERVICE_ALPHA),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert unknown.status_code == 400, unknown.text
        assert unknown.json()["error"]["code"] == "forbidden_field"
        # Cross-server: the identity exists ONLY on SERVER_B's map.
        _map_service(app_fixture, SERVER_B, SERVICE_BETA, "beta-only")
        cross = client.post(
            "/api/v1/agent-sessions",
            json=_create_session_body(SERVER_A, service_id=SERVICE_BETA),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert cross.status_code == 400, cross.text
        assert cross.json()["error"]["code"] == "forbidden_field"
        # General Consult never takes a service scope.
        general = client.post(
            "/api/v1/agent-sessions",
            json=_create_session_body(GENERAL_SERVER, service_id=SERVICE_ALPHA),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert general.status_code == 400, general.text
        # The rejections admitted nothing.
        page = client.get(
            "/api/v1/agent-sessions", params={"limit": 50}, cookies=jar
        )
        assert page.json()["total"] == 0


async def test_create_replay_scope_conflict_is_rejected(
    app_fixture: FastAPI,
) -> None:
    with TestClient(app_fixture) as client:
        jar = _login(client)
        _map_service(app_fixture, SERVER_A, SERVICE_ALPHA, "nginx-proxy")
        _map_service(app_fixture, SERVER_A, SERVICE_BETA, "grafana")
        key = str(uuid.uuid4())
        first = client.post(
            "/api/v1/agent-sessions",
            json=_create_session_body(SERVER_A, key, service_id=SERVICE_ALPHA),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert first.status_code == 201, first.text
        # SAME key + SAME scope: the idempotent convergence.
        same = client.post(
            "/api/v1/agent-sessions",
            json=_create_session_body(SERVER_A, key, service_id=SERVICE_ALPHA),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert same.status_code == 201, same.text
        assert same.json()["session_id"] == first.json()["session_id"]
        # SAME key + DIFFERENT scope: an honest 409, never a silent rebind.
        other = client.post(
            "/api/v1/agent-sessions",
            json=_create_session_body(SERVER_A, key, service_id=SERVICE_BETA),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert other.status_code == 409, other.text
        assert other.json()["error"]["code"] == "session_scope_conflict"
        # The stored scope is untouched by the rejected replay.
        detail = client.get(
            f"/api/v1/agent-sessions/{first.json()['session_id']}",
            params={"server_id": str(SERVER_A)},
            cookies=jar,
        )
        assert detail.json()["service_id"] == str(SERVICE_ALPHA)


async def test_service_name_follows_map_lifecycle(
    app_fixture: FastAPI,
) -> None:
    """Renaming the map entry changes the projection; deleting the entry
    keeps the stored identity but reports an unavailable (null) name —
    never a borrowed or stale label."""
    with TestClient(app_fixture) as client:
        jar = _login(client)
        _map_service(app_fixture, SERVER_A, SERVICE_ALPHA, "nginx-proxy")
        created = client.post(
            "/api/v1/agent-sessions",
            json=_create_session_body(SERVER_A, service_id=SERVICE_ALPHA),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        session_id = created.json()["session_id"]

        app_fixture.state.service_map.upsert_entry(
            SERVER_A, name="edge-nginx", application_id=SERVICE_ALPHA
        )
        detail = client.get(
            f"/api/v1/agent-sessions/{session_id}",
            params={"server_id": str(SERVER_A)},
            cookies=jar,
        )
        assert detail.json()["service_name"] == "edge-nginx"

        app_fixture.state.service_map.delete_entry(SERVER_A, SERVICE_ALPHA)
        detail = client.get(
            f"/api/v1/agent-sessions/{session_id}",
            params={"server_id": str(SERVER_A)},
            cookies=jar,
        )
        body = detail.json()
        assert body["service_id"] == str(SERVICE_ALPHA)
        assert body["service_name"] is None


async def test_legacy_runbook_session_projects_service_scope(
    app_fixture: FastAPI,
    migrated_url: str,
) -> None:
    """Projection-only backfill: a legacy NULL-service RUNBOOK session
    shows the service identity/name of its first non-null runbook_ref user
    entry; interactive legacy sessions project nulls; no rows are written."""
    import psycopg
    from psycopg.rows import dict_row

    engine, sessions = create_app_engine(migrated_url)
    legacy_id = uuid.uuid4()
    await acreate_session(
        sessions,
        server_id=SERVER_A,
        source=SessionSourceKind.RUNBOOK,
        session_id=legacy_id,
    )
    interactive_id = uuid.uuid4()
    await acreate_session(
        sessions,
        server_id=SERVER_A,
        source=SessionSourceKind.INTERACTIVE,
        session_id=interactive_id,
    )
    run_id = uuid.uuid4()
    await acreate_run(
        sessions,
        session_id=legacy_id,
        server_id=SERVER_A,
        run_id=run_id,
        model_profile="default",
        client_request_id=uuid.uuid4(),
    )
    entry = UserMessageEntry(
        kind=EntryKind.USER_MESSAGE,
        text="restart the proxy",
        client_request_id=uuid.uuid4(),
        source=SessionSourceKind.RUNBOOK,
        runbook_ref=RunbookRef(
            service_id=str(SERVICE_ALPHA), runbook_id=str(uuid.uuid4())
        ),
    )
    await aappend_entry(
        sessions,
        session_id=legacy_id,
        server_id=SERVER_A,
        kind=EntryKind.USER_MESSAGE,
        payload=entry.model_dump(mode="json"),
        run_id=run_id,
        durable_source_id="turn-legacy-1",
    )
    await engine.dispose()

    with TestClient(app_fixture) as client:
        jar = _login(client)
        _map_service(app_fixture, SERVER_A, SERVICE_ALPHA, "nginx-proxy")
        detail = client.get(
            f"/api/v1/agent-sessions/{legacy_id}",
            params={"server_id": str(SERVER_A)},
            cookies=jar,
        )
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["service_id"] == str(SERVICE_ALPHA)
        assert body["service_name"] == "nginx-proxy"

        listed = client.get(
            "/api/v1/agent-sessions",
            params={"server_id": str(SERVER_A)},
            cookies=jar,
        )
        items = {
            item["session_id"]: item for item in listed.json()["items"]
        }
        assert items[str(legacy_id)]["service_name"] == "nginx-proxy"
        # The interactive legacy session guesses nothing.
        assert items[str(interactive_id)]["service_id"] is None
        assert items[str(interactive_id)]["service_name"] is None

    # Projection-only: the session row still carries NULL service_id.
    with psycopg.connect(migrated_url, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT service_id FROM sessions WHERE session_id = %s",
            (legacy_id,),
        ).fetchone()
    assert row is not None and row["service_id"] is None


async def test_deleted_map_entry_turns_keep_identity(
    app_fixture: FastAPI,
    migrated_url: str,
) -> None:
    """A deleted map entry must not brick turns: the persisted identity and
    the last persisted name ride along (header service_name goes null);
    a session deleted before its FIRST turn admits with the deterministic
    unavailable fallback; another server's name is never borrowed."""
    with TestClient(app_fixture) as client:
        jar = _login(client)
        store = app_fixture.state.service_map
        store.upsert_entry(
            SERVER_A, name="nginx-proxy", application_id=SERVICE_ALPHA
        )
        created = client.post(
            "/api/v1/agent-sessions",
            json=_create_session_body(SERVER_A, service_id=SERVICE_ALPHA),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        scoped_id = uuid.UUID(created.json()["session_id"])
        first = client.post(
            f"/api/v1/agent-sessions/{scoped_id}/turns",
            params={"server_id": str(SERVER_A)},
            json={
                "session_id": str(scoped_id),
                "text": "check the proxy logs",
                "client_request_id": str(uuid.uuid4()),
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert first.status_code in (200, 201), first.text
        created2 = client.post(
            "/api/v1/agent-sessions",
            json=_create_session_body(SERVER_A, service_id=SERVICE_ALPHA),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert created2.status_code == 201, created2.text
        scoped2 = uuid.UUID(created2.json()["session_id"])
        # The operator deletes the map entry; BOTH sessions keep working
        # (a create-time service_id now refuses, turn-time identity stays).
        assert store.delete_entry(SERVER_A, SERVICE_ALPHA) is True
        # A DIFFERENT server maps the same name: never borrowed.
        store.upsert_entry(SERVER_B, name="nginx-proxy", application_id=SERVICE_BETA)

        second = client.post(
            f"/api/v1/agent-sessions/{scoped_id}/turns",
            params={"server_id": str(SERVER_A)},
            json={
                "session_id": str(scoped_id),
                "text": "and now restart it",
                "client_request_id": str(uuid.uuid4()),
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert second.status_code in (200, 201), second.text
        detail = client.get(
            f"/api/v1/agent-sessions/{scoped_id}",
            params={"server_id": str(SERVER_A)},
            cookies=jar,
        )
        assert detail.json()["service_id"] == str(SERVICE_ALPHA)
        assert detail.json()["service_name"] is None

        # Deleted BEFORE scoped2's first turn: identity-only admission.
        turn = client.post(
            f"/api/v1/agent-sessions/{scoped2}/turns",
            params={"server_id": str(SERVER_A)},
            json={
                "session_id": str(scoped2),
                "text": "hello",
                "client_request_id": str(uuid.uuid4()),
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert turn.status_code in (200, 201), turn.text
        # Create-time scope refuses the deleted identity (SCOPE-1 rule),
        # while the admitted turns above keep working — no bricking.
        rejected = client.post(
            "/api/v1/agent-sessions",
            json=_create_session_body(SERVER_A, service_id=SERVICE_ALPHA),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert rejected.status_code == 400, rejected.text

    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(migrated_url, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT session_id, seq, payload FROM session_entries
            WHERE session_id IN (%s, %s) AND kind = 'user_message'
            ORDER BY session_id, seq
            """,
            (scoped_id, scoped2),
        ).fetchall()
    first_session = [r for r in rows if r["session_id"] == scoped_id]
    second_session = [r for r in rows if r["session_id"] == scoped2]
    # Follow-on after deletion: the LAST PERSISTED name rides along.
    follow = first_session[1]["payload"]
    assert follow["text"] == "and now restart it"
    assert SERVICE_MAP_SECTION_HEADER not in follow["text"]
    assert follow["service_ref"] == {
        "service_id": str(SERVICE_ALPHA),
        "name": "nginx-proxy",
    }
    # First turn after deletion: no name available — identity only.
    orphan = second_session[0]["payload"]
    assert orphan["service_ref"] == {
        "service_id": str(SERVICE_ALPHA),
        "name": None,
    }
    assert SERVICE_MAP_SECTION_HEADER not in orphan["text"]

async def test_service_bound_turns_first_full_then_compact_identity(
    app_fixture: FastAPI,
    migrated_url: str,
) -> None:
    """SCOPE-2: the FIRST service-bound user entry persists the full
    validated Service Map section + prompt (with its service_ref); a
    follow-on entry carries only the compact service_ref and NO duplicated
    section; the model-context projection gives turn 2 the identity
    prefix; an unbound chat gets neither section nor ref. Turn bodies carry
    no scope authority."""
    from mai.agents.session_history import build_native_model_history
    from mai.database import create_app_engine as _engine
    from mai.session_store import aread_session_history

    engine, engine_sessions = _engine(migrated_url)
    with TestClient(app_fixture) as client:
        jar = _login(client)
        store = app_fixture.state.service_map
        store.upsert_entry(
            SERVER_A,
            name="nginx-proxy",
            application_id=SERVICE_ALPHA,
            description="reverse proxy for the edge host",
            custom_instructions="use the compose project on the edge host",
            instructions={"install": "docker compose up -d"},
        )
        created = client.post(
            "/api/v1/agent-sessions",
            json=_create_session_body(SERVER_A, service_id=SERVICE_ALPHA),
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert created.status_code == 201, created.text
        scoped_id = uuid.UUID(created.json()["session_id"])
        for text in ("check the proxy logs", "and now restart it"):
            turn = client.post(
                f"/api/v1/agent-sessions/{scoped_id}/turns",
                params={"server_id": str(SERVER_A)},
                json={
                    "session_id": str(scoped_id),
                    "text": text,
                    "client_request_id": str(uuid.uuid4()),
                },
                headers=_mutation_headers(jar),
                cookies=jar,
            )
            assert turn.status_code in (200, 201), turn.text
        # Unbound chat: a plain session with one turn.
        plain = _create_session(client, jar, SERVER_A)
        plain_id = uuid.UUID(plain["session_id"])
        turn = client.post(
            f"/api/v1/agent-sessions/{plain_id}/turns",
            params={"server_id": str(SERVER_A)},
            json={
                "session_id": str(plain_id),
                "text": "plain question",
                "client_request_id": str(uuid.uuid4()),
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert turn.status_code in (200, 201), turn.text

    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(migrated_url, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT session_id, seq, payload FROM session_entries
            WHERE session_id IN (%s, %s) AND kind = 'user_message'
            ORDER BY session_id, seq
            """,
            (scoped_id, plain_id),
        ).fetchall()
    scoped_rows = [row for row in rows if row["session_id"] == scoped_id]
    plain_rows = [row for row in rows if row["session_id"] == plain_id]
    assert len(scoped_rows) == 2 and len(plain_rows) == 1
    first, second = scoped_rows[0]["payload"], scoped_rows[1]["payload"]
    # FIRST entry: full persisted material, service_ref attached.
    assert first["text"].startswith("check the proxy logs")
    assert SERVICE_MAP_SECTION_HEADER in first["text"]
    assert "nginx-proxy" in first["text"]
    assert "reverse proxy for the edge host" in first["text"]
    assert "use the compose project on the edge host" in first["text"]
    assert "docker compose up -d" in first["text"]
    assert first["service_ref"] == {
        "service_id": str(SERVICE_ALPHA),
        "name": "nginx-proxy",
    }
    assert first["runbook_ref"] is None
    # FOLLOW-ON entry: compact identity, NO duplicated full section.
    assert second["text"] == "and now restart it"
    assert SERVICE_MAP_SECTION_HEADER not in second["text"]
    assert second["service_ref"] == {
        "service_id": str(SERVICE_ALPHA),
        "name": "nginx-proxy",
    }
    # Unbound chat stays entirely scope-free.
    assert plain_rows[0]["payload"]["service_ref"] is None
    assert SERVICE_MAP_SECTION_HEADER not in plain_rows[0]["payload"]["text"]

    # Model-context projection: first entry renders as-is (the material is
    # already in its text), the follow-on gains the compact prefix.
    page = await aread_session_history(
        engine_sessions,
        session_id=scoped_id,
        server_id=SERVER_A,
        limit=10,
    )
    native = build_native_model_history(
        list(page.items),
        provider="openai",
        model_profile="default",
    )
    prompts = [
        part.content
        for message in native.messages
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]
    assert prompts[0].startswith("check the proxy logs")
    assert "[service scope:" not in prompts[0]
    assert prompts[1] == (
        f"[service scope: @nginx-proxy ({SERVICE_ALPHA})]\nand now restart it"
    )
    await engine.dispose()


def _facade_grants(migrated_url: str, run_id: uuid.UUID) -> list[str]:
    """The persisted job facade's requested_grants (the durable truth)."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(migrated_url, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT requested_grants FROM job_runs WHERE run_id = %s",
            (run_id,),
        ).fetchone()
    assert row is not None, "the admitted run must persist the facade"
    return row["requested_grants"]


def _bridge_descriptor(identifier: str) -> dict[str, object]:
    """A registry-shaped read descriptor for a non-plugin capability."""
    return {
        "identifier": identifier,
        "version": "1.0.0",
        "config_schema": None,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": [],
            "properties": {},
        },
        "output_schema": {"type": "object"},
        "execution_environments": ["controller"],
        "effect_class": "read",
        "resource_scope": [],
        "required_grants": [],
    }


def _refusing_executor():
    async def _execute(call: CapabilityCall) -> ExecutorResult:
        raise AssertionError(f"executor invoked for a surface test: {call}")

    return _execute


def _fixture_dispatcher(registry: CapabilityRegistry) -> CapabilityDispatcher:
    """A real dispatcher over a never-dialed approval store; only the
    bridge's tool-surface composition is under test here."""
    _, sessions = create_app_engine(
        "postgresql://unused:unused@127.0.0.1:1/unused"
    )

    def _unused_env() -> EnvResolver:
        import tempfile

        path = Path(tempfile.mkdtemp()) / "fixture.env"
        path.write_text("# fixture env: no secrets\n")
        return EnvResolver(path)

    return CapabilityDispatcher(
        registry=registry,
        approvals=ApprovalStore(sessions),
        grants=lambda: {},
        policy=lambda: Policy(revision=1, denied_capabilities=frozenset()),
        adopted=lambda server_id: True,
        credentials=_unused_env,
        executor=_refusing_executor(),
        output_budget_bytes=64 * 1024,
    )


async def test_general_consult_surface_matches_worker_pseudo_server_grants(
    app_fixture: FastAPI, migrated_url: str
) -> None:
    """General Consult must admit the worker pseudo-server grant surface
    (worker.py ``_read_grants`` enrols the zero UUID): an enabled GLOBAL
    plugin binding exposes its capabilities, alongside the internal-API,
    history-tag and web-research vocabularies — and NEVER the built-in
    server defaults (docs.retrieve, service.http, host.*) that the worker
    never auto-grants to the pseudo-server. The route freezes exactly the
    resolved tuple into the persisted job facade."""
    _write_onepassword_config(app_fixture.state.plugins, enabled=True)
    _bind_capabilities(
        app_fixture.state.plugins,
        GENERAL_SERVER,
        ("secret.get", "secret.list"),
    )
    with TestClient(app_fixture) as client:
        jar = _login(client)
        created = _create_session(client, jar, GENERAL_SERVER)
        session_id = uuid.UUID(created["session_id"])
        body = _turn(client, jar, session_id, GENERAL_SERVER)
        persisted = _facade_grants(migrated_url, uuid.UUID(body["run_id"]))

        admitted = _profile_capabilities(app_fixture, GENERAL_SERVER)
        # Documented dedup order: explicit grants (none), then binding
        # capability order, then internal API, then tags, then web
        # research — deduplicated, General's profile tuple stays ().
        assert admitted == (
            "secret.get",
            "secret.list",
            *INTERNAL_API_CAPABILITY_IDS,
            *TAGS_CAPABILITY_IDS,
            "web.search",
            "web.read",
        )
        assert len(set(admitted)) == len(admitted)
        assert not (
            {"docs.retrieve", "service.http", "host.logs", "host.shell",
             "host.file_read"}
            & set(admitted)
        )
        # The route froze EXACTLY this tuple: the facade stores the same
        # set, sorted by the store's canonical json.dumps(sorted(...)).
        assert persisted == sorted(admitted)


async def test_server_chat_resolves_grants_profile_and_both_binding_scopes(
    app_fixture: FastAPI, migrated_url: str
) -> None:
    """Server chat admits grant-store grants + profile capabilities +
    BOTH binding scopes (server-specific AND global) + the same reviewed
    defaults as runbook admission, in the documented dedup order. A
    cross-server binding and another server's profile never leak."""
    plugins = app_fixture.state.plugins
    _write_onepassword_config(plugins, enabled=True)
    _bind_capabilities(plugins, GENERAL_SERVER, ("secret.list",))
    _bind_capabilities(plugins, SERVER_A, ("secret.get",))
    _write_grants(app_fixture.state.grants, [_grant([SERVER_A], "host.shell")])
    app_fixture.state.profiles.write(
        SERVER_A,
        _profile_yaml(SERVER_A, "binding-scope", ("shell.exec", "files.read")),
        expected_revision=0,
    )

    with TestClient(app_fixture) as client:
        jar = _login(client)
        a_session = uuid.UUID(
            _create_session(client, jar, SERVER_A)["session_id"]
        )
        b_session = uuid.UUID(
            _create_session(client, jar, SERVER_B)["session_id"]
        )

        a_admitted = _profile_capabilities(app_fixture, SERVER_A)
        assert a_admitted == (
            "host.shell",
            "shell.exec",
            "files.read",
            "secret.list",
            "secret.get",
            *INTERNAL_API_CAPABILITY_IDS,
            *BUILTIN_DEFAULT_CAPABILITIES,
        )
        b_admitted = _profile_capabilities(app_fixture, SERVER_B)
        assert "secret.get" not in b_admitted  # server-A binding never leaks
        assert "shell.exec" not in b_admitted  # server A's profile not B's
        assert "secret.list" in b_admitted  # the GLOBAL binding does

        a_persisted = _facade_grants(
            migrated_url,
            uuid.UUID(_turn(client, jar, a_session, SERVER_A)["run_id"]),
        )
        b_persisted = _facade_grants(
            migrated_url,
            uuid.UUID(_turn(client, jar, b_session, SERVER_B)["run_id"]),
        )
        assert a_persisted == sorted(a_admitted)
        assert b_persisted == sorted(b_admitted)
        assert "secret.get" not in b_persisted
        assert "host.shell" not in b_persisted


async def test_disabled_plugin_binding_fails_closed(
    app_fixture: FastAPI, migrated_url: str
) -> None:
    """A stored-but-DISABLED plugin contributes nothing to any admitted
    surface, even with a standing global grant binding."""
    _write_onepassword_config(app_fixture.state.plugins, enabled=False)
    _bind_capabilities(
        app_fixture.state.plugins, GENERAL_SERVER, ("secret.list",)
    )
    with TestClient(app_fixture) as client:
        jar = _login(client)
        created = _create_session(client, jar, GENERAL_SERVER)
        session_id = uuid.UUID(created["session_id"])
        body = _turn(client, jar, session_id, GENERAL_SERVER)
        persisted = _facade_grants(migrated_url, uuid.UUID(body["run_id"]))
        admitted = _profile_capabilities(app_fixture, GENERAL_SERVER)
        assert "secret.list" not in admitted
        assert "secret.list" not in persisted
        assert "secret.get" not in admitted


async def test_general_excludes_host_surface_unless_explicitly_granted(
    app_fixture: FastAPI, migrated_url: str
) -> None:
    """Worker parity (plan §A): a global PLUGIN binding naming a host
    capability does NOT admit it for the pseudo-server; only an explicit
    GRANT-STORE entry naming the zero UUID re-includes it."""
    plugins = app_fixture.state.plugins
    _write_onepassword_config(plugins, enabled=True)
    _bind_capabilities(plugins, GENERAL_SERVER, ("host.shell",))
    _write_grants(
        app_fixture.state.grants,
        [_grant([GENERAL_SERVER], "docs.retrieve")],
    )
    with TestClient(app_fixture) as client:
        jar = _login(client)
        created = _create_session(client, jar, GENERAL_SERVER)
        session_id = uuid.UUID(created["session_id"])
        body = _turn(client, jar, session_id, GENERAL_SERVER)
        persisted = _facade_grants(migrated_url, uuid.UUID(body["run_id"]))
        admitted = _profile_capabilities(app_fixture, GENERAL_SERVER)
        # The explicit grant-store entry re-includes docs.retrieve first.
        assert admitted[0] == "docs.retrieve"
        assert "docs.retrieve" in persisted
        # The plugin-bound host capability stays excluded for General.
        assert "host.shell" not in admitted
        assert "host.shell" not in persisted


async def test_resolved_capabilities_deduplicate_in_documented_order(
    app_fixture: FastAPI,
) -> None:
    """The resolver's documented order: grant-store entries first
    (canonical sorted order), then profile capabilities, then plugin
    bindings, then the internal API and the built-in defaults — first
    occurrence wins, so built-in duplicates collapse into the earlier
    source's position."""
    _write_grants(
        app_fixture.state.grants,
        [_grant([SERVER_A], "web.search"), _grant([SERVER_A], "host.logs")],
    )
    app_fixture.state.profiles.write(
        SERVER_A,
        _profile_yaml(SERVER_A, "dedup-order", ("files.read",)),
        expected_revision=0,
    )
    admitted = _profile_capabilities(app_fixture, SERVER_A)
    # web.search and host.logs recur in BUILTIN_DEFAULT_CAPABILITIES but
    # first occurrence wins: they stay at their grant-store positions and
    # collapse OUT of the built-in tail.
    assert admitted == (
        "host.logs",
        "web.search",
        "files.read",
        *INTERNAL_API_CAPABILITY_IDS,
        "docs.retrieve",
        "web.read",
        "service.http",
        *TAGS_CAPABILITY_IDS,
    )
    assert len(set(admitted)) == len(admitted)


async def test_general_bridge_exposes_only_registered_admitted_tools(
    app_fixture: FastAPI,
) -> None:
    """A REAL CapabilityToolBridge over the admitted General context
    exposes exactly registered ∩ allowed: the two bound secret tools —
    not the unbound (though registered) secret.store and not the
    withheld server defaults (registered here as a fixture descriptor)."""
    _write_onepassword_config(app_fixture.state.plugins, enabled=True)
    _bind_capabilities(
        app_fixture.state.plugins,
        GENERAL_SERVER,
        ("secret.get", "secret.list"),
    )
    admitted = _profile_capabilities(app_fixture, GENERAL_SERVER)
    registry = CapabilityRegistry()
    for document in (
        SECRET_LIST_CAPABILITY,
        SECRET_GET_CAPABILITY,
        SECRET_STORE_CAPABILITY,
        _bridge_descriptor("docs.retrieve"),
    ):
        registry.register(parse_descriptor(document))
    context = ExecutionContext(
        principal="fixture-admin",
        server_id=GENERAL_SERVER,
        run_id=uuid.uuid5(uuid.UUID(int=0), "bridge-fixture"),
        parent_scope=None,
        profile_revision="1",
        prompt_revision="1",
        credential_references=(),
        allowed_capabilities=admitted,
        mode=PolicyMode.DELEGATED,
    )
    bridge = CapabilityToolBridge(
        registry=registry,
        dispatcher=_fixture_dispatcher(registry),
        context=context,
    )
    assert bridge.enabled_capabilities == ("secret.get", "secret.list")
    assert [tool.name for tool in bridge.tools] == [
        tool_name_for("secret.get"),
        tool_name_for("secret.list"),
    ]
    # Registered but NOT admitted: no tool, no descriptor.
    assert bridge.descriptor_for("secret.store") is None
    assert bridge.descriptor_for("docs.retrieve") is None


async def test_server_bridge_exposes_only_registered_admitted_tools(
    app_fixture: FastAPI,
) -> None:
    """Server-chat bridge: the granted-and-registered host tool is
    exposed; an admitted-but-unregistered plugin capability and an
    admitted-but-unregistered internal capability expose nothing."""
    _write_grants(app_fixture.state.grants, [_grant([SERVER_A], "host.shell")])
    admitted = _profile_capabilities(app_fixture, SERVER_A)
    assert "host.shell" in admitted
    registry = CapabilityRegistry()
    registry.register(host_shell_descriptor())
    registry.register(parse_descriptor(SECRET_LIST_CAPABILITY))
    registry.register(parse_descriptor(_bridge_descriptor("app.discover")))
    context = ExecutionContext(
        principal="fixture-admin",
        server_id=SERVER_A,
        run_id=uuid.uuid5(uuid.UUID(int=0), "bridge-fixture-server"),
        parent_scope=None,
        profile_revision="1",
        prompt_revision="1",
        credential_references=(),
        allowed_capabilities=admitted,
        mode=PolicyMode.DELEGATED,
    )
    bridge = CapabilityToolBridge(
        registry=registry,
        dispatcher=_fixture_dispatcher(registry),
        context=context,
    )
    # host.shell: granted AND registered → exposed (grant-store first).
    # app.discover: ALWAYS in the internal-API admission vocabulary AND
    # registered here → exposed. secret.list: registered but never
    # bound/granted for SERVER_A → NOT exposed.
    assert bridge.enabled_capabilities == ("host.shell", "app.discover")
    assert [tool.name for tool in bridge.tools] == [
        tool_name_for("host.shell"),
        tool_name_for("app.discover"),
    ]


async def test_delegation_narrows_the_admitted_surface(
    app_fixture: FastAPI,
) -> None:
    """Delegation stays a narrowing operation (C03 rule 3): the child
    effective set is child ∩ parent — a child run can never re-introduce
    a capability the admitted tuple withholds."""
    _write_onepassword_config(app_fixture.state.plugins, enabled=True)
    _bind_capabilities(
        app_fixture.state.plugins, GENERAL_SERVER, ("secret.list",)
    )
    admitted = _profile_capabilities(app_fixture, GENERAL_SERVER)
    parent = {cap: frozenset({GENERAL_SERVER}) for cap in admitted}
    child = dict(parent)
    child["host.shell"] = frozenset({GENERAL_SERVER})  # attempted widening
    child["secret.store"] = frozenset({GENERAL_SERVER})
    effective = effective_child_grants(parent, child)
    assert set(effective) <= set(parent)
    assert "host.shell" not in effective
    assert "secret.store" not in effective
    assert "secret.list" in effective
    assert "web.search" in effective


# ---------------------------------------------------------------------------
# ask_24: the API composition root shares the knowledge/learning
# dependencies (one instance family; client-only queue seam)
# ---------------------------------------------------------------------------


def test_api_composition_shares_knowledge_dependencies_and_queue_seam(
    migrated_url: str, tmp_path: Path
) -> None:
    """A SYNC test on a FRESH app instance: the composition coroutines run
    on TestClient's OWN portal loop (never pytest-asyncio's), so the
    pooled engine connections it binds are disposed on the SAME loop at
    lifespan exit — the previous async variant deadlocked the two
    threads' loops at dispose."""
    import json

    from mai.applications.knowledge_read import KnowledgeReadStore
    from mai.applications.learning_operations import LearningOperationStore
    from mai.applications.metadata_mutations import MetadataMutationCoordinator
    from mai.capabilities.dispatch import CapabilityCall
    from mai.jobs import engine as jobs_engine
    from mai.jobs.engine import EngineClient
    from mai.policy.evaluate import ExecutionContext, PolicyMode

    app = _build_app(migrated_url, tmp_path / "data-root-comp")
    # AC 1: the API process NEVER launches the durable engine — no
    # launch-recovery scheduler, no stolen pending workflows.
    assert jobs_engine._LAUNCHED is None

    with TestClient(app) as client:  # the lifespan RUNS the composition here
        # Entering TestClient re-ran the lifespan and rebuilt
        # app.state: acquire the composition family INSIDE the block so
        # the calls AND the assertions exercise the SAME instances
        # (never a stale pre-lifespan view).
        comp = app.state.internal_api
        by_id = {d.identifier: d for d in comp.descriptors}

        # The composition root created the shared dependency family.
        assert isinstance(app.state.knowledge_read, KnowledgeReadStore)
        assert isinstance(app.state.learning_operations, LearningOperationStore)
        assert isinstance(
            app.state.metadata_coordinator, MetadataMutationCoordinator
        )
        assert isinstance(app.state.learn_engine_client, EngineClient)
        assert callable(app.state.learn_engine_enqueue)
        # The coordinator SHARES the composition's own instances
        # (identity, not copies): one map store, one operation store,
        # one adoption seam.
        assert (
            app.state.metadata_coordinator._learning
            is app.state.learning_operations
        )
        assert (
            app.state.metadata_coordinator._map_store is app.state.service_map
        )
        assert (
            app.state.metadata_coordinator._adoption is app.state.app_adoption
        )

        application_id = uuid.uuid4()

        def _call(cap_id: str, payload: dict[str, object]) -> CapabilityCall:
            return CapabilityCall(
                descriptor=by_id[cap_id],
                payload=dict(payload),
                context=ExecutionContext(
                    principal="principal",
                    server_id=SERVER_A,
                    run_id=uuid.uuid4(),
                    parent_scope=None,
                    profile_revision="1",
                    prompt_revision="1",
                    credential_references=(),
                    allowed_capabilities=(cap_id,),
                    mode=PolicyMode.DELEGATED,
                ),
                credentials={},
                op_id=uuid.uuid4(),
            )

        learn_env = json.loads(
            client.portal.call(
                comp.executors["app.learn_service"],
                _call(
                    "app.learn_service",
                    {
                        "application_id": str(application_id),
                        "facts": [
                            {
                                "category": "deployment",
                                "key": "health-path",
                                "statement": "health lives at /healthz",
                                "source_kind": "tool_result",
                                "source_refs": [str(uuid.uuid4())],
                            }
                        ],
                        "expected_generation": 1,
                    },
                ),
            ).output
        )
        assert learn_env["status"] == "ok", learn_env
        assert learn_env["accepted"] is True
        assert learn_env["receipt_status"] == "accepted"
        operation_id = learn_env["operation_id"]

        # The REAL queue client recorded the deterministic workflow
        # (client-only: enqueue without a launched scheduler).
        assert (
            app.state.learn_engine_client.run_state(str(operation_id))
            is not None
        )

        # The SAME admission is readable through the lane's SHARED read
        # store — one instance family across the API surface.
        status_env = json.loads(
            client.portal.call(
                comp.executors["app.get_learning_status"],
                _call(
                    "app.get_learning_status",
                    {"application_id": str(application_id)},
                ),
            ).output
        )
        assert status_env["found"] is True, status_env
        assert status_env["operation_id"] == operation_id
        assert status_env["receipt_status"] == "accepted"

    # Lifespan shutdown already closed the learn client that ACTUALLY
    # enqueued (no leaked system-DB pool); a second close is idempotent.
    app.state.learn_engine_client.close()
