"""Focused tests for the model HTTP API (card 06-model-api).

Card ``06-model-api`` against contracts.md **C03** (credential-reference
only), **C06** (configuration/selection separation, capability gates,
classified probe outcomes), **C08** (session/origin/CSRF boundary,
registration under the shared app factory), **C01** (envelope, revisions),
**C02** (confined atomic store semantics through the reviewed
``mai.agents.model_store``).

Boundary discipline (test policy): every HTTP claim runs through the
real ASGI app (``create_app`` + lifespan + middleware + handlers) against
a real disposable Postgres (fixture ``migrated_url``) through the real
linear Alembic chain. The probe endpoints face a REAL local HTTP
endpoint (an in-process ``http.server`` socket speaking the OpenAI
chat-completions wire format) so reachability/auth/payload outcomes are
classified from actual transport status codes — not from mocked
exceptions. Live provider runs stay AGT-PROVIDER (P06 gate, owner
``06-verify``).

Scenario coverage owned here (writer half; assembly is ``06-verify``):
authenticated config/version updates and selection lifecycle, bounded
probe jobs returning distinct reachability/auth/tool outcomes,
credential-reference-only responses (token values never returned), the
unsafe-URL and inline-credential negative controls, and the session
boundary.

Registered limitations: no live external provider (the fixture endpoint
is a real local socket with canned OpenAI-shaped responses); no browser
(UI journey is ``06-web-models``).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

import mai.api.models as models_surface
from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE

#: The suites run async tests through the anyio pytest plugin (the repo's
#: async test convention); sync tests stay unmarked and pass through.
pytestmark = pytest.mark.anyio

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: Synthetic fixture credentials (NOT secrets; disposable fixtures only).
FIXTURE_PASSWORD = "fixture-password-06-model-api-only"
FIXTURE_SECRET = "fixture-model-key-06-model-api-only"
#: The credential REFERENCE the fixture connection declares (C03 rule 6).
FIXTURE_CREDENTIAL_REF = "MAI_FIXTURE_MODEL_KEY"

ORIGIN = "http://127.0.0.1:8443"  # loopback allowlist entry
EVIL_ORIGIN = "https://attacker.example"

#: A provider response-body marker: must NEVER appear in any HTTP
#: response of this surface (C01 rule 3 / acceptance 2).
ENDPOINT_BODY_CANARY = "ENDPOINT-BODY-CANARY-never-echo-4f1a"

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = Path("/tmp/mai-fxt-06-model-api")


# ---------------------------------------------------------------------------
# helpers: persistent test loop + ASGI harness (no test-client dependency)
# ---------------------------------------------------------------------------

_TEST_LOOP: asyncio.AbstractEventLoop | None = None
_TEST_LOOP_LOCK = threading.Lock()


def _run(coro: object) -> object:
    """Run one coroutine on the persistent test loop; block for the result."""
    global _TEST_LOOP
    with _TEST_LOOP_LOCK:
        if _TEST_LOOP is None or _TEST_LOOP.is_closed():
            _TEST_LOOP = asyncio.new_event_loop()
            thread = threading.Thread(target=_TEST_LOOP.run_forever, daemon=True)
            thread.start()
    return asyncio.run_coroutine_threadsafe(coro, _TEST_LOOP).result()


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


def _scope(method: str, path: str, headers: dict[str, str], body: bytes) -> dict:
    scope_headers = [(b"host", b"fixture")]
    scope_headers.extend(
        (key.encode("ascii").lower(), value.encode("ascii"))
        for key, value in headers.items()
    )
    url, _, query = path.partition("?")
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": url,
        "raw_path": path.encode("ascii"),
        "query_string": query.encode("ascii"),
        "root_path": "",
        "headers": scope_headers,
        "client": ("127.0.0.1", 0),
        "server": ("fixture", 80),
    }


async def call_app(
    app: FastAPI,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> HttpResponse:
    """Run one bounded HTTP request through the real ASGI callable."""
    flat_headers = dict(headers or {})
    if cookies:
        flat_headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    body = b""
    if json_body is not None:
        body = json.dumps(json_body).encode()
        flat_headers["content-type"] = "application/json"
    response = HttpResponse()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            response.status = message["status"]
            response.headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            response.body += message.get("body", b"")

    await app(_scope(method, path, flat_headers, body), receive, send)
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


def _mutation_headers(jar: dict[str, str]) -> dict[str, str]:
    return {"origin": ORIGIN, "x-csrf-token": jar[CSRF_COOKIE]}


def _read_cookies(jar: dict[str, str]) -> dict[str, str]:
    return {SESSION_COOKIE: jar[SESSION_COOKIE], CSRF_COOKIE: jar[CSRF_COOKIE]}


async def put_connection(
    app: FastAPI,
    jar: dict[str, str],
    name: str,
    payload: dict[str, Any],
    *,
    expected_revision: int = 0,
) -> HttpResponse:
    body = dict(payload)
    body["expected_revision"] = expected_revision
    return await call_app(
        app,
        "PUT",
        f"/api/v1/models/connections/{name}",
        json_body=body,
        headers=_mutation_headers(jar),
        cookies=_read_cookies(jar),
    )


async def put_profile(
    app: FastAPI,
    jar: dict[str, str],
    name: str,
    payload: dict[str, Any],
    *,
    expected_revision: int = 0,
) -> HttpResponse:
    body = dict(payload)
    body["expected_revision"] = expected_revision
    return await call_app(
        app,
        "PUT",
        f"/api/v1/models/profiles/{name}",
        json_body=body,
        headers=_mutation_headers(jar),
        cookies=_read_cookies(jar),
    )


CONNECTION_OK: dict[str, Any] = {
    "base_url": "http://127.0.0.1:9/v1",
    "timeout_seconds": 2,
    "capabilities": {"tool_calls": True},
}
PROFILE_OK: dict[str, Any] = {"connection": "gateway", "model": "fixture-model"}


async def seed_pair(
    app: FastAPI,
    jar: dict[str, str],
    *,
    connection: str = "gateway",
    profile: str = "fast",
    capabilities: dict[str, bool] | None = None,
    profile_overrides: dict[str, Any] | None = None,
) -> None:
    """Store one valid (connection, profile) pair through the real surface."""
    payload = dict(CONNECTION_OK)
    if capabilities is not None:
        payload["capabilities"] = capabilities
    created = await put_connection(app, jar, connection, payload)
    assert created.status == 200, created.body
    document = dict(PROFILE_OK)
    document["connection"] = connection
    document.update(profile_overrides or {})
    profiled = await put_profile(app, jar, profile, document)
    assert profiled.status == 200, profiled.body


# ---------------------------------------------------------------------------
# the real local OpenAI-compatible fixture endpoint (real socket, canned
# chat-completions responses; NOT a mock of the SDK/producer code paths)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StubEndpoint:
    """One live local HTTP endpoint scripted per probe scenario."""

    server: ThreadingHTTPServer
    thread: threading.Thread
    seen: list[dict[str, Any]] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _completion(text: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-fixture",
        "object": "chat.completion",
        "created": 1,
        "model": "fixture-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _tool_completion() -> dict[str, Any]:
    return {
        "id": "chatcmpl-fixture",
        "object": "chat.completion",
        "created": 1,
        "model": "fixture-model",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_fixture_1",
                            "type": "function",
                            "function": {"name": "probe_ping", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _unauthorized() -> tuple[int, dict[str, Any], float]:
    return (
        401,
        {
            "error": {
                "message": ENDPOINT_BODY_CANARY,
                "type": "invalid_request_error",
                "code": "invalid_api_key",
            }
        },
        0.0,
    )


def _bad_request() -> tuple[int, dict[str, Any], float]:
    return (
        400,
        {
            "error": {
                "message": ENDPOINT_BODY_CANARY,
                "type": "invalid_request_error",
            }
        },
        0.0,
    )


def stub_endpoint(
    respond,  # (call_index, captured) -> (status, payload, delay)
) -> StubEndpoint:
    """Launch one scripted endpoint on an ephemeral loopback port."""
    seen: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = {}
            seen.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": body,
                }
            )
            status, payload, delay = respond(len(seen), seen[-1])
            if delay:
                time.sleep(delay)
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:  # silence fixture noise
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return StubEndpoint(server=server, thread=thread, seen=seen)


def _refused_port() -> int:
    """A loopback port with no listener (bind, observe, release)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# disposable fixture database (the sibling-card harness shape)
# ---------------------------------------------------------------------------


def _alembic_upgrade() -> None:
    from alembic import command
    from alembic.config import Config as AlembicConfig

    config = AlembicConfig(os.path.join(BACKEND_ROOT, "alembic.ini"))
    command.upgrade(config, "head")


def _drop_app_tables(url: str) -> None:
    import psycopg

    with psycopg.connect(url) as conn:
        # Whole-schema reset (D09): FK-safe and immune to future
        # revisions (the enumerated drop list went stale across
        # migrations).
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
            f"{LIVE_URL_VAR} is not set; the model HTTP surface claims need "
            "a real disposable Postgres fixture"
        )
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        _drop_app_tables(url)
        _alembic_upgrade()
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    return url


@pytest.fixture(scope="session")
def bootstrapped_url(migrated_url: str) -> str:
    """Create the engine-owned ``dbos`` schema the way production does."""
    from mai.jobs import engine as jobs_engine

    jobs_engine.launch_worker(migrated_url)
    jobs_engine.shutdown()
    return migrated_url


async def _truncate(url: str) -> None:
    """Reset the facade tables between tests (the session-scoped DB stays)."""
    from sqlalchemy import text

    from mai.database import create_app_engine

    engine, _sessions = create_app_engine(url)
    try:
        async with engine.begin() as conn:
            for table in (
                "job_resource_locks",
                "job_schedule_fires",
                "job_steps",
                "job_events",
                "job_schedules",
                "job_runs",
                "approvals",
                "profile_projection_journal",
                "profile_projection",
                "admin_sessions",
                "admin_credential",
            ):
                present = await conn.execute(
                    text("SELECT to_regclass(:t) IS NOT NULL"),
                    {"t": f"public.{table}"},
                )
                if not present.scalar():
                    continue
                await conn.execute(text(f"DELETE FROM {table}"))
    finally:
        await engine.dispose()


@pytest.fixture
async def models_app(bootstrapped_url: str, tmp_path: Path):
    """App on the real fixture DB with a fresh confined data root + .env."""
    await _truncate(bootstrapped_url)
    env_file = tmp_path / "fixtures.env"
    env_file.write_text(f"{FIXTURE_CREDENTIAL_REF}={FIXTURE_SECRET}\n")
    os.chmod(env_file, 0o600)  # the resolver's fail-closed permission guard
    app = create_app(
        env={
            "MAI_DATABASE_URL": bootstrapped_url,
            "MAI_PUBLIC_ORIGINS": ORIGIN,
            "MAI_DATA_ROOT": str(tmp_path / "data-root"),
            "MAI_ENV_FILE": str(env_file),
        }
    )
    async with app.router.lifespan_context(app):
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()


@pytest.fixture
async def jar(models_app: FastAPI) -> dict[str, str]:
    return await login_session(models_app)


# ---------------------------------------------------------------------------
# pure surface tests (no fixture DB required)
# ---------------------------------------------------------------------------


class TestProbePolicy:
    def test_probe_bounds_are_module_frozen(self) -> None:
        """The probe is a bounded diagnostic: hard deadline, small output."""
        assert 0 < models_surface.PROBE_DEADLINE_SECONDS <= 30
        assert 0 < models_surface.PROBE_MAX_OUTPUT_TOKENS <= 128


class TestBodyShapes:
    def test_unknown_fields_rejected_everywhere(self) -> None:
        """Schema-strict bodies: unknown fields are rejected, never ignored."""
        for body_class, seed in (
            (models_surface.ModelConnectionBody, {"base_url": "http://e/v1"}),
            (models_surface.ModelProfileBody, {"connection": "c", "model": "m"}),
            (
                models_surface.ModelSelectionBody,
                {"connection": "c", "profile": "p"},
            ),
            (models_surface.ModelProbeBody, {}),
        ):
            with pytest.raises(ValidationError):
                body_class.model_validate({**seed, "api_key": "sk-forged"})  # type: ignore[call-arg]

    def test_probe_defaults_to_the_stored_selection(self) -> None:
        body = models_surface.ModelProbeBody()
        assert body.connection is None
        assert body.profile is None
        assert body.tools is False

    def test_connection_body_has_no_credential_value_field(self) -> None:
        """C03 rule 6 at the wire: no credential-value field exists at all."""
        assert "credential_ref" in models_surface.ModelConnectionBody.model_fields
        assert "api_key" not in models_surface.ModelConnectionBody.model_fields
        assert "token" not in models_surface.ModelConnectionBody.model_fields


def test_surface_registers_under_the_shared_boundary() -> None:
    """Real app registration (acceptance 3): every route joins /api/v1 with
    the documented contract (routes exist only through real registration)."""
    schema = create_app(env={}).openapi()
    model_paths = {
        path for path in schema["paths"] if path.startswith("/api/v1/models")
    }
    expected = {
        "/api/v1/models/connections",
        "/api/v1/models/connections/{name}",
        "/api/v1/models/profiles",
        "/api/v1/models/profiles/{name}",
        "/api/v1/models/selection",
        "/api/v1/models/test",
    }
    assert model_paths == expected
    components = set(schema["components"]["schemas"])
    assert "ModelProbeOutcome" in components
    assert "ModelConnectionView" in components
    # Component-namespace discipline (the 06-chat-api lesson): this card's
    # wire types are all Model-prefixed, so no earlier component may be
    # silently replaced by a same-named later one. (The ChatCancelBody
    # witness died with the D09 chat deletion; RunSubmitBody is retained.)
    assert "RunSubmitBody" in components
    assert "CancelBody" in components


# ---------------------------------------------------------------------------
# live boundary tests (real fixture DB; session/origin/CSRF + store)
# ---------------------------------------------------------------------------


async def test_models_boundary_session_origin_csrf(models_app, jar) -> None:
    """AUTH-S3 on the model surface: session → origin → CSRF, in order."""
    no_session = await call_app(models_app, "GET", "/api/v1/models/connections")
    assert no_session.status == 401
    assert _error(no_session)["code"] == "unauthenticated"

    no_origin = await call_app(
        models_app,
        "PUT",
        "/api/v1/models/connections/gateway",
        json_body={**CONNECTION_OK, "expected_revision": 0},
        cookies=_read_cookies(jar),
    )
    assert no_origin.status == 403
    assert _error(no_origin)["code"] == "origin_rejected"

    evil = await call_app(
        models_app,
        "PUT",
        "/api/v1/models/connections/gateway",
        json_body={**CONNECTION_OK, "expected_revision": 0},
        headers={"origin": EVIL_ORIGIN, "x-csrf-token": jar[CSRF_COOKIE]},
        cookies=_read_cookies(jar),
    )
    assert evil.status == 403
    assert _error(evil)["code"] == "origin_rejected"

    bad_csrf = await call_app(
        models_app,
        "PUT",
        "/api/v1/models/connections/gateway",
        json_body={**CONNECTION_OK, "expected_revision": 0},
        headers={"origin": ORIGIN, "x-csrf-token": "forged"},
        cookies=_read_cookies(jar),
    )
    assert bad_csrf.status == 403

    # Safe methods apply the session layer only.
    read = await call_app(
        models_app, "GET", "/api/v1/models/connections", cookies=_read_cookies(jar)
    )
    assert read.status == 200


async def test_models_store_absent_fails_closed(models_app, jar) -> None:
    """An unusable store root never touches state: fail closed 503."""
    models_app.state.models = None
    response = await call_app(
        models_app, "GET", "/api/v1/models/connections", cookies=_read_cookies(jar)
    )
    assert response.status == 503
    assert _error(response)["code"] == "unavailable"


async def test_connection_config_roundtrip_revisions_and_rejections(
    models_app, jar
) -> None:
    """AC1: authenticated config/version updates; producer-owned 422s."""
    created = await put_connection(models_app, jar, "gateway", CONNECTION_OK)
    assert created.status == 200
    first = created.json()
    assert first["name"] == "gateway"
    assert first["revision"] == 1
    assert first["schema_revision"] == 1
    assert len(first["sha256"]) == 64

    view = await call_app(
        models_app,
        "GET",
        "/api/v1/models/connections/gateway",
        cookies=_read_cookies(jar),
    )
    assert view.status == 200
    document = view.json()
    assert document["base_url"] == CONNECTION_OK["base_url"]
    assert document["credential_ref"] is None
    assert document["capabilities"] == {"tool_calls": True, "reasoning": False}
    assert document["revision"] == 1

    # List carries the value-free reference only.
    listing = await call_app(
        models_app, "GET", "/api/v1/models/connections", cookies=_read_cookies(jar)
    )
    assert listing.status == 200
    entry = listing.json()["connections"][0]
    assert set(entry) == {"name", "revision", "schema_revision", "sha256"}

    updated = await put_connection(
        models_app, jar, "gateway", CONNECTION_OK, expected_revision=1
    )
    assert updated.status == 200
    assert updated.json()["revision"] == 2

    stale = await put_connection(
        models_app, jar, "gateway", CONNECTION_OK, expected_revision=1
    )
    assert stale.status == 409
    assert _error(stale)["code"] == "conflict"

    # Unsafe-URL negative controls: producer-classified, value-free 422s.
    unsafe_schemes = [
        ("ftp://endpoint/v1", "base_url", "invalid_field"),
        ("http://user:pass@endpoint/v1", "base_url", "inline_credential_forbidden"),
        ("http://endpoint/v1?x=1", "base_url", "invalid_field"),
        ("http://endpoint/v1#frag", "base_url", "invalid_field"),
        ("not a url", "base_url", "invalid_field"),
    ]
    for base_url, field_name, code in unsafe_schemes:
        rejected = await put_connection(
            models_app, jar, "badurl", {"base_url": base_url}
        )
        assert rejected.status == 422, (base_url, rejected.body)
        assert _error(rejected)["fields"] == {field_name: code}

    # Inline credential material is refused at the wire AND the producer.
    inline = await put_connection(
        models_app,
        jar,
        "gateway",
        {**CONNECTION_OK, "api_key": "sk-forged-value"},
        expected_revision=2,
    )
    assert inline.status == 422  # strict body: no api_key field exists
    credential_header = await put_connection(
        models_app,
        jar,
        "gateway",
        {**CONNECTION_OK, "headers": {"X-Api-Key": "forged"}},
        expected_revision=2,
    )
    assert credential_header.status == 422
    assert (
        _error(credential_header)["fields"]["headers.X-Api-Key"]
        == "inline_credential_forbidden"
    )

    invalid_name = await put_connection(models_app, jar, "UPPER-case", CONNECTION_OK)
    assert invalid_name.status == 422
    assert _error(invalid_name)["fields"] == {"document": "invalid_name"}

    deleted = await call_app(
        models_app,
        "DELETE",
        "/api/v1/models/connections/gateway?expected_revision=9",
        headers=_mutation_headers(jar),
        cookies=_read_cookies(jar),
    )
    assert deleted.status == 409
    deleted = await call_app(
        models_app,
        "DELETE",
        "/api/v1/models/connections/gateway?expected_revision=2",
        headers=_mutation_headers(jar),
        cookies=_read_cookies(jar),
    )
    assert deleted.status == 204
    gone = await call_app(
        models_app,
        "GET",
        "/api/v1/models/connections/gateway",
        cookies=_read_cookies(jar),
    )
    assert gone.status == 404
    assert _error(gone)["code"] == "not_found"


async def test_profile_roundtrip_and_selection_lifecycle(models_app, jar) -> None:
    """AC1: profile config, validated selection, conflict + gate outcomes."""
    await seed_pair(models_app, jar)  # gateway(tool_calls) + fast

    selected = await call_app(
        models_app,
        "PUT",
        "/api/v1/models/selection",
        json_body={"connection": "gateway", "profile": "fast", "expected_revision": 0},
        headers=_mutation_headers(jar),
        cookies=_read_cookies(jar),
    )
    assert selected.status == 200
    assert selected.json()["connection"] == "gateway"
    assert selected.json()["revision"] == 1

    current = await call_app(
        models_app, "GET", "/api/v1/models/selection", cookies=_read_cookies(jar)
    )
    assert current.status == 200
    assert current.json()["profile"] == "fast"

    stale = await call_app(
        models_app,
        "PUT",
        "/api/v1/models/selection",
        json_body={"connection": "gateway", "profile": "fast", "expected_revision": 0},
        headers=_mutation_headers(jar),
        cookies=_read_cookies(jar),
    )
    assert stale.status == 409

    profile_view = await call_app(
        models_app, "GET", "/api/v1/models/profiles/fast", cookies=_read_cookies(jar)
    )
    assert profile_view.status == 200
    assert profile_view.json()["model"] == PROFILE_OK["model"]

    # Pairing gate: the profile names a different connection.
    await put_connection(models_app, jar, "other", CONNECTION_OK)
    await put_profile(models_app, jar, "stray", {"connection": "nope", "model": "m"})
    mismatch = await call_app(
        models_app,
        "PUT",
        "/api/v1/models/selection",
        json_body={"connection": "gateway", "profile": "stray", "expected_revision": 1},
        headers=_mutation_headers(jar),
        cookies=_read_cookies(jar),
    )
    assert mismatch.status == 422
    assert _error(mismatch)["fields"] == {"connection": "invalid_selection"}

    # Capability gate: reasoning_effort without the declared capability.
    await put_profile(
        models_app,
        jar,
        "thinker",
        {"connection": "gateway", "model": "m", "reasoning_effort": "high"},
    )
    gated = await call_app(
        models_app,
        "PUT",
        "/api/v1/models/selection",
        json_body={"connection": "gateway", "profile": "thinker", "expected_revision": 1},
        headers=_mutation_headers(jar),
        cookies=_read_cookies(jar),
    )
    assert gated.status == 422
    assert _error(gated)["fields"] == {"reasoning_effort": "unsupported_option"}

    # Deleting a selection-named record is refused; re-point first.
    dangling = await call_app(
        models_app,
        "DELETE",
        "/api/v1/models/connections/gateway?expected_revision=1",
        headers=_mutation_headers(jar),
        cookies=_read_cookies(jar),
    )
    assert dangling.status == 409
    assert "selection" in _error(dangling)["message"]

    re_pointed_ok = await put_profile(
        models_app, jar, "otherfast", {"connection": "other", "model": "m"}
    )
    assert re_pointed_ok.status == 200
    re_pointed = await call_app(
        models_app,
        "PUT",
        "/api/v1/models/selection",
        json_body={
            "connection": "other",
            "profile": "otherfast",
            "expected_revision": 1,
        },
        headers=_mutation_headers(jar),
        cookies=_read_cookies(jar),
    )
    assert re_pointed.status == 200
    freed = await call_app(
        models_app,
        "DELETE",
        "/api/v1/models/connections/gateway?expected_revision=1",
        headers=_mutation_headers(jar),
        cookies=_read_cookies(jar),
    )
    assert freed.status == 204

    cleared = await call_app(
        models_app,
        "DELETE",
        "/api/v1/models/selection?expected_revision=2",
        headers=_mutation_headers(jar),
        cookies=_read_cookies(jar),
    )
    assert cleared.status == 204
    empty = await call_app(
        models_app, "GET", "/api/v1/models/selection", cookies=_read_cookies(jar)
    )
    assert empty.status == 404


async def test_unknown_records_fail_classified(models_app, jar) -> None:
    """404s and the no-selection probe stay classified, never a 500."""
    missing = await call_app(
        models_app,
        "GET",
        "/api/v1/models/connections/ghost",
        cookies=_read_cookies(jar),
    )
    assert missing.status == 404
    probe_unknown = await call_app(
        models_app,
        "POST",
        "/api/v1/models/test",
        json_body={"connection": "ghost", "profile": "ghost"},
        headers=_mutation_headers(jar),
        cookies=_read_cookies(jar),
    )
    assert probe_unknown.status == 404
    no_selection = await call_app(
        models_app,
        "POST",
        "/api/v1/models/test",
        json_body={},
        headers=_mutation_headers(jar),
        cookies=_read_cookies(jar),
    )
    assert no_selection.status == 404


async def test_probe_reports_distinct_outcomes(models_app, jar) -> None:
    """AC1: one bounded probe per failure class — distinct, value-free."""
    await seed_pair(models_app, jar)
    await call_app(
        models_app,
        "PUT",
        "/api/v1/models/selection",
        json_body={"connection": "gateway", "profile": "fast", "expected_revision": 0},
        headers=_mutation_headers(jar),
        cookies=_read_cookies(jar),
    )

    # ok — the endpoint answers the bounded round.
    ok_endpoint = stub_endpoint(lambda index, seen: _completion_ok())
    try:
        ok_conn = await put_connection(
            models_app,
            jar,
            "gateway",
            {**CONNECTION_OK, "base_url": ok_endpoint.base_url},
            expected_revision=1,
        )
        assert ok_conn.status == 200
        ok = await call_app(
            models_app,
            "POST",
            "/api/v1/models/test",
            json_body={},
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        assert ok.status == 200
        outcome = ok.json()
        assert outcome["outcome"] == "ok"
        assert outcome["connection"] == "gateway"
        assert outcome["profile"] == "fast"
        assert outcome["model"] == "fixture-model"
        assert outcome["connection_revision"] == 2
        assert outcome["profile_revision"] == 1
        assert isinstance(outcome["latency_ms"], int)
    finally:
        ok_endpoint.stop()

    # auth — the endpoint rejects the RESOLVED credential (C03 rule 6:
    # the .env value reached the endpoint; the endpoint body never comes
    # back through any response).
    auth_endpoint = stub_endpoint(lambda index, seen: _unauthorized())
    try:
        cred_conn = await put_connection(
            models_app,
            jar,
            "gateway",
            {
                "base_url": auth_endpoint.base_url,
                "credential_ref": FIXTURE_CREDENTIAL_REF,
                "timeout_seconds": 2,
            },
            expected_revision=2,
        )
        assert cred_conn.status == 200
        auth = await call_app(
            models_app,
            "POST",
            "/api/v1/models/test",
            json_body={},
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        assert auth.status == 200
        outcome = auth.json()
        assert outcome["outcome"] == "auth"
        assert outcome["detail"]["status_code"] == "401"
        assert FIXTURE_SECRET not in auth.body.decode()
        assert ENDPOINT_BODY_CANARY not in auth.body.decode()
        # The endpoint really received the resolved material (call-time
        # resolution through the configured .env source only).
        assert auth_endpoint.seen[0]["authorization"] == f"Bearer {FIXTURE_SECRET}"
    finally:
        auth_endpoint.stop()

    # rejected_request — the endpoint refused the payload (the tool/400
    # class); the response stays value-free.
    reject_endpoint = stub_endpoint(lambda index, seen: _bad_request())
    try:
        await put_connection(
            models_app,
            jar,
            "gateway",
            {"base_url": reject_endpoint.base_url, "timeout_seconds": 2},
            expected_revision=3,
        )
        rejected = await call_app(
            models_app,
            "POST",
            "/api/v1/models/test",
            json_body={},
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        assert rejected.status == 200
        outcome = rejected.json()
        assert outcome["outcome"] == "rejected_request"
        assert outcome["detail"]["status_code"] == "400"
        assert ENDPOINT_BODY_CANARY not in rejected.body.decode()
    finally:
        reject_endpoint.stop()

    # reachability — nothing listens on the port (connection refused).
    await put_connection(
        models_app,
        jar,
        "gateway",
        {"base_url": f"http://127.0.0.1:{_refused_port()}/v1", "timeout_seconds": 2},
        expected_revision=4,
    )
    unreachable = await call_app(
        models_app,
        "POST",
        "/api/v1/models/test",
        json_body={},
        headers=_mutation_headers(jar),
        cookies=_read_cookies(jar),
    )
    assert unreachable.status == 200
    outcome = unreachable.json()
    assert outcome["outcome"] == "reachability"

    # The four classes are distinct outcomes (acceptance 1).
    assert {ok.json()["outcome"], auth.json()["outcome"],
            rejected.json()["outcome"], outcome["outcome"]} == {
        "ok",
        "auth",
        "rejected_request",
        "reachability",
    }


def _completion_ok() -> tuple[int, dict[str, Any], float]:
    return (200, _completion("ok"), 0.0)


async def test_probe_tool_smoke_round_trips(models_app, jar) -> None:
    """AC1 (tool): the smoke round really carries a tool call and returns."""
    calls: list[dict[str, Any]] = []

    def respond(index: int, seen: dict[str, Any]) -> tuple[int, dict[str, Any], float]:
        calls.append(seen)
        if index == 1:
            return (200, _tool_completion(), 0.0)
        return (200, _completion("ok"), 0.0)

    endpoint = stub_endpoint(respond)
    try:
        await seed_pair(models_app, jar)  # gateway declares tool_calls: True
        await put_connection(
            models_app,
            jar,
            "gateway",
            {"base_url": endpoint.base_url, "timeout_seconds": 2,
             "capabilities": {"tool_calls": True}},
            expected_revision=1,
        )
        smoke = await call_app(
            models_app,
            "POST",
            "/api/v1/models/test",
            json_body={"tools": True, "connection": "gateway", "profile": "fast"},
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        assert smoke.status == 200, smoke.body
        outcome = smoke.json()
        assert outcome["outcome"] == "ok"
        assert outcome["tools"] is True
        # The endpoint really saw a tool-carrying request, then the local
        # tool result on the follow-up round.
        assert "tools" in endpoint.seen[0]["body"]
        assert len(endpoint.seen) == 2
    finally:
        endpoint.stop()


async def test_probe_tool_gate_blocks_undeclared_capability(models_app, jar) -> None:
    """C06 rule 1: a tool probe without the declared capability is a 422
    BEFORE any network effect."""
    await seed_pair(
        models_app, jar, capabilities={"tool_calls": False}
    )
    endpoint = stub_endpoint(lambda index, seen: _completion_ok())
    try:
        await put_connection(
            models_app,
            jar,
            "gateway",
            {"base_url": endpoint.base_url, "timeout_seconds": 2},
            expected_revision=1,
        )
        gated = await call_app(
            models_app,
            "POST",
            "/api/v1/models/test",
            json_body={"tools": True, "connection": "gateway", "profile": "fast"},
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        assert gated.status == 422
        assert _error(gated)["fields"] == {"tools": "unsupported_option"}
        assert endpoint.seen == []  # no network effect happened
    finally:
        endpoint.stop()


async def test_probe_deadline_classifies_hanging_endpoint(
    models_app, jar, monkeypatch
) -> None:
    """A hung endpoint is a bounded reachability outcome, never a pinned
    request (acceptance 2: bounded test jobs)."""
    monkeypatch.setattr(models_surface, "PROBE_DEADLINE_SECONDS", 1.0)
    await seed_pair(models_app, jar)
    endpoint = stub_endpoint(lambda index, seen: (200, _completion("ok"), 5.0))
    try:
        await put_connection(
            models_app,
            jar,
            "gateway",
            {"base_url": endpoint.base_url, "timeout_seconds": 10},
            expected_revision=1,
        )
        started = time.monotonic()
        hung = await call_app(
            models_app,
            "POST",
            "/api/v1/models/test",
            json_body={"connection": "gateway", "profile": "fast"},
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        elapsed = time.monotonic() - started
        assert hung.status == 200
        outcome = hung.json()
        assert outcome["outcome"] == "reachability"
        assert outcome["detail"]["reason"] == "probe_deadline_exceeded"
        assert elapsed < 5  # the probe deadline bounded the request
    finally:
        endpoint.stop()


async def test_credential_values_never_returned(models_app, jar) -> None:
    """AC2: no route returns token values; the .env never reaches a body."""
    endpoint = stub_endpoint(lambda index, seen: _unauthorized())
    try:
        await seed_pair(models_app, jar)
        await put_connection(
            models_app,
            jar,
            "gateway",
            {
                "base_url": endpoint.base_url,
                "credential_ref": FIXTURE_CREDENTIAL_REF,
                "timeout_seconds": 2,
            },
            expected_revision=1,
        )
        probed = await call_app(
            models_app,
            "POST",
            "/api/v1/models/test",
            json_body={"connection": "gateway", "profile": "fast"},
            headers=_mutation_headers(jar),
            cookies=_read_cookies(jar),
        )
        assert probed.status == 200
        assert FIXTURE_SECRET not in probed.body.decode()

        # The stored view carries the REFERENCE only.
        view = await call_app(
            models_app,
            "GET",
            "/api/v1/models/connections/gateway",
            cookies=_read_cookies(jar),
        )
        assert view.status == 200
        assert view.json()["credential_ref"] == FIXTURE_CREDENTIAL_REF
        assert FIXTURE_SECRET not in view.body.decode()

        # A producer rejection stays value-free too.
        rejected = await put_connection(
            models_app,
            jar,
            "gateway",
            {**CONNECTION_OK, "base_url": "http://user:pass@h/v1"},
            expected_revision=2,
        )
        assert rejected.status == 422
        assert FIXTURE_SECRET not in rejected.body.decode()
    finally:
        endpoint.stop()
