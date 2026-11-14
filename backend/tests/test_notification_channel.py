"""Focused tests for the live notification wakeup channel (task nbi-live,
docs/architecture/notification-inbox.md §8) on a REAL disposable Postgres
through the real linear Alembic chain and the real ASGI app — never mocks:

1. **Identity-only frames** — the NOTIFY payload whitelist drops anything
   beyond ``{kind, alert_id, server_id}`` (title/body/source never cross
   the wire), and every SSE frame's JSON key set is exactly the identity
   triple with the notice's title/source nowhere in the stream body.
2. **Durable-first + loss ≤ one coarse tick** — publishers fire the
   ``pg_notify`` strictly AFTER the row's commit; an injected NOTIFY
   loss (listener killed across a real service ingest) is recovered by
   the reconnect resync broadcast well inside one coarse tick (0.2 s
   configured here), and the client's refetch after reconnect yields
   EXACTLY the durable state (one notice, right id, no duplicates).
3. **Bounded fanout** — outbox overflow coalesces into ONE resync frame
   (memory bounded by the outbox limit), a pending burst collapses into
   a refetch signal, and closed sinks are evicted, never retained.
4. **SSE auth before the first byte** — no session → 401 envelope, bad
   Origin → 403 envelope, both answered as ordinary JSON responses
   (never ``text/event-stream`` bytes); a valid session streams.
5. **Mutation wakeups** — ack/unack/mark-all-read publish one identity
   ``updated`` frame per touched row after the durable commit; the
   created frame of the pre-stream ingest does not leak into the stream.

The fixture DB is disposable (drop schema → ``alembic upgrade head`` per
module); minted keys are throwaway tokens printed nowhere; the fixture
password is an inert constant.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI

import mai.api.notifications as notifications_api
from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE
from mai.contracts.server import SCHEMA_VERSION
from mai.notification_channel import (
    NotificationChannelHub,
    WakeupFrame,
    notify_notification_wakeup,
)
from mai.profiles.files import ProfileFileStore

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: Synthetic fixture credential (NOT a secret; disposable fixture DB only).
FIXTURE_PASSWORD = "fixture-password-nbi-live-only"
ORIGIN = "http://127.0.0.1:8443"  # loopback allowlist entry
EVIL_ORIGIN = "https://attacker.example"

#: Fast-test coarse tick (the hub's env seam, clamped 0.05..60 s).
FAST_TICK_SECONDS = "0.2"

#: Frozen fixed fixture identity (canonical lowercase UUID string).
SERVER_A = "a10b0000-0000-4000-8000-00000000a001"

#: 43-char unpadded base64 body; the frozen schema-card fixture host key.
HOST_KEY = "SHA256:ZmXWe/xAeTz26CuAmJ1jHtg8ZPX0mMhVfrBPM+C/zS4"

EVENTS_PATH = "/api/v1/notifications/events"
NOTIFICATIONS_PATH = "/api/v1/notifications"


# ---------------------------------------------------------------------------
# Fixtures: real disposable Postgres + real app through lifespan
# ---------------------------------------------------------------------------


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(os.path.join(BACKEND_ROOT, "alembic.ini"))


def _drop_app_tables(url: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


@pytest.fixture(scope="module")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``head``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the live wakeup channel claims "
            "need a real disposable Postgres fixture"
        )
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


def _truncate(url: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            "TRUNCATE ingest_keys, notification_alerts, observation_findings, "
            "server_observations, job_runs, job_steps, job_events, "
            "job_schedules, job_schedule_fires, approvals, admin_sessions, "
            "admin_credential RESTART IDENTITY CASCADE"
        )
        conn.commit()


def _build_app(url: str, data_root: str) -> FastAPI:
    return create_app(
        env={
            "MAI_DATA_ROOT": data_root,
            "MAI_DATABASE_URL": url,
            "MAI_PUBLIC_ORIGINS": ORIGIN,
            "MAI_NOTIFICATION_CHANNEL_TICK_SECONDS": FAST_TICK_SECONDS,
        }
    )


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[FastAPI]:
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
def data_root(tmp_path: Any) -> str:
    return str(tmp_path / "data-root")


@pytest.fixture
async def live_app(migrated_url: str, data_root: str) -> AsyncIterator[FastAPI]:
    """App on the real fixture DB with the fast coarse tick."""
    _truncate(migrated_url)
    app = _build_app(migrated_url, data_root)
    async with _running(app):
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()


@pytest.fixture
async def session_jar(live_app: FastAPI) -> dict[str, str]:
    """One authenticated cookie jar per test (fresh bootstrap per test)."""
    bootstrap = await call_app(
        live_app,
        "POST",
        "/api/v1/auth/bootstrap",
        json_body={"password": FIXTURE_PASSWORD},
        headers={"origin": ORIGIN},
    )
    assert bootstrap.status == 201, bootstrap.body
    login = await call_app(
        live_app,
        "POST",
        "/api/v1/auth/login",
        json_body={"password": FIXTURE_PASSWORD},
        headers={"origin": ORIGIN},
    )
    assert login.status == 204, login.body
    cookies: dict[str, str] = {}
    for raw in login.header_all("set-cookie"):
        name, _, value = raw.partition("=")
        cookies[name] = value.split(";", 1)[0]
    assert SESSION_COOKIE in cookies and CSRF_COOKIE in cookies
    return cookies


def _profile_text(server_id: uuid.UUID, name: str) -> str:
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
  host_key: "{HOST_KEY}"
  credential_ref: "MAI_SSH_KEY_NBI_LIVE_FIXTURE"
privilege: none
capabilities:
  - shell.exec
"""


@pytest.fixture
def data_root_with_profile(data_root: str) -> str:
    ProfileFileStore(data_root).write(
        uuid.UUID(SERVER_A),
        _profile_text(uuid.UUID(SERVER_A), f"fixture-{SERVER_A[:8]}"),
        expected_revision=0,
    )
    return data_root


# ---------------------------------------------------------------------------
# Minimal ASGI harness (no test-client dependency; bodies + cookies)
# ---------------------------------------------------------------------------


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


async def call_app(
    app: FastAPI,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    headers: Mapping[str, str] | None = None,
    cookies: Mapping[str, str] | None = None,
) -> HttpResponse:
    """Run one HTTP request through the real ASGI callable."""
    path, _, query = path.partition("?")
    scope_headers = [(b"host", b"fixture")]
    scope_headers.extend(
        (key.encode("ascii").lower(), value.encode("ascii"))
        for key, value in (headers or {}).items()
    )
    if cookies:
        scope_headers.append(
            (b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode())
        )
    body = b""
    if json_body is not None:
        body = json.dumps(json_body).encode()
        scope_headers.append((b"content-type", b"application/json"))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
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

    try:
        await app(scope, receive, send)
    except Exception:  # noqa: BLE001 - ServerErrorMiddleware re-raises after reply
        pass
    return response


def _error(response: HttpResponse) -> dict[str, Any]:
    envelope = response.json()
    assert set(envelope) == {"error"}
    body = envelope["error"]
    assert set(body) == {"code", "message", "request_id", "fields"}
    assert "Traceback" not in response.body.decode()
    return body


@dataclass(slots=True)
class StreamCall:
    """One open streaming request through the real ASGI callable.

    Mirrors the ``test_jobs_http`` harness discipline: the ASGI receive
    channel delivers the request body once and then BLOCKS (a live
    client), so the disconnect listener never sees a second
    ``http.request`` (which would kill the stream).
    """

    body: str = ""
    status: int | None = None
    headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    task: asyncio.Task[None] | None = None
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
            raise AssertionError(
                f"stream started with {self.status}: {self.body!r}"
            )

    async def wait_until(self, predicate: Any, timeout: float, what: str) -> None:
        """Drain the live stream until ``predicate(body)`` or it ends."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(self.body):
                return
            if self.task is not None and self.task.done():
                break
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=0.05)
        if not predicate(self.body):
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")

    async def close(self) -> None:
        """Simulate the client disconnecting (the browser closes the tab)."""
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
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
        # delivered once; afterwards the channel BLOCKS until a disconnect
        # (it never re-delivers ``http.request``).
        if not call._request_consumed:
            call._request_consumed = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()  # connected until close() cancels
        return {"type": "http.disconnect"}  # pragma: no cover - unreachable

    async def runner() -> None:
        await app(_scope("GET", path, flat_headers), receive, call._on_message)

    call.task = asyncio.get_running_loop().create_task(runner())
    await call.wait_started()
    return call


def _scope(method: str, path: str, headers: Mapping[str, str]) -> dict[str, Any]:
    path, _, query = path.partition("?")
    scope_headers = [(b"host", b"fixture")]
    scope_headers.extend(
        (key.encode("ascii").lower(), value.encode("ascii"))
        for key, value in headers.items()
    )
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query.encode("ascii"),
        "root_path": "",
        "headers": scope_headers,
        "client": ("127.0.0.1", 0),
        "server": ("fixture", 80),
    }


def _data_frames(body: str | bytes) -> list[dict[str, Any]]:
    """Parse the SSE ``data:`` frames out of one stream body."""
    text = body.decode() if isinstance(body, bytes) else body
    frames: list[dict[str, Any]] = []
    for line in text.splitlines():
        if line.startswith("data: "):
            frames.append(json.loads(line[len("data: ") :]))
    return frames


def _mutation_headers(jar: dict[str, str]) -> dict[str, str]:
    """Origin + CSRF headers for one authenticated mutation."""
    return {"origin": ORIGIN, "x-csrf-token": jar[CSRF_COOKIE]}


async def _mint_key(
    app: FastAPI,
    jar: dict[str, str],
    *,
    label: str = "probe-source",
    server_id: str | None = None,
) -> str:
    """Create one key through the real surface; return the plaintext."""
    body: dict[str, Any] = {"label": label}
    if server_id is not None:
        body["server_id"] = server_id
    response = await call_app(
        app,
        "POST",
        "/api/v1/ingest-keys",
        json_body=body,
        cookies=jar,
        headers=_mutation_headers(jar),
    )
    assert response.status == 201, response.body
    return str(response.json()["key"])


async def _ingest(
    app: FastAPI,
    payload: Mapping[str, Any],
    *,
    token: str,
) -> HttpResponse:
    headers: dict[str, str] = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
    }
    return await call_app(
        app,
        "POST",
        "/api/v1/notifications/ingest",
        json_body=dict(payload),
        headers=headers,
    )


async def _next_frame(sink: Any, timeout: float) -> Any:
    return await asyncio.wait_for(sink.outbox.get(), timeout)


# ---------------------------------------------------------------------------
# 1. SSE auth BEFORE the first stream byte
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestEventsAuth:
    async def test_missing_session_401_before_any_stream_byte(
        self, live_app: FastAPI
    ) -> None:
        """No session: an ordinary 401 envelope, never a stream."""
        response = await call_app(
            live_app, "GET", EVENTS_PATH, headers={"origin": ORIGIN}
        )
        assert response.status == 401, response.body
        assert _error(response)["code"] == "unauthenticated"
        assert response.header("content-type") == "application/json"
        assert "text/event-stream" not in (response.body.decode())

    async def test_bad_origin_403_before_any_stream_byte(
        self, live_app: FastAPI, session_jar: dict[str, str]
    ) -> None:
        """A valid session but a non-allowlisted Origin: 403, no stream."""
        response = await call_app(
            live_app,
            "GET",
            EVENTS_PATH,
            headers={"origin": EVIL_ORIGIN},
            cookies=session_jar,
        )
        assert response.status == 403, response.body
        assert _error(response)["code"] == "origin_rejected"
        assert response.header("content-type") == "application/json"


# ---------------------------------------------------------------------------
# 2. Identity-only wakeups through the real channel (service + routes)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestWakeupStream:
    async def test_ingest_wakeup_is_identity_only(
        self,
        live_app: FastAPI,
        session_jar: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The service's post-commit NOTIFY wakes the live stream within
        one tick; the frame carries exactly {kind, alert_id, server_id}
        and the notice's title/source never cross the wire."""
        monkeypatch.setattr(
            notifications_api, "SSE_MAX_STREAM_SECONDS", 3.0
        )
        token = await _mint_key(live_app, session_jar)
        stream = await open_stream(
            live_app,
            EVENTS_PATH,
            headers={"origin": ORIGIN},
            cookies=session_jar,
        )
        assert stream.header("content-type") == "text/event-stream; charset=utf-8"
        assert stream.header("cache-control") == "no-store"
        secret_title = "secret-title-nbi-live-ф"
        ingest = await _ingest(
            live_app,
            {"title": secret_title, "source": "probe-source", "body": "secret body"},
            token=token,
        )
        assert ingest.status == 200, ingest.body
        alert_id = ingest.json()["alert_id"]

        def got_frame(body: str) -> bool:
            return f'"alert_id":"{alert_id}"' in body

        start = time.monotonic()
        await stream.wait_until(got_frame, timeout=2.5, what="the created wakeup")
        elapsed = time.monotonic() - start
        await stream.close()
        frames = _data_frames(stream.body)
        assert len(frames) == 1, stream.body
        frame = frames[0]
        # Identity ONLY: no title/body/source keys, no secret content.
        assert set(frame) == {"kind", "alert_id", "server_id"}
        assert frame["kind"] == "created"
        assert frame["alert_id"] == alert_id
        assert frame["server_id"] is None  # a global key publishes global rows
        assert secret_title not in stream.body
        assert "probe-source" not in stream.body
        assert "secret body" not in stream.body
        assert elapsed < 2.5, elapsed  # well inside one coarse tick (0.2 s)

    async def test_ack_unack_mark_all_publish_updated_after_commit(
        self,
        live_app: FastAPI,
        session_jar: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ack/unack/mark-all-read each publish one identity ``updated``
        frame AFTER the durable commit; the pre-stream created frame
        (already flushed while nobody listened) does not leak in."""
        monkeypatch.setattr(
            notifications_api, "SSE_MAX_STREAM_SECONDS", 4.0
        )
        token = await _mint_key(live_app, session_jar)
        ingest = await _ingest(
            live_app,
            {"title": "state change probe", "dedup_key": "state-1"},
            token=token,
        )
        assert ingest.status == 200, ingest.body
        alert_id = ingest.json()["alert_id"]
        await asyncio.sleep(0.6)  # > one tick: the created frame flushed

        stream = await open_stream(
            live_app,
            EVENTS_PATH,
            headers={"origin": ORIGIN},
            cookies=session_jar,
        )
        ack = await call_app(
            live_app,
            "POST",
            f"/api/v1/notifications/{alert_id}/ack",
            cookies=session_jar,
            headers=_mutation_headers(session_jar),
        )
        assert ack.status == 200, ack.body
        await stream.wait_until(
            lambda body: body.count("data: ") >= 1, 2.5, "the ack wakeup"
        )
        unack = await call_app(
            live_app,
            "POST",
            f"/api/v1/notifications/{alert_id}/unack",
            cookies=session_jar,
            headers=_mutation_headers(session_jar),
        )
        assert unack.status == 200, unack.body
        mark_all = await call_app(
            live_app,
            "POST",
            "/api/v1/notifications/mark-all-read",
            cookies=session_jar,
            headers=_mutation_headers(session_jar),
        )
        assert mark_all.status == 200, mark_all.body
        assert mark_all.json() == {"updated": 1}
        await stream.wait_until(
            lambda body: body.count("data: ") >= 3, 2.5, "all three wakeups"
        )
        await stream.close()
        frames = _data_frames(stream.body)
        assert len(frames) == 3, frames  # ack + unack + mark-all, nothing else
        assert [frame["kind"] for frame in frames] == ["updated"] * 3
        for frame in frames:
            assert set(frame) == {"kind", "alert_id", "server_id"}
            assert frame["alert_id"] == alert_id
            assert frame["server_id"] is None


# ---------------------------------------------------------------------------
# 3. Bounded fanout: overflow coalesces, dead sinks evicted
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestBoundedFanout:
    async def test_outbox_overflow_coalesces_into_one_resync(self) -> None:
        """Identity frames are volatile: a full outbox drains them and
        keeps exactly ONE refetch frame — memory bounded by the limit."""
        hub = NotificationChannelHub(coarse_tick_seconds=0.05, outbox_limit=4)
        sink = hub.subscribe()
        for n in range(4):
            sink.outbox.put_nowait(
                WakeupFrame(
                    kind="created", alert_id=str(uuid.uuid4()), server_id=None
                )
            )
        hub._fanout(
            WakeupFrame(kind="updated", alert_id=str(uuid.uuid4()), server_id=None)
        )
        assert sink.outbox.qsize() == 1  # the whole backlog → one resync
        frame = sink.outbox.get_nowait()
        assert frame.kind == "resync"
        assert frame.wire() == '{"kind":"resync"}'
        assert sink.overflow_resyncs == 1

    async def test_closed_sink_is_evicted_and_ignored(self) -> None:
        """Dead clients are never retained: unsubscribe shrinks the map
        and offers to a closed sink are no-ops."""
        hub = NotificationChannelHub()
        sink = hub.subscribe()
        assert hub.sink_count() == 1
        hub.unsubscribe(sink)
        assert hub.sink_count() == 0 and sink.closed
        hub._fanout(WakeupFrame(kind="created", alert_id=str(uuid.uuid4())))
        assert sink.outbox.qsize() == 0

    async def test_pending_burst_stays_bounded(self) -> None:
        """A burst beyond the pending bound collapses into a resync —
        the hub never buffers an unbounded wakeup queue."""
        hub = NotificationChannelHub(pending_limit=8)
        for _ in range(100):
            hub.publish_local(
                WakeupFrame(kind="created", alert_id=str(uuid.uuid4()))
            )
        assert len(hub._pending) <= 8
        assert hub._pending[0].kind == "resync"
        assert hub.coalesced_wakeup_count > 0

    async def test_notify_payload_whitelist_drops_content(
        self, migrated_url: str
    ) -> None:
        """A NOTIFY payload carrying anything beyond the identity triple
        is DROPPED at the listener; a valid one fans out identity only."""
        from mai.database import create_app_engine

        engine, _factory = create_app_engine(migrated_url)
        hub = NotificationChannelHub(coarse_tick_seconds=0.05)
        sink = hub.subscribe()
        try:
            hub._on_notify_payload(
                "mai_notification_alerts",
                json.dumps(
                    {
                        "kind": "created",
                        "alert_id": str(uuid.uuid4()),
                        "server_id": None,
                        "title": "LEAK-ATTEMPT",
                    }
                ),
            )
            assert hub.notify_count == 0
            assert hub._pending == []
            good_id = str(uuid.uuid4())
            hub._on_notify_payload(
                "mai_notification_alerts",
                json.dumps(
                    {
                        "kind": "updated",
                        "alert_id": good_id,
                        "server_id": None,
                    }
                ),
            )
            assert hub.notify_count == 1
            hub.start(engine)
            frame = await _next_frame(sink, timeout=2.0)
            assert frame.kind == "updated" and frame.alert_id == good_id
            assert set(json.loads(frame.wire())) == {
                "kind",
                "alert_id",
                "server_id",
            }
        finally:
            await hub.stop()
            await engine.dispose()

    async def test_publisher_payload_is_identity_only(
        self, migrated_url: str
    ) -> None:
        """The publisher's actual pg_notify payload parses to exactly the
        identity triple (nothing else is ever serialized)."""
        from mai.database import create_app_engine

        engine, _factory = create_app_engine(migrated_url)
        hub = NotificationChannelHub(coarse_tick_seconds=0.05)
        sink = hub.subscribe()
        hub.start(engine)
        try:
            # The listener's own connect broadcast proves LISTEN is armed;
            # publishing only after it cannot lose the NOTIFY.
            connect_frame = await _next_frame(sink, timeout=2.0)
            assert connect_frame.kind == "resync"
            alert_id = uuid.uuid4()
            server_id = uuid.UUID(SERVER_A)
            await notify_notification_wakeup(
                engine,
                kind="created",
                alert_id=alert_id,
                server_id=server_id,
            )
            frame = await _next_frame(sink, timeout=2.0)
            assert frame.kind == "created"
            assert frame.wire() == (
                '{"kind":"created",'
                f'"alert_id":"{alert_id}","server_id":"{server_id}"'
                "}"
            )
        finally:
            await hub.stop()
            await engine.dispose()


# ---------------------------------------------------------------------------
# 4. Injected NOTIFY loss: reconnect resync + exact durable refetch
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestNotifyLossRecovery:
    async def test_injected_notify_loss_catches_up_within_one_tick(
        self,
        live_app: FastAPI,
        session_jar: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Kill the listener, ingest through the REAL service lane (the
        NOTIFY fires into the void), restart the listener: the reconnect
        resync reaches both a direct sink AND the live SSE stream well
        inside one coarse tick (0.2 s + 0.5 s reconnect backoff), and the
        client's refetch lands EXACTLY on the durable state."""
        monkeypatch.setattr(
            notifications_api, "SSE_MAX_STREAM_SECONDS", 8.0
        )
        hub = live_app.state.notification_channel
        assert hub.running
        direct_sink = hub.subscribe()  # a second consumer for timing proof
        stream = await open_stream(
            live_app,
            EVENTS_PATH,
            headers={"origin": ORIGIN},
            cookies=session_jar,
        )
        assert hub.sink_count() == 2

        # LISTENER DOWN: wakeups fired from here are lost.
        hub.restart_listener()
        await asyncio.sleep(0.1)
        token = await _mint_key(live_app, session_jar)
        ingest = await _ingest(
            live_app,
            {"title": "lost-wakeup probe", "dedup_key": "loss-1"},
            token=token,
        )
        assert ingest.status == 200, ingest.body
        alert_id = ingest.json()["alert_id"]
        await asyncio.sleep(0.3)  # the notify fired while nobody listened

        # LISTENER BACK: the reconnect broadcast = the refetch signal.
        started = time.monotonic()
        hub.restart_listener()
        frame = await _next_frame(direct_sink, timeout=3.0)
        elapsed = time.monotonic() - started
        assert frame.kind == "resync", frame
        assert elapsed < 1.5, elapsed  # backoff 0.5 + tick 0.2, comfortably

        # The SSE stream saw the same refetch signal.
        await stream.wait_until(
            lambda body: '"kind":"resync"' in body, 3.0, "the reconnect resync"
        )
        await stream.close()
        frames = _data_frames(stream.body)
        resyncs = [f for f in frames if f["kind"] == "resync"]
        assert resyncs, frames

        # Client refetch after reconnect = EXACT durable state: one
        # notice, the committed id, no duplicates, honest counts.
        page = await call_app(
            live_app,
            "GET",
            NOTIFICATIONS_PATH,
            cookies=session_jar,
        )
        assert page.status == 200, page.body
        document = page.json()
        assert document["total"] == 1
        assert [alert["alert_id"] for alert in document["alerts"]] == [alert_id]
        count = await call_app(
            live_app,
            "GET",
            f"{NOTIFICATIONS_PATH}/unread-count",
            cookies=session_jar,
        )
        assert count.json() == {"total": 1}
        hub.unsubscribe(direct_sink)
