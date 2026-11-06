"""Focused tests for the capability-grant configuration HTTP API (card 03-grants-api).

Card ``03-grants-api`` against contracts.md **C03 rules 3–5** (grant
semantics, revision-bound approvals, ask/delegated modes), **C01**
(envelope, identity, revision conflicts), **C02** (canonical service,
version triple, frozen document vocabulary) and **C08 rule 2** (session →
origin → CSRF on every mutation); the storage semantics underneath are
covered by ``tests/test_policy_config.py`` (03-policy-config) and the
decision/dispatch semantics by ``tests/test_policy.py`` /
``tests/test_dispatch.py`` (03-policy / 03-dispatcher); this suite proves
the HTTP surface joining them.

Boundary discipline: every claim runs through the real ASGI app
(``create_app`` + lifespan + middleware + handlers) against a real
disposable Postgres (fixture ``migrated_url``) through the real linear
Alembic chain, and a real confined data root on the local filesystem —
never mocks. The dispatcher-level claims compose the real production
boundary (:class:`~mai.capabilities.dispatch.CapabilityDispatcher` over
the same committed canonical file), so "no unauthorized target action" is
observed at the executor seam, not asserted.

Registered P03 scenario coverage owned here (writer half; assembly is
``03-verify``): *actual HTTP add/revoke fixture grant, stale revision and
wrong server edit tests; restart and verify effective policy plus no
unauthorized target action.*

Secret hygiene: fixture credentials/principals are inert constants for
the disposable fixture DB; session and CSRF tokens are minted at runtime
and never printed into assertion messages; grants carry opaque server
UUIDs and slug-shaped capability ids only.
"""

from __future__ import annotations

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
from mai.capabilities.dispatch import CapabilityCall, CapabilityDispatcher
from mai.credentials.env import EnvResolver
from mai.database import create_app_engine
from mai.plugins.registry import CapabilityRegistry, parse_descriptor
from mai.policy.approvals import ApprovalRequest, ApprovalStore
from mai.policy.config import GrantConfigError, GrantConfigErrorCode, GrantConfigStore
from mai.policy.evaluate import ExecutionContext, PolicyMode

BACKEND_ROOT = Path(__file__).resolve().parents[1]

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: Synthetic fixture credential (NOT a secret; disposable fixture DB only).
FIXTURE_PASSWORD = "fixture-password-03-grants-api-only"
ORIGIN = "http://127.0.0.1:8443"  # loopback allowlist entry
EVIL_ORIGIN = "https://attacker.example"

#: Fixture capability slugs (C03 rule 1 shape) and effect classes.
CAPABILITY = "deploy.service"  # mutate: approval only in ask mode
PRIVILEGED_CAPABILITY = "shell.raw"  # privileged: approval in every mode
UNGRANTED_CAPABILITY = "logs.collect"  # never in the grant set
DENIED_CAPABILITY = "network.probe"  # granted, then deny-listed

#: Opaque server identities (C01 rule 1) — never display names.
SERVER_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
SERVER_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
RUN_1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
PRINCIPAL = "admin"

#: Inert fixture operation payload (digest-bound into approvals).
PAYLOAD = {"op": "deploy", "v": 1}


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
            f"{LIVE_URL_VAR} is not set; the grants HTTP surface claims "
            "need a real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        with psycopg.connect(url) as conn:
            # Whole-schema reset (D09): FK-safe and immune to future revisions
            # (the enumerated drop list went stale across migrations).
            conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
            conn.execute("CREATE SCHEMA public")
            conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
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
async def grants_app(migrated_url: str, tmp_path: Path):
    """App on the real fixture DB with a fresh confined data root."""
    _truncate(migrated_url)
    app = _build_app(migrated_url, tmp_path / "data-root")
    async with _running(app):
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()


@pytest.fixture
def config_store(tmp_path: Path) -> GrantConfigStore:
    """The host-side view over the same committed data root.

    Every provider read re-resolves the files — the exact composition a
    dispatcher host uses, so HTTP commits are visible to it immediately.
    """
    return GrantConfigStore(tmp_path / "data-root")


@pytest.fixture
async def approvals(migrated_url: str):
    """The host-side approval store over its own pooled connections."""
    engine, factory = create_app_engine(migrated_url)
    yield ApprovalStore(factory)
    await engine.dispose()


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


async def _login(app: FastAPI) -> dict[str, str]:
    """Login against an already-bootstrapped credential; return the jar."""
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
    return await _login(app)


def _mutation_headers(jar: Mapping[str, str]) -> dict[str, str]:
    return {
        "origin": ORIGIN,
        "x-csrf-token": jar[CSRF_COOKIE],
    }


# ---------------------------------------------------------------------------
# Grants-specific request helpers
# ---------------------------------------------------------------------------


def _entry(capability: str, *servers: uuid.UUID) -> dict[str, Any]:
    return {
        "capability_id": capability,
        "server_ids": [str(server) for server in servers],
    }


def _edit_body(
    expected_revision: int,
    *,
    grants: list[dict[str, Any]] | None = None,
    denied: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "expected_revision": expected_revision,
        "denied_capabilities": list(denied or ()),
        "grants": list(grants or ()),
    }
    body.update(extra)
    return body


async def _get_grants(app: FastAPI, jar: Mapping[str, str]) -> HttpResponse:
    return await call_app(app, "GET", "/api/v1/grants", cookies=jar)


async def _put_grants(
    app: FastAPI, jar: Mapping[str, str], body: Mapping[str, Any]
) -> HttpResponse:
    return await call_app(
        app,
        "PUT",
        "/api/v1/grants",
        json_body=dict(body),
        headers=_mutation_headers(jar),
        cookies=jar,
    )


def _assert_no_configuration(config_store: GrantConfigStore) -> None:
    """The canonical file was never created (negative-control observable)."""
    with pytest.raises(GrantConfigError) as raised:
        config_store.read()
    assert raised.value.code is GrantConfigErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------
# Dispatcher composition (the production consumer of committed grants)
# ---------------------------------------------------------------------------


def _descriptor_document(identifier: str, effect_class: str) -> dict[str, object]:
    return {
        "identifier": identifier,
        "version": "1.0.0",
        "input_schema": {},
        "output_schema": {},
        "execution_environments": ["controller"],
        "effect_class": effect_class,
        "resource_scope": ["deploy"] if effect_class != "read" else [],
        "required_grants": [],
    }


def _registry_with(*documents: dict[str, object]) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    for document in documents:
        registry.register(parse_descriptor(document))
    return registry


def _context(
    *,
    server_id: uuid.UUID = SERVER_A,
    allowed: tuple[str, ...] = (CAPABILITY,),
    mode: PolicyMode = PolicyMode.DELEGATED,
) -> ExecutionContext:
    return ExecutionContext(
        principal=PRINCIPAL,
        server_id=server_id,
        run_id=RUN_1,
        parent_scope=None,
        profile_revision="prof-1",
        prompt_revision="prompt-1",
        credential_references=(),
        allowed_capabilities=allowed,
        mode=mode,
    )


def _recording_executor() -> tuple[Any, list[CapabilityCall]]:
    """The would-be privileged effect: invocation IS the observable."""
    calls: list[CapabilityCall] = []

    async def _execute(call: CapabilityCall) -> Any:
        calls.append(call)
        from mai.capabilities.dispatch import ExecutorResult

        return ExecutorResult(ok=True, output="done")

    return _execute, calls


def _refusing_executor() -> Any:
    """Fails the test when invoked: deny outcomes must never reach it."""
    from mai.capabilities.dispatch import ExecutorResult

    async def _execute(call: CapabilityCall) -> ExecutorResult:
        raise AssertionError(f"executor invoked for a denied call: {call}")

    return _execute


def _dispatcher(
    config_store: GrantConfigStore,
    approvals: ApprovalStore,
    *,
    registry: CapabilityRegistry,
    executor: Any,
    grants: Any = None,
    policy: Any = None,
) -> CapabilityDispatcher:
    """The host composition: providers re-read the committed canonical
    file per call (gate and immediate recheck), never a cached copy."""
    return CapabilityDispatcher(
        registry=registry,
        approvals=approvals,
        grants=grants if grants is not None else lambda: config_store.read().grants,
        policy=policy if policy is not None else lambda: config_store.read().policy,
        adopted=lambda server_id: True,  # enrollment is the profile domain
        credentials=lambda: EnvResolver(config_store.root / "unused.env"),
        executor=executor,
    )


async def _create_approval(
    approvals: ApprovalStore,
    *,
    capability: str,
    server_id: uuid.UUID,
    policy_revision: int,
) -> Any:
    from mai.capabilities.dispatch import canonical_payload_hash as _hash

    return await approvals.create(
        ApprovalRequest(
            capability_id=capability,
            server_id=server_id,
            payload_hash=_hash(PAYLOAD),
            policy_revision=policy_revision,
            created_by="requester",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
    )


# ---------------------------------------------------------------------------
# Boundary: session → origin → CSRF on every mutation (C08 rule 2, AC1)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_without_a_session_is_unauthenticated(
    grants_app: FastAPI,
) -> None:
    """No cookie ⇒ value-free 401 (G1 fail-closed; no clearance to send)."""
    response = await _get_grants(grants_app, {})
    assert response.status == 401
    assert _error(response)["code"] == "unauthenticated"
    assert response.header_all("set-cookie") == []


@pytest.mark.anyio
async def test_get_with_a_forged_token_clears_both_cookies(
    grants_app: FastAPI,
) -> None:
    """A forged/stale token ⇒ 401 with both cookies cleared (ADR §4)."""
    forged = {SESSION_COOKIE: "0" * 43, CSRF_COOKIE: "irrelevant"}
    response = await _get_grants(grants_app, forged)
    assert response.status == 401
    assert _error(response)["code"] == "unauthenticated"
    cleared = " ".join(response.header_all("set-cookie"))
    assert "mai_session=" in cleared and "mai_csrf=" in cleared


@pytest.mark.anyio
async def test_put_without_a_session_never_touches_the_canonical_file(
    grants_app: FastAPI, config_store: GrantConfigStore
) -> None:
    """An unauthenticated edit is rejected before any state is read or
    written: the data root gains no policy namespace at all."""
    response = await call_app(
        grants_app,
        "PUT",
        "/api/v1/grants",
        json_body=_edit_body(0, grants=[_entry(CAPABILITY, SERVER_A)]),
    )
    assert response.status == 401
    assert _error(response)["code"] == "unauthenticated"
    _assert_no_configuration(config_store)


@pytest.mark.anyio
async def test_put_rejects_a_foreign_origin(
    grants_app: FastAPI, config_store: GrantConfigStore
) -> None:
    """Layer 2: a valid session from an attacker origin mutates nothing."""
    jar = await login_session(grants_app)
    response = await call_app(
        grants_app,
        "PUT",
        "/api/v1/grants",
        json_body=_edit_body(0, grants=[_entry(CAPABILITY, SERVER_A)]),
        headers={"origin": EVIL_ORIGIN, "x-csrf-token": jar[CSRF_COOKIE]},
        cookies=jar,
    )
    assert response.status == 403
    assert _error(response)["code"] == "origin_rejected"
    _assert_no_configuration(config_store)


@pytest.mark.anyio
async def test_put_rejects_missing_and_mismatched_csrf(
    grants_app: FastAPI, config_store: GrantConfigStore
) -> None:
    """Layer 3: double-submit failure is a 403 that writes nothing."""
    jar = await login_session(grants_app)
    body = _edit_body(0, grants=[_entry(CAPABILITY, SERVER_A)])
    missing = await call_app(
        grants_app,
        "PUT",
        "/api/v1/grants",
        json_body=body,
        headers={"origin": ORIGIN},
        cookies=jar,
    )
    mismatched = await call_app(
        grants_app,
        "PUT",
        "/api/v1/grants",
        json_body=body,
        headers={"origin": ORIGIN, "x-csrf-token": "not-the-cookie"},
        cookies=jar,
    )
    assert missing.status == 403 and mismatched.status == 403
    assert _error(missing)["code"] == "csrf_rejected"
    assert _error(mismatched)["code"] == "csrf_rejected"
    _assert_no_configuration(config_store)


@pytest.mark.anyio
async def test_put_with_a_revoked_session_is_unauthenticated(
    grants_app: FastAPI, config_store: GrantConfigStore
) -> None:
    """Session revocation is effective on the next request (ADR §4)."""
    jar = await login_session(grants_app)
    logout = await call_app(
        grants_app,
        "POST",
        "/api/v1/auth/logout",
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert logout.status == 204
    response = await _put_grants(
        grants_app, jar, _edit_body(0, grants=[_entry(CAPABILITY, SERVER_A)])
    )
    assert response.status == 401
    assert _error(response)["code"] == "unauthenticated"
    _assert_no_configuration(config_store)


@pytest.mark.anyio
async def test_routes_fail_closed_without_the_configured_store(
    migrated_url: str, tmp_path: Path
) -> None:
    """A missing lifespan-built store answers 503 and touches nothing."""
    _truncate(migrated_url)
    app = _build_app(migrated_url, tmp_path / "data-root")
    async with _running(app):
        app.state.grants = None  # the fail-closed wiring path
        jar = await login_session(app)
        got = await _get_grants(app, jar)
        put = await _put_grants(app, jar, _edit_body(0))
        assert got.status == 503 and put.status == 503
        assert _error(got)["code"] == "unavailable"
        assert _error(put)["code"] == "unavailable"
    await app.state.engine.dispose()
    assert not (tmp_path / "data-root" / "policy" / "config.json").exists()


# ---------------------------------------------------------------------------
# Versioned reads and edits (AC1; scenario: add/revoke/stale)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_before_the_first_commit_is_not_found(
    grants_app: FastAPI,
) -> None:
    """Before the first commit the truth is ``not_found`` — never a faked
    empty default (C08 rule 6: empty/unknown shown, not all-green)."""
    jar = await login_session(grants_app)
    response = await _get_grants(grants_app, jar)
    assert response.status == 404
    assert _error(response)["code"] == "not_found"


@pytest.mark.anyio
async def test_baseline_commit_adds_the_fixture_grant(
    grants_app: FastAPI, config_store: GrantConfigStore
) -> None:
    """The actual add: revision 0 → 1 through the canonical service."""
    jar = await login_session(grants_app)
    put = await _put_grants(
        grants_app,
        jar,
        _edit_body(0, grants=[_entry(CAPABILITY, SERVER_A, SERVER_B)]),
    )
    assert put.status == 200, put.body
    committed = put.json()
    assert committed["revision"] == 1
    assert committed["schema_revision"] == 1
    assert len(committed["sha256"]) == 64
    got = await _get_grants(grants_app, jar)
    assert got.status == 200
    view = got.json()
    assert view["revision"] == 1 and view["sha256"] == committed["sha256"]
    assert view["policy"] == {"revision": 1, "denied_capabilities": []}
    assert view["grants"] == [
        {"capability_id": CAPABILITY, "server_ids": sorted([str(SERVER_A), str(SERVER_B)])}
    ]
    # The canonical file really exists below the configured data root.
    stored = config_store.read()
    assert stored.revision == 1
    assert stored.grants == {CAPABILITY: {SERVER_A, SERVER_B}}


@pytest.mark.anyio
async def test_edit_adds_a_second_grant_with_a_new_revision(
    grants_app: FastAPI, config_store: GrantConfigStore
) -> None:
    """Versioned edit: the caller's revision gates the write (C01 rule 6)."""
    jar = await login_session(grants_app)
    first = await _put_grants(
        grants_app, jar, _edit_body(0, grants=[_entry(CAPABILITY, SERVER_A)])
    )
    assert first.status == 200 and first.json()["revision"] == 1
    second = await _put_grants(
        grants_app,
        jar,
        _edit_body(
            1, grants=[_entry(CAPABILITY, SERVER_A), _entry(UNGRANTED_CAPABILITY, SERVER_B)]
        ),
    )
    assert second.status == 200, second.body
    assert second.json()["revision"] == 2
    view = (await _get_grants(grants_app, jar)).json()
    assert {g["capability_id"] for g in view["grants"]} == {
        CAPABILITY,
        UNGRANTED_CAPABILITY,
    }
    stored = config_store.read()
    assert stored.revision == 2
    assert stored.grants[UNGRANTED_CAPABILITY] == {SERVER_B}


@pytest.mark.anyio
async def test_revoke_removes_the_grant_and_revisions(
    grants_app: FastAPI, config_store: GrantConfigStore
) -> None:
    """The actual revoke: the committed grant set no longer names the
    capability — the only revocation channel is a new revision."""
    jar = await login_session(grants_app)
    assert (await _put_grants(
        grants_app, jar, _edit_body(0, grants=[_entry(CAPABILITY, SERVER_A)])
    )).status == 200
    revoked = await _put_grants(grants_app, jar, _edit_body(1, grants=[]))
    assert revoked.status == 200, revoked.body
    assert revoked.json()["revision"] == 2
    view = (await _get_grants(grants_app, jar)).json()
    assert view["grants"] == []
    assert view["policy"]["revision"] == 2
    stored = config_store.read()
    assert stored.revision == 2 and stored.grants == {}


@pytest.mark.anyio
async def test_stale_revision_is_conflicted_and_leaves_the_store_unchanged(
    grants_app: FastAPI, config_store: GrantConfigStore
) -> None:
    """A superseded caller gets ``409 conflict`` before any filesystem
    effect; the committed configuration is untouched."""
    jar = await login_session(grants_app)
    assert (await _put_grants(
        grants_app, jar, _edit_body(0, grants=[_entry(CAPABILITY, SERVER_A)])
    )).status == 200
    assert (await _put_grants(
        grants_app, jar, _edit_body(1, grants=[_entry(CAPABILITY, SERVER_A)])
    )).status == 200
    stale = await _put_grants(
        grants_app,
        jar,
        _edit_body(1, grants=[_entry(UNGRANTED_CAPABILITY, SERVER_B)]),
    )
    assert stale.status == 409
    assert _error(stale)["code"] == "conflict"
    view = (await _get_grants(grants_app, jar)).json()
    assert view["revision"] == 2
    assert [g["capability_id"] for g in view["grants"]] == [CAPABILITY]
    stored = config_store.read()
    assert stored.revision == 2
    assert set(stored.grants) == {CAPABILITY}


@pytest.mark.anyio
async def test_duplicate_grant_entries_fail_producer_validation(
    grants_app: FastAPI, config_store: GrantConfigStore
) -> None:
    """A schema violation the transport cannot express (duplicate grant)
    is rejected by the producer with its stable code in ``fields``."""
    jar = await login_session(grants_app)
    response = await _put_grants(
        grants_app,
        jar,
        _edit_body(
            0,
            grants=[
                _entry(CAPABILITY, SERVER_A),
                _entry(CAPABILITY, SERVER_B),
            ],
        ),
    )
    assert response.status == 422
    body = _error(response)
    assert body["code"] == "validation_failed"
    assert body["fields"] == {"document": "grants_invalid"}
    _assert_no_configuration(config_store)


@pytest.mark.anyio
async def test_transport_rejects_malformed_slugs_and_server_ids(
    grants_app: FastAPI, config_store: GrantConfigStore
) -> None:
    """Slug charset and UUID typing die at 422 with field paths, before
    the canonical service is reached."""
    jar = await login_session(grants_app)
    bad_slug = await _put_grants(
        grants_app,
        jar,
        _edit_body(0, grants=[_entry("Deploy.Service", SERVER_A)]),
    )
    assert bad_slug.status == 422
    assert _error(bad_slug)["code"] == "validation_failed"
    assert "body.grants.0.capability_id" in _error(bad_slug)["fields"]
    bad_uuid = await _put_grants(
        grants_app,
        jar,
        _edit_body(0, grants=[{"capability_id": CAPABILITY, "server_ids": ["server-a"]}]),
    )
    assert bad_uuid.status == 422
    assert "body.grants.0.server_ids.0" in _error(bad_uuid)["fields"]
    bad_revision = await _put_grants(grants_app, jar, _edit_body(-1))
    assert bad_revision.status == 422
    _assert_no_configuration(config_store)


@pytest.mark.anyio
async def test_unknown_body_fields_never_reach_the_canonical_document(
    grants_app: FastAPI, config_store: GrantConfigStore
) -> None:
    """The caller can never nominate a revision or smuggle prose/mode
    annotations: the route derives the revision and the canonical
    vocabulary is exact (C02 rule 6, C03 rule 4)."""
    jar = await login_session(grants_app)
    smuggled = await _put_grants(
        grants_app,
        jar,
        _edit_body(
            0,
            grants=[_entry(CAPABILITY, SERVER_A)],
            policy={"revision": 99, "denied_capabilities": ["injected.capability"]},
            mode="delegated",
        ),
    )
    assert smuggled.status == 200, smuggled.body
    view = (await _get_grants(grants_app, jar)).json()
    assert set(view) == {"revision", "schema_revision", "sha256", "policy", "grants"}
    assert view["revision"] == 1 and view["policy"]["revision"] == 1
    assert view["policy"]["denied_capabilities"] == []
    assert "mode" not in json.dumps(view)
    document = json.loads(
        (config_store.root / "policy" / "config.json").read_text(encoding="utf-8")
    )
    assert set(document) == {"policy", "grants"}
    assert document["policy"]["revision"] == 1


@pytest.mark.anyio
async def test_openapi_contract_declares_the_grants_surface(
    grants_app: FastAPI,
) -> None:
    """The current contract: both routes under the frozen prefix, session
    plus mutation layers documented, no public schema listener (G1)."""
    schema = grants_app.openapi()
    paths = schema["paths"]
    assert set(paths["/api/v1/grants"]) == {"get", "put"}
    read = paths["/api/v1/grants"]["get"]
    assert read["tags"] == ["grants"]
    assert {"200", "401", "404", "503"} == set(read["responses"])
    edit = paths["/api/v1/grants"]["put"]
    assert edit["tags"] == ["grants"]
    assert {"200", "401", "403", "409", "422", "503"} == set(edit["responses"])
    for route in grants_app.routes:
        path = getattr(route, "path", "")
        if path == "/api/v1/grants":
            continue
    assert "/docs" not in paths and "/openapi.json" not in paths


# ---------------------------------------------------------------------------
# Scenario: committed grants drive the real dispatcher boundary
# (add → allowed effect; revoke → next effect denied; wrong server;
# mid-flight recheck; modes; restart)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_http_add_authorizes_and_revocation_denies_the_next_effect(
    grants_app: FastAPI,
    config_store: GrantConfigStore,
    approvals: ApprovalStore,
) -> None:
    """The brief's scenario core: the actual HTTP add/revoke of a fixture
    grant moves (and stops) the real dispatcher's effect."""
    jar = await login_session(grants_app)
    execute, calls = _recording_executor()
    dispatcher = _dispatcher(
        config_store,
        approvals,
        registry=_registry_with(_descriptor_document(CAPABILITY, "mutate")),
        executor=execute,
    )

    added = await _put_grants(
        grants_app, jar, _edit_body(0, grants=[_entry(CAPABILITY, SERVER_A)])
    )
    assert added.status == 200 and added.json()["revision"] == 1
    allowed = await dispatcher.dispatch(
        context=_context(), capability_id=CAPABILITY, payload=PAYLOAD
    )
    assert allowed.allowed is True
    assert allowed.effect == "executed"
    assert allowed.policy_revision == 1
    assert len(calls) == 1

    revoked = await _put_grants(grants_app, jar, _edit_body(1, grants=[]))
    assert revoked.status == 200 and revoked.json()["revision"] == 2
    denied = await dispatcher.dispatch(
        context=_context(), capability_id=CAPABILITY, payload=PAYLOAD
    )
    assert denied.allowed is False
    assert denied.reason == "capability_not_granted"
    assert denied.policy_revision == 2
    assert denied.effect == "not_attempted"
    assert len(calls) == 1  # no unauthorized target action


@pytest.mark.anyio
async def test_grant_for_server_a_never_authorizes_server_b(
    grants_app: FastAPI,
    config_store: GrantConfigStore,
    approvals: ApprovalStore,
) -> None:
    """Wrong-server edit: scope is explicit per grant — widening to B via
    HTTP is the only way B becomes a lawful target."""
    jar = await login_session(grants_app)
    execute, calls = _recording_executor()
    dispatcher = _dispatcher(
        config_store,
        approvals,
        registry=_registry_with(_descriptor_document(CAPABILITY, "mutate")),
        executor=execute,
    )
    assert (await _put_grants(
        grants_app, jar, _edit_body(0, grants=[_entry(CAPABILITY, SERVER_A)])
    )).status == 200

    wrong_server = await dispatcher.dispatch(
        context=_context(server_id=SERVER_B),
        capability_id=CAPABILITY,
        payload=PAYLOAD,
    )
    assert wrong_server.allowed is False
    assert wrong_server.reason == "server_scope_mismatch"
    assert calls == []

    widened = await _put_grants(
        grants_app,
        jar,
        _edit_body(1, grants=[_entry(CAPABILITY, SERVER_A, SERVER_B)]),
    )
    assert widened.status == 200 and widened.json()["revision"] == 2
    lawful = await dispatcher.dispatch(
        context=_context(server_id=SERVER_B),
        capability_id=CAPABILITY,
        payload=PAYLOAD,
    )
    assert lawful.allowed is True and lawful.effect == "executed"
    assert len(calls) == 1


@pytest.mark.anyio
async def test_revocation_while_an_operation_waits_denies_at_the_recheck(
    grants_app: FastAPI,
    config_store: GrantConfigStore,
    approvals: ApprovalStore,
) -> None:
    """C03 rule 3 / G2 step 4 at the HTTP boundary: a job whose gate saw
    revision 1 is denied at the immediate recheck after the admin revokes
    via HTTP — the consumed decision is reported honestly, the effect
    never happens."""
    jar = await login_session(grants_app)
    assert (await _put_grants(
        grants_app,
        jar,
        _edit_body(0, grants=[_entry(PRIVILEGED_CAPABILITY, SERVER_A)]),
    )).status == 200
    approval = await _create_approval(
        approvals,
        capability=PRIVILEGED_CAPABILITY,
        server_id=SERVER_A,
        policy_revision=1,
    )

    before = config_store.read()  # the gate's view: granted at revision 1
    revoked = await _put_grants(grants_app, jar, _edit_body(1, grants=[]))
    assert revoked.status == 200 and revoked.json()["revision"] == 2
    after = config_store.read()  # the recheck's view: revoked at revision 2

    #: The dispatcher fetches grants and policy once per phase (gate,
    #: immediate recheck). Each phase must see one coherent world: the
    #: gate reads the pre-revocation snapshot, the recheck the
    #: post-revocation one — exactly what a live store read returns
    #: before/after the HTTP commit above.
    fetches = {"n": 0}

    def _snapshot() -> Any:
        world = before if fetches["n"] // 2 == 0 else after
        fetches["n"] += 1
        return world

    dispatcher = _dispatcher(
        config_store,
        approvals,
        registry=_registry_with(_descriptor_document(PRIVILEGED_CAPABILITY, "privileged")),
        executor=_refusing_executor(),
        grants=lambda: _snapshot().grants,
        policy=lambda: _snapshot().policy,
    )
    outcome = await dispatcher.dispatch(
        context=_context(allowed=(PRIVILEGED_CAPABILITY,), mode=PolicyMode.ASK),
        capability_id=PRIVILEGED_CAPABILITY,
        payload=PAYLOAD,
        approval_id=approval.approval_id,
    )
    assert outcome.allowed is False
    assert outcome.reason == "capability_not_granted"  # recheck verdict
    assert outcome.policy_revision == 2
    assert outcome.approval_spent is True
    assert outcome.effect == "not_attempted"
    spent = await approvals.get(approval.approval_id)
    assert spent is not None and spent.status == "consumed"


@pytest.mark.anyio
async def test_mode_change_never_enables_an_absent_capability(
    grants_app: FastAPI,
    config_store: GrantConfigStore,
    approvals: ApprovalStore,
) -> None:
    """AC2: ask/delegated change prompting *within* grants only. The same
    committed configuration (one revision, unchanged by any mode flip)
    allows exactly the granted capability and denies the absent and the
    deny-listed ones identically in both modes."""
    jar = await login_session(grants_app)
    assert (await _put_grants(
        grants_app,
        jar,
        _edit_body(
            0,
            grants=[
                _entry(CAPABILITY, SERVER_A),
                _entry(DENIED_CAPABILITY, SERVER_A),
            ],
            denied=[DENIED_CAPABILITY],
        ),
    )).status == 200
    execute, calls = _recording_executor()
    registry = _registry_with(
        _descriptor_document(CAPABILITY, "mutate"),
        _descriptor_document(UNGRANTED_CAPABILITY, "mutate"),
        _descriptor_document(DENIED_CAPABILITY, "mutate"),
    )

    # ask mode: the granted mutate effect prompts — it is denied without a
    # decision and allowed only with one (approval_spent True).
    ask_prompting = await _dispatcher(
        config_store,
        approvals,
        registry=registry,
        executor=_refusing_executor(),
    ).dispatch(
        context=_context(mode=PolicyMode.ASK),
        capability_id=CAPABILITY,
        payload=PAYLOAD,
    )
    assert ask_prompting.allowed is False
    assert ask_prompting.reason == "approval_required_but_absent"
    ask_approval = await _create_approval(
        approvals, capability=CAPABILITY, server_id=SERVER_A, policy_revision=1
    )
    ask_allowed = await _dispatcher(
        config_store,
        approvals,
        registry=registry,
        executor=execute,
    ).dispatch(
        context=_context(mode=PolicyMode.ASK),
        capability_id=CAPABILITY,
        payload=PAYLOAD,
        approval_id=ask_approval.approval_id,
    )
    assert ask_allowed.allowed is True
    assert ask_allowed.approval_spent is True
    ask_absent = await _dispatcher(
        config_store,
        approvals,
        registry=registry,
        executor=_refusing_executor(),
    ).dispatch(
        context=_context(
            allowed=(UNGRANTED_CAPABILITY,), mode=PolicyMode.ASK
        ),
        capability_id=UNGRANTED_CAPABILITY,
        payload=PAYLOAD,
    )
    assert ask_absent.allowed is False
    assert ask_absent.reason == "capability_not_granted"
    ask_denied = await _dispatcher(
        config_store,
        approvals,
        registry=registry,
        executor=_refusing_executor(),
    ).dispatch(
        context=_context(
            allowed=(DENIED_CAPABILITY,), mode=PolicyMode.ASK
        ),
        capability_id=DENIED_CAPABILITY,
        payload=PAYLOAD,
    )
    assert ask_denied.allowed is False
    assert ask_denied.reason == "capability_denied_by_policy"

    # delegated mode: same grants, mode flipped, config revision unchanged.
    view = (await _get_grants(grants_app, jar)).json()
    assert view["revision"] == 1  # a mode flip is not a configuration write
    delegated_allowed = await _dispatcher(
        config_store,
        approvals,
        registry=registry,
        executor=execute,
    ).dispatch(
        context=_context(mode=PolicyMode.DELEGATED),
        capability_id=CAPABILITY,
        payload=PAYLOAD,
    )
    assert delegated_allowed.allowed is True
    assert delegated_allowed.approval_spent is False  # no prompt in delegated
    assert delegated_allowed.effect == "executed"
    # ...and an approval presented to a delegated call cannot ride along.
    spare_approval = await _create_approval(
        approvals, capability=CAPABILITY, server_id=SERVER_A, policy_revision=1
    )
    ride_along = await _dispatcher(
        config_store,
        approvals,
        registry=registry,
        executor=_refusing_executor(),
    ).dispatch(
        context=_context(mode=PolicyMode.DELEGATED),
        capability_id=CAPABILITY,
        payload=PAYLOAD,
        approval_id=spare_approval.approval_id,
    )
    assert ride_along.allowed is False
    assert ride_along.reason == "approval_presented_but_not_required"
    delegated_absent = await _dispatcher(
        config_store,
        approvals,
        registry=registry,
        executor=_refusing_executor(),
    ).dispatch(
        context=_context(
            allowed=(UNGRANTED_CAPABILITY,), mode=PolicyMode.DELEGATED
        ),
        capability_id=UNGRANTED_CAPABILITY,
        payload=PAYLOAD,
    )
    assert delegated_absent.allowed is False
    assert delegated_absent.reason == "capability_not_granted"
    delegated_denied = await _dispatcher(
        config_store,
        approvals,
        registry=registry,
        executor=_refusing_executor(),
    ).dispatch(
        context=_context(
            allowed=(DENIED_CAPABILITY,), mode=PolicyMode.DELEGATED
        ),
        capability_id=DENIED_CAPABILITY,
        payload=PAYLOAD,
    )
    assert delegated_denied.allowed is False
    assert delegated_denied.reason == "capability_denied_by_policy"
    assert len(calls) == 2  # exactly the two lawful granted effects


@pytest.mark.anyio
async def test_restart_serves_the_effective_policy_from_the_canonical_file(
    migrated_url: str, tmp_path: Path
) -> None:
    """Restart: a fresh app (fresh store, fresh engine) over the same data
    root serves the committed configuration and enforces it — the granted
    target acts, the revoked capability and the wrong server do not."""
    _truncate(migrated_url)
    data_root = tmp_path / "data-root"

    first = _build_app(migrated_url, data_root)
    async with _running(first):
        jar = await login_session(first)
        added = await _put_grants(
            first, jar, _edit_body(0, grants=[_entry(CAPABILITY, SERVER_A)])
        )
        assert added.status == 200 and added.json()["revision"] == 1
    await first.state.engine.dispose()

    # ...restart: a brand-new app instance and store over the same root.
    second = _build_app(migrated_url, data_root)
    async with _running(second):
        jar2 = await _login(second)
        view = (await _get_grants(second, jar2)).json()
        assert view["revision"] == 1
        assert view["grants"] == [
            {"capability_id": CAPABILITY, "server_ids": [str(SERVER_A)]}
        ]
        config_store = GrantConfigStore(data_root)
        execute, calls = _recording_executor()
        dispatcher = _dispatcher(
            config_store,
            ApprovalStore(second.state.session_factory),
            registry=_registry_with(_descriptor_document(CAPABILITY, "mutate")),
            executor=execute,
        )
        allowed = await dispatcher.dispatch(
            context=_context(), capability_id=CAPABILITY, payload=PAYLOAD
        )
        assert allowed.allowed is True and allowed.effect == "executed"
        wrong_server = await dispatcher.dispatch(
            context=_context(server_id=SERVER_B),
            capability_id=CAPABILITY,
            payload=PAYLOAD,
        )
        assert wrong_server.allowed is False
        assert wrong_server.reason == "server_scope_mismatch"
        revoked = await _put_grants(second, jar2, _edit_body(1, grants=[]))
        assert revoked.status == 200 and revoked.json()["revision"] == 2
        denied = await dispatcher.dispatch(
            context=_context(), capability_id=CAPABILITY, payload=PAYLOAD
        )
        assert denied.allowed is False
        assert denied.reason == "capability_not_granted"
        assert len(calls) == 1  # only the one authorized target acted
    await second.state.engine.dispose()
