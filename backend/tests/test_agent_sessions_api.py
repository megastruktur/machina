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
