"""Focused tests for the MAI API lifecycle and health surface (card 01-api).

The app is exercised through its real ASGI interface (raw scope/receive/send
calls against ``create_app()`` — registration, lifespan, middleware, error
handlers all run; no test-client library is added to the locked dependency
lane). Live-Postgres halves are gated on ``MAI_TEST_DATABASE_URL`` exactly
like ``tests/test_database.py``.

Covered acceptance (brief 01-api):

1. lifespan starts/stops dependencies; ``/api/v1`` prefix exported; safe
   structured errors (C01 envelope, no traceback/secret material);
2. liveness distinct from dependency readiness; route table contains no
   unauthenticated privileged operation (G1: two GET health routes only);
3. routes exposed through real app registration; generated OpenAPI contract
   verified against the implemented routes (no handwritten types).
"""

from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import dataclass, field
from typing import Any

import anyio
import pytest
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncEngine

import mai.api.app as app_module
from mai.api.app import API_PREFIX, HEALTH_PATH, READINESS_PATH, create_app
from mai.database import DatabaseHealth, DatabaseStatus

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

requires_live_postgres = pytest.mark.skipif(
    not os.environ.get(LIVE_URL_VAR),
    reason=f"{LIVE_URL_VAR} not set; live Postgres fixture absent",
)

WELL_FORMED_URL = "postgresql://mai:pw@localhost:5432/mai"
SECRET_MARKER = "must-never-appear"


def _closed_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --- minimal ASGI harness (no httpx/TestClient dependency) -------------------


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

    def json(self) -> Any:
        return json.loads(self.body)


async def call_asgi_http(app: Any, method: str, path: str) -> HttpResponse:
    """Run one HTTP request through the real ASGI callable."""
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
        "headers": [(b"host", b"fixture")],
        "client": ("127.0.0.1", 0),
        "server": ("fixture", 80),
    }
    response = HttpResponse()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            response.status = message["status"]
            response.headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            response.body += message.get("body", b"")

    # ServerErrorMiddleware re-raises after sending the handler response; the
    # response messages are the observable contract, so the raise is swallowed.
    try:
        await app(scope, receive, send)
    except Exception:  # noqa: BLE001 - see comment above
        pass
    return response


async def run_lifespan(app: Any) -> list[str]:
    """Drive ASGI lifespan startup+shutdown; return the app-sent messages."""
    received: list[str] = []
    sent: list[str] = []

    async def receive() -> dict[str, str]:
        if not received:
            received.append("startup")
            return {"type": "lifespan.startup"}
        received.append("shutdown")
        return {"type": "lifespan.shutdown"}

    async def send(message: dict[str, str]) -> None:
        sent.append(message["type"])

    await app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
    return sent


# --- registration: real app surface, no unauthenticated privilege (AC2) ------


def _route_table(app: Any) -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }


def test_app_registers_exactly_the_g1_health_surface() -> None:
    app = create_app(env={})
    assert _route_table(app) == {
        ("GET", HEALTH_PATH),
        ("GET", READINESS_PATH),
    }
    api_routes = [route for route in app.routes if isinstance(route, APIRoute)]
    assert len(api_routes) == 2  # no other surface is registered at P01
    for route in api_routes:
        assert route.methods <= {"GET", "HEAD"}  # no mutating/privileged verb
        assert route.path.startswith(API_PREFIX)
    # No public docs/OpenAPI listeners (G1 minimal unauthenticated surface).
    paths = {route.path for route in api_routes}
    assert "/docs" not in paths
    assert "/openapi.json" not in paths


@pytest.mark.anyio
async def test_unknown_route_and_method_get_structured_errors() -> None:
    app = create_app(env={})
    await run_lifespan(app)

    missing = await call_asgi_http(app, "GET", f"{API_PREFIX}/nope")
    assert missing.status == 404
    assert missing.json()["error"]["code"] == "not_found"
    assert missing.header("x-request-id")

    root = await call_asgi_http(app, "GET", "/")
    assert root.status == 404
    assert root.json()["error"]["code"] == "not_found"

    post = await call_asgi_http(app, "POST", HEALTH_PATH)
    assert post.status == 405
    assert post.json()["error"]["code"] == "method_not_allowed"


# --- OpenAPI contract (AC3): generated schema matches implemented routes -----


def test_openapi_contract_matches_implemented_routes() -> None:
    app = create_app(env={})
    schema = app.openapi()
    assert schema["openapi"].startswith("3.")
    assert set(schema["paths"]) == {HEALTH_PATH, READINESS_PATH}
    assert set(schema["paths"][HEALTH_PATH]) == {"get"}
    assert "200" in schema["paths"][HEALTH_PATH]["get"]["responses"]
    ready = schema["paths"][READINESS_PATH]["get"]
    assert set(ready["responses"]) == {"200", "503"}


# --- lifespan: starts/stops the dependency engine (AC1) ----------------------


@pytest.mark.anyio
async def test_lifespan_starts_and_disposes_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Observe the dependency stop through the public dispose API.
    dispose_calls: list[Any] = []
    original_dispose = AsyncEngine.dispose

    async def spy_dispose(self: AsyncEngine, **kwargs: Any) -> None:
        dispose_calls.append(self)
        await original_dispose(self, **kwargs)

    monkeypatch.setattr(AsyncEngine, "dispose", spy_dispose)

    app = create_app(env={"MAI_DATABASE_URL": WELL_FORMED_URL})
    sent = await run_lifespan(app)
    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    assert app.state.database_url == WELL_FORMED_URL
    assert app.state.database_config_error is None
    assert app.state.engine is not None
    assert app.state.session_factory is not None
    # The lifespan disposed exactly the engine it started (dependency stop).
    assert dispose_calls == [app.state.engine]


@pytest.mark.anyio
async def test_lifespan_survives_missing_database_config() -> None:
    app = create_app(env={})
    sent = await run_lifespan(app)  # startup must not fail
    assert "lifespan.startup.failed" not in sent
    assert app.state.database_url is None
    assert app.state.database_config_error is DatabaseHealth.NOT_CONFIGURED


@pytest.mark.anyio
async def test_lifespan_survives_rejected_database_config() -> None:
    app = create_app(env={"MAI_DATABASE_URL": "mysql://mai:pw@localhost:5432/mai"})
    sent = await run_lifespan(app)
    assert "lifespan.startup.failed" not in sent
    assert app.state.database_url is None
    assert app.state.database_config_error is DatabaseHealth.INVALID_CONFIG


# --- liveness: never touches dependencies (AC2) ------------------------------


@pytest.mark.anyio
async def test_liveness_answers_without_any_dependency() -> None:
    app = create_app(env={})  # nothing configured, no engine exists
    await run_lifespan(app)
    response = await call_asgi_http(app, "GET", HEALTH_PATH)
    assert response.status == 200
    assert response.json() == {"status": "ok"}


# --- readiness: classified, secret-free dependency truth (AC1/AC2) -----------


@pytest.mark.anyio
async def test_readiness_not_configured() -> None:
    app = create_app(env={})
    await run_lifespan(app)
    response = await call_asgi_http(app, "GET", READINESS_PATH)
    assert response.status == 503
    assert response.json() == {"status": "unhealthy", "database": "not_configured"}


@pytest.mark.anyio
async def test_readiness_invalid_config_is_rejected_without_echoing_the_url() -> None:
    app = create_app(env={"MAI_DATABASE_URL": "mysql://mai:pw@localhost:5432/mai"})
    await run_lifespan(app)
    response = await call_asgi_http(app, "GET", READINESS_PATH)
    assert response.status == 503
    assert response.json() == {"status": "unhealthy", "database": "invalid_config"}
    # Negative control: the rejected URL (incl. credentials) is not echoed.
    text = response.body.decode()
    assert "mysql" not in text
    assert "mai:pw" not in text


@pytest.mark.anyio
async def test_readiness_unreachable_reports_classified_failure_without_secrets() -> None:
    port = _closed_loopback_port()
    url = f"postgresql://mai:{SECRET_MARKER}@127.0.0.1:{port}/mai"
    app = create_app(env={"MAI_DATABASE_URL": url})
    await run_lifespan(app)
    response = await call_asgi_http(app, "GET", READINESS_PATH)
    assert response.status == 503
    assert response.json() == {"status": "unhealthy", "database": "unreachable"}
    # Negative control (real TCP connect against a closed loopback port):
    # neither the password nor the URL reaches the response body.
    text = response.body.decode()
    assert SECRET_MARKER not in text
    assert url not in text
    assert "Traceback" not in text


@pytest.mark.anyio
async def test_internal_error_collapses_to_safe_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(url: str | None) -> DatabaseStatus:
        raise RuntimeError(f"postgres://admin:hunter2@db/internal {SECRET_MARKER}")

    monkeypatch.setattr(app_module, "check_database", boom)
    # Well-formed config so readiness reaches the probe (a rejected config
    # short-circuits to its recorded classification without probing).
    app = create_app(env={"MAI_DATABASE_URL": WELL_FORMED_URL})
    await run_lifespan(app)
    response = await call_asgi_http(app, "GET", READINESS_PATH)
    assert response.status == 500
    error = response.json()["error"]
    assert error["code"] == "internal_error"
    assert error["message"] == "internal server error"
    assert error["fields"] == {}
    assert re.fullmatch(r"[0-9a-f]{32}", error["request_id"])
    text = response.body.decode()
    # C01: no traceback, stack frame, or secret material in any body.
    assert "hunter2" not in text
    assert SECRET_MARKER not in text
    assert "Traceback" not in text


# --- live disposable-Postgres path (feature-gate scenario, healthy half) -----


@requires_live_postgres
@pytest.mark.anyio
async def test_live_readiness_ok_and_liveness_with_postgres_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # env=None exercises the production path: resolve from os.environ.
    monkeypatch.setenv("MAI_DATABASE_URL", os.environ[LIVE_URL_VAR])
    app = create_app(env=None)
    await run_lifespan(app)
    assert app.state.database_url == os.environ[LIVE_URL_VAR]

    ready = await call_asgi_http(app, "GET", READINESS_PATH)
    assert ready.status == 200
    assert ready.json() == {"status": "ok", "database": "ok"}

    live = await call_asgi_http(app, "GET", HEALTH_PATH)
    assert live.status == 200
    assert live.json() == {"status": "ok"}
