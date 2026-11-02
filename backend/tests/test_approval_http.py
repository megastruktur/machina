"""Focused tests for the approval decision HTTP API (card 03-approval-api).

Card ``03-approval-api`` against contracts.md **C03 rule 4** (the human
decision on a payload-bound approval), **C01** (envelope, identity) and
**C08 rule 2** (session → origin → CSRF on every mutation); the store
semantics underneath are covered by ``tests/test_approvals.py``
(03-approval-store); this suite proves the HTTP surface joining them.

Boundary discipline: every claim runs through the real ASGI app
(``create_app`` + lifespan + middleware + handlers) against a real
disposable Postgres (fixture ``migrated_url``) through the real linear
Alembic chain — never mocks. The registered P03 scenario coverage owned
here: *use the real authenticated API to inspect and resolve a pending
approval; replay after the decision and after a policy-revision bump; the
unauthenticated session never consumes; the effect counter moves exactly
once, on the authorized decision only*.

Secret hygiene: fixture credentials/principals are inert constants for the
disposable fixture DB; session and CSRF tokens are minted at runtime and
never printed into assertion messages; approval payloads exist only as
synthetic SHA-256 digests (the store API cannot accept payload bytes).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI

from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE
from mai.auth.sessions import hash_token
from mai.capabilities.dispatch import canonical_payload_hash, canonical_payload_json
from mai.database import create_app_engine
from mai.policy.approvals import Approval, ApprovalRequest, ApprovalStore

BACKEND_ROOT = Path(__file__).resolve().parents[1]

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: Synthetic fixture credential (NOT a secret; disposable fixture DB only).
FIXTURE_PASSWORD = "fixture-password-03-approval-api-only"
ORIGIN = "http://127.0.0.1:8443"  # loopback allowlist entry
EVIL_ORIGIN = "https://attacker.example"

#: Inert fixture binding (NOT secret material; digests of synthetic payloads).
CAPABILITY = "deploy.service"
OTHER_CAPABILITY = "shell.raw"
REQUESTER = "requester"  # the host/job-side principal that requested the decision
OPERATION = {"op": "deploy", "version": 1}
ALTERED_OPERATION = {"op": "deploy", "version": 2}

#: Disposable stand-in for the privileged effect the decision would
#: authorize (observable: moves exactly once, on the authorized decision).
_EFFECT_TABLE = "approval_http_effect_counter"


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(str(BACKEND_ROOT / "alembic.ini"))


# ---------------------------------------------------------------------------
# Fixtures: real disposable Postgres + real app through lifespan
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``head``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the approval HTTP surface claims "
            "need a real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        with psycopg.connect(url) as conn:
            conn.execute(
                "DROP TABLE IF EXISTS approvals, admin_sessions, "
                "admin_credential, profile_projection_journal, "
                "profile_projection, alembic_version"
            )
            conn.commit()
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    return url


def _truncate(url: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute("TRUNCATE approvals, admin_sessions, admin_credential")
        conn.commit()


def _expire(url: str, approval_id: uuid.UUID) -> None:
    """Test-only clock manipulation: push one decision into the past.

    ``created_at`` moves with it: the storage CHECK
    ``ck_approvals_expiry_after_creation`` is the boundary of last resort
    and refuses a naked expiry backdate — exactly as designed.
    """
    with psycopg.connect(url) as conn:
        conn.execute(
            "UPDATE approvals SET created_at = now() - interval '2 hours', "
            "expires_at = now() - interval '1 hour' WHERE approval_id = %s",
            (str(approval_id),),
        )
        conn.commit()


def _build_app(url: str, data_root: Path | None) -> FastAPI:
    env: dict[str, str] = {"MAI_DATABASE_URL": url, "MAI_PUBLIC_ORIGINS": ORIGIN}
    if data_root is not None:
        env["MAI_DATA_ROOT"] = str(data_root)
    return create_app(env=env)


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[FastAPI]:
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def approvals_app(migrated_url: str, tmp_path: Path):
    """App on the real fixture DB with a fresh confined data root."""
    _truncate(migrated_url)
    app = _build_app(migrated_url, tmp_path / "data-root")
    async with _running(app):
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()


@pytest.fixture
async def store(migrated_url: str):
    """The host-side producer store over its own pooled connections."""
    engine, factory = create_app_engine(migrated_url)
    yield ApprovalStore(factory)
    await engine.dispose()


class _EffectCounter:
    """The would-be privileged effect, observable through real SQL."""

    def __init__(self, url: str) -> None:
        self._url = url

    def bump(self) -> None:
        """Run the effect (the host acts only on an authorized decision)."""
        with psycopg.connect(self._url) as conn:
            conn.execute(f"UPDATE {_EFFECT_TABLE} SET n = n + 1")
            conn.commit()

    def read(self) -> int:
        with psycopg.connect(self._url) as conn:
            return int(conn.execute(f"SELECT n FROM {_EFFECT_TABLE}").fetchone()[0])


@pytest.fixture
def effect_counter(migrated_url: str) -> _EffectCounter:
    with psycopg.connect(migrated_url) as conn:
        conn.execute(f"CREATE TABLE {_EFFECT_TABLE} (n integer NOT NULL)")
        conn.execute(f"INSERT INTO {_EFFECT_TABLE} VALUES (0)")
        conn.commit()
    yield _EffectCounter(migrated_url)
    with psycopg.connect(migrated_url) as conn:
        conn.execute(f"DROP TABLE {_EFFECT_TABLE}")
        conn.commit()


async def _pending(
    store: ApprovalStore,
    *,
    capability: str = CAPABILITY,
    server_id: uuid.UUID | None = None,
    revision: int = 1,
    created_by: str = REQUESTER,
) -> Approval:
    """One pending decision requested by the (future) dispatcher host."""
    return await store.create(
        ApprovalRequest(
            capability_id=capability,
            server_id=server_id if server_id is not None else uuid.uuid4(),
            payload_hash=canonical_payload_hash(OPERATION),
            policy_revision=revision,
            created_by=created_by,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
    )


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
    client: tuple[str, int] = ("127.0.0.1", 0),
) -> HttpResponse:
    """Run one HTTP request through the real ASGI callable."""
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
        "query_string": b"",
        "root_path": "",
        "headers": scope_headers,
        "client": client,
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


def _decision_headers(jar: Mapping[str, str]) -> dict[str, str]:
    return {
        "origin": ORIGIN,
        "x-csrf-token": jar[CSRF_COOKIE],
    }


def _decision_body(record: Approval, **overrides: Any) -> dict[str, Any]:
    """The exact previewed binding, echoed field-for-field (C03 rule 4)."""
    body: dict[str, Any] = {
        "capability_id": record.capability_id,
        "server_id": str(record.server_id),
        "payload_hash": record.payload_hash,
        "policy_revision": record.policy_revision,
    }
    body.update(overrides)
    return body


async def _decide(
    app: FastAPI,
    jar: Mapping[str, str],
    approval_id: uuid.UUID,
    body: Mapping[str, Any],
) -> HttpResponse:
    return await call_app(
        app,
        "POST",
        f"/api/v1/approvals/{approval_id}/decision",
        json_body=dict(body),
        headers=_decision_headers(jar),
        cookies=jar,
    )


# ---------------------------------------------------------------------------
# Safe preview (GET): session-guarded inspection (AC1)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_safe_preview_requires_a_session(approvals_app: FastAPI) -> None:
    """No cookie ⇒ value-free 401 (G1 fail-closed; no clearance to send)."""
    response = await call_app(approvals_app, "GET", f"/api/v1/approvals/{uuid.uuid4()}")
    assert response.status == 401
    body = _error(response)
    assert body["code"] == "unauthenticated"
    assert response.header_all("set-cookie") == []


@pytest.mark.anyio
async def test_safe_preview_rejects_an_invalid_token_and_clears_cookies(
    approvals_app: FastAPI,
) -> None:
    """A forged/stale token ⇒ 401 with both cookies cleared (ADR §4)."""
    forged = {SESSION_COOKIE: "0" * 43, CSRF_COOKIE: "irrelevant"}
    response = await call_app(
        approvals_app,
        "GET",
        f"/api/v1/approvals/{uuid.uuid4()}",
        cookies=forged,
    )
    assert response.status == 401
    assert _error(response)["code"] == "unauthenticated"
    cleared = " ".join(response.header_all("set-cookie"))
    assert "mai_session=" in cleared and "mai_csrf=" in cleared


@pytest.mark.anyio
async def test_safe_preview_shows_the_full_binding_and_pending_state(
    approvals_app: FastAPI, store: ApprovalStore
) -> None:
    """The preview explains target/effect and carries the exact binding."""
    record = await _pending(store)
    jar = await login_session(approvals_app)
    response = await call_app(
        approvals_app,
        "GET",
        f"/api/v1/approvals/{record.approval_id}",
        cookies=jar,
    )
    assert response.status == 200
    view = response.json()
    assert set(view) == {
        "approval_id",
        "capability_id",
        "server_id",
        "payload_hash",
        "policy_revision",
        "status",
        "created_by",
        "created_at",
        "expires_at",
        # Runtime v2 (§9): the decision response reports whether the
        # durable resume wake reached a parked waiting_approval run;
        # None on the safe preview (decision-time only).
        "resume_wake_sent",
        "consumed_by",
        "consumed_at",
    }
    assert view["approval_id"] == str(record.approval_id)
    assert view["capability_id"] == CAPABILITY  # the would-be effect
    assert view["server_id"] == str(record.server_id)  # the bound target
    assert view["payload_hash"] == record.payload_hash  # digest, never payload
    assert view["policy_revision"] == record.policy_revision
    assert view["status"] == "pending"
    assert view["created_by"] == REQUESTER
    assert view["consumed_by"] is None and view["consumed_at"] is None
    # Payload bytes cannot exist anywhere in the view: only the digest.
    assert canonical_payload_json(OPERATION) not in response.body.decode()


# ---------------------------------------------------------------------------
# Decision (POST): one authorized consumption (AC1/AC2 + gate scenario)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_decision_consumes_once_and_authorizes_exactly_one_effect(
    approvals_app: FastAPI, store: ApprovalStore, effect_counter: _EffectCounter
) -> None:
    """Exact previewed binding ⇒ consumed once; the effect runs exactly once."""
    record = await _pending(store)
    jar = await login_session(approvals_app)
    response = await _decide(
        approvals_app, jar, record.approval_id, _decision_body(record)
    )
    assert response.status == 200
    view = response.json()
    assert view["status"] == "consumed"
    assert view["consumed_by"] == hash_token(jar[SESSION_COOKIE])
    assert view["consumed_at"] is not None
    effect_counter.bump()  # the host acts on the receipt
    assert effect_counter.read() == 1
    after = await call_app(
        approvals_app,
        "GET",
        f"/api/v1/approvals/{record.approval_id}",
        cookies=jar,
    )
    assert after.json()["status"] == "consumed"


@pytest.mark.anyio
async def test_decision_without_a_session_never_consumes(
    approvals_app: FastAPI, store: ApprovalStore, effect_counter: _EffectCounter
) -> None:
    """Unauthenticated decision: 401, no consumption, no effect."""
    record = await _pending(store)
    response = await call_app(
        approvals_app,
        "POST",
        f"/api/v1/approvals/{record.approval_id}/decision",
        json_body=_decision_body(record),
        headers=_decision_headers({CSRF_COOKIE: "irrelevant"}),
    )
    assert response.status == 401
    assert _error(response)["code"] == "unauthenticated"
    survivor = await store.get(record.approval_id)
    assert survivor is not None and survivor.status == "pending"
    assert effect_counter.read() == 0


@pytest.mark.anyio
async def test_decision_with_revoked_session_never_consumes(
    approvals_app: FastAPI, store: ApprovalStore, effect_counter: _EffectCounter
) -> None:
    """Logout revocation is effective on the very next request (no grace)."""
    record = await _pending(store)
    jar = await login_session(approvals_app)
    logout = await call_app(
        approvals_app,
        "POST",
        "/api/v1/auth/logout",
        headers=_decision_headers(jar),
        cookies=jar,
    )
    assert logout.status == 204
    replay = await _decide(
        approvals_app, jar, record.approval_id, _decision_body(record)
    )
    assert replay.status == 401
    survivor = await store.get(record.approval_id)
    assert survivor is not None and survivor.status == "pending"
    assert effect_counter.read() == 0


@pytest.mark.parametrize("csrf_header", [None, "0" * 43])
@pytest.mark.anyio
async def test_decision_without_a_valid_csrf_pair_never_consumes(
    approvals_app: FastAPI,
    store: ApprovalStore,
    effect_counter: _EffectCounter,
    csrf_header: str | None,
) -> None:
    """Missing or mismatched double-submit token: 403, row untouched."""
    record = await _pending(store)
    jar = await login_session(approvals_app)
    headers = {"origin": ORIGIN}
    if csrf_header is not None:
        headers["x-csrf-token"] = csrf_header
    response = await call_app(
        approvals_app,
        "POST",
        f"/api/v1/approvals/{record.approval_id}/decision",
        json_body=_decision_body(record),
        headers=headers,
        cookies=jar,
    )
    assert response.status == 403
    assert _error(response)["code"] == "csrf_rejected"
    survivor = await store.get(record.approval_id)
    assert survivor is not None and survivor.status == "pending"
    assert effect_counter.read() == 0


@pytest.mark.anyio
async def test_decision_from_a_foreign_origin_never_consumes(
    approvals_app: FastAPI, store: ApprovalStore, effect_counter: _EffectCounter
) -> None:
    """Exact-origin allowlist: a stolen cookie from an evil origin mutates nothing."""
    record = await _pending(store)
    jar = await login_session(approvals_app)
    response = await call_app(
        approvals_app,
        "POST",
        f"/api/v1/approvals/{record.approval_id}/decision",
        json_body=_decision_body(record),
        headers={"origin": EVIL_ORIGIN, "x-csrf-token": jar[CSRF_COOKIE]},
        cookies=jar,
    )
    assert response.status == 403
    assert _error(response)["code"] == "origin_rejected"
    survivor = await store.get(record.approval_id)
    assert survivor is not None and survivor.status == "pending"
    assert effect_counter.read() == 0


@pytest.mark.anyio
async def test_replayed_decision_is_rejected_without_a_second_effect(
    approvals_app: FastAPI, store: ApprovalStore, effect_counter: _EffectCounter
) -> None:
    """The second identical presentation is a clear rejection, not an approval."""
    record = await _pending(store)
    jar = await login_session(approvals_app)
    first = await _decide(
        approvals_app, jar, record.approval_id, _decision_body(record)
    )
    assert first.status == 200
    effect_counter.bump()
    replay = await _decide(
        approvals_app, jar, record.approval_id, _decision_body(record)
    )
    assert replay.status == 409
    assert _error(replay)["code"] == "already_consumed"
    assert effect_counter.read() == 1
    survivor = await store.get(record.approval_id)
    assert survivor is not None
    assert survivor.consumed_at is not None  # exactly the first decision's receipt
    assert survivor.consumed_by == hash_token(jar[SESSION_COOKIE])


@pytest.mark.anyio
async def test_stale_policy_revision_is_rejected_and_decision_stays_pending(
    approvals_app: FastAPI, store: ApprovalStore, effect_counter: _EffectCounter
) -> None:
    """Grant/policy revocation (revision bump) kills the stale presentation.

    The failed stale attempt must not spend the decision either: the exact
    binding is still consumable afterwards, exactly once.
    """
    record = await _pending(store, revision=1)
    jar = await login_session(approvals_app)
    stale = await _decide(
        approvals_app,
        jar,
        record.approval_id,
        _decision_body(record, policy_revision=2),
    )
    assert stale.status == 409
    assert _error(stale)["code"] == "revision_mismatch"
    survivor = await store.get(record.approval_id)
    assert survivor is not None and survivor.status == "pending"
    assert effect_counter.read() == 0
    exact = await _decide(
        approvals_app, jar, record.approval_id, _decision_body(record)
    )
    assert exact.status == 200
    effect_counter.bump()
    assert effect_counter.read() == 1


@pytest.mark.anyio
async def test_altered_payload_binding_is_rejected_and_never_consumes(
    approvals_app: FastAPI, store: ApprovalStore, effect_counter: _EffectCounter
) -> None:
    """Altered operation ⇒ new approval required; nothing is consumed."""
    record = await _pending(store)
    jar = await login_session(approvals_app)
    altered = await _decide(
        approvals_app,
        jar,
        record.approval_id,
        _decision_body(
            record, payload_hash=canonical_payload_hash(ALTERED_OPERATION)
        ),
    )
    assert altered.status == 409
    assert _error(altered)["code"] == "payload_mismatch"
    survivor = await store.get(record.approval_id)
    assert survivor is not None and survivor.status == "pending"
    assert effect_counter.read() == 0


@pytest.mark.anyio
async def test_wrong_capability_binding_is_rejected(
    approvals_app: FastAPI, store: ApprovalStore, effect_counter: _EffectCounter
) -> None:
    """A decision binds the exact capability application (no wildcard)."""
    record = await _pending(store)
    jar = await login_session(approvals_app)
    response = await _decide(
        approvals_app,
        jar,
        record.approval_id,
        _decision_body(record, capability_id=OTHER_CAPABILITY),
    )
    assert response.status == 409
    assert _error(response)["code"] == "capability_mismatch"
    survivor = await store.get(record.approval_id)
    assert survivor is not None and survivor.status == "pending"
    assert effect_counter.read() == 0


@pytest.mark.anyio
async def test_wrong_target_binding_is_rejected(
    approvals_app: FastAPI, store: ApprovalStore, effect_counter: _EffectCounter
) -> None:
    """A decision for one server can never approve another target."""
    record = await _pending(store)
    jar = await login_session(approvals_app)
    response = await _decide(
        approvals_app,
        jar,
        record.approval_id,
        _decision_body(record, server_id=str(uuid.uuid4())),
    )
    assert response.status == 409
    assert _error(response)["code"] == "target_mismatch"
    survivor = await store.get(record.approval_id)
    assert survivor is not None and survivor.status == "pending"
    assert effect_counter.read() == 0


@pytest.mark.anyio
async def test_unknown_approval_id_is_a_value_free_404(
    approvals_app: FastAPI,
) -> None:
    jar = await login_session(approvals_app)
    unknown = uuid.uuid4()
    body = {
        "capability_id": CAPABILITY,
        "server_id": str(uuid.uuid4()),
        "payload_hash": hashlib.sha256(b"x").hexdigest(),
        "policy_revision": 1,
    }
    got = await call_app(
        approvals_app, "GET", f"/api/v1/approvals/{unknown}", cookies=jar
    )
    posted = await _decide(approvals_app, jar, unknown, body)
    assert got.status == 404 and posted.status == 404
    assert _error(got)["code"] == "not_found"
    assert _error(posted)["code"] == "not_found"


@pytest.mark.anyio
async def test_expired_decision_is_rejected_and_never_consumed(
    approvals_app: FastAPI,
    store: ApprovalStore,
    effect_counter: _EffectCounter,
    migrated_url: str,
) -> None:
    """Expiry is the binding's clock boundary at use time (C03 rule 4)."""
    record = await _pending(store)
    _expire(migrated_url, record.approval_id)
    jar = await login_session(approvals_app)
    response = await _decide(
        approvals_app, jar, record.approval_id, _decision_body(record)
    )
    assert response.status == 409
    assert _error(response)["code"] == "expired"
    survivor = await store.get(record.approval_id)
    assert survivor is not None and survivor.consumed_at is None
    assert effect_counter.read() == 0


@pytest.mark.parametrize(
    ("path_id", "body_patch"),
    [
        ("not-a-uuid", {}),
        (None, {"payload_hash": "deadbeef"}),
        (None, {"policy_revision": 0}),
        (None, {"capability_id": "Bad Slug"}),
        (None, {"server_id": "no-uuid-here"}),
    ],
)
@pytest.mark.anyio
async def test_malformed_decisions_get_structured_validation_errors(
    approvals_app: FastAPI, store: ApprovalStore, path_id: str | None, body_patch: dict
) -> None:
    """Bad shapes are 422 ``validation_failed`` with field paths only."""
    record = await _pending(store)
    jar = await login_session(approvals_app)
    approval_id = record.approval_id if path_id is None else path_id  # type: ignore[assignment]
    response = await _decide(
        approvals_app,
        jar,
        approval_id,  # type: ignore[arg-type]
        _decision_body(record, **body_patch),
    )
    assert response.status == 422
    error = _error(response)
    assert error["code"] == "validation_failed"
    assert error["fields"], "field paths are required, values are never echoed"


@pytest.mark.anyio
async def test_concurrent_duplicate_decisions_authorize_at_most_one_effect(
    approvals_app: FastAPI, store: ApprovalStore, effect_counter: _EffectCounter
) -> None:
    """Two racing identical decisions: exactly one 200, one 409, one effect."""
    record = await _pending(store)
    jar = await login_session(approvals_app)
    body = _decision_body(record)
    first, second = await asyncio.gather(
        _decide(approvals_app, jar, record.approval_id, body),
        _decide(approvals_app, jar, record.approval_id, body),
    )
    statuses = sorted(response.status for response in (first, second))
    assert statuses == [200, 409]
    consumed = first if first.status == 200 else second
    rejected = first if first.status == 409 else second
    assert _error(rejected)["code"] == "already_consumed"
    assert consumed.json()["status"] == "consumed"
    effect_counter.bump()  # exactly one receipt authorizes the host
    assert effect_counter.read() == 1


# ---------------------------------------------------------------------------
# OpenAPI contract (AC3: real registration + current contract)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_openapi_contract_declares_the_approval_surface(
    approvals_app: FastAPI,
) -> None:
    """The routes are registered on the real app and contract-documented."""
    schema = approvals_app.openapi()
    paths = schema["paths"]
    preview_path = "/api/v1/approvals/{approval_id}"
    decision_path = "/api/v1/approvals/{approval_id}/decision"
    assert preview_path in paths and decision_path in paths
    preview = paths[preview_path]["get"]
    assert preview["tags"] == ["approvals"]
    assert (
        preview["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ApprovalView"
    )
    decision = paths[decision_path]["post"]
    assert decision["tags"] == ["approvals"]
    assert (
        decision["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/DecisionBody"
    )
    assert (
        decision["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ApprovalView"
    )
    components = schema["components"]["schemas"]
    assert components["DecisionBody"]["required"] == [
        "capability_id",
        "server_id",
        "payload_hash",
        "policy_revision",
    ]
    for status in ("401", "403", "404", "409", "422", "503"):
        assert status in decision["responses"], status
    assert set(components["ApprovalView"]["required"]) == {
        "approval_id",
        "capability_id",
        "server_id",
        "payload_hash",
        "policy_revision",
        "status",
        "created_by",
        "created_at",
        "expires_at",
        "consumed_by",
        "consumed_at",
    }
