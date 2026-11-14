"""Focused tests for the machine ingest lane + ingest-key HTTP surface
(task nbi-ingest-api, auth ADR §13) on a REAL disposable Postgres through
the real linear Alembic chain and the real ASGI app (``create_app`` +
lifespan + middleware + handlers), never mocks:

1. **Auth matrix (value-free)** — absent/malformed/unknown/revoked/
   expired Bearer credentials and G1 setup state collapse into ONE
   ``401 unauthenticated`` envelope; a valid key publishes (200);
   session routes keep rejecting Bearer credentials (no boundary leak).
2. **Scope matrix (no existence oracle)** — a scoped key publishes for
   its own server (payload server_id optional); unknown ≡ foreign ≡
   garbage ≡ global-key-with-server ≡ gone-key-server all answer the
   byte-identical ``422 invalid_scope`` envelope.
3. **Body budget ordering** — 16384 passes; 16385 answers the
   byte-identical ``413 body_too_large`` whether the key is valid,
   invalid or absent (Content-Length pre-check AND bounded chunked
   read), writes no row, and still counts against the key's rate window
   (61st request in the window → ``429 throttled``).
4. **Payload + idempotent replay** — bound violations are ``422
   validation_failed``; a supplied dedup_key replays onto the committed
   row (``deduped``, never a duplicate); an absent one is unique per
   message; source defaults to the key label.
5. **Key management** — mint returns plaintext EXACTLY ONCE; the list
   page is bounded/paginated and never carries key material; mutations
   enforce session → origin → CSRF; revoke is effective on the next
   ingest and the receipt is idempotent.
6. **Captain read surface (nbi-read-api)** — the canonical filter matrix
   (all / scope=global / server_id ± include_global / unread / origin /
   source / q over title, summary, body and source, case-insensitively
   with escaped wildcards), honest pagination (deterministic order,
   COUNT under the same predicate, bounded limits), bidirectional
   idempotent ack → unack on the ONE ``acknowledged_at`` column with a
   value-free scope-blind 404, and a mark-all-read receipt that equals
   the rows actually stamped; every mutation keeps session → origin →
   CSRF and evidence is never deleted.

Secret hygiene: the fixture password is an inert constant for the
disposable fixture DB; minted keys are throwaway tokens printed nowhere.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI

from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE
from mai.contracts.observations import MAX_INGEST_BODY_BYTES
from mai.contracts.server import SCHEMA_VERSION
from mai.profiles.files import ProfileFileStore

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: Synthetic fixture credential (NOT a secret; disposable fixture DB only).
FIXTURE_PASSWORD = "fixture-password-nbi-ingest-api-only"
ORIGIN = "http://127.0.0.1:8443"  # loopback allowlist entry
EVIL_ORIGIN = "https://attacker.example"

#: Frozen fixed fixture identities (canonical lowercase UUID strings,
#: distinct from every other card's constants).
SERVER_A = "a10b0000-0000-4000-8000-00000000a001"
SERVER_B = "b20c0000-0000-4000-8000-00000000b002"
SERVER_GONE = "c30d0000-0000-4000-8000-00000000c003"  # never seeded
SERVER_UNKNOWN = "d40e0000-0000-4000-8000-00000000d004"  # never seeded

#: 43-char unpadded base64 body; the frozen schema-card fixture host key.
HOST_KEY = "SHA256:ZmXWe/xAeTz26CuAmJ1jHtg8ZPX0mMhVfrBPM+C/zS4"

INGEST_PATH = "/api/v1/notifications/ingest"
NOTIFICATIONS_PATH = "/api/v1/notifications"


# ---------------------------------------------------------------------------
# Fixtures: real disposable Postgres + real app through lifespan
# ---------------------------------------------------------------------------


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(os.path.join(BACKEND_ROOT, "alembic.ini"))


def _drop_app_tables(url: str) -> None:
    with psycopg.connect(url) as conn:
        # Whole-schema reset (D09 discipline): FK-safe and immune to
        # future revisions.
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``head``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the ingest HTTP surface claims "
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
async def ingest_app(migrated_url: str, data_root: str) -> AsyncIterator[FastAPI]:
    """App on the real fixture DB with a fresh confined data root."""
    _truncate(migrated_url)
    app = _build_app(migrated_url, data_root)
    async with _running(app):
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()


@pytest.fixture
async def session_jar(ingest_app: FastAPI) -> dict[str, str]:
    """One authenticated cookie jar per test (fresh bootstrap per test)."""
    bootstrap = await call_app(
        ingest_app,
        "POST",
        "/api/v1/auth/bootstrap",
        json_body={"password": FIXTURE_PASSWORD},
        headers={"origin": ORIGIN},
    )
    assert bootstrap.status == 201, bootstrap.body
    login = await call_app(
        ingest_app,
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


# ---------------------------------------------------------------------------
# Seed helpers (real canonical profile files; synthetic text)
# ---------------------------------------------------------------------------


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
  credential_ref: "MAI_SSH_KEY_INGEST_FIXTURE"
privilege: none
capabilities:
  - shell.exec
"""


def _seed_profile(data_root: str, server_id: str) -> None:
    ProfileFileStore(data_root).write(
        uuid.UUID(server_id),
        _profile_text(uuid.UUID(server_id), f"fixture-{server_id[:8]}"),
        expected_revision=0,
    )


@pytest.fixture
def seeded_server_a(data_root: str) -> str:
    _seed_profile(data_root, SERVER_A)
    return SERVER_A


@pytest.fixture
def seeded_server_b(data_root: str) -> str:
    _seed_profile(data_root, SERVER_B)
    return SERVER_B


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
    raw_body: bytes | None = None,
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
    if raw_body is not None:
        body = raw_body
    elif json_body is not None:
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


def _canonical_error(response: HttpResponse) -> str:
    """The C01 envelope minus the per-request ``request_id``.

    Envelopes that must be byte-identical modulo the per-request id
    compare equal through this canonical form — and their raw bodies then
    differ in the request id alone.
    """
    envelope = response.json()
    envelope["error"].pop("request_id")
    return json.dumps(envelope, sort_keys=True)


def _mutation_headers(jar: dict[str, str]) -> dict[str, str]:
    """Origin + CSRF headers for one authenticated mutation."""
    return {"origin": ORIGIN, "x-csrf-token": jar[CSRF_COOKIE]}


async def _mint_key(
    app: FastAPI,
    jar: dict[str, str],
    *,
    label: str = "probe-source",
    server_id: str | None = None,
    expires_at: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Create one key through the real surface; return (plaintext, view)."""
    body: dict[str, Any] = {"label": label}
    if server_id is not None:
        body["server_id"] = server_id
    if expires_at is not None:
        body["expires_at"] = expires_at
    response = await call_app(
        app,
        "POST",
        "/api/v1/ingest-keys",
        json_body=body,
        cookies=jar,
        headers=_mutation_headers(jar),
    )
    assert response.status == 201, response.body
    payload = response.json()
    return str(payload["key"]), payload


async def _ingest(
    app: FastAPI,
    payload: Mapping[str, Any] | None = None,
    *,
    token: str | None = None,
    authorization: str | None = None,
    content_length: int | None = None,
    raw_body: bytes | None = None,
) -> HttpResponse:
    headers: dict[str, str] = {"content-type": "application/json"}
    if authorization is not None:
        headers["authorization"] = authorization
    elif token is not None:
        headers["authorization"] = f"Bearer {token}"
    if content_length is not None:
        headers["content-length"] = str(content_length)
    body = (
        raw_body
        if raw_body is not None
        else json.dumps(dict(payload or {})).encode()
    )
    return await call_app(
        app, "POST", INGEST_PATH, raw_body=body, headers=headers
    )


def _row_count(url: str, table: str) -> int:
    with psycopg.connect(url) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _alert_rows(url: str) -> list[tuple[object, ...]]:
    with psycopg.connect(url) as conn:
        return list(
            conn.execute(
                "SELECT server_id, origin, severity, source, title "
                "FROM notification_alerts ORDER BY created_at"
            ).fetchall()
        )


# ---------------------------------------------------------------------------
# TestIngestAuth — the value-free auth matrix
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestIngestAuth:
    """Absent/malformed/unknown/revoked/expired/G1 → one 401 envelope."""

    async def test_setup_state_refuses_before_anything_else(
        self, ingest_app: FastAPI, migrated_url: str
    ) -> None:
        """No credential record: G1 answers 401 before budget and keys."""
        assert _row_count(migrated_url, "admin_credential") == 0
        small = await _ingest(ingest_app, {"title": "probe"})
        assert small.status == 401, small.body
        assert _error(small)["code"] == "unauthenticated"

        oversized = await _ingest(
            ingest_app, raw_body=b"x" * (MAX_INGEST_BODY_BYTES + 1)
        )
        # G1 precedes the body budget: not a 413, the same value-free 401.
        assert oversized.status == 401, oversized.body
        assert _canonical_error(oversized) == _canonical_error(small)
        assert _row_count(migrated_url, "notification_alerts") == 0

    async def test_absent_bearer_is_value_free_401(
        self, ingest_app: FastAPI, session_jar: dict[str, str], migrated_url: str
    ) -> None:
        response = await _ingest(ingest_app, {"title": "probe"})
        assert response.status == 401, response.body
        error = _error(response)
        assert error["code"] == "unauthenticated"
        assert error["fields"] == {}
        assert "key" not in response.body.decode().lower().replace(
            "unauthenticated", ""
        )
        assert _row_count(migrated_url, "notification_alerts") == 0

    async def test_malformed_bearer_forms_share_one_envelope(
        self, ingest_app: FastAPI, session_jar: dict[str, str]
    ) -> None:
        absent = await _ingest(ingest_app, {"title": "probe"})
        for header in (
            "Basic YWJj",
            "Bearer",
            "Bearer    ",
            "Token abcdef",
            f"bearer\t{ 'x' * 43 }",
        ):
            response = await _ingest(
                ingest_app, {"title": "probe"}, authorization=header
            )
            assert response.status == 401, (header, response.body)
            assert _canonical_error(response) == _canonical_error(absent), header

    async def test_bearer_scheme_is_case_insensitive(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
    ) -> None:
        token, _ = await _mint_key(ingest_app, session_jar, server_id=SERVER_A)
        response = await _ingest(
            ingest_app,
            {"title": "probe"},
            authorization=f"bearer {token}",
        )
        assert response.status == 200, response.body
        assert response.json()["disposition"] == "created"

    async def test_unknown_revoked_expired_share_the_absent_envelope(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
        migrated_url: str,
    ) -> None:
        absent = await _ingest(ingest_app, {"title": "probe"})
        unknown = await _ingest(ingest_app, {"title": "probe"}, token="x" * 43)

        revoked_token, revoked_view = await _mint_key(
            ingest_app, session_jar, server_id=SERVER_A
        )
        revoke = await call_app(
            ingest_app,
            "POST",
            f"/api/v1/ingest-keys/{revoked_view['key_id']}/revoke",
            cookies=session_jar,
            headers=_mutation_headers(session_jar),
        )
        assert revoke.status == 200, revoke.body
        revoked = await _ingest(
            ingest_app, {"title": "probe"}, token=revoked_token
        )

        expired_token, _ = await _mint_key(
            ingest_app,
            session_jar,
            server_id=SERVER_A,
            expires_at="2020-01-01T00:00:00Z",
        )
        expired = await _ingest(ingest_app, {"title": "probe"}, token=expired_token)

        canonical = _canonical_error(absent)
        for response, name in (
            (unknown, "unknown"),
            (revoked, "revoked"),
            (expired, "expired"),
        ):
            assert response.status == 401, (name, response.body)
            assert _canonical_error(response) == canonical, name
        assert _error(absent)["code"] == "unauthenticated"

    async def test_session_surface_still_rejects_bearer(
        self, ingest_app: FastAPI, session_jar: dict[str, str], seeded_server_a: str
    ) -> None:
        """No boundary leak: Bearer credentials never open session routes."""
        token, _ = await _mint_key(ingest_app, session_jar)
        for path in ("/api/v1/ingest-keys", "/api/v1/servers"):
            response = await call_app(
                ingest_app, "GET", path, headers={"authorization": f"Bearer {token}"}
            )
            assert response.status == 401, (path, response.body)
            assert _error(response)["code"] == "unauthenticated"


# ---------------------------------------------------------------------------
# TestIngestScope — unknown ≡ foreign ≡ global-abuse (R3-F3)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestIngestScope:
    """One byte-identical 422 invalid_scope for every violation."""

    async def test_scoped_key_publishes_for_own_server(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
        migrated_url: str,
    ) -> None:
        token, view = await _mint_key(
            ingest_app, session_jar, label="host-monitor", server_id=SERVER_A
        )
        response = await _ingest(
            ingest_app,
            {"title": "disk almost full", "server_id": SERVER_A},
            token=token,
        )
        assert response.status == 200, response.body
        body = response.json()
        assert body["disposition"] == "created"
        uuid.UUID(body["alert_id"])
        # source defaults to the key label; severity defaults to info.
        [row] = _alert_rows(migrated_url)
        assert row[0] == uuid.UUID(SERVER_A)
        assert row[1] == "external"
        assert row[2] == "info"
        assert row[3] == view["label"]
        assert view["server_id"] == SERVER_A

    async def test_scoped_key_defaults_to_key_scope(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
        migrated_url: str,
    ) -> None:
        token, _ = await _mint_key(ingest_app, session_jar, server_id=SERVER_A)
        response = await _ingest(ingest_app, {"title": "probe"}, token=token)
        assert response.status == 200, response.body
        [row] = _alert_rows(migrated_url)
        assert row[0] == uuid.UUID(SERVER_A)

    async def test_every_scope_violation_is_the_same_422(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
        data_root: str,
    ) -> None:
        # Seed a second REAL profile so "existing foreign" is exercised
        # next to "unknown" — they must stay indistinguishable.
        _seed_profile(data_root, SERVER_B)
        scoped_token, _ = await _mint_key(
            ingest_app, session_jar, server_id=SERVER_A
        )
        global_token, _ = await _mint_key(ingest_app, session_jar)

        baseline = await _ingest(
            ingest_app,
            {"title": "probe", "server_id": SERVER_UNKNOWN},
            token=scoped_token,
        )
        assert baseline.status == 422, baseline.body
        assert _error(baseline)["code"] == "invalid_scope"

        violations = [
            # scoped key + existing foreign server
            (
                await _ingest(
                    ingest_app,
                    {"title": "probe", "server_id": SERVER_B},
                    token=scoped_token,
                )
            ),
            # scoped key + garbage server id
            (
                await _ingest(
                    ingest_app,
                    {"title": "probe", "server_id": "not-a-uuid"},
                    token=scoped_token,
                )
            ),
            # global key + an existing server
            (
                await _ingest(
                    ingest_app,
                    {"title": "probe", "server_id": SERVER_A},
                    token=global_token,
                )
            ),
            # global key + garbage server id
            (
                await _ingest(
                    ingest_app,
                    {"title": "probe", "server_id": SERVER_UNKNOWN},
                    token=global_token,
                )
            ),
        ]
        for response in violations:
            assert response.status == 422, response.body
            assert _canonical_error(response) == _canonical_error(baseline)

    async def test_scoped_key_with_gone_server_is_invalid_scope(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
        data_root: str,
    ) -> None:
        """The key's own server profile vanished: identical 422, no oracle.

        Mint requires an existing profile, so the fixture seeds one, mints
        the scoped key, then deletes the canonical profile — the honest
        decommissioning path that leaves a key pointing at a gone server.
        """
        _seed_profile(data_root, SERVER_GONE)
        token, _ = await _mint_key(ingest_app, session_jar, server_id=SERVER_GONE)
        ProfileFileStore(data_root).delete(
            uuid.UUID(SERVER_GONE), expected_revision=1
        )
        response = await _ingest(ingest_app, {"title": "probe"}, token=token)
        assert response.status == 422, response.body
        assert _error(response)["code"] == "invalid_scope"

    async def test_global_key_publishes_null_server_row(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
        migrated_url: str,
    ) -> None:
        token, _ = await _mint_key(ingest_app, session_jar, label="fleet-relay")
        first = await _ingest(
            ingest_app, {"title": "backup done", "dedup_key": "backup-ok"}, token=token
        )
        assert first.status == 200, first.body
        assert first.json()["disposition"] == "created"
        replay = await _ingest(
            ingest_app, {"title": "backup done", "dedup_key": "backup-ok"}, token=token
        )
        assert replay.status == 200, replay.body
        assert replay.json()["disposition"] == "deduped"
        assert replay.json()["alert_id"] == first.json()["alert_id"]
        [row] = _alert_rows(migrated_url)
        assert row[0] is None  # NULL server: the global partial index arbiter


# ---------------------------------------------------------------------------
# TestIngestPayload — bounds and idempotent replay
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestIngestPayload:
    async def test_replay_is_idempotent_200_deduped(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
        migrated_url: str,
    ) -> None:
        token, _ = await _mint_key(ingest_app, session_jar, server_id=SERVER_A)
        payload = {"title": "disk almost full", "dedup_key": "disk-/var"}
        first = await _ingest(ingest_app, payload, token=token)
        assert first.status == 200, first.body
        assert first.json()["disposition"] == "created"
        replay = await _ingest(ingest_app, payload, token=token)
        assert replay.status == 200, replay.body
        assert replay.json()["disposition"] == "deduped"
        assert replay.json()["alert_id"] == first.json()["alert_id"]
        assert _row_count(migrated_url, "notification_alerts") == 1

    async def test_without_dedup_key_every_message_is_unique(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
        migrated_url: str,
    ) -> None:
        token, _ = await _mint_key(ingest_app, session_jar, server_id=SERVER_A)
        for index in range(2):
            response = await _ingest(
                ingest_app, {"title": f"probe {index}"}, token=token
            )
            assert response.status == 200, response.body
            assert response.json()["disposition"] == "created"
        assert _row_count(migrated_url, "notification_alerts") == 2

    async def test_payload_bound_violations_are_422(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
        migrated_url: str,
    ) -> None:
        token, _ = await _mint_key(ingest_app, session_jar, server_id=SERVER_A)
        cases: list[tuple[str, Mapping[str, Any] | bytes]] = [
            ("missing_title", {"body": "b"}),
            ("null_title", {"title": None}),
            ("empty_title", {"title": ""}),
            ("long_title", {"title": "x" * 201}),
            ("long_body", {"title": "t", "body": "x" * 4097}),
            ("long_source", {"title": "t", "source": "x" * 65}),
            ("bad_severity", {"title": "t", "severity": "bogus"}),
            ("unknown_field", {"title": "t", "nuisance": 1}),
            ("malformed_json", b"{nope"),
            ("non_object_json", b"[1,2]"),
            ("empty_body", b""),
        ]
        for name, payload in cases:
            if isinstance(payload, bytes):
                response = await _ingest(ingest_app, raw_body=payload, token=token)
            else:
                response = await _ingest(ingest_app, dict(payload), token=token)
            assert response.status == 422, (name, response.body)
            assert _error(response)["code"] == "validation_failed", name
        assert _row_count(migrated_url, "notification_alerts") == 0

    async def test_explicit_source_and_severity_land_in_row(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
        migrated_url: str,
    ) -> None:
        token, _ = await _mint_key(ingest_app, session_jar, server_id=SERVER_A)
        response = await _ingest(
            ingest_app,
            {"title": "cpu hot", "severity": "warning", "source": "sensors"},
            token=token,
        )
        assert response.status == 200, response.body
        [row] = _alert_rows(migrated_url)
        assert row[3] == "sensors" and row[2] == "warning"


# ---------------------------------------------------------------------------
# TestBodyBudget — 413 BEFORE payload AND key validation (R3-F2)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestBodyBudget:
    def _budget_pair(self) -> tuple[bytes, bytes]:
        """Bodies at MAX and MAX+1 bytes with a VALID payload inside.

        Trailing JSON whitespace pads the document to the exact byte
        boundary without breaking the payload bounds (every text field is
        frozen well below the byte budget).
        """
        raw = json.dumps({"title": "budget-boundary", "body": "x" * 64}).encode()
        pad = MAX_INGEST_BODY_BYTES - len(raw)
        assert pad > 0
        exact = raw + b" " * pad
        over = raw + b" " * (pad + 1)
        assert len(exact) == MAX_INGEST_BODY_BYTES
        assert len(over) == MAX_INGEST_BODY_BYTES + 1
        return exact, over

    async def test_exact_budget_body_is_processed(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
        migrated_url: str,
    ) -> None:
        exact, _ = self._budget_pair()
        token, _ = await _mint_key(ingest_app, session_jar, server_id=SERVER_A)
        response = await _ingest(ingest_app, raw_body=exact, token=token)
        assert response.status == 200, response.body
        assert response.json()["disposition"] == "created"

    async def test_one_byte_over_chunked_is_413(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
        migrated_url: str,
    ) -> None:
        _, over = self._budget_pair()
        token, _ = await _mint_key(ingest_app, session_jar, server_id=SERVER_A)
        # No Content-Length header: the bounded stream read decides.
        response = await _ingest(ingest_app, raw_body=over, token=token)
        assert response.status == 413, response.body
        assert _error(response)["code"] == "body_too_large"
        assert _row_count(migrated_url, "notification_alerts") == 0

    async def test_content_length_precheck_answers_413_without_reading(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
        migrated_url: str,
    ) -> None:
        token, _ = await _mint_key(ingest_app, session_jar, server_id=SERVER_A)
        response = await _ingest(
            ingest_app,
            raw_body=b'{"title":"tiny"}',
            token=token,
            content_length=MAX_INGEST_BODY_BYTES + 1,
        )
        assert response.status == 413, response.body
        assert _error(response)["code"] == "body_too_large"
        assert _row_count(migrated_url, "notification_alerts") == 0

    async def test_413_envelope_is_identical_across_key_validity(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
    ) -> None:
        _, over = self._budget_pair()
        valid_token, _ = await _mint_key(ingest_app, session_jar, server_id=SERVER_A)
        with_valid = await _ingest(
            ingest_app, raw_body=over, token=valid_token
        )
        with_invalid = await _ingest(
            ingest_app, raw_body=over, token="y" * 43
        )
        without = await _ingest(ingest_app, raw_body=over)
        assert with_valid.status == with_invalid.status == without.status == 413
        canonical = _canonical_error(with_valid)
        assert _canonical_error(with_invalid) == canonical
        assert _canonical_error(without) == canonical
        assert _error(with_valid)["code"] == "body_too_large"

    async def test_oversized_request_counts_against_rate_window(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
    ) -> None:
        """The refused 413 consumed window slot #1: only 59 small requests
        still fit, and the 60th small request trips the 429 window."""
        _, over = self._budget_pair()
        token, _ = await _mint_key(ingest_app, session_jar, server_id=SERVER_A)
        oversized = await _ingest(ingest_app, raw_body=over, token=token)
        assert oversized.status == 413, oversized.body
        payload = {"title": "window filler", "dedup_key": "window-probe"}
        for _ in range(59):
            response = await _ingest(ingest_app, payload, token=token)
            assert response.status == 200, response.body
        exhausted = await _ingest(ingest_app, payload, token=token)
        assert exhausted.status == 429, exhausted.body
        assert _error(exhausted)["code"] == "throttled"


# ---------------------------------------------------------------------------
# TestRateWindow — durable fixed window (60/minute)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestRateWindow:
    async def test_61st_request_in_window_is_429(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
        migrated_url: str,
    ) -> None:
        token, _ = await _mint_key(ingest_app, session_jar, server_id=SERVER_A)
        payload = {"title": "rate probe", "dedup_key": "rate-probe"}
        for index in range(60):
            response = await _ingest(ingest_app, payload, token=token)
            assert response.status == 200, (index, response.body)
        assert _row_count(migrated_url, "notification_alerts") == 1  # deduped
        exhausted = await _ingest(ingest_app, payload, token=token)
        assert exhausted.status == 429, exhausted.body
        assert _error(exhausted)["code"] == "throttled"

    async def test_rate_window_is_per_key(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
    ) -> None:
        first_token, _ = await _mint_key(
            ingest_app, session_jar, label="rate-a", server_id=SERVER_A
        )
        second_token, _ = await _mint_key(
            ingest_app, session_jar, label="rate-b", server_id=SERVER_A
        )
        payload = {"title": "rate probe"}
        for _ in range(60):
            response = await _ingest(ingest_app, payload, token=first_token)
            assert response.status == 200, response.body
        # A different key is a different window: still served.
        other = await _ingest(ingest_app, payload, token=second_token)
        assert other.status == 200, other.body
        exhausted = await _ingest(ingest_app, payload, token=first_token)
        assert exhausted.status == 429, exhausted.body


# ---------------------------------------------------------------------------
# TestKeyManagement — session→origin→CSRF admin surface
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestKeyManagement:
    async def test_mint_returns_plaintext_once_and_list_never_leaks(
        self, ingest_app: FastAPI, session_jar: dict[str, str], seeded_server_a: str
    ) -> None:
        token, view = await _mint_key(
            ingest_app, session_jar, label="sources-tab", server_id=SERVER_A
        )
        assert isinstance(token, str) and len(token) >= 32
        assert view["label"] == "sources-tab"
        assert view["server_id"] == SERVER_A
        assert view["revoked_at"] is None and view["last_used_at"] is None

        listing = await call_app(
            ingest_app, "GET", "/api/v1/ingest-keys", cookies=session_jar
        )
        assert listing.status == 200, listing.body
        page = listing.json()
        assert page["limit"] == 50 and page["offset"] == 0 and page["total"] == 1
        assert page["has_more"] is False
        [item] = page["keys"]
        assert set(item) == {
            "key_id",
            "label",
            "server_id",
            "created_at",
            "last_used_at",
            "expires_at",
            "revoked_at",
        }
        text = listing.body.decode()
        assert token not in text  # plaintext never leaves the mint receipt
        assert "key_hash" not in text
        assert item["key_id"] == view["key_id"]

    async def test_list_is_bounded_and_paginated(
        self, ingest_app: FastAPI, session_jar: dict[str, str]
    ) -> None:
        for index in range(3):
            await _mint_key(ingest_app, session_jar, label=f"key-{index}")
        first = await call_app(
            ingest_app,
            "GET",
            "/api/v1/ingest-keys?limit=2&offset=0",
            cookies=session_jar,
        )
        page = first.json()
        assert (page["total"], len(page["keys"]), page["has_more"]) == (3, 2, True)
        second = await call_app(
            ingest_app,
            "GET",
            "/api/v1/ingest-keys?limit=2&offset=2",
            cookies=session_jar,
        )
        tail = second.json()
        assert (tail["total"], len(tail["keys"]), tail["has_more"]) == (3, 1, False)
        for query in ("limit=0", "limit=201", "offset=-1"):
            response = await call_app(
                ingest_app,
                "GET",
                f"/api/v1/ingest-keys?{query}",
                cookies=session_jar,
            )
            assert response.status == 422, (query, response.body)
            assert _error(response)["code"] == "validation_failed"

    async def test_create_requires_session_origin_csrf(
        self, ingest_app: FastAPI, session_jar: dict[str, str]
    ) -> None:
        no_session = await call_app(
            ingest_app,
            "POST",
            "/api/v1/ingest-keys",
            json_body={"label": "x"},
            headers={"origin": ORIGIN},
        )
        assert no_session.status == 401
        assert _error(no_session)["code"] == "unauthenticated"

        no_origin = await call_app(
            ingest_app,
            "POST",
            "/api/v1/ingest-keys",
            json_body={"label": "x"},
            cookies=session_jar,
            headers={"x-csrf-token": session_jar[CSRF_COOKIE]},
        )
        assert no_origin.status == 403
        assert _error(no_origin)["code"] == "origin_rejected"

        no_csrf = await call_app(
            ingest_app,
            "POST",
            "/api/v1/ingest-keys",
            json_body={"label": "x"},
            cookies=session_jar,
            headers={"origin": ORIGIN},
        )
        assert no_csrf.status == 403
        assert _error(no_csrf)["code"] == "csrf_rejected"

        bad_csrf = await call_app(
            ingest_app,
            "POST",
            "/api/v1/ingest-keys",
            json_body={"label": "x"},
            cookies=session_jar,
            headers={
                "origin": ORIGIN,
                "x-csrf-token": "not-the-cookie-value",
            },
        )
        assert bad_csrf.status == 403
        assert _error(bad_csrf)["code"] == "csrf_rejected"

    async def test_revoke_is_effective_next_ingest_and_idempotent(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
    ) -> None:
        token, view = await _mint_key(ingest_app, session_jar, server_id=SERVER_A)
        assert (
            await _ingest(ingest_app, {"title": "probe"}, token=token)
        ).status == 200
        revoke = await call_app(
            ingest_app,
            "POST",
            f"/api/v1/ingest-keys/{view['key_id']}/revoke",
            cookies=session_jar,
            headers=_mutation_headers(session_jar),
        )
        assert revoke.status == 200, revoke.body
        assert revoke.json() == {"key_id": view["key_id"], "revoked": True}

        after = await _ingest(ingest_app, {"title": "probe"}, token=token)
        assert after.status == 401, after.body
        assert _error(after)["code"] == "unauthenticated"

        again = await call_app(
            ingest_app,
            "POST",
            f"/api/v1/ingest-keys/{view['key_id']}/revoke",
            cookies=session_jar,
            headers=_mutation_headers(session_jar),
        )
        assert again.status == 200, again.body
        assert again.json() == {"key_id": view["key_id"], "revoked": False}

        unknown = await call_app(
            ingest_app,
            "POST",
            f"/api/v1/ingest-keys/{uuid.uuid4()}/revoke",
            cookies=session_jar,
            headers=_mutation_headers(session_jar),
        )
        assert unknown.status == 200, unknown.body
        assert unknown.json()["revoked"] is False

    async def test_create_validates_label_scope_and_expiry(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
    ) -> None:
        for name, body in (
            ("empty_label", {"label": ""}),
            ("long_label", {"label": "x" * 65}),
            ("unknown_field", {"label": "x", "mode": "write"}),
            ("unknown_server", {"label": "x", "server_id": SERVER_UNKNOWN}),
            ("naive_expiry", {"label": "x", "expires_at": "2020-01-01T00:00:00"}),
            ("bad_server_format", {"label": "x", "server_id": "nope"}),
        ):
            response = await call_app(
                ingest_app,
                "POST",
                "/api/v1/ingest-keys",
                json_body=body,
                cookies=session_jar,
                headers=_mutation_headers(session_jar),
            )
            assert response.status == 422, (name, response.body)
            assert _error(response)["code"] == "validation_failed", name

        boundary = await call_app(
            ingest_app,
            "POST",
            "/api/v1/ingest-keys",
            json_body={"label": "x" * 64},
            cookies=session_jar,
            headers=_mutation_headers(session_jar),
        )
        assert boundary.status == 201, boundary.body

    async def test_create_without_server_mints_global_key(
        self, ingest_app: FastAPI, session_jar: dict[str, str]
    ) -> None:
        _, view = await _mint_key(ingest_app, session_jar, label="fleet")
        assert view["server_id"] is None


# ---------------------------------------------------------------------------
# Captain read surface (nbi-read-api): seed helpers + filter matrix
# ---------------------------------------------------------------------------


async def _seed_inbox(
    app: FastAPI,
    jar: dict[str, str],
) -> dict[str, list[str]]:
    """Three external notices (SERVER_A, SERVER_B, global) via the REAL
    ingest lane, each through its own scope-pinned key. Returns their
    alert ids in creation order."""
    a_token, _ = await _mint_key(app, jar, label="host-monitor", server_id=SERVER_A)
    b_token, _ = await _mint_key(app, jar, label="backupd", server_id=SERVER_B)
    g_token, _ = await _mint_key(app, jar, label="fleet-feed")
    seeded: dict[str, list[str]] = {SERVER_A: [], SERVER_B: [], "global": []}
    for token, scope, payload in (
        (
            a_token,
            SERVER_A,
            {
                "title": "Disk almost full",
                "body": "/var usage 94% on db-1",
                "severity": "warning",
                "source": "host-monitor",
                "dedup_key": "disk-var",
            },
        ),
        (
            b_token,
            SERVER_B,
            {
                "title": "Backup failed",
                "body": "nightly backup did not run",
                "severity": "critical",
                "source": "backupd",
                "dedup_key": "backup-nightly",
            },
        ),
        (
            g_token,
            "global",
            {
                "title": "Fleet announcement",
                "body": "maintenance window tonight",
                "severity": "info",
                "source": "fleet-feed",
                "dedup_key": "announce-tonight",
            },
        ),
    ):
        response = await _ingest(app, payload, token=token)
        assert response.status == 200, response.body
        seeded[scope].append(str(response.json()["alert_id"]))
    return seeded


def _seed_finding_notice(
    url: str, *, server_id: str, summary: str
) -> str:
    """Test-only state setup: one finding-anchored notice with a summary.

    The REAL producer writes these through ``evaluate_findings``; this
    file seeds the row directly (same discipline as the monitoring
    suite's filter tests) to prove the ``q`` summary arm against a row
    that carries ``summary`` instead of ``body``.
    """
    finding_id = str(uuid.uuid4())
    alert_id = str(uuid.uuid4())
    with psycopg.connect(url) as conn:
        conn.execute(
            "INSERT INTO observation_findings (finding_id, server_id, kind,"
            " dedup_key, severity, summary, retain_until)"
            " VALUES (%s, %s, 'seed.kind', %s, 'info', %s,"
            " now() + interval '7 days')",
            (finding_id, server_id, f"seed-{alert_id[:8]}", summary),
        )
        conn.execute(
            "INSERT INTO notification_alerts (alert_id, server_id, origin,"
            " finding_id, dedup_key, severity, title, summary, delivery_status,"
            " delivered_at, retain_until)"
            " VALUES (%s, %s, 'finding', %s, %s, 'info', 'Seeded finding"
            " notice', %s, 'delivered', now(), now() + interval '30 days')",
            (alert_id, server_id, finding_id, f"seed-alert-{alert_id[:8]}", summary),
        )
        conn.commit()
    return alert_id


async def _list_inbox(
    app: FastAPI,
    jar: dict[str, str],
    query: str = "",
) -> Any:
    """GET /api/v1/notifications (one bounded page) as parsed JSON."""
    path = NOTIFICATIONS_PATH + (f"?{query}" if query else "")
    response = await call_app(app, "GET", path, cookies=jar)
    assert response.status == 200, response.body
    return response.json()


async def _mutation(
    app: FastAPI,
    jar: dict[str, str],
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> HttpResponse:
    """POST one read-surface mutation with default origin+CSRF headers."""
    return await call_app(
        app,
        "POST",
        path,
        cookies=jar,
        headers=headers if headers is not None else _mutation_headers(jar),
    )


def _ack_state(url: str) -> dict[str, str | None]:
    """Every notice's ``acknowledged_at`` keyed by alert id (raw truth)."""
    with psycopg.connect(url) as conn:
        return {
            str(row[0]): None if row[1] is None else str(row[1])
            for row in conn.execute(
                "SELECT alert_id, acknowledged_at FROM notification_alerts"
            ).fetchall()
        }


@pytest.mark.anyio
class TestReadFilters:
    """The canonical filter matrix over the REAL ingested inbox."""

    async def test_all_notices_page_shape_and_deterministic_order(
        self,
        ingest_app: FastAPI,
        migrated_url: str,
        session_jar: dict[str, str],
        seeded_server_a: str,
        seeded_server_b: str,
    ) -> None:
        seeded = await _seed_inbox(ingest_app, session_jar)
        page = await _list_inbox(ingest_app, session_jar)
        assert page["total"] == 3
        assert page["limit"] == 50 and page["offset"] == 0
        assert page["has_more"] is False
        assert len(page["alerts"]) == 3
        by_id = {view["alert_id"]: view for view in page["alerts"]}
        assert set(by_id) == {
            seeded[SERVER_A][0],
            seeded[SERVER_B][0],
            seeded["global"][0],
        }
        global_view = by_id[seeded["global"][0]]
        assert global_view["server_id"] is None  # global notice: NULL scope
        assert global_view["origin"] == "external"
        assert global_view["source"] == "fleet-feed"
        assert global_view["body"] == "maintenance window tonight"
        assert global_view["severity"] == "info"
        assert global_view["delivery_status"] == "delivered"
        assert global_view["acked"] is False
        assert global_view["acknowledged_at"] is None
        assert global_view["summary"] is None  # external text lives in body
        assert by_id[seeded[SERVER_A][0]]["server_id"] == SERVER_A
        assert by_id[seeded[SERVER_B][0]]["severity"] == "critical"
        # Deterministic ordering: created_at DESC, alert_id DESC.
        moments = [
            (
                datetime.fromisoformat(view["created_at"].replace("Z", "+00:00")),
                view["alert_id"],
            )
            for view in page["alerts"]
        ]
        assert moments == sorted(moments, reverse=True)

    async def test_scope_global_returns_only_null_server_rows(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
        seeded_server_b: str,
    ) -> None:
        await _seed_inbox(ingest_app, session_jar)
        page = await _list_inbox(ingest_app, session_jar, "scope=global")
        assert page["total"] == 1
        [view] = page["alerts"]
        assert view["server_id"] is None
        assert view["source"] == "fleet-feed"

    async def test_server_filter_includes_global_by_default(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
        seeded_server_b: str,
    ) -> None:
        seeded = await _seed_inbox(ingest_app, session_jar)
        page = await _list_inbox(
            ingest_app, session_jar, f"server_id={SERVER_A}"
        )
        assert page["total"] == 2  # SERVER_A + the global notice
        assert {view["alert_id"] for view in page["alerts"]} == {
            seeded[SERVER_A][0],
            seeded["global"][0],
        }

    async def test_server_filter_include_global_false_is_strict(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
        seeded_server_b: str,
    ) -> None:
        seeded = await _seed_inbox(ingest_app, session_jar)
        page = await _list_inbox(
            ingest_app, session_jar, f"server_id={SERVER_A}&include_global=false"
        )
        assert page["total"] == 1
        [view] = page["alerts"]
        assert view["alert_id"] == seeded[SERVER_A][0]
        assert view["server_id"] == SERVER_A

    async def test_include_global_is_ignored_without_server_id(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
        seeded_server_b: str,
    ) -> None:
        await _seed_inbox(ingest_app, session_jar)
        page = await _list_inbox(
            ingest_app, session_jar, "include_global=false"
        )
        assert page["total"] == 3  # nothing to exclude: ALL notices

    async def test_unread_filter_follows_acknowledged_at(
        self,
        ingest_app: FastAPI,
        migrated_url: str,
        session_jar: dict[str, str],
        seeded_server_a: str,
        seeded_server_b: str,
    ) -> None:
        seeded = await _seed_inbox(ingest_app, session_jar)
        unread = await _list_inbox(ingest_app, session_jar, "unread=true")
        assert unread["total"] == 3
        read = await _list_inbox(ingest_app, session_jar, "unread=false")
        assert read["total"] == 0
        acked = await _mutation(
            ingest_app,
            session_jar,
            f"{NOTIFICATIONS_PATH}/{seeded[SERVER_A][0]}/ack",
        )
        assert acked.status == 200, acked.body
        unread_after = await _list_inbox(ingest_app, session_jar, "unread=true")
        assert unread_after["total"] == 2
        read_after = await _list_inbox(ingest_app, session_jar, "unread=false")
        assert read_after["total"] == 1
        assert read_after["alerts"][0]["alert_id"] == seeded[SERVER_A][0]
        assert read_after["alerts"][0]["acked"] is True

    async def test_origin_filter_pins_the_vocabulary(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
        seeded_server_b: str,
    ) -> None:
        await _seed_inbox(ingest_app, session_jar)
        external = await _list_inbox(ingest_app, session_jar, "origin=external")
        assert external["total"] == 3
        finding = await _list_inbox(ingest_app, session_jar, "origin=finding")
        assert finding["total"] == 0
        job = await _list_inbox(ingest_app, session_jar, "origin=job_result")
        assert job["total"] == 0

    async def test_source_filter_is_exact_match(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
        seeded_server_b: str,
    ) -> None:
        await _seed_inbox(ingest_app, session_jar)
        page = await _list_inbox(ingest_app, session_jar, "source=backupd")
        assert page["total"] == 1
        assert page["alerts"][0]["source"] == "backupd"
        # Exact match: case differs → no hit.
        none = await _list_inbox(ingest_app, session_jar, "source=BACKUPD")
        assert none["total"] == 0

    async def test_q_matches_each_field_case_insensitively(
        self,
        ingest_app: FastAPI,
        migrated_url: str,
        session_jar: dict[str, str],
        seeded_server_a: str,
        seeded_server_b: str,
    ) -> None:
        seeded = await _seed_inbox(ingest_app, session_jar)
        seeded[SERVER_A].append(
            _seed_finding_notice(
                migrated_url,
                server_id=SERVER_A,
                summary="Quarterly compliance digest",
            )
        )

        async def ids_for(q: str) -> set[str]:
            page = await _list_inbox(
                ingest_app, session_jar, f"q={q.replace(' ', '+')}"
            )
            return {view["alert_id"] for view in page["alerts"]}

        # title (case-insensitive)
        assert await ids_for("DISK ALMOST") == {seeded[SERVER_A][0]}
        # body
        assert await ids_for("94% on") == {seeded[SERVER_A][0]}
        # summary (the finding notice carries summary, not body)
        assert await ids_for("quarterly compliance") == {seeded[SERVER_A][1]}
        # source
        assert await ids_for("FLEET-FEED") == {seeded["global"][0]}
        # wildcards stay LITERAL: underscore/percent are escaped, so the
        # pattern "f_e" matches nothing (never "fleet-feed" as a wildcard).
        assert await ids_for("f_e") == set()
        # a bare % only matches rows that CONTAIN a literal % (one body).
        assert await ids_for("%") == {seeded[SERVER_A][0]}
        assert await ids_for("not-in-any-notice") == set()

    async def test_server_id_and_scope_are_mutually_exclusive(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
    ) -> None:
        response = await call_app(
            ingest_app,
            "GET",
            f"{NOTIFICATIONS_PATH}?server_id={SERVER_A}&scope=global",
            cookies=session_jar,
        )
        assert response.status == 422, response.body
        assert _error(response)["code"] == "validation_failed"

    async def test_scope_vocabulary_is_pinned(
        self, ingest_app: FastAPI, session_jar: dict[str, str]
    ) -> None:
        response = await call_app(
            ingest_app,
            "GET",
            f"{NOTIFICATIONS_PATH}?scope=fleet",
            cookies=session_jar,
        )
        assert response.status == 422, response.body
        assert _error(response)["code"] == "validation_failed"

    async def test_pagination_bounds_and_load_more_slices(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
        seeded_server_b: str,
    ) -> None:
        seeded = await _seed_inbox(ingest_app, session_jar)
        for query in ("limit=0", "limit=201", "offset=-1"):
            response = await call_app(
                ingest_app,
                "GET",
                f"{NOTIFICATIONS_PATH}?{query}",
                cookies=session_jar,
            )
            assert response.status == 422, (query, response.body)
            assert _error(response)["code"] == "validation_failed"
        first = await _list_inbox(ingest_app, session_jar, "limit=2&offset=0")
        assert first["total"] == 3
        assert len(first["alerts"]) == 2
        assert first["has_more"] is True
        second = await _list_inbox(ingest_app, session_jar, "limit=2&offset=2")
        assert second["total"] == 3
        assert len(second["alerts"]) == 1
        assert second["has_more"] is False
        # Load-more slices: no duplicate and no gap under static data.
        seen = [view["alert_id"] for view in first["alerts"] + second["alerts"]]
        assert len(seen) == len(set(seen)) == 3
        assert set(seen) == {
            seeded[SERVER_A][0],
            seeded[SERVER_B][0],
            seeded["global"][0],
        }

    async def test_reads_require_a_session_and_reject_bearer(
        self, ingest_app: FastAPI, session_jar: dict[str, str]
    ) -> None:
        for path in (
            NOTIFICATIONS_PATH,
            f"{NOTIFICATIONS_PATH}/unread-count",
        ):
            response = await call_app(ingest_app, "GET", path)
            assert response.status == 401, (path, response.body)
            assert _error(response)["code"] == "unauthenticated"
        bearer = await call_app(
            ingest_app,
            "GET",
            NOTIFICATIONS_PATH,
            headers={"authorization": "Bearer not-a-session"},
        )
        assert bearer.status == 401, bearer.body


@pytest.mark.anyio
class TestUnreadCount:
    """The count route answers under the EXACT list predicate."""

    _FILTER_SETS = (
        "",
        "scope=global",
        None,  # replaced with SERVER_A per test
        "unread=false",
        "origin=external",
        "q=backup",
    )

    async def test_count_matches_the_unread_list_total_under_every_filter(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
        seeded_server_b: str,
    ) -> None:
        await _seed_inbox(ingest_app, session_jar)
        for query in self._FILTER_SETS:
            resolved = query if query is not None else f"server_id={SERVER_A}"
            path = NOTIFICATIONS_PATH + "/unread-count" + (
                f"?{resolved}" if resolved else ""
            )
            response = await call_app(
                ingest_app, "GET", path, cookies=session_jar
            )
            assert response.status == 200, response.body
            if "unread=false" in resolved:
                # Contradiction: no notice is both read and unread.
                expected = {"total": 0}
            else:
                # The count is UNREAD-pinned by definition: its honest
                # list twin is the same filter with unread=true.
                list_query = (
                    resolved
                    if "unread=" in resolved
                    else (f"{resolved}&unread=true" if resolved else "unread=true")
                )
                page = await _list_inbox(ingest_app, session_jar, list_query)
                expected = {"total": page["total"]}
            assert response.json() == expected, resolved

    async def test_count_drops_after_ack_and_unread_false_is_zero(
        self,
        ingest_app: FastAPI,
        session_jar: dict[str, str],
        seeded_server_a: str,
        seeded_server_b: str,
    ) -> None:
        seeded = await _seed_inbox(ingest_app, session_jar)
        acked = await _mutation(
            ingest_app,
            session_jar,
            f"{NOTIFICATIONS_PATH}/{seeded[SERVER_B][0]}/ack",
        )
        assert acked.status == 200, acked.body
        count = await call_app(
            ingest_app,
            "GET",
            f"{NOTIFICATIONS_PATH}/unread-count",
            cookies=session_jar,
        )
        assert count.json() == {"total": 2}
        # An honest 0: no unread notice matches a read-only filter.
        flipped = await call_app(
            ingest_app,
            "GET",
            f"{NOTIFICATIONS_PATH}/unread-count?unread=false",
            cookies=session_jar,
        )
        assert flipped.json() == {"total": 0}


@pytest.mark.anyio
class TestAcknowledgment:
    """Bidirectional idempotent ack on the ONE truth column."""

    async def test_ack_unack_round_trip_is_idempotent_and_preserves_rows(
        self,
        ingest_app: FastAPI,
        migrated_url: str,
        session_jar: dict[str, str],
        seeded_server_a: str,
        seeded_server_b: str,
    ) -> None:
        seeded = await _seed_inbox(ingest_app, session_jar)
        alert_id = seeded[SERVER_A][0]
        first = await _mutation(
            ingest_app,
            session_jar,
            f"{NOTIFICATIONS_PATH}/{alert_id}/ack",
        )
        assert first.status == 200, first.body
        receipt = first.json()["alert"]
        assert receipt["alert_id"] == alert_id
        assert receipt["acked"] is True
        assert receipt["acknowledged_at"] is not None
        stamped = _ack_state(migrated_url)[alert_id]
        assert stamped is not None
        # Idempotent replay: 200, never re-stamped.
        replay = await _mutation(
            ingest_app,
            session_jar,
            f"{NOTIFICATIONS_PATH}/{alert_id}/ack",
        )
        assert replay.status == 200, replay.body
        assert replay.json()["alert"]["acknowledged_at"] == receipt["acknowledged_at"]
        assert _ack_state(migrated_url)[alert_id] == stamped
        # Unacknowledge: cleared, honest view, idempotent replay.
        unack = await _mutation(
            ingest_app,
            session_jar,
            f"{NOTIFICATIONS_PATH}/{alert_id}/unack",
        )
        assert unack.status == 200, unack.body
        cleared = unack.json()["alert"]
        assert cleared["acked"] is False and cleared["acknowledged_at"] is None
        assert _ack_state(migrated_url)[alert_id] is None
        replay_clear = await _mutation(
            ingest_app,
            session_jar,
            f"{NOTIFICATIONS_PATH}/{alert_id}/unack",
        )
        assert replay_clear.status == 200, replay_clear.body
        assert replay_clear.json()["alert"]["acked"] is False
        # Re-ack re-arms/re-opens; evidence never deleted: row count stable.
        re_ack = await _mutation(
            ingest_app,
            session_jar,
            f"{NOTIFICATIONS_PATH}/{alert_id}/ack",
        )
        assert re_ack.status == 200, re_ack.body
        assert re_ack.json()["alert"]["acked"] is True
        assert len(_ack_state(migrated_url)) == 3  # nothing deleted

    async def test_unknown_alert_is_value_free_404(
        self, ingest_app: FastAPI, session_jar: dict[str, str]
    ) -> None:
        for suffix in ("ack", "unack"):
            response = await _mutation(
                ingest_app,
                session_jar,
                f"{NOTIFICATIONS_PATH}/{uuid.uuid4()}/{suffix}",
            )
            assert response.status == 404, (suffix, response.body)
            assert _error(response)["code"] == "not_found"

    async def test_mutations_reject_missing_layers_without_effect(
        self,
        ingest_app: FastAPI,
        migrated_url: str,
        session_jar: dict[str, str],
        seeded_server_a: str,
        seeded_server_b: str,
    ) -> None:
        seeded = await _seed_inbox(ingest_app, session_jar)
        alert_id = seeded[SERVER_A][0]
        path = f"{NOTIFICATIONS_PATH}/{alert_id}/ack"
        no_session = await call_app(ingest_app, "POST", path)
        assert no_session.status == 401, no_session.body
        evil_origin = await call_app(
            ingest_app,
            "POST",
            path,
            cookies=session_jar,
            headers={"origin": EVIL_ORIGIN, "x-csrf-token": session_jar[CSRF_COOKIE]},
        )
        assert evil_origin.status == 403, evil_origin.body
        assert _error(evil_origin)["code"] == "origin_rejected"
        missing_csrf = await call_app(
            ingest_app,
            "POST",
            path,
            cookies=session_jar,
            headers={"origin": ORIGIN},  # no X-CSRF-Token
        )
        assert missing_csrf.status == 403, missing_csrf.body
        assert _error(missing_csrf)["code"] == "csrf_rejected"
        assert _ack_state(migrated_url)[alert_id] is None  # never stamped


@pytest.mark.anyio
class TestMarkAllRead:
    """ONE conditional UPDATE; the receipt equals the stamped rows."""

    async def test_receipt_equals_rows_stamped_under_server_filter(
        self,
        ingest_app: FastAPI,
        migrated_url: str,
        session_jar: dict[str, str],
        seeded_server_a: str,
        seeded_server_b: str,
    ) -> None:
        seeded = await _seed_inbox(ingest_app, session_jar)
        # A second SERVER_A notice: the filter must stamp exactly the
        # matching unread rows and nothing else.
        a2_token, _ = await _mint_key(
            ingest_app, session_jar, label="host-monitor", server_id=SERVER_A
        )
        second = await _ingest(
            ingest_app,
            {"title": "Disk warning again", "dedup_key": "disk-var-2"},
            token=a2_token,
        )
        assert second.status == 200, second.body
        seeded[SERVER_A].append(str(second.json()["alert_id"]))
        # Read one A notice first: the unread filter must skip it.
        acked = await _mutation(
            ingest_app,
            session_jar,
            f"{NOTIFICATIONS_PATH}/{seeded[SERVER_A][0]}/ack",
        )
        assert acked.status == 200, acked.body
        before = _ack_state(migrated_url)
        assert before[seeded[SERVER_A][0]] is not None

        response = await _mutation(
            ingest_app,
            session_jar,
            f"{NOTIFICATIONS_PATH}/mark-all-read"
            f"?server_id={SERVER_A}&include_global=false&unread=true",
        )
        assert response.status == 200, response.body
        assert response.json() == {"updated": 1}  # only A's unread notice
        after = _ack_state(migrated_url)
        assert after[seeded[SERVER_A][1]] is not None  # stamped
        assert after[seeded[SERVER_A][0]] == before[seeded[SERVER_A][0]]
        assert after[seeded[SERVER_B][0]] is None  # untouched
        assert after[seeded["global"][0]] is None  # untouched
        # Idempotent replay: nothing left unread → receipt 0.
        replay = await _mutation(
            ingest_app,
            session_jar,
            f"{NOTIFICATIONS_PATH}/mark-all-read"
            f"?server_id={SERVER_A}&include_global=false&unread=true",
        )
        assert replay.status == 200, replay.body
        assert replay.json() == {"updated": 0}

    async def test_no_filters_stamp_every_unread_notice(
        self,
        ingest_app: FastAPI,
        migrated_url: str,
        session_jar: dict[str, str],
        seeded_server_a: str,
        seeded_server_b: str,
    ) -> None:
        await _seed_inbox(ingest_app, session_jar)
        response = await _mutation(
            ingest_app,
            session_jar,
            f"{NOTIFICATIONS_PATH}/mark-all-read",
        )
        assert response.status == 200, response.body
        assert response.json() == {"updated": 3}
        states = _ack_state(migrated_url)
        assert all(at is not None for at in states.values())
        replay = await _mutation(
            ingest_app,
            session_jar,
            f"{NOTIFICATIONS_PATH}/mark-all-read",
        )
        assert replay.json() == {"updated": 0}  # first stamp wins, always

    async def test_scope_global_stamps_only_global_rows(
        self,
        ingest_app: FastAPI,
        migrated_url: str,
        session_jar: dict[str, str],
        seeded_server_a: str,
        seeded_server_b: str,
    ) -> None:
        seeded = await _seed_inbox(ingest_app, session_jar)
        response = await _mutation(
            ingest_app,
            session_jar,
            f"{NOTIFICATIONS_PATH}/mark-all-read?scope=global",
        )
        assert response.status == 200, response.body
        assert response.json() == {"updated": 1}
        states = _ack_state(migrated_url)
        assert states[seeded["global"][0]] is not None
        assert states[seeded[SERVER_A][0]] is None
        assert states[seeded[SERVER_B][0]] is None

    async def test_mutations_reject_missing_layers_without_effect(
        self,
        ingest_app: FastAPI,
        migrated_url: str,
        session_jar: dict[str, str],
        seeded_server_a: str,
        seeded_server_b: str,
    ) -> None:
        seeded = await _seed_inbox(ingest_app, session_jar)
        path = f"{NOTIFICATIONS_PATH}/mark-all-read"
        no_session = await call_app(ingest_app, "POST", path)
        assert no_session.status == 401, no_session.body
        evil_origin = await call_app(
            ingest_app,
            "POST",
            path,
            cookies=session_jar,
            headers={"origin": EVIL_ORIGIN, "x-csrf-token": session_jar[CSRF_COOKIE]},
        )
        assert evil_origin.status == 403, evil_origin.body
        assert _error(evil_origin)["code"] == "origin_rejected"
        missing_csrf = await call_app(
            ingest_app,
            "POST",
            path,
            cookies=session_jar,
            headers={"origin": ORIGIN},
        )
        assert missing_csrf.status == 403, missing_csrf.body
        assert _error(missing_csrf)["code"] == "csrf_rejected"
        assert all(at is None for at in _ack_state(migrated_url).values())
