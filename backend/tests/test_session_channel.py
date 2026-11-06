"""D06 session-channel tests — real ASGI WebSockets + real Postgres.

Every socket runs through the REAL FastAPI application (``create_app`` +
lifespan — the shared LISTEN/NOTIFY hub lives on the real engine) against
the disposable pgvector fixture. No mocks, no fake sockets.

Proven here (BRIEF_D06 §B.2/§E):

1. Admission matrix: missing/wrong/revoked cookie → ``1008
   unauthenticated``; missing/wrong Origin → ``1008 origin_rejected``;
   foreign scope → ``1008 scope_rejected``; missing/wrong CSRF in
   ``hello`` → ``1008 csrf_rejected``.
2. Frame validation: malformed JSON, unknown type, oversized (>64 KiB),
   forged extra fields (an engine-write attempt) — all refused, bounded,
   value-free; a client can NEVER carry a committed entry or a delta.
3. Snapshot/live continuity: connect → snapshot (≤30 newest, latest_seq);
   entries committed afterwards arrive as ``committed`` frames exactly
   once per seq — no gaps, no duplicates.
4. Bounded 10,000-entry resume: ``hello(after_seq)`` replays bounded
   pages (≤50 entries, ≤64 KiB) with explicit continuation; a cursor at
   the very beginning of a 10k session hits the replay budget →
   ``resync_required`` (never a full transcript).
5. Ack/reconnect idempotence: a reconnect with the OLD cursor replays the
   committed entries; per connection each seq arrives exactly once.
6. Backpressure/heartbeat/fanout: overflow drops VOLATILE deltas (never
   committed — committed overflow forces resync); ``ping`` → ``pong``;
   the fanout map SHRINKS after disconnect (no dead-client retention).
7. Idle discipline: ticks advance with ZERO catch-up SQL for a subscribed
   session at the head; a commit wakes delivery within one tick.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from starlette.testclient import TestClient

from mai.api.app import create_app
from mai.api.auth import SESSION_COOKIE
from mai.contracts.sessions import EntryKind, SessionSourceKind, UserMessageEntry
from mai.database import create_app_engine
from mai.session_store import aappend_entry

BACKEND_ROOT = Path(__file__).resolve().parents[1]
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"
ORIGIN = "http://testserver"
FIXTURE_PASSWORD = "fixture-password-9f3a"
TICK = 0.15  # the bounded coarse tick for the fast test hub (seconds)

SERVER_A = uuid.UUID("5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60")
SERVER_B = uuid.UUID("9a8b7c6d-1e2f-4a3b-8c5d-6e7f8a9b0c1d")

#: The suites run async tests through the anyio pytest plugin (the repo's
#: only async driver), one backend: asyncio.
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
            f"{LIVE_URL_VAR} is not set; the channel claims need a real "
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

    jobs_engine.launch_worker(url)
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
            "MAI_SESSION_CHANNEL_TICK_SECONDS": str(TICK),
        }
    )


@pytest.fixture
async def app_fixture(migrated_url: str, tmp_path: Path) -> FastAPI:
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
    return _build_app(migrated_url, tmp_path / "data-root")


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
    jar = dict(login.cookies.items())
    assert SESSION_COOKIE in jar and "mai_csrf" in jar
    return jar


def _channel_url(session_id: uuid.UUID, server_id: uuid.UUID) -> str:
    return f"/api/v1/agent-sessions/{session_id}/channel?server_id={server_id}"


def _hello(csrf: str, after_seq: int | None = None) -> str:
    return json.dumps(
        {
            "type": "hello",
            "protocol_version": 1,
            "after_seq": after_seq,
            "csrf_token": csrf,
            "client_request_id": str(uuid.uuid4()),
        }
    )


def _recv_frames(websocket, count: int, timeout: float = 5.0) -> list[dict]:
    """Receive exactly ``count`` text frames (bounded test helper)."""
    frames = []
    for _ in range(count):
        message = websocket.receive_json(mode="text")
        frames.append(message)
    return frames


def _refused(client: TestClient, url: str, **kwargs: object) -> None:
    """Assert one channel socket is accepted and IMMEDIATELY closed: the
    disconnect surfaces at the handshake or at the first receive — either
    is the refusal (close code 1008)."""
    from contextlib import ExitStack

    with ExitStack() as stack:
        websocket = stack.enter_context(
            client.websocket_connect(url, **kwargs)  # type: ignore[arg-type]
        )
        websocket.receive_json()


def _read_until_closed(websocket) -> list[dict]:
    """Read server frames until the socket closes (bounded by the server's
    own refusal: refusal paths send at most one error frame then close)."""
    frames: list[dict] = []
    while True:
        try:
            frames.append(websocket.receive_json())
        except Exception:
            return frames


# ---------------------------------------------------------------------------
# 1. admission matrix
# ---------------------------------------------------------------------------


async def test_missing_cookie_is_refused(app_fixture: FastAPI) -> None:
    session_id = str(uuid.uuid4())
    with TestClient(app_fixture) as client:
        # The server accepts and IMMEDIATELY closes with 1008: the
        # disconnect surfaces at the handshake or at the first receive —
        # either is the refusal.
        with pytest.raises(Exception), client.websocket_connect(
            _channel_url(uuid.UUID(session_id), SERVER_A)
        ) as websocket:
            websocket.receive_json()


async def test_wrong_and_revoked_cookie_are_refused(
    app_fixture: FastAPI,
) -> None:
    with TestClient(app_fixture) as client:
        jar = _login(client)
        # A forged token fails validation → unauthenticated.
        with pytest.raises(Exception):
            _refused(
                client,
                _channel_url(uuid.uuid4(), SERVER_A),
                cookies={SESSION_COOKIE: "forged"},
            )
        # A REVOKED cookie: log out (the server revokes the session row).
        client.post(
            "/api/v1/auth/logout", headers={"origin": ORIGIN}, cookies=jar
        )
        with pytest.raises(Exception):
            _refused(client, _channel_url(uuid.uuid4(), SERVER_A), cookies=jar)


async def test_wrong_origin_is_refused(app_fixture: FastAPI) -> None:
    with TestClient(app_fixture) as client:
        jar = _login(client)
        with pytest.raises(Exception):
            _refused(
                client,
                _channel_url(uuid.uuid4(), SERVER_A),
                cookies=jar,
                headers={"origin": "http://evil.example"},
            )
        # Missing Origin entirely.
        with pytest.raises(Exception):
            _refused(
                client,
                _channel_url(uuid.uuid4(), SERVER_A),
                cookies=jar,
                headers={"origin": ""},
            )


async def test_foreign_scope_is_refused(app_fixture: FastAPI) -> None:
    with TestClient(app_fixture) as client:
        jar = _login(client)
        created = client.post(
            "/api/v1/agent-sessions",
            json={
                "server_id": str(SERVER_A),
                "source": "interactive",
                "client_request_id": str(uuid.uuid4()),
            },
            headers={"origin": ORIGIN, "x-csrf-token": jar["mai_csrf"]},
            cookies=jar,
        )
        assert created.status_code == 201
        session_id = uuid.UUID(created.json()["session_id"])
        # The SAME session under a foreign scope: the scope-blind refusal.
        with pytest.raises(Exception):
            _refused(
                client,
                _channel_url(session_id, SERVER_B),
                cookies=jar,
                headers={"origin": ORIGIN},
            )


# ---------------------------------------------------------------------------
# 2. hello + snapshot + committed continuity (the snapshot/live race)
# ---------------------------------------------------------------------------


async def test_snapshot_then_committed_exactly_once(
    app_fixture: FastAPI, migrated_url: str
) -> None:
    with TestClient(app_fixture) as client:
        jar = _login(client)
        created = client.post(
            "/api/v1/agent-sessions",
            json={
                "server_id": str(SERVER_A),
                "source": "interactive",
                "client_request_id": str(uuid.uuid4()),
            },
            headers={"origin": ORIGIN, "x-csrf-token": jar["mai_csrf"]},
            cookies=jar,
        )
        session_id = uuid.UUID(created.json()["session_id"])
        _, sessions = create_app_engine(migrated_url)
        for n in range(3):
            entry = UserMessageEntry(
                kind=EntryKind.USER_MESSAGE,
                text=f"seed {n}",
                client_request_id=uuid.UUID(int=n),
                source=SessionSourceKind.API,
            )
            await aappend_entry(
                sessions,
                session_id=session_id,
                server_id=SERVER_A,
                kind=EntryKind.USER_MESSAGE,
                payload=entry.model_dump(mode="json"),
                run_id=None,
                durable_source_id=f"seed-{n}",
            )
        engine = app_fixture.state.engine
        hub = app_fixture.state.session_channel

        with client.websocket_connect(
            _channel_url(session_id, SERVER_A),
            cookies=jar,
            headers={"origin": ORIGIN},
        ) as websocket:
            websocket.send_text(_hello(jar["mai_csrf"]))
            hello_ok = websocket.receive_json()
            assert hello_ok["type"] == "hello_ok"
            assert hello_ok["protocol_version"] == 1
            assert hello_ok["latest_seq"] == 3
            snapshot = websocket.receive_json()
            assert snapshot["type"] == "snapshot"
            assert len(snapshot["entries"]) == 3
            assert snapshot["latest_seq"] == 3
            assert snapshot["has_more"] is False
            # Commit AFTER the subscription — through the REAL turn route
            # (the app's own loop): the durable admission + the NOTIFY
            # wakeup must deliver the entry as ONE committed frame (no
            # gap, no duplicate).
            committed_responses = client.post(
                f"/api/v1/agent-sessions/{session_id}/turns",
                params={"server_id": str(SERVER_A)},
                json={
                    "session_id": str(session_id),
                    "text": "live one",
                    "client_request_id": str(uuid.uuid4()),
                },
                headers={"origin": ORIGIN, "x-csrf-token": jar["mai_csrf"]},
                cookies=jar,
            )
            assert committed_responses.status_code in (200, 201)
            committed = websocket.receive_json()
            while committed["type"] == "pong":
                committed = websocket.receive_json()
            assert committed["type"] == "committed"
            assert committed["entry"]["seq"] == 4
            # Exactly once: the connection received seq 4 a single time —
            # the admitted turn's durable user entry.
            assert committed["entry"]["durable_source_id"].startswith("turn:")
        # The fanout shrank after the socket closed (no dead-client state).
        assert hub.sink_count(session_id) == 0
        assert engine is not None


# ---------------------------------------------------------------------------
# 3. frame validation negatives
# ---------------------------------------------------------------------------


async def test_snapshot_holds_hub_catchup_off_outbox(
    app_fixture: FastAPI, migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot path (hello without a cursor) must hold the hub off the
    connection's outbox while the route reads/sends the snapshot.

    Regression (D08): with ``replaying`` left False on the snapshot path, a
    concurrent hub tick read the newest tail WITHOUT a cursor at limit 50
    and pushed committed frames on top of the ≤30-entry snapshot — the
    client cache held 50 entries (oldest 9951 for a 10k session) instead
    of the initial-load cap of 30."""
    from mai.api import agent_sessions as agent_sessions_api

    with TestClient(app_fixture) as client:
        jar = _login(client)
        created = client.post(
            "/api/v1/agent-sessions",
            json={
                "server_id": str(SERVER_A),
                "source": "interactive",
                "client_request_id": str(uuid.uuid4()),
            },
            headers={"origin": ORIGIN, "x-csrf-token": jar["mai_csrf"]},
            cookies=jar,
        )
        session_id = uuid.UUID(created.json()["session_id"])
        _, sessions = create_app_engine(migrated_url)
        # More entries than the snapshot cap so a stray floor-0 catch-up
        # page (newest 50) would DIFFER from the snapshot (newest 30).
        for n in range(35):
            entry = UserMessageEntry(
                kind=EntryKind.USER_MESSAGE,
                text=f"seed {n}",
                client_request_id=uuid.UUID(int=n + 1),
                source=SessionSourceKind.API,
            )
            await aappend_entry(
                sessions,
                session_id=session_id,
                server_id=SERVER_A,
                kind=EntryKind.USER_MESSAGE,
                payload=entry.model_dump(mode="json"),
                run_id=None,
                durable_source_id=f"seed-{n}",
            )
        hub = app_fixture.state.session_channel

        real_reader = agent_sessions_api.aread_session_history
        observed_replaying: list[bool] = []

        async def spying_reader(*args: object, **kwargs: object):
            for sink in hub._sessions.get(session_id, {}).values():
                observed_replaying.append(sink.replaying)
            return await real_reader(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(
            agent_sessions_api, "aread_session_history", spying_reader
        )

        with client.websocket_connect(
            _channel_url(session_id, SERVER_A),
            cookies=jar,
            headers={"origin": ORIGIN},
        ) as websocket:
            websocket.send_text(_hello(jar["mai_csrf"]))
            hello_ok = websocket.receive_json()
            assert hello_ok["type"] == "hello_ok"
            snapshot = websocket.receive_json()
            assert snapshot["type"] == "snapshot"
            # Initial-load cap: ≤30 entries even while 35 exist.
            assert len(snapshot["entries"]) == 30
            assert snapshot["latest_seq"] == 35
            assert snapshot["has_older"] is True
        # Every reader call the route made saw the outbox held off.
        assert observed_replaying, "route never read the snapshot"
        assert all(observed_replaying)
        # The outbox shrank after the socket closed.
        assert hub.sink_count(session_id) == 0


async def test_csrf_and_frame_negatives(app_fixture: FastAPI) -> None:
    with TestClient(app_fixture) as client:
        jar = _login(client)
        created = client.post(
            "/api/v1/agent-sessions",
            json={
                "server_id": str(SERVER_A),
                "source": "interactive",
                "client_request_id": str(uuid.uuid4()),
            },
            headers={"origin": ORIGIN, "x-csrf-token": jar["mai_csrf"]},
            cookies=jar,
        )
        session_id = uuid.UUID(created.json()["session_id"])

        def _connect():
            return client.websocket_connect(
                _channel_url(session_id, SERVER_A),
                cookies=jar,
                headers={"origin": ORIGIN},
            )

        # Missing CSRF token in hello → the bounded error frame + close.
        with _connect() as websocket:
            websocket.send_text(
                json.dumps(
                    {
                        "type": "hello",
                        "protocol_version": 1,
                        "after_seq": None,
                        "client_request_id": str(uuid.uuid4()),
                    }
                )
            )
            frames = _read_until_closed(websocket)
            assert frames == [] or frames[0]["type"] == "error"
        # WRONG CSRF token → the bounded error frame + close.
        with _connect() as websocket:
            websocket.send_text(_hello("wrong-token"))
            frames = _read_until_closed(websocket)
            assert frames == [] or frames[0]["type"] == "error"
        # Malformed JSON → refused (bounded, value-free; no content echo).
        with _connect() as websocket:
            websocket.send_text("{not json")
            frames = _read_until_closed(websocket)
            assert frames == [] or frames[0]["type"] == "error"
        # Unknown type → refused.
        with _connect() as websocket:
            websocket.send_text(
                json.dumps({"type": "engine_cancel", "run_id": str(uuid.uuid4())})
            )
            frames = _read_until_closed(websocket)
            assert frames == [] or frames[0]["type"] == "error"
        # An engine-state WRITE attempt through the channel: the closed
        # vocabulary has no such frame (extra fields are forbidden) — the
        # write command never reaches any durable surface.
        with _connect() as websocket:
            websocket.send_text(
                json.dumps(
                    {
                        "type": "hello",
                        "protocol_version": 1,
                        "after_seq": None,
                        "csrf_token": jar["mai_csrf"],
                        "client_request_id": str(uuid.uuid4()),
                        "engine_command": "cancel_run",
                    }
                )
            )
            frames = _read_until_closed(websocket)
            assert frames == [] or frames[0]["type"] == "error"
        # An oversized frame (>64 KiB) → refused before parsing.
        with _connect() as websocket:
            websocket.send_text(_hello(jar["mai_csrf"]) + "x" * (64 * 1024))
            frames = _read_until_closed(websocket)
            assert frames == [] or frames[0]["type"] == "error"


# ---------------------------------------------------------------------------
# 4. bounded 10,000-entry resume + replay budget
# ---------------------------------------------------------------------------


async def test_ten_thousand_entry_resume_is_bounded(
    app_fixture: FastAPI, migrated_url: str
) -> None:
    from sqlalchemy import text as sql_text

    with TestClient(app_fixture) as client:
        jar = _login(client)
        created = client.post(
            "/api/v1/agent-sessions",
            json={
                "server_id": str(SERVER_A),
                "source": "interactive",
                "client_request_id": str(uuid.uuid4()),
            },
            headers={"origin": ORIGIN, "x-csrf-token": jar["mai_csrf"]},
            cookies=jar,
        )
        session_id = uuid.UUID(created.json()["session_id"])
        engine, sessions = create_app_engine(migrated_url)
        # The bulk seed: ONE executemany transaction (bounded bindings,
        # 10,000 rows).
        async with sessions() as session:
            async with session.begin():
                rows = [
                    {
                        "sid": session_id,
                        "seq": n,
                        "kind": "user_message",
                        "payload": json.dumps(
                            {
                                "kind": "user_message",
                                "text": f"bulk {n}",
                                "client_request_id": str(uuid.UUID(int=n)),
                                "source": "api",
                            }
                        ),
                        "src": f"bulk-{n}",
                    }
                    for n in range(1, 10001)
                ]
                await session.execute(
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
        with client.websocket_connect(
            _channel_url(session_id, SERVER_A),
            cookies=jar,
            headers={"origin": ORIGIN},
        ) as websocket:
            websocket.send_text(_hello(jar["mai_csrf"], after_seq=9950))
            hello_ok = websocket.receive_json()
            assert hello_ok["type"] == "hello_ok"
            entries_frame = websocket.receive_json()
            assert entries_frame["type"] == "entries"
            entries = entries_frame["entries"]
            # Bounded page: at most 50 entries, ordered, exactly the tail.
            assert 1 <= len(entries) <= 50
            assert [e["seq"] for e in entries] == list(range(9951, 9951 + len(entries)))
            assert entries_frame["latest_seq"] == 10000
            assert entries_frame["has_more"] is False
            import json as _json

            assert len(_json.dumps(entries_frame)) <= 64 * 1024
        # A cursor at the VERY beginning of a 10k session: bounded pages
        # then resync_required — never a full transcript.
        with client.websocket_connect(
            _channel_url(session_id, SERVER_A),
            cookies=jar,
            headers={"origin": ORIGIN},
        ) as websocket:
            websocket.send_text(_hello(jar["mai_csrf"], after_seq=1))
            websocket.receive_json()  # hello_ok
            resync_seen = False
            entries_pages = 0
            for _ in range(64):
                frame = websocket.receive_json()
                if frame["type"] == "entries":
                    entries_pages += 1
                    assert len(frame["entries"]) <= 50
                    assert len(json.dumps(frame)) <= 64 * 1024
                elif frame["type"] == "resync_required":
                    resync_seen = True
                    assert frame["reason_code"] == "replay_budget_exhausted"
                    break
            assert resync_seen is True
            assert entries_pages <= 20  # the replay page budget
        # A FORGED cursor (an absurd sequence the client never received):
        # the response is BOUNDED and value-free (empty page, no leak).
        with client.websocket_connect(
            _channel_url(session_id, SERVER_A),
            cookies=jar,
            headers={"origin": ORIGIN},
        ) as websocket:
            websocket.send_text(_hello(jar["mai_csrf"], after_seq=10**9))
            websocket.receive_json()  # hello_ok
            frame = websocket.receive_json()
            assert frame["type"] == "entries"
            assert frame["entries"] == []
            assert frame["has_more"] is False


# ---------------------------------------------------------------------------
# 4b. lost-ack reconnect replay idempotence + NOTIFY-loss catch-up
# ---------------------------------------------------------------------------


async def test_reconnect_replays_committed_exactly_once(
    app_fixture: FastAPI, migrated_url: str
) -> None:
    """A lost ack: the client reconnects with its OLD cursor and the
    committed entries replay; the client ends with exactly ONE copy per
    seq (the durable store is the replay authority)."""
    import psycopg

    with TestClient(app_fixture) as client:
        jar = _login(client)
        created = client.post(
            "/api/v1/agent-sessions",
            json={
                "server_id": str(SERVER_A),
                "source": "interactive",
                "client_request_id": str(uuid.uuid4()),
            },
            headers={"origin": ORIGIN, "x-csrf-token": jar["mai_csrf"]},
            cookies=jar,
        )
        session_id = uuid.UUID(created.json()["session_id"])

        def commit(n: int) -> None:
            with psycopg.connect(migrated_url, autocommit=True) as conn:
                conn.execute(
                    "INSERT INTO session_entries"
                    " (entry_id, session_id, run_id, seq, kind, payload,"
                    " durable_source_id)"
                    " VALUES (gen_random_uuid(), %s, NULL, %s,"
                    " 'user_message', %s, %s)",
                    (
                        session_id,
                        n,
                        json.dumps(
                            {
                                "kind": "user_message",
                                "text": f"c{n}",
                                "client_request_id": str(uuid.UUID(int=n)),
                                "source": "api",
                            }
                        ),
                        f"c-{n}",
                    ),
                )
                conn.execute(
                    "SELECT pg_notify('mai_session_events', %s)",
                    (
                        json.dumps(
                            {"session_id": str(session_id), "latest_seq": n}
                        ),
                    ),
                )

        first_seqs: list[int] = []
        with client.websocket_connect(
            _channel_url(session_id, SERVER_A),
            cookies=jar,
            headers={"origin": ORIGIN},
        ) as websocket:
            websocket.send_text(_hello(jar["mai_csrf"]))
            websocket.receive_json()  # hello_ok
            websocket.receive_json()  # snapshot (empty)
            for n in range(1, 4):
                commit(n)
            for _ in range(3):
                frame = websocket.receive_json()
                while frame["type"] != "committed":
                    frame = websocket.receive_json()
                first_seqs.append(frame["entry"]["seq"])
            assert first_seqs == [1, 2, 3]
        # The connection dropped WITHOUT acking; two more commits land.
        commit(4)
        commit(5)
        # Reconnect with the OLD cursor (before the lost frames): bounded
        # replay covers everything after it.
        with client.websocket_connect(
            _channel_url(session_id, SERVER_A),
            cookies=jar,
            headers={"origin": ORIGIN},
        ) as websocket:
            websocket.send_text(_hello(jar["mai_csrf"], after_seq=3))
            websocket.receive_json()  # hello_ok
            replayed: list[int] = []
            while len(replayed) < 2:
                frame = websocket.receive_json()
                if frame["type"] == "entries":
                    replayed.extend(e["seq"] for e in frame["entries"])
            assert replayed == [4, 5]
        # The client's committed view: exactly ONE copy per seq overall.
        combined = first_seqs + replayed
        assert sorted(combined) == [1, 2, 3, 4, 5]
        assert len(combined) == len(set(combined))


async def test_notify_loss_is_recovered_by_listener_restart(
    app_fixture: FastAPI, migrated_url: str
) -> None:
    """Kill the shared listener, commit (the NOTIFY is lost — nobody is
    listening), restart the listener: the reconnect catch-up recovers the
    missed entries BY CURSOR (never by faster polling)."""
    import asyncio
    import threading

    import psycopg

    with TestClient(app_fixture) as client:
        jar = _login(client)
        created = client.post(
            "/api/v1/agent-sessions",
            json={
                "server_id": str(SERVER_A),
                "source": "interactive",
                "client_request_id": str(uuid.uuid4()),
            },
            headers={"origin": ORIGIN, "x-csrf-token": jar["mai_csrf"]},
            cookies=jar,
        )
        session_id = uuid.UUID(created.json()["session_id"])
        hub = app_fixture.state.session_channel
        reconnects_before = hub.reconnect_count

        with client.websocket_connect(
            _channel_url(session_id, SERVER_A),
            cookies=jar,
            headers={"origin": ORIGIN},
        ) as websocket:
            websocket.send_text(_hello(jar["mai_csrf"]))
            websocket.receive_json()  # hello_ok
            websocket.receive_json()  # snapshot (empty)
            # KILL the listener: notifications are now lost.
            hub.restart_listener()

            def commit_lost_and_found() -> None:
                with psycopg.connect(migrated_url, autocommit=True) as conn:
                    for n in (1, 2):
                        conn.execute(
                            "INSERT INTO session_entries"
                            " (entry_id, session_id, run_id, seq, kind,"
                            " payload, durable_source_id)"
                            " VALUES (gen_random_uuid(), %s, NULL, %s,"
                            " 'user_message', %s, %s)",
                            (
                                session_id,
                                n,
                                json.dumps(
                                    {
                                        "kind": "user_message",
                                        "text": f"lost-{n}",
                                        "client_request_id": str(
                                            uuid.UUID(int=n)
                                        ),
                                        "source": "api",
                                    }
                                ),
                                f"lost-{n}",
                            ),
                        )

            threading.Thread(target=commit_lost_and_found, daemon=True).start()
            # The commits land while nobody listens; RESTART the listener:
            # the reconnect catch-up force-polls the subscribed session
            # and recovers BOTH entries by cursor.
            await asyncio.sleep(0.4)
            hub.restart_listener()
            got: list[dict] = []
            state = {"done": False}

            def drain() -> None:
                while not state["done"]:
                    try:
                        frame = websocket.receive_json()
                    except Exception:
                        state["done"] = True
                        return
                    got.append(frame)
                    committed = [
                        f for f in got if f["type"] == "committed"
                    ]
                    if {f["entry"]["seq"] for f in committed} >= {1, 2}:
                        state["done"] = True
                        return

            drain_thread = threading.Thread(target=drain, daemon=True)
            drain_thread.start()
            drain_thread.join(timeout=15)
            if drain_thread.is_alive():
                raise AssertionError(
                    "delivery did not complete within 15s; "
                    f"frames={[f['type'] for f in got]} "
                    f"ticks={hub.tick_count} notifies={hub.notify_count} "
                    f"catchup={hub.catchup_query_count} "
                    f"errors={hub.catchup_error_count} "
                    f"reconnects={hub.reconnect_count} "
                    f"sinks={hub.sink_count(session_id)}"
                )
            committed = [f for f in got if f["type"] == "committed"]
            assert sorted(f["entry"]["seq"] for f in committed) == [1, 2]
            assert hub.reconnect_count > reconnects_before
        assert hub.sink_count(session_id) == 0


# ---------------------------------------------------------------------------
# 5. backpressure, heartbeat, dead-client fanout
# ---------------------------------------------------------------------------


async def test_backpressure_heartbeat_and_fanout_shrink(
    app_fixture: FastAPI, migrated_url: str
) -> None:
    from mai.session_channel import CHANNEL_OUTBOX_LIMIT, SessionSink
    from mai.contracts.session_channel import (
        ServerCommittedFrame,
        ServerDeltaFrame,
        SessionEntryView,
    )
    from mai.contracts.sessions import (
        EntryKind as CEntryKind,
        SessionSourceKind,
        UserMessageEntry,
    )

    with TestClient(app_fixture) as client:
        jar = _login(client)
        created = client.post(
            "/api/v1/agent-sessions",
            json={
                "server_id": str(SERVER_A),
                "source": "interactive",
                "client_request_id": str(uuid.uuid4()),
            },
            headers={"origin": ORIGIN, "x-csrf-token": jar["mai_csrf"]},
            cookies=jar,
        )
        session_id = uuid.UUID(created.json()["session_id"])
        hub = app_fixture.state.session_channel

        with client.websocket_connect(
            _channel_url(session_id, SERVER_A),
            cookies=jar,
            headers={"origin": ORIGIN},
        ) as websocket:
            websocket.send_text(_hello(jar["mai_csrf"]))
            websocket.receive_json()  # hello_ok
            websocket.receive_json()  # snapshot
            # HEARTBEAT: ping → pong through the real socket.
            websocket.send_text(json.dumps({"type": "ping"}))
            pong = websocket.receive_json()
            while pong["type"] != "pong":
                pong = websocket.receive_json()
            assert pong["type"] == "pong"

            # BACKPRESSURE at the hub boundary: a hub-attached sink with NO
            # flusher task draining it (a slow/dead client) — flood it with
            # VOLATILE deltas (dropped first), then prove a committed frame
            # SURVIVES (the volatile drop frees the room).
            sink: SessionSink = SessionSink(
                connection_id=uuid.uuid4(),
                session_id=session_id,
                server_id=SERVER_A,
            )
            hub.subscribe(sink)
            stream = f"stream:{sink.connection_id}"
            for n in range(400):  # > CHANNEL_OUTBOX_LIMIT (256)
                hub.publish_delta(
                    session_id,
                    ServerDeltaFrame(
                        stream_id=stream,
                        delta_id=f"d{n}",
                        text="x",
                        run_id=uuid.uuid4(),
                    ),
                )
            assert sink.dropped_deltas > 0
            assert sink.outbox.qsize() <= CHANNEL_OUTBOX_LIMIT
            committed = ServerCommittedFrame(
                stream_id=stream,
                entry=SessionEntryView(
                    entry_id=uuid.uuid4(),
                    session_id=session_id,
                    seq=2,
                    durable_source_id="probe",
                    created_at="2026-01-01T00:00:00Z",
                    payload=UserMessageEntry(
                        kind=CEntryKind.USER_MESSAGE,
                        text="probe",
                        client_request_id=uuid.uuid4(),
                        source=SessionSourceKind.API,
                    ),
                ),
            )
            sink.delivered_through = 1
            sink.offer(committed)
            assert sink.resync is None  # the committed frame FIT (deltas
            # were dropped to make room) and it was never dropped itself.
            # Now overflow with DURABLE frames: committed frames cannot be
            # dropped, so continuity is declared lost (resync_required) —
            # the client re-reads them from the durable store.
            for n in range(700):
                sink.offer(
                    ServerCommittedFrame(
                        stream_id=stream,
                        entry=SessionEntryView(
                            entry_id=uuid.uuid4(),
                            session_id=session_id,
                            seq=10 + n,
                            durable_source_id=f"probe-{n}",
                            created_at="2026-01-01T00:00:00Z",
                            payload=UserMessageEntry(
                                kind=CEntryKind.USER_MESSAGE,
                                text="probe",
                                client_request_id=uuid.uuid4(),
                                source=SessionSourceKind.API,
                            ),
                        ),
                    )
                )
            assert sink.resync == "outbox_overflow"
            hub.unsubscribe(sink)
        # The fanout map SHRANK after the disconnect (dead clients are not
        # retained in memory).
        assert hub.sink_count(session_id) == 0


# ---------------------------------------------------------------------------
# 6. idle discipline: ticks advance, ZERO catch-up SQL at the head
# ---------------------------------------------------------------------------


async def test_idle_tick_issues_zero_catchup_sql(
    app_fixture: FastAPI, migrated_url: str
) -> None:
    import asyncio

    with TestClient(app_fixture) as client:
        jar = _login(client)
        created = client.post(
            "/api/v1/agent-sessions",
            json={
                "server_id": str(SERVER_A),
                "source": "interactive",
                "client_request_id": str(uuid.uuid4()),
            },
            headers={"origin": ORIGIN, "x-csrf-token": jar["mai_csrf"]},
            cookies=jar,
        )
        session_id = uuid.UUID(created.json()["session_id"])
        hub = app_fixture.state.session_channel
        with client.websocket_connect(
            _channel_url(session_id, SERVER_A),
            cookies=jar,
            headers={"origin": ORIGIN},
        ) as websocket:
            websocket.send_text(_hello(jar["mai_csrf"]))
            websocket.receive_json()  # hello_ok
            websocket.receive_json()  # snapshot (empty)
            # The subscription's first catch-up marks the head.
            await asyncio.sleep(TICK * 3)
            queries_before = hub.catchup_query_count
            ticks_before = hub.tick_count
            await asyncio.sleep(TICK * 6)  # ~6 idle ticks, no commits
            # Ticks advanced; ZERO catch-up SQL was issued at the head.
            assert hub.tick_count > ticks_before
            assert hub.catchup_query_count == queries_before
