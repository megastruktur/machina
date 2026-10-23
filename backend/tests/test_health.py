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
    # FastAPI ≥0.143 (Starlette 1.7) wraps include_router output in
    # _IncludedRouter; unwrap it so the table covers every APIRoute.
    routes: list[Any] = []
    for route in app.routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            routes.extend(original.routes)
        else:
            routes.append(route)
    return {
        (method, route.path)
        for route in routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }


#: Frozen P02 public surface: health plus the 02-auth-api boundary (C08
#: rule 1/2) and the 02-profile-api canonical profile surface (C02).
_AUTH_SURFACE = frozenset(
    {
        ("POST", "/api/v1/auth/bootstrap"),
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/logout"),
        ("GET", "/api/v1/auth/csrf"),
    }
)

#: 02-profile-api surface (C01/C02): create/read/edit canonical profiles.
_PROFILE_SURFACE = frozenset(
    {
        ("POST", "/api/v1/servers"),
        ("GET", "/api/v1/servers/{server_id}"),
        ("PUT", "/api/v1/servers/{server_id}"),
    }
)

#: 03-approval-api surface (C03 rule 4): safe preview + one-use decision.
_APPROVAL_SURFACE = frozenset(
    {
        ("GET", "/api/v1/approvals/{approval_id}"),
        ("POST", "/api/v1/approvals/{approval_id}/decision"),
    }
)

#: 03-grants-api surface (C03 rules 3–5): versioned read + edit of the
#: capability-grant configuration.
_GRANTS_SURFACE = frozenset(
    {
        ("GET", "/api/v1/grants"),
        ("PUT", "/api/v1/grants"),
    }
)

#: 04-password-api surface (C03 rule 4 + C04 rule 2): the submit-only
#: password challenge transport — deliberately no read endpoint.
_CHALLENGE_SURFACE = frozenset(
    {
        ("POST", "/api/v1/password-challenges"),
    }
)

#: The frozen feature surface registered by the later api modules
#: (05-jobs, 06-chat, 06-model, 07-docs, 08-app, 09-plugins, 10-monitor,
#: 11-knowledge). Every pair below is transcribed from the actual route
#: definitions in the owning api module — the pin is a deliberate,
#: reviewable list, never derived from the running app.
_FEATURE_SURFACE = frozenset(
    {
        # (05-jobs-api: durable runs + the run event stream.)
        ("GET", "/api/v1/jobs/runs"),
        ("POST", "/api/v1/jobs/runs"),
        ("GET", "/api/v1/jobs/runs/{run_id}"),
        ("POST", "/api/v1/jobs/runs/{run_id}/cancel"),
        ("GET", "/api/v1/jobs/runs/{run_id}/events"),
        # (05-jobs-api: the durable schedule configuration surface.)
        ("GET", "/api/v1/schedules"),
        ("POST", "/api/v1/schedules"),
        ("GET", "/api/v1/schedules/{schedule_id}"),
        ("PATCH", "/api/v1/schedules/{schedule_id}"),
        ("DELETE", "/api/v1/schedules/{schedule_id}"),
        ("GET", "/api/v1/schedules/{schedule_id}/fires"),
        # (06-chat-api: conversations, the conversation event stream and the
        # lazy-loaded single tool step.)
        ("GET", "/api/v1/chat/runs"),
        ("GET", "/api/v1/chat/runs/{run_id}"),
        ("PATCH", "/api/v1/chat/runs/{run_id}"),
        ("GET", "/api/v1/chat/runs/{run_id}/events"),
        ("GET", "/api/v1/chat/runs/{run_id}/steps/{seq}"),
        ("POST", "/api/v1/chat/messages"),
        ("POST", "/api/v1/chat/runs/{run_id}/cancel"),
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
        # (s3-plugins-api remediation: the probe route shipped without
        # extending this pin; transcribed from mai.api.plugins.)
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
        # (uir u2-terminal: terminal attach info; the sibling WebSocket route
        # is not an APIRoute and stays outside this HTTP pin.)
        ("GET", "/api/v1/servers/{server_id}/terminal/info"),
        # (mai-wire-desktop: the launcher's server list route.)
        ("GET", "/api/v1/servers"),
    }
)

#: The whole registered table, pinned explicitly: health + the boundary +
#: the profile/approval/grants/challenge surfaces + the frozen feature
#: surface. Any new route must extend this pin consciously.
_FROZEN_SURFACE = (
    frozenset({("GET", HEALTH_PATH), ("GET", READINESS_PATH)})
    | _AUTH_SURFACE
    | _PROFILE_SURFACE
    | _APPROVAL_SURFACE
    | _GRANTS_SURFACE
    | _CHALLENGE_SURFACE
    | _FEATURE_SURFACE
)

#: The setup-state public surface of the auth ADR §8 G1 gate: the two
#: health reads and the one-shot bootstrap. Every other operation is
#: session-gated and declares its 401 refusal in the generated schema.
_SETUP_PUBLIC_SURFACE = frozenset(
    {
        ("GET", HEALTH_PATH),
        ("GET", READINESS_PATH),
        ("POST", "/api/v1/auth/bootstrap"),
    }
)


def test_app_registers_exactly_the_g1_health_surface() -> None:
    """The whole frozen route table is registered — nothing more, nothing less.

    (02-profile-api remediation: the P02 route-table pin expanded by the
    frozen profile routes; intent unchanged — every privileged operation is
    session-gated, no unauthenticated mutating privilege, no docs listeners,
    everything under the /api/v1 prefix. 03-approval-api remediation: the
    pin expanded again by the frozen approval preview/decision routes.
    03-grants-api remediation: the pin expanded by the frozen
    grant-configuration read/edit routes. 04-password-api remediation: the
    pin expanded by the frozen submit-only challenge route. Later api
    modules (05–11 and the desktop launcher route) expanded it again;
    every pair is transcribed from the owning api module's route
    definition, so the pin stays a deliberate, reviewable list.)
    """
    app = create_app(env={})
    api_routes = [
        route
        for included in app.routes
        for route in (
            getattr(included, "original_router", included).routes
            if getattr(included, "original_router", None) is not None
            else [included]
        )
        if isinstance(route, APIRoute)
    ]
    table = {
        (method, route.path) for route in api_routes for method in route.methods
    }
    assert table == _FROZEN_SURFACE
    mutating = {
        route.path
        for route in api_routes
        if route.methods - {"GET", "HEAD"}  # every mutating verb is boundary-owned
    }
    assert mutating == {
        path
        for method, path in _FROZEN_SURFACE
        if method in {"POST", "PUT", "PATCH", "DELETE"}
    }
    for route in api_routes:
        assert route.path.startswith(API_PREFIX)
    # No public docs/OpenAPI listeners (G1 minimal unauthenticated surface).
    paths = {route.path for route in api_routes}
    assert "/docs" not in paths
    assert "/openapi.json" not in paths
    # G1 public-surface guarantee: the setup-state public surface is pinned
    # explicitly (auth ADR §8) — every other registered operation must
    # declare its 401 refusal in the generated schema. A new route that
    # reaches a privileged effect without a session-gated refusal breaks
    # the boundary contract.
    schema = app.openapi()
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            declared = set(operation.get("responses", {}))
            if (method.upper(), path) in _SETUP_PUBLIC_SURFACE:
                assert "401" not in declared, (method, path)
            else:
                assert "401" in declared, (method, path)


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
    # (02-profile-api remediation: the P02 path pin expanded by the frozen
    # profile routes; the health contract itself is unchanged. Later api
    # modules expanded it again — every path below is transcribed from the
    # owning api module's route definition.)
    assert set(schema["paths"]) == {
        HEALTH_PATH,
        READINESS_PATH,
        "/api/v1/auth/bootstrap",
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/api/v1/auth/csrf",
        "/api/v1/servers",
        "/api/v1/servers/{server_id}",
        # (03-approval-api remediation: the path pin expanded by the frozen
        # approval preview/decision routes; the health contract is unchanged.)
        "/api/v1/approvals/{approval_id}",
        "/api/v1/approvals/{approval_id}/decision",
        # (03-grants-api remediation: the path pin expanded by the frozen
        # grant-configuration read/edit routes; health contract unchanged.)
        "/api/v1/grants",
        # (04-password-api remediation: the path pin expanded by the frozen
        # submit-only challenge route; health contract unchanged.)
        "/api/v1/password-challenges",
        # (05-jobs-api: durable runs + schedules + the run event stream.)
        "/api/v1/jobs/runs",
        "/api/v1/jobs/runs/{run_id}",
        "/api/v1/jobs/runs/{run_id}/cancel",
        "/api/v1/jobs/runs/{run_id}/events",
        "/api/v1/schedules",
        "/api/v1/schedules/{schedule_id}",
        "/api/v1/schedules/{schedule_id}/fires",
        # (06-chat-api: conversations + the conversation event stream + the
        # lazy-loaded single tool step.)
        "/api/v1/chat/messages",
        "/api/v1/chat/runs",
        "/api/v1/chat/runs/{run_id}",
        "/api/v1/chat/runs/{run_id}/cancel",
        "/api/v1/chat/runs/{run_id}/events",
        "/api/v1/chat/runs/{run_id}/steps/{seq}",
        # (06-model-api: stored connections/profiles/selection + probe.)
        "/api/v1/models/connections",
        "/api/v1/models/connections/{name}",
        "/api/v1/models/profiles",
        "/api/v1/models/profiles/{name}",
        "/api/v1/models/selection",
        "/api/v1/models/test",
        # (07-docs-api: canonical documents, retrieval, index, generate,
        # research.)
        "/api/v1/docs",
        "/api/v1/docs/{doc_id}",
        "/api/v1/docs/retrieve",
        "/api/v1/docs/index",
        "/api/v1/docs/index/reconcile",
        "/api/v1/docs/generate",
        "/api/v1/docs/research",
        # (08-app-api: observed inventory + explicit adoptions + lifecycle
        # intent submits.)
        "/api/v1/servers/{server_id}/applications",
        "/api/v1/servers/{server_id}/applications/discoveries",
        "/api/v1/servers/{server_id}/applications/plan",
        "/api/v1/servers/{server_id}/applications/{application_id}",
        "/api/v1/servers/{server_id}/applications/{application_id}/install",
        "/api/v1/servers/{server_id}/applications/{application_id}/update",
        "/api/v1/servers/{server_id}/applications/{application_id}/remove",
        "/api/v1/servers/{server_id}/applications/{application_id}/execute",
        "/api/v1/applications/catalog",
        "/api/v1/applications/catalog/{runbook_id}",
        # (09-plugins-api: plugin configs/bindings/discovery + skills.)
        "/api/v1/plugins",
        "/api/v1/plugins/{plugin}/config",
        "/api/v1/plugins/{plugin}/schema",
        "/api/v1/plugins/{plugin}/bindings",
        "/api/v1/plugins/{plugin}/bindings/{server_id}",
        "/api/v1/plugins/{plugin}/discovery",
        # (s3-plugins-api remediation: the probe path shipped without
        # extending this pin.)
        "/api/v1/plugins/{plugin}/probe",
        "/api/v1/skills",
        "/api/v1/skills/{skill}",
        # (10-monitor-api: observations/findings/notifications + policy.)
        "/api/v1/monitoring/servers/{server_id}/observations",
        "/api/v1/monitoring/servers/{server_id}/observations/{observation_id}",
        "/api/v1/monitoring/servers/{server_id}/findings",
        "/api/v1/monitoring/servers/{server_id}/findings/{finding_id}",
        "/api/v1/monitoring/servers/{server_id}/findings/evaluate",
        "/api/v1/monitoring/servers/{server_id}/notifications",
        "/api/v1/monitoring/servers/{server_id}/notifications/{alert_id}",
        "/api/v1/monitoring/servers/{server_id}/notifications/{alert_id}/ack",
        "/api/v1/monitoring/notification-policy",
        # (11-knowledge-api: memory surface.)
        "/api/v1/servers/{server_id}/memory/status",
        "/api/v1/servers/{server_id}/memory/recall",
        "/api/v1/servers/{server_id}/memory/retain",
        "/api/v1/servers/{server_id}/memory/reflect",
        "/api/v1/servers/{server_id}/memory/export",
        "/api/v1/servers/{server_id}/memory/delete",
        # (uir u2-terminal: terminal attach info view.)
        "/api/v1/servers/{server_id}/terminal/info",
    }
    assert set(schema["paths"][HEALTH_PATH]) == {"get"}
    assert "200" in schema["paths"][HEALTH_PATH]["get"]["responses"]
    ready = schema["paths"][READINESS_PATH]["get"]
    assert set(ready["responses"]) == {"200", "503"}
    assert set(schema["paths"]["/api/v1/auth/bootstrap"]) == {"post"}
    assert set(schema["paths"]["/api/v1/auth/login"]) == {"post"}
    assert set(schema["paths"]["/api/v1/auth/logout"]) == {"post"}
    assert set(schema["paths"]["/api/v1/auth/csrf"]) == {"get"}
    # Envelope documentation exists for the boundary's rejection codes.
    assert {"401", "403", "429"} <= set(
        schema["paths"]["/api/v1/auth/login"]["post"]["responses"]
    )
    assert {"401", "403"} <= set(
        schema["paths"]["/api/v1/auth/logout"]["post"]["responses"]
    )
    # 02-profile-api surface: generated models, not handwritten types.
    # (mai-wire-desktop added the launcher's server list read to the same
    # path — the profile surface pin grew with it.)
    assert set(schema["paths"]["/api/v1/servers"]) == {"get", "post"}
    assert set(schema["paths"]["/api/v1/servers/{server_id}"]) == {"get", "put"}
    assert "requestBody" in schema["paths"]["/api/v1/servers"]["post"]
    assert {"ProfileCreate", "ProfileEdit", "ProfileResponse"} <= set(
        schema["components"]["schemas"]
    )


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
    # The lifespan disposed exactly the engines it started (dependency
    # stop): the app engine and the 04-password-api off-loop lane engine.
    assert dispose_calls == [app.state.engine, app.state.password_lane_engine]


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
