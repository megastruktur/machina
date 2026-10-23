"""Focused tests for the server profile HTTP API (card 02-profile-api).

Cards/contracts: contracts.md C01 (identity, envelope, revision conflict,
principal-from-session), C02 (canonical service + projection pipeline,
explicit data root), C08 rule 2/4 (boundary layers on every mutation, G1);
the store/projection semantics underneath are covered by
``tests/test_profile_files.py`` (02-file-store) and
``tests/test_profile_projection.py`` (02-profile-projection); this suite
proves the HTTP surface joining them.

Boundary discipline: every claim runs through the real ASGI app
(``create_app`` + lifespan + middleware + handlers) against a real
disposable Postgres (fixture ``migrated_url``) and a real confined data
root on the local filesystem — never mocks. The file/SQL pair is inspected
directly (``profile_projection`` rows, canonical ``profile.yaml`` +
``profile.meta.json``) so the acceptance "goes through canonical service
and projection" is observed at both durable boundaries.

Secret hygiene: fixture SSH values are synthetic constants for the
disposable fixture (``documentation``/``RFC 5737``-style names); session
and CSRF tokens are minted at runtime and never printed into assertion
messages.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from fastapi.routing import APIRoute

from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE
from mai.api.servers import resolve_profiles_root

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: Synthetic fixture credential (NOT a secret; disposable fixture DB only).
FIXTURE_PASSWORD = "fixture-password-02-profile-api-only"
ORIGIN = "http://127.0.0.1:8443"  # loopback allowlist entry
EVIL_ORIGIN = "https://attacker.example"

HOST_KEY_A = "SHA256:" + "A" * 43  # OpenSSH default fingerprint shape
HOST_KEY_B = "SHA256:" + "bB" * 21 + "b"  # 43 chars, distinct from A


def _profile_body(
    name: str,
    *,
    prompt_revision: int = 1,
    host_key: str = HOST_KEY_A,
    capabilities: list[str] | None = None,
    privilege: str = "sudo",
    environment: str = "linux_ssh",
    ssh_host: str = "build.example.internal",
) -> dict[str, Any]:
    """One valid profile payload (producer-validated values)."""
    return {
        "name": name,
        "prompt_revision": prompt_revision,
        "environment": environment,
        "ssh": {
            "host": ssh_host,
            "port": 22,
            "user": "deploy",
            "host_key": host_key,
            "credential_ref": "MAI_SSH_KEY_BUILD_HOST",
        },
        "privilege": privilege,
        "capabilities": capabilities
        if capabilities is not None
        else ["site.deploy"],
    }


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(os.path.join(BACKEND_ROOT, "alembic.ini"))


# ---------------------------------------------------------------------------
# Fixtures: real disposable Postgres + real app through lifespan
# ---------------------------------------------------------------------------


def _reset_fixture_db(url: str) -> None:
    """Reset the disposable fixture database to pre-bootstrap state.

    The list is explicit and dependency-safe: every application table
    created by the migration chain (``0001``..``0007``) plus the
    ``server_observations_seq`` sequence, which ``0007`` owns and does
    not drop with its table. The same pattern is used by
    ``tests/test_jobs_http.py`` and ``tests/test_schedules_http.py``.
    No wildcard, no ``CASCADE``, no constraint-disabling and no product
    migration is involved.
    """
    with psycopg.connect(url) as conn:
        conn.execute(
            "DROP TABLE IF EXISTS job_resource_locks, job_schedule_fires, "
            "job_schedules, job_events, job_steps, job_runs, approvals, "
            "profile_projection_journal, profile_projection, admin_sessions, "
            "admin_credential, alembic_version, notification_alerts, "
            "observation_findings, server_observations, knowledge_doc_chunks, "
            "knowledge_documents, application_projection_journal, "
            "application_projection"
        )
        conn.execute("DROP SEQUENCE IF EXISTS server_observations_seq")
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> Iterator[str]:
    """Disposable fixture database migrated from zero through ``head`` (0007).

    The reset must be complete: an earlier module in the same pytest
    process may have migrated the shared disposable database all the way
    to ``0007``, so dropping only the profile-era tables would leave the
    later-schema tables behind and make ``upgrade head`` fail with
    ``DuplicateTable`` (observed on ``job_runs``). The session teardown
    resets the same database back to pre-bootstrap state so a repeated
    run against the very same disposable database starts clean again.
    """
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the HTTP surface claims need a "
            "real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        _reset_fixture_db(url)
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    yield url
    _reset_fixture_db(url)


def _truncate(url: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            "TRUNCATE admin_sessions, admin_credential, "
            "profile_projection, profile_projection_journal"
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
async def servers_app(migrated_url: str, tmp_path: Path):
    """App on the real fixture DB with a fresh confined data root."""
    _truncate(migrated_url)
    app = _build_app(migrated_url, tmp_path / "data-root")
    async with _running(app):
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()


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


def _mutation_headers(jar: Mapping[str, str]) -> dict[str, str]:
    return {
        "origin": ORIGIN,
        "x-csrf-token": jar[CSRF_COOKIE],
    }


async def create_server(
    app: FastAPI,
    jar: Mapping[str, str],
    body: Mapping[str, Any],
) -> HttpResponse:
    return await call_app(
        app,
        "POST",
        "/api/v1/servers",
        json_body=dict(body),
        headers=_mutation_headers(jar),
        cookies=jar,
    )


async def edit_server(
    app: FastAPI,
    jar: Mapping[str, str],
    server_id: uuid.UUID,
    body: Mapping[str, Any],
    *,
    expected_revision: int,
) -> HttpResponse:
    return await call_app(
        app,
        "PUT",
        f"/api/v1/servers/{server_id}",
        json_body={**dict(body), "expected_revision": expected_revision},
        headers=_mutation_headers(jar),
        cookies=jar,
    )


async def get_server(app: FastAPI, jar: Mapping[str, str], server_id: uuid.UUID):
    return await call_app(
        app, "GET", f"/api/v1/servers/{server_id}", cookies=jar
    )


# ---------------------------------------------------------------------------
# Durable-boundary observers (file truth + SQL projection truth)
# ---------------------------------------------------------------------------


def _canonical_files(data_root: Path, server_id: uuid.UUID) -> tuple[Path, Path]:
    server_dir = data_root / "servers" / str(server_id)
    return server_dir / "profile.yaml", server_dir / "profile.meta.json"


def _projection_rows(url: str, server_id: uuid.UUID | None = None) -> list[dict]:
    """Mirror the projection card's staleness semantics (live-intent join)."""
    query = (
        "SELECT p.server_id::text, p.revision, p.schema_revision, p.sha256, "
        "(j.intent_id IS NOT NULL) AS stale "
        "FROM profile_projection p "
        "LEFT JOIN profile_projection_journal j "
        "ON j.server_id = p.server_id AND j.phase <> 'reconciled'"
    )
    params: tuple = ()
    if server_id is not None:
        query += " WHERE p.server_id = %s"
        params = (str(server_id),)
    with psycopg.connect(url) as conn:
        rows = conn.execute(query + " ORDER BY p.server_id", params).fetchall()
    return [
        {
            "server_id": row[0],
            "revision": row[1],
            "schema_revision": row[2],
            "sha256": row[3],
            "stale": row[4],
        }
        for row in rows
    ]


def _live_intent_count(url: str, server_id: uuid.UUID) -> int:
    with psycopg.connect(url) as conn:
        (count,) = conn.execute(
            "SELECT count(*) FROM profile_projection_journal "
            "WHERE server_id = %s AND phase <> 'reconciled'",
            (str(server_id),),
        ).fetchone()
    return int(count)


# ---------------------------------------------------------------------------
# Registration + OpenAPI (AC3: real registration, generated schema only)
# ---------------------------------------------------------------------------


def _api_routes(app: FastAPI) -> list[APIRoute]:
    """Every APIRoute, unwrapping FastAPI ≥0.143 _IncludedRouter wrappers."""
    routes: list[APIRoute] = []
    for included in app.routes:
        original = getattr(included, "original_router", None)
        candidates = original.routes if original is not None else [included]
        routes.extend(route for route in candidates if isinstance(route, APIRoute))
    return routes


def test_full_surface_registers_the_frozen_routes() -> None:
    """Full MAI v1 surface: health + auth + profiles + P03–P12 features.

    The pin is the WHOLE frozen table: any new route must extend it
    consciously (the same expansion discipline every remediation below
    recorded), never silently.
    """
    app = create_app(env={})
    api_routes = _api_routes(app)
    table = {
        (method, route.path) for route in api_routes for method in route.methods
    }
    assert table == {
        ("GET", "/api/v1/health"),
        ("GET", "/api/v1/health/ready"),
        ("GET", "/api/v1/auth/csrf"),
        ("POST", "/api/v1/auth/bootstrap"),
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/logout"),
        # (02-profile-api: canonical profiles; the launcher's list route
        # added by mai-wire-desktop.)
        ("GET", "/api/v1/servers"),
        ("POST", "/api/v1/servers"),
        ("GET", "/api/v1/servers/{server_id}"),
        ("PUT", "/api/v1/servers/{server_id}"),
        # (03-approval-api remediation: the pin expanded by the frozen
        # approval preview/decision routes; intent unchanged.)
        ("GET", "/api/v1/approvals/{approval_id}"),
        ("POST", "/api/v1/approvals/{approval_id}/decision"),
        # (03-grants-api remediation: the pin expanded by the frozen
        # grant-configuration read/edit routes; intent unchanged.)
        ("GET", "/api/v1/grants"),
        ("PUT", "/api/v1/grants"),
        # (04-password-api remediation: the pin expanded by the frozen
        # submit-only challenge route; intent unchanged.)
        ("POST", "/api/v1/password-challenges"),
        # (05-jobs-api: durable runs + schedules + the run event stream.)
        ("GET", "/api/v1/jobs/runs"),
        ("POST", "/api/v1/jobs/runs"),
        ("GET", "/api/v1/jobs/runs/{run_id}"),
        ("POST", "/api/v1/jobs/runs/{run_id}/cancel"),
        ("GET", "/api/v1/jobs/runs/{run_id}/events"),
        ("GET", "/api/v1/schedules"),
        ("POST", "/api/v1/schedules"),
        ("GET", "/api/v1/schedules/{schedule_id}"),
        ("PATCH", "/api/v1/schedules/{schedule_id}"),
        ("DELETE", "/api/v1/schedules/{schedule_id}"),
        ("GET", "/api/v1/schedules/{schedule_id}/fires"),
        # (06-chat-api: conversations + the conversation event stream.)
        ("POST", "/api/v1/chat/messages"),
        ("GET", "/api/v1/chat/runs"),
        ("GET", "/api/v1/chat/runs/{run_id}"),
        ("PATCH", "/api/v1/chat/runs/{run_id}"),
        ("POST", "/api/v1/chat/runs/{run_id}/cancel"),
        ("GET", "/api/v1/chat/runs/{run_id}/events"),
        # (mai-wire-desktop/chat: the lazy-loaded single tool step read.)
        ("GET", "/api/v1/chat/runs/{run_id}/steps/{seq}"),
        # (06-model-api: stored connections/profiles/selection + probe.)
        ("GET", "/api/v1/models/connections"),
        ("GET", "/api/v1/models/connections/{name}"),
        ("PUT", "/api/v1/models/connections/{name}"),
        ("DELETE", "/api/v1/models/connections/{name}"),
        ("GET", "/api/v1/models/profiles"),
        ("GET", "/api/v1/models/profiles/{name}"),
        ("PUT", "/api/v1/models/profiles/{name}"),
        ("DELETE", "/api/v1/models/profiles/{name}"),
        ("GET", "/api/v1/models/selection"),
        ("PUT", "/api/v1/models/selection"),
        ("DELETE", "/api/v1/models/selection"),
        ("POST", "/api/v1/models/test"),
        # (07-docs-api: canonical documents, retrieval, index, generate,
        # research.)
        ("GET", "/api/v1/docs"),
        ("POST", "/api/v1/docs"),
        ("GET", "/api/v1/docs/{doc_id}"),
        ("PUT", "/api/v1/docs/{doc_id}"),
        ("DELETE", "/api/v1/docs/{doc_id}"),
        ("POST", "/api/v1/docs/retrieve"),
        ("GET", "/api/v1/docs/index"),
        ("POST", "/api/v1/docs/index/reconcile"),
        ("POST", "/api/v1/docs/generate"),
        ("POST", "/api/v1/docs/research"),
        # (08-app-api: observed inventory + explicit adoptions + lifecycle
        # intent submits.)
        ("GET", "/api/v1/servers/{server_id}/applications"),
        ("GET", "/api/v1/servers/{server_id}/applications/discoveries"),
        ("POST", "/api/v1/servers/{server_id}/applications"),
        ("DELETE", "/api/v1/servers/{server_id}/applications/{application_id}"),
        ("POST", "/api/v1/servers/{server_id}/applications/plan"),
        ("POST", "/api/v1/servers/{server_id}/applications/{application_id}/install"),
        ("POST", "/api/v1/servers/{server_id}/applications/{application_id}/update"),
        ("POST", "/api/v1/servers/{server_id}/applications/{application_id}/remove"),
        ("POST", "/api/v1/servers/{server_id}/applications/{application_id}/execute"),
        ("GET", "/api/v1/applications/catalog"),
        ("POST", "/api/v1/applications/catalog"),
        ("DELETE", "/api/v1/applications/catalog/{runbook_id}"),
        # (09-plugins-api: plugin configs/bindings/discovery + skills.)
        ("GET", "/api/v1/plugins"),
        ("GET", "/api/v1/plugins/{plugin}/config"),
        ("PUT", "/api/v1/plugins/{plugin}/config"),
        ("DELETE", "/api/v1/plugins/{plugin}/config"),
        ("GET", "/api/v1/plugins/{plugin}/schema"),
        ("GET", "/api/v1/plugins/{plugin}/bindings"),
        ("PUT", "/api/v1/plugins/{plugin}/bindings/{server_id}"),
        ("DELETE", "/api/v1/plugins/{plugin}/bindings/{server_id}"),
        ("POST", "/api/v1/plugins/{plugin}/discovery"),
        ("POST", "/api/v1/plugins/{plugin}/probe"),
        ("GET", "/api/v1/skills"),
        ("GET", "/api/v1/skills/{skill}"),
        # (10-monitor-api: observations/findings/notifications + policy.)
        ("GET", "/api/v1/monitoring/servers/{server_id}/observations"),
        ("GET", "/api/v1/monitoring/servers/{server_id}/observations/{observation_id}"),
        ("GET", "/api/v1/monitoring/servers/{server_id}/findings"),
        ("GET", "/api/v1/monitoring/servers/{server_id}/findings/{finding_id}"),
        ("POST", "/api/v1/monitoring/servers/{server_id}/findings/evaluate"),
        ("GET", "/api/v1/monitoring/servers/{server_id}/notifications"),
        ("GET", "/api/v1/monitoring/servers/{server_id}/notifications/{alert_id}"),
        ("POST", "/api/v1/monitoring/servers/{server_id}/notifications/{alert_id}/ack"),
        ("GET", "/api/v1/monitoring/notification-policy"),
        # (11-knowledge-api: memory surface.)
        ("GET", "/api/v1/servers/{server_id}/memory/status"),
        ("POST", "/api/v1/servers/{server_id}/memory/recall"),
        ("POST", "/api/v1/servers/{server_id}/memory/retain"),
        ("POST", "/api/v1/servers/{server_id}/memory/reflect"),
        ("GET", "/api/v1/servers/{server_id}/memory/export"),
        ("POST", "/api/v1/servers/{server_id}/memory/delete"),
        ("GET", "/api/v1/servers/{server_id}/terminal/info"),
    }
    # Every route stays under the frozen prefix; no docs listeners (G1).
    for route in api_routes:
        assert route.path.startswith("/api/v1")
    paths = {route.path for route in api_routes}
    assert "/docs" not in paths and "/openapi.json" not in paths


def test_openapi_declares_the_profile_contract() -> None:
    schema = create_app(env={}).openapi()
    servers_path = "/api/v1/servers"
    item_path = "/api/v1/servers/{server_id}"
    assert {servers_path, item_path} <= set(schema["paths"])
    create = schema["paths"][servers_path]["post"]
    assert {"201", "401", "409", "422", "503"} == set(create["responses"])
    listed = schema["paths"][servers_path]["get"]
    assert {"200", "401", "409", "422", "503"} == set(listed["responses"])
    read = schema["paths"][item_path]["get"]
    assert {"200", "401", "404", "422"} == set(read["responses"])
    edit = schema["paths"][item_path]["put"]
    assert {"200", "401", "404", "409", "422"} == set(edit["responses"])
    # Generated from the code (real request bodies/models), never handwritten.
    assert "requestBody" in create and "requestBody" in edit
    component_names = set(schema["components"]["schemas"])
    assert {
        "ProfileCreate",
        "ProfileEdit",
        "ProfileResponse",
        "ProfileMutationResult",
        "ProfileListResponse",
    } <= component_names
    # No invented security scheme; the session cookie is the only principal.
    assert "securitySchemes" not in schema["components"]


# ---------------------------------------------------------------------------
# G1 setup state + boundary layers (C08 rule 2/4; C01 rule 2)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_setup_state_fails_closed_on_every_profile_route(servers_app) -> None:
    """G1: without a credential, create/read/edit refuse value-free."""
    unknown_id = uuid.uuid4()
    create = await call_app(
        servers_app, "POST", "/api/v1/servers", json_body=_profile_body("x")
    )
    assert create.status == 401
    assert _error(create)["code"] == "unauthenticated"
    read = await call_app(servers_app, "GET", f"/api/v1/servers/{unknown_id}")
    assert read.status == 401
    assert _error(read)["code"] == "unauthenticated"
    edit = await call_app(
        servers_app,
        "PUT",
        f"/api/v1/servers/{unknown_id}",
        json_body={**_profile_body("x"), "expected_revision": 0},
    )
    assert edit.status == 401
    assert _error(edit)["code"] == "unauthenticated"


@pytest.mark.anyio
async def test_profile_denials_precede_any_state_effect(servers_app) -> None:
    """Negative control: every boundary denial left file+SQL untouched."""
    jar = await login_session(servers_app)
    data_root: Path = servers_app.state.profiles.root
    mutations = [
        (  # missing Origin
            await call_app(
                servers_app,
                "POST",
                "/api/v1/servers",
                json_body=_profile_body("no-origin"),
                headers={"x-csrf-token": jar[CSRF_COOKIE]},
                cookies=jar,
            )
        ),
        (  # foreign Origin
            await call_app(
                servers_app,
                "POST",
                "/api/v1/servers",
                json_body=_profile_body("evil-origin"),
                headers={"origin": EVIL_ORIGIN, "x-csrf-token": jar[CSRF_COOKIE]},
                cookies=jar,
            )
        ),
        (  # missing CSRF header
            await call_app(
                servers_app,
                "POST",
                "/api/v1/servers",
                json_body=_profile_body("no-csrf"),
                headers={"origin": ORIGIN},
                cookies=jar,
            )
        ),
        (  # CSRF header/cookie mismatch
            await call_app(
                servers_app,
                "POST",
                "/api/v1/servers",
                json_body=_profile_body("bad-csrf"),
                headers={"origin": ORIGIN, "x-csrf-token": "not-the-cookie"},
                cookies=jar,
            )
        ),
        (  # Referer is never an Origin substitute
            await call_app(
                servers_app,
                "PUT",
                f"/api/v1/servers/{uuid.uuid4()}",
                json_body={**_profile_body("referer"), "expected_revision": 0},
                headers={"referer": ORIGIN, "x-csrf-token": jar[CSRF_COOKIE]},
                cookies=jar,
            )
        ),
    ]
    for response in mutations:
        assert response.status == 403, response.body
        assert _error(response)["code"] in {"origin_rejected", "csrf_rejected"}
    # The whole canonical service stayed untouched: no files, no SQL rows.
    assert list(data_root.iterdir()) == []
    url = servers_app.state.database_url
    assert _projection_rows(url) == []
    with psycopg.connect(url) as conn:
        (journal,) = conn.execute(
            "SELECT count(*) FROM profile_projection_journal"
        ).fetchone()
    assert journal == 0


@pytest.mark.anyio
async def test_get_is_safe_origin_free_but_never_anonymous(servers_app) -> None:
    """Layer 1 only for the safe method; layers 2–3 are mutation-only."""
    jar = await login_session(servers_app)
    created = await create_server(servers_app, jar, _profile_body("alpha"))
    assert created.status == 201
    server_id = uuid.UUID(created.json()["server_id"])
    read = await call_app(
        servers_app,
        "GET",
        f"/api/v1/servers/{server_id}",
        cookies=jar,  # no Origin, no CSRF: safe method
    )
    assert read.status == 200
    # But never anonymous:
    anonymous = await call_app(servers_app, "GET", f"/api/v1/servers/{server_id}")
    assert anonymous.status == 401
    assert _error(anonymous)["code"] == "unauthenticated"


@pytest.mark.anyio
async def test_forged_privileged_header_is_never_an_input(servers_app) -> None:
    """C01 rule 2: X-Admin-style headers change nothing on profile routes."""
    unknown_id = uuid.uuid4()
    forged = {"x-admin": "true", "x-remote-admin": "yes"}
    read = await call_app(
        servers_app, "GET", f"/api/v1/servers/{unknown_id}", headers=forged
    )
    assert read.status == 401
    create = await call_app(
        servers_app,
        "POST",
        "/api/v1/servers",
        json_body=_profile_body("forged"),
        headers=forged,
    )
    assert create.status == 401


# ---------------------------------------------------------------------------
# Create/get through the canonical service and projection (AC1)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_and_get_round_trip_through_file_and_sql(
    servers_app,
) -> None:
    jar = await login_session(servers_app)
    body = _profile_body("alpha", prompt_revision=1, capabilities=["site.deploy"])
    created = await create_server(servers_app, jar, body)
    assert created.status == 201, created.body
    result = created.json()
    server_id = uuid.UUID(result["server_id"])
    assert result["revision"] == 1
    assert result["schema_revision"] == 1
    assert len(result["sha256"]) == 64

    # Durable boundary 1: the canonical file pair under the configured root.
    data_root: Path = servers_app.state.profiles.root
    profile_file, meta_file = _canonical_files(data_root, server_id)
    assert profile_file.is_file() and meta_file.is_file()
    stored_text = profile_file.read_text()
    assert f"server_id: {server_id}" in stored_text
    assert "prompt_revision: 1" in stored_text

    # Durable boundary 2: the SQL projection holds the same revision.
    url = servers_app.state.database_url
    rows = _projection_rows(url, server_id)
    assert len(rows) == 1
    assert rows[0]["revision"] == 1
    assert rows[0]["sha256"] == result["sha256"]
    assert rows[0]["stale"] is False

    # The API read comes from the authoritative file + projection view.
    read = await get_server(servers_app, jar, server_id)
    assert read.status == 200
    payload = read.json()
    assert payload["server_id"] == str(server_id)
    assert payload["name"] == "alpha"
    assert payload["prompt_revision"] == 1
    assert payload["environment"] == "linux_ssh"
    assert payload["privilege"] == "sudo"
    assert payload["capabilities"] == ["site.deploy"]
    assert payload["ssh"] == body["ssh"]
    assert payload["revision"] == 1
    assert payload["sha256"] == result["sha256"]
    assert payload["projection"] == {"revision": 1, "stale": False}


@pytest.mark.anyio
async def test_unknown_server_read_and_edit_are_value_free_404(servers_app) -> None:
    jar = await login_session(servers_app)
    unknown_id = uuid.uuid4()
    read = await get_server(servers_app, jar, unknown_id)
    assert read.status == 404
    assert _error(read)["code"] == "not_found"
    edit = await edit_server(
        servers_app, jar, unknown_id, _profile_body("ghost"), expected_revision=0
    )
    assert edit.status == 404
    assert _error(edit)["code"] == "not_found"
    # No journal residue for the unknown identity.
    assert _live_intent_count(servers_app.state.database_url, unknown_id) == 0


@pytest.mark.anyio
async def test_invalid_uuid_path_gets_structured_validation_error(
    servers_app,
) -> None:
    jar = await login_session(servers_app)
    read = await call_app(
        servers_app, "GET", "/api/v1/servers/not-a-uuid", cookies=jar
    )
    assert read.status == 422
    body = _error(read)
    assert body["code"] == "validation_failed"
    assert body["fields"]


# ---------------------------------------------------------------------------
# Launcher list: session-checked safe read of every stored profile
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_is_safe_but_never_anonymous(servers_app) -> None:
    """Layer 1 only: no Origin/CSRF for the safe read, but never anonymous."""
    anonymous = await call_app(servers_app, "GET", "/api/v1/servers")
    assert anonymous.status == 401
    assert _error(anonymous)["code"] == "unauthenticated"

    jar = await login_session(servers_app)
    empty = await call_app(servers_app, "GET", "/api/v1/servers", cookies=jar)
    assert empty.status == 200
    assert empty.json() == {"servers": []}


@pytest.mark.anyio
async def test_list_returns_every_profile_sorted_by_name(servers_app) -> None:
    jar = await login_session(servers_app)
    data_root: Path = servers_app.state.profiles.root
    # Non-canonical directory litter is not a profile and never breaks the list.
    (data_root / "servers" / "not-a-uuid").mkdir(parents=True)
    zulu = await create_server(servers_app, jar, _profile_body("zulu-target"))
    alpha = await create_server(
        servers_app, jar, _profile_body("alpha-target", prompt_revision=2)
    )
    assert zulu.status == 201 and alpha.status == 201

    listed = await call_app(servers_app, "GET", "/api/v1/servers", cookies=jar)
    assert listed.status == 200
    payload = listed.json()
    assert [item["name"] for item in payload["servers"]] == [
        "alpha-target",
        "zulu-target",
    ]
    alpha_view = payload["servers"][0]
    assert alpha_view["server_id"] == alpha.json()["server_id"]
    assert alpha_view["revision"] == 1
    assert alpha_view["projection"] == {"revision": 1, "stale": False}
    # A list row and the single read of the same server never disagree.
    single = await get_server(servers_app, jar, uuid.UUID(alpha_view["server_id"]))
    assert single.status == 200
    assert single.json() == alpha_view


# ---------------------------------------------------------------------------
# Two fixture server IDs, prompt edit, display rename (AC2)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_two_servers_stay_scoped_and_rename_retargets_nothing(
    servers_app,
) -> None:
    url = servers_app.state.database_url
    data_root: Path = servers_app.state.profiles.root
    jar = await login_session(servers_app)

    alpha = await create_server(
        servers_app, jar, _profile_body("alpha", host_key=HOST_KEY_A)
    )
    beta = await create_server(
        servers_app, jar, _profile_body("beta", host_key=HOST_KEY_B, ssh_host="db.example.internal")
    )
    assert alpha.status == 201 and beta.status == 201
    alpha_id = uuid.UUID(alpha.json()["server_id"])
    beta_id = uuid.UUID(beta.json()["server_id"])
    alpha_file, _ = _canonical_files(data_root, alpha_id)
    beta_file, _ = _canonical_files(data_root, beta_id)
    beta_bytes_before = beta_file.read_bytes()

    # Prompt edit on alpha: prompt edition 2 through the canonical pipeline.
    edited = await edit_server(
        servers_app,
        jar,
        alpha_id,
        _profile_body("alpha", prompt_revision=2, host_key=HOST_KEY_A),
        expected_revision=1,
    )
    assert edited.status == 200, edited.body
    assert edited.json()["revision"] == 2

    # Alpha advanced; beta is byte- and revision-identical (scoped).
    assert "prompt_revision: 2" in alpha_file.read_text()
    assert beta_file.read_bytes() == beta_bytes_before
    rows = {row["server_id"]: row for row in _projection_rows(url)}
    assert rows[str(alpha_id)]["revision"] == 2
    assert rows[str(beta_id)]["revision"] == 1
    assert rows[str(beta_id)]["sha256"] == beta.json()["sha256"]

    # Display rename on alpha does not retarget operations: the same UUID
    # still addresses the same server; beta keeps its identity and state.
    renamed = await edit_server(
        servers_app,
        jar,
        alpha_id,
        _profile_body("alpha-renamed", prompt_revision=2, host_key=HOST_KEY_A),
        expected_revision=2,
    )
    assert renamed.status == 200
    got_alpha = await get_server(servers_app, jar, alpha_id)
    got_beta = await get_server(servers_app, jar, beta_id)
    assert got_alpha.json()["name"] == "alpha-renamed"
    assert got_alpha.json()["server_id"] == str(alpha_id)
    assert got_beta.json()["name"] == "beta"
    assert got_beta.json()["prompt_revision"] == 1
    assert got_beta.json()["revision"] == 1
    assert beta_file.read_bytes() == beta_bytes_before


# ---------------------------------------------------------------------------
# Stale revision conflict (C01 rule 6) leaves no residue (AC2)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_stale_update_conflicts_without_journal_residue(
    servers_app,
) -> None:
    url = servers_app.state.database_url
    data_root: Path = servers_app.state.profiles.root
    jar = await login_session(servers_app)
    created = await create_server(servers_app, jar, _profile_body("alpha"))
    server_id = uuid.UUID(created.json()["server_id"])
    profile_file, _ = _canonical_files(data_root, server_id)
    before = profile_file.read_bytes()

    # Stale caller (revision already 1, claims 0): structured conflict.
    stale = await edit_server(
        servers_app, jar, server_id, _profile_body("alpha"), expected_revision=0
    )
    assert stale.status == 409
    body = _error(stale)
    assert body["code"] == "conflict"
    assert body["fields"] == {}

    # Negative control: the denial preceded ANY durable effect and left no
    # live journal intent that could block the next legitimate write.
    assert profile_file.read_bytes() == before
    rows = _projection_rows(url, server_id)
    assert rows[0]["revision"] == 1
    assert _live_intent_count(url, server_id) == 0

    # The server stays fully operable after the stale denial.
    retry = await edit_server(
        servers_app,
        jar,
        server_id,
        _profile_body("alpha", prompt_revision=2),
        expected_revision=1,
    )
    assert retry.status == 200
    assert retry.json()["revision"] == 2


@pytest.mark.anyio
async def test_second_live_intent_is_a_conflict_not_a_clobber(
    servers_app,
) -> None:
    """Concurrent-write serialization: journal pending ⇒ structured 409."""
    url = servers_app.state.database_url
    jar = await login_session(servers_app)
    created = await create_server(servers_app, jar, _profile_body("alpha"))
    server_id = uuid.UUID(created.json()["server_id"])
    projection = servers_app.state.projection
    # Simulate another in-flight writer exactly as the journal defines it:
    # a live prepared intent (schema-valid payload) blocks a second one
    # (partial unique index, C05 rule 6 mirror).
    valid_document = yaml.safe_dump(
        {
            "schema_version": 1,
            "server_id": str(server_id),
            "name": "in-flight",
            "prompt_revision": 2,
            "environment": "linux_ssh",
            "ssh": _profile_body("in-flight")["ssh"],
            "privilege": "sudo",
            "capabilities": ["site.deploy"],
        },
        sort_keys=False,
    )
    await projection.prepare_upsert(
        server_id, valid_document, expected_revision=1
    )
    blocked = await edit_server(
        servers_app, jar, server_id, _profile_body("alpha"), expected_revision=1
    )
    assert blocked.status == 409
    body = _error(blocked)
    assert body["code"] == "conflict"
    assert "in progress" in body["message"]
    # Cleanup for the shared fixture DB: drop the simulated intent.
    with psycopg.connect(url) as conn:
        conn.execute(
            "DELETE FROM profile_projection_journal WHERE server_id = %s",
            (str(server_id),),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Producer validation mapped onto the C01 envelope (AC1)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_producer_rejections_are_422_with_stable_field_codes(
    servers_app,
) -> None:
    data_root: Path = servers_app.state.profiles.root
    jar = await login_session(servers_app)
    cases = [
        (_profile_body("bad-privilege", privilege="diagonal"), "privilege"),
        (_profile_body("bad-env", environment="windows_rdp"), "environment"),
        (
            _profile_body("bad-cap", capabilities=["Site.Deploy"]),
            "capabilities",
        ),
        (_profile_body("bad-key", host_key="sha256:short"), "ssh.host_key"),
        (_profile_body("bad-ref", prompt_revision=0), "prompt_revision"),
    ]
    for body, field_name in cases:
        response = await create_server(servers_app, jar, body)
        assert response.status == 422, (field_name, response.body)
        error = _error(response)
        assert error["code"] == "validation_failed"
        assert error["fields"], field_name
    # Nothing was persisted by any rejected payload.
    assert list(data_root.iterdir()) == []
    assert _projection_rows(servers_app.state.database_url) == []


# ---------------------------------------------------------------------------
# Fail-closed canonical service (C02 rule 1: explicit root, no invention)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_unusable_data_root_fails_closed_without_touching_sql(
    migrated_url: str, tmp_path: Path
) -> None:
    _truncate(migrated_url)
    blocked_root = tmp_path / "blocker-file"
    blocked_root.write_text("a file, not a directory")
    app = _build_app(migrated_url, blocked_root / "data-root")
    async with _running(app):
        jar = await login_session(app)
        created = await create_server(app, jar, _profile_body("alpha"))
        assert created.status == 503
        body = _error(created)
        assert body["code"] == "unavailable"
        assert app.state.profiles is None
        assert app.state.profiles_error == "profiles_root_unusable"
        # Liveness/readiness stay truthful while the surface fails closed.
        health = await call_app(app, "GET", "/api/v1/health")
        assert health.status == 200
    await app.state.engine.dispose()
    assert _projection_rows(migrated_url) == []


# ---------------------------------------------------------------------------
# Data root resolution (C02 rule 1: a literal ``~`` never reaches the store)
# ---------------------------------------------------------------------------


def test_default_profiles_root_is_absolute_and_home_anchored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without ``MAI_DATA_ROOT`` the default ``~/.mai`` resolves to ``$HOME``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    resolved = resolve_profiles_root({})
    assert resolved == str(tmp_path / ".mai")
    assert os.path.isabs(resolved)


def test_profiles_root_override_wins_and_tilde_still_expands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``MAI_DATA_ROOT`` wins over the default; ``~``-prefixed values expand."""
    monkeypatch.setenv("HOME", str(tmp_path))
    absolute = tmp_path / "elsewhere" / "mai-root"
    assert resolve_profiles_root({"MAI_DATA_ROOT": str(absolute)}) == str(absolute)
    assert resolve_profiles_root({"MAI_DATA_ROOT": "~/custom-root"}) == str(
        tmp_path / "custom-root"
    )


# ---------------------------------------------------------------------------
# Persisted state survives a fresh app instance (restart half of AC1/AC2)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_state_survives_a_fresh_app_instance(migrated_url: str, tmp_path: Path):
    _truncate(migrated_url)
    data_root = tmp_path / "data-root"
    first = _build_app(migrated_url, data_root)
    async with _running(first):
        jar = await login_session(first)
        alpha = await create_server(first, jar, _profile_body("alpha"))
        beta = await create_server(
            first, jar, _profile_body("beta", host_key=HOST_KEY_B)
        )
        alpha_id = uuid.UUID(alpha.json()["server_id"])
        beta_id = uuid.UUID(beta.json()["server_id"])
        edited = await edit_server(
            first,
            jar,
            alpha_id,
            _profile_body("alpha", prompt_revision=2),
            expected_revision=1,
        )
        assert edited.status == 200
        stale = await edit_server(
            first, jar, alpha_id, _profile_body("alpha"), expected_revision=0
        )
        assert stale.status == 409
    await first.state.engine.dispose()

    # A brand-new app process (same DB, same root) observes everything.
    second = _build_app(migrated_url, data_root)
    async with _running(second):
        fresh = await call_app(
            second,
            "POST",
            "/api/v1/auth/login",
            json_body={"password": FIXTURE_PASSWORD},
            headers={"origin": ORIGIN},
        )
        assert fresh.status == 204
        new_jar = _set_cookies(fresh)
        got_alpha = await get_server(second, new_jar, alpha_id)
        got_beta = await get_server(second, new_jar, beta_id)
        assert got_alpha.status == 200
        assert got_beta.status == 200
        assert got_alpha.json()["revision"] == 2
        assert got_alpha.json()["prompt_revision"] == 2
        assert got_beta.json()["revision"] == 1
    await second.state.engine.dispose()
