"""Focused tests for the HTTP authentication boundary (card 02-auth-api).

Cards/contracts: auth ADR ``3e79449`` §2–§5, §8 G1, §9 S1–S4; contracts.md
C01 (envelope, principal-from-session), C08 rule 1/2 (cookies, origin/CSRF,
throttling). The store semantics underneath are covered by
``tests/test_sessions.py`` (02-auth-store); this suite proves the BOUNDARY:
layered rejection of unauthenticated/foreign-origin/CSRF-less mutations,
cookie flags, §5.3 throttle arithmetic and latency parity, trusted-proxy
forwarded-metadata rules, and the C01 envelope with no secret material.

Boundary discipline: every claim runs through the real ASGI app
(``create_app`` + lifespan + middleware + handlers) against a real
disposable Postgres (fixture ``migrated_url``), never mocks. The §5.3
delays are asserted through an injected sleep recorder (deterministic,
no wall-clock sleeps); the real 1 s→2 s→4 s→60 s latency profile is proven
once by the live entrypoint transcript in
``mai_evidence/02-auth-api/04-entrypoint-transcript.txt``.

Secret hygiene: the fixture password below is a synthetic constant for the
disposable fixture DB; session/CSRF tokens are minted at runtime and never
printed into assertion messages.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from fastapi.routing import APIRoute

from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE, LoginThrottle

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

REVISION_IDS = ("0001", "0002")

#: Synthetic fixture credential (NOT a secret; disposable fixture DB only).
FIXTURE_PASSWORD = "fixture-password-02-auth-api-only"
ORIGIN = "http://127.0.0.1:8443"  # loopback allowlist entry → no Secure flag
REMOTE_ORIGIN = "https://mai.example.net"
EVIL_ORIGIN = "https://attacker.example"


def _alembic_config() -> AlembicConfig:
    config = AlembicConfig(os.path.join(BACKEND_ROOT, "alembic.ini"))
    return config


# ---------------------------------------------------------------------------
# Fixtures: real disposable Postgres + real app through lifespan
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``0002``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the HTTP boundary claims need a "
            "real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        with psycopg.connect(url) as conn:
            conn.execute(
                "DROP TABLE IF EXISTS admin_sessions, admin_credential, "
                "profile_projection_journal, profile_projection, alembic_version"
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
        conn.execute("TRUNCATE admin_sessions, admin_credential")
        conn.commit()


def _build_app(
    url: str,
    *,
    origins: str = ORIGIN,
    trusted: str = "",
) -> FastAPI:
    env: dict[str, str] = {"MAI_DATABASE_URL": url}
    if origins:
        env["MAI_PUBLIC_ORIGINS"] = origins
    if trusted:
        env["MAI_TRUSTED_PROXIES"] = trusted
    return create_app(env=env)


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[FastAPI]:
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def auth_app(migrated_url: str):
    """App on the real fixture DB, empty credential/session state."""
    _truncate(migrated_url)
    app = _build_app(migrated_url)
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


def _set_cookies(response: HttpResponse) -> dict[str, dict[str, str]]:
    """Parse Set-Cookie headers: name → {value, flag: "1", ...}."""
    parsed: dict[str, dict[str, str]] = {}
    for raw in response.header_all("set-cookie"):
        parts = [part.strip() for part in raw.split(";")]
        name, _, value = parts[0].partition("=")
        attrs = parsed.setdefault(name, {"value": value})
        for attr in parts[1:]:
            key, eq, attr_value = attr.partition("=")
            attrs[key.lower()] = attr_value if eq else "1"
    return parsed


def _error(response: HttpResponse) -> dict[str, Any]:
    envelope = response.json()
    assert set(envelope) == {"error"}
    body = envelope["error"]
    assert set(body) == {"code", "message", "request_id", "fields"}
    assert "Traceback" not in response.body.decode()
    return body


def _throttle_recorder(**overrides: Any) -> tuple[LoginThrottle, list[float]]:
    """Throttle with tiny delays and a recorded sleep (no real waiting)."""
    slept: list[float] = []

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    throttle = LoginThrottle(
        base_delay_s=0.01, max_delay_s=0.6, sleep=record_sleep, **overrides
    )
    return throttle, slept


async def login_request(
    app: FastAPI,
    password: str,
    *,
    origin: str | None = ORIGIN,
) -> HttpResponse:
    headers = {"origin": origin} if origin is not None else {}
    return await call_app(app, "POST", "/api/v1/auth/login", json_body={"password": password}, headers=headers)


async def bootstrap_via_endpoint(app: FastAPI, password: str) -> None:
    response = await call_app(
        app,
        "POST",
        "/api/v1/auth/bootstrap",
        json_body={"password": password},
        headers={"origin": ORIGIN},
    )
    assert response.status == 201


async def session_cookies(app: FastAPI) -> dict[str, str]:
    """Login once and return the cookie jar for authenticated requests."""
    response = await login_request(app, FIXTURE_PASSWORD)
    assert response.status == 204, response.body
    jar = {name: value["value"] for name, value in _set_cookies(response).items()}
    assert SESSION_COOKIE in jar and CSRF_COOKIE in jar
    return jar


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


def test_auth_surface_registers_the_frozen_routes() -> None:
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
        # (02-profile-api remediation: the P02 route pin expanded by the
        # frozen canonical-profile routes; intent unchanged.)
        ("POST", "/api/v1/servers"),
        ("GET", "/api/v1/servers/{server_id}"),
        ("PUT", "/api/v1/servers/{server_id}"),
    }
    mutating = {route.path for route in api_routes if route.methods - {"GET", "HEAD"}}
    assert mutating == {
        "/api/v1/auth/bootstrap",
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/api/v1/servers",
        "/api/v1/servers/{server_id}",
    }
    assert "/docs" not in {route.path for route in api_routes}
    assert "/openapi.json" not in {route.path for route in api_routes}


def test_openapi_declares_the_frozen_auth_contract() -> None:
    schema = create_app(env={}).openapi()
    assert set(schema["paths"]) == {
        "/api/v1/health",
        "/api/v1/health/ready",
        "/api/v1/auth/bootstrap",
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/api/v1/auth/csrf",
        # (02-profile-api remediation: the path pin expanded by the frozen
        # canonical-profile routes; auth assertions below are unchanged.)
        "/api/v1/servers",
        "/api/v1/servers/{server_id}",
    }
    login = schema["paths"]["/api/v1/auth/login"]["post"]
    assert {"204", "401", "403", "422", "429"} == set(login["responses"])
    bootstrap = schema["paths"]["/api/v1/auth/bootstrap"]["post"]
    assert {"201", "403", "409", "422"} == set(bootstrap["responses"])
    logout = schema["paths"]["/api/v1/auth/logout"]["post"]
    assert {"204", "401", "403"} == set(logout["responses"])
    csrf = schema["paths"]["/api/v1/auth/csrf"]["get"]
    assert {"204", "401"} == set(csrf["responses"])
    # No invented security scheme; the session is the only principal input.
    assert "securitySchemes" not in schema["components"]
    # No handwritten client types: the exporter (02-api-client) owns that.
    assert "requestBody" in login  # the schema is generated from the code


# ---------------------------------------------------------------------------
# G1 setup state + bootstrap (acceptance 2)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_setup_state_fails_closed_on_every_auth_route(auth_app) -> None:
    """G1: without a credential, login/logout/csrf refuse value-free."""
    wrong_password = "not-the-fixture-password"
    login = await login_request(auth_app, wrong_password)
    assert login.status == 401
    assert _error(login)["code"] == "unauthenticated"

    csrf = await call_app(auth_app, "GET", "/api/v1/auth/csrf")
    assert csrf.status == 401
    assert _error(csrf)["code"] == "unauthenticated"

    logout = await call_app(
        auth_app,
        "POST",
        "/api/v1/auth/logout",
        headers={"origin": ORIGIN, "x-csrf-token": "anything"},
        cookies={SESSION_COOKIE: "forged", CSRF_COOKIE: "forged"},
    )
    assert logout.status == 401
    assert _error(logout)["code"] == "unauthenticated"


@pytest.mark.anyio
async def test_bootstrap_is_one_shot_and_permanently_409_after(auth_app) -> None:
    first = await call_app(
        auth_app,
        "POST",
        "/api/v1/auth/bootstrap",
        json_body={"password": FIXTURE_PASSWORD},
        headers={"origin": ORIGIN},
    )
    assert first.status == 201
    with psycopg.connect(auth_app.state.database_url) as conn:
        (count,) = conn.execute("SELECT count(*) FROM admin_credential").fetchone()
    assert count == 1

    for _ in range(2):  # permanent, not per-process state
        again = await call_app(
            auth_app,
            "POST",
            "/api/v1/auth/bootstrap",
            json_body={"password": "whatever-second-attempt"},
            headers={"origin": ORIGIN},
        )
        assert again.status == 409
        assert _error(again)["code"] == "conflict"


@pytest.mark.anyio
async def test_bootstrap_requires_exact_origin_before_any_effect(auth_app) -> None:
    for headers in (
        {},  # missing Origin
        {"origin": EVIL_ORIGIN},  # foreign origin
        {"referer": ORIGIN},  # Referer is never an Origin substitute
    ):
        response = await call_app(
            auth_app,
            "POST",
            "/api/v1/auth/bootstrap",
            json_body={"password": FIXTURE_PASSWORD},
            headers=headers,
        )
        assert response.status == 403, headers
        assert _error(response)["code"] == "origin_rejected"

    # Negative control: the denials preceded ANY credential effect.
    with psycopg.connect(auth_app.state.database_url) as conn:
        (count,) = conn.execute("SELECT count(*) FROM admin_credential").fetchone()
    assert count == 0


@pytest.mark.anyio
async def test_forged_privileged_header_is_never_an_input(auth_app) -> None:
    """C01 rule 2: X-Admin-style headers change nothing."""
    forged = {"x-admin": "true"}
    csrf = await call_app(auth_app, "GET", "/api/v1/auth/csrf", headers=forged)
    assert csrf.status == 401
    logout = await call_app(
        auth_app, "POST", "/api/v1/auth/logout", headers={**forged, "origin": ORIGIN}
    )
    assert logout.status == 401


@pytest.mark.anyio
async def test_missing_store_fails_closed(migrated_url: str) -> None:
    """No lifespan ⇒ no store ⇒ every auth route refuses (fail-closed)."""
    app = _build_app(migrated_url)  # lifespan never ran
    login = await login_request(app, FIXTURE_PASSWORD)
    assert login.status == 401
    assert _error(login)["code"] == "unauthenticated"
    csrf = await call_app(app, "GET", "/api/v1/auth/csrf")
    assert csrf.status == 401
    bootstrap = await call_app(
        app,
        "POST",
        "/api/v1/auth/bootstrap",
        json_body={"password": FIXTURE_PASSWORD},
        headers={"origin": ORIGIN},
    )
    assert bootstrap.status == 401


# ---------------------------------------------------------------------------
# Login: cookies, origins, validation (acceptance 1)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_login_success_sets_frozen_loopback_cookies(auth_app) -> None:
    await bootstrap_via_endpoint(auth_app, FIXTURE_PASSWORD)
    response = await login_request(auth_app, FIXTURE_PASSWORD)
    assert response.status == 204
    assert response.body == b""
    assert response.header("x-request-id")

    cookies = _set_cookies(response)
    assert set(cookies) == {SESSION_COOKIE, CSRF_COOKIE}
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        flags = cookies[name]
        assert flags["httponly" if name == SESSION_COOKIE else "samesite"]
        assert flags["path"] == "/"
        assert flags["samesite"] == "lax"
        assert "domain" not in flags  # host-only
        assert "secure" not in flags  # loopback origin is development-only
    assert "httponly" not in cookies[CSRF_COOKIE]  # readable by first-party JS
    assert len(cookies[SESSION_COOKIE]["value"]) >= 43  # 256-bit urlsafe token


@pytest.mark.anyio
async def test_login_marks_cookies_secure_on_remote_origin(auth_app) -> None:
    app = _build_app(
        auth_app.state.database_url,
        origins=f"{REMOTE_ORIGIN},{ORIGIN}",
    )
    async with _running(app):
        await bootstrap_via_endpoint(app, FIXTURE_PASSWORD)
        response = await login_request(app, FIXTURE_PASSWORD, origin=REMOTE_ORIGIN)
        assert response.status == 204
        cookies = _set_cookies(response)
        assert cookies[SESSION_COOKIE]["secure"] == "1"
        assert cookies[CSRF_COOKIE]["secure"] == "1"
    await app.state.engine.dispose()


@pytest.mark.anyio
async def test_login_requires_exact_origin_and_runs_no_effect(auth_app) -> None:
    await bootstrap_via_endpoint(auth_app, FIXTURE_PASSWORD)
    for origin in (None, EVIL_ORIGIN):
        response = await login_request(
            auth_app, FIXTURE_PASSWORD, origin=origin
        )
        assert response.status == 403
        assert _error(response)["code"] == "origin_rejected"
    referer_only = await call_app(
        auth_app,
        "POST",
        "/api/v1/auth/login",
        json_body={"password": FIXTURE_PASSWORD},
        headers={"referer": ORIGIN},
    )
    assert referer_only.status == 403
    assert _error(referer_only)["code"] == "origin_rejected"

    with psycopg.connect(auth_app.state.database_url) as conn:
        (count,) = conn.execute("SELECT count(*) FROM admin_sessions").fetchone()
    assert count == 0  # denied logins mint no session rows


@pytest.mark.anyio
async def test_wrong_password_is_a_value_free_401(auth_app) -> None:
    await bootstrap_via_endpoint(auth_app, FIXTURE_PASSWORD)
    response = await login_request(auth_app, "not-the-fixture-password")
    assert response.status == 401
    body = _error(response)
    assert body["code"] == "unauthenticated"
    assert "not-the-fixture-password" not in response.body.decode()
    with psycopg.connect(auth_app.state.database_url) as conn:
        (count,) = conn.execute("SELECT count(*) FROM admin_sessions").fetchone()
    assert count == 0


@pytest.mark.anyio
async def test_fresh_token_minted_per_login(auth_app) -> None:
    await bootstrap_via_endpoint(auth_app, FIXTURE_PASSWORD)
    first = await login_request(auth_app, FIXTURE_PASSWORD)
    second = await login_request(auth_app, FIXTURE_PASSWORD)
    first_token = _set_cookies(first)[SESSION_COOKIE]["value"]
    second_token = _set_cookies(second)[SESSION_COOKIE]["value"]
    assert first_token != second_token  # no fixation, §4


@pytest.mark.anyio
async def test_invalid_payloads_get_structured_validation_errors(auth_app) -> None:
    for payload in ({}, {"password": ""}, {"password": "x" * 2000}, "x", []):
        response = await call_app(
            auth_app,
            "POST",
            "/api/v1/auth/login",
            json_body=payload,
            headers={"origin": ORIGIN},
        )
        assert response.status == 422, payload
        body = _error(response)
        assert body["code"] == "validation_failed"
        assert body["fields"]  # field paths present, values never echoed


# ---------------------------------------------------------------------------
# CSRF mint + session rejection clearance (acceptance 1)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_csrf_mint_requires_a_valid_session(auth_app) -> None:
    await bootstrap_via_endpoint(auth_app, FIXTURE_PASSWORD)
    no_session = await call_app(auth_app, "GET", "/api/v1/auth/csrf")
    assert no_session.status == 401

    jar = await session_cookies(auth_app)
    minted = await call_app(
        auth_app, "GET", "/api/v1/auth/csrf", cookies=jar
    )
    assert minted.status == 204
    fresh = _set_cookies(minted)[CSRF_COOKIE]
    assert fresh["value"] and fresh["value"] != jar[CSRF_COOKIE]


@pytest.mark.anyio
async def test_expired_session_gets_401_with_cookie_clearance(
    auth_app, migrated_url: str
) -> None:
    await bootstrap_via_endpoint(auth_app, FIXTURE_PASSWORD)
    jar = await session_cookies(auth_app)

    # Backdate within the migration CHECKs: move the anchor columns along.
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "UPDATE admin_sessions SET created_at = created_at - interval '31 days',"
            " last_seen_at = last_seen_at - interval '31 days',"
            " expires_at = expires_at - interval '31 days',"
            " idle_expires_at = idle_expires_at - interval '31 days'"
        )
        conn.commit()

    response = await call_app(
        auth_app, "GET", "/api/v1/auth/csrf", cookies=jar
    )
    assert response.status == 401
    assert _error(response)["code"] == "unauthenticated"
    cookies = _set_cookies(response)
    assert cookies[SESSION_COOKIE]["max-age"] == "0"  # ADR §4 clearance
    assert cookies[CSRF_COOKIE]["max-age"] == "0"


# ---------------------------------------------------------------------------
# Logout: three §5 layers + revocation replay (acceptance 1, S2/S3)
# ---------------------------------------------------------------------------


async def _logout(
    app: FastAPI,
    jar: Mapping[str, str],
    *,
    origin: str | None = ORIGIN,
    csrf_header: str | None = None,
    csrf_cookie: str | None = None,
) -> HttpResponse:
    headers: dict[str, str] = {}
    if origin is not None:
        headers["origin"] = origin
    if csrf_header is not None:
        headers["x-csrf-token"] = csrf_header
    cookies = dict(jar)
    if csrf_cookie is not None:
        cookies[CSRF_COOKIE] = csrf_cookie
    return await call_app(app, "POST", "/api/v1/auth/logout", headers=headers, cookies=cookies)


@pytest.mark.anyio
async def test_logout_requires_all_three_layers_without_side_effects(
    auth_app,
) -> None:
    await bootstrap_via_endpoint(auth_app, FIXTURE_PASSWORD)
    jar = await session_cookies(auth_app)

    no_origin = await _logout(auth_app, jar, origin=None)
    assert no_origin.status == 403
    assert _error(no_origin)["code"] == "origin_rejected"

    evil = await _logout(auth_app, jar, origin=EVIL_ORIGIN)
    assert evil.status == 403
    assert _error(evil)["code"] == "origin_rejected"

    no_header = await _logout(auth_app, jar, csrf_header=None)
    assert no_header.status == 403
    assert _error(no_header)["code"] == "csrf_rejected"

    mismatched = await _logout(
        auth_app, jar, csrf_header="deliberately-wrong-token"
    )
    assert mismatched.status == 403
    assert _error(mismatched)["code"] == "csrf_rejected"

    # Negative control: every denial ran NO revocation — session still live.
    still_live = await call_app(auth_app, "GET", "/api/v1/auth/csrf", cookies=jar)
    assert still_live.status == 204


@pytest.mark.anyio
async def test_logout_revokes_presenting_session_and_replay_fails(auth_app) -> None:
    await bootstrap_via_endpoint(auth_app, FIXTURE_PASSWORD)
    jar = await session_cookies(auth_app)

    done = await _logout(auth_app, jar, csrf_header=jar[CSRF_COOKIE])
    assert done.status == 204
    cookies = _set_cookies(done)
    assert cookies[SESSION_COOKIE]["max-age"] == "0"
    assert cookies[CSRF_COOKIE]["max-age"] == "0"

    # Replay of the presented cookie AFTER logout ⇒ 401, effective now (S2).
    replay = await call_app(auth_app, "GET", "/api/v1/auth/csrf", cookies=jar)
    assert replay.status == 401
    assert _error(replay)["code"] == "unauthenticated"
    assert _set_cookies(replay)[SESSION_COOKIE]["max-age"] == "0"


@pytest.mark.anyio
async def test_cross_origin_logout_denial_leaves_session_intact(auth_app) -> None:
    """S3: a stolen cookie from an evil origin can mutate nothing."""
    await bootstrap_via_endpoint(auth_app, FIXTURE_PASSWORD)
    jar = await session_cookies(auth_app)

    stolen = await _logout(
        auth_app,
        jar,
        origin=EVIL_ORIGIN,
        csrf_header=jar[CSRF_COOKIE],
    )
    assert stolen.status == 403
    assert _error(stolen)["code"] == "origin_rejected"

    still_live = await call_app(auth_app, "GET", "/api/v1/auth/csrf", cookies=jar)
    assert still_live.status == 204  # no revocation happened


# ---------------------------------------------------------------------------
# §5.3 throttling: arithmetic, ceiling, reset, parity (acceptance 2)
# ---------------------------------------------------------------------------


def _install_throttle(app: FastAPI, **overrides: Any) -> list[float]:
    throttle, slept = _throttle_recorder(**overrides)
    app.state.login_throttle = throttle
    return slept


@pytest.mark.anyio
async def test_failed_logins_double_delay_and_success_resets(auth_app) -> None:
    await bootstrap_via_endpoint(auth_app, FIXTURE_PASSWORD)
    slept = _install_throttle(auth_app)

    for index in range(3):
        response = await login_request(auth_app, "not-the-fixture-password")
        assert response.status == 401
        assert slept[-1] == 0.01 * 2**index  # 1 s doubling, scaled

    success = await login_request(auth_app, FIXTURE_PASSWORD)
    assert success.status == 204
    assert len(slept) == 3  # success never sleeps (§5.3 delays failures only)

    after = await login_request(auth_app, "not-the-fixture-password")
    assert after.status == 401
    assert slept[-1] == 0.01  # counter reset by the successful login


@pytest.mark.anyio
async def test_global_ceiling_rejects_before_the_credential_store(
    auth_app,
) -> None:
    await bootstrap_via_endpoint(auth_app, FIXTURE_PASSWORD)
    slept = _install_throttle(auth_app, max_attempts=3)

    for _ in range(3):
        response = await login_request(auth_app, "not-the-fixture-password")
        assert response.status == 401

    # Ceiling tripped: even the CORRECT password is rejected before the
    # credential store is consulted (a flood never reaches Argon2id).
    flooded = await login_request(auth_app, FIXTURE_PASSWORD)
    assert flooded.status == 429
    assert _error(flooded)["code"] == "throttled"
    assert slept[-1] == 0.01 * 2**3  # same delay slot as a 4th wrong attempt


@pytest.mark.anyio
async def test_throttled_and_wrong_password_share_latency_profile(
    migrated_url: str,
) -> None:
    """ADR §5.3: 429 vs 401 at the same attempt position ⇒ same delay."""
    _truncate(migrated_url)
    wrong_password_app = _build_app(migrated_url)
    ceiling_app = _build_app(migrated_url)
    async with _running(wrong_password_app), _running(ceiling_app):
        # One credential row shared by both apps on the same fixture DB.
        await bootstrap_via_endpoint(wrong_password_app, FIXTURE_PASSWORD)
        plain = _install_throttle(wrong_password_app)
        ceiling = _install_throttle(ceiling_app, max_attempts=3)

        for _ in range(4):  # plain: four wrong-password attempts
            response = await login_request(
                wrong_password_app, "not-the-fixture-password"
            )
            assert response.status == 401
        for _ in range(3):  # ceiling app: three wrong, then tripped
            await login_request(ceiling_app, "not-the-fixture-password")

        throttled = await login_request(ceiling_app, FIXTURE_PASSWORD)
        assert throttled.status == 429
        assert plain[-1] == ceiling[-1]  # identical delay at position four
    await wrong_password_app.state.engine.dispose()
    await ceiling_app.state.engine.dispose()


@pytest.mark.anyio
async def test_throttle_keys_on_the_trusted_proxys_client_ip(
    auth_app,
) -> None:
    """Behind the trusted hop the rightmost XFF entry keys the counter."""
    app = _build_app(
        auth_app.state.database_url,
        trusted="127.0.0.1/32",
    )
    async with _running(app):
        await bootstrap_via_endpoint(app, FIXTURE_PASSWORD)
        slept = _install_throttle(app)

        async def attempt(xff: str) -> HttpResponse:
            return await call_app(
                app,
                "POST",
                "/api/v1/auth/login",
                json_body={"password": "not-the-fixture-password"},
                headers={"origin": ORIGIN, "x-forwarded-for": xff},
                client=("127.0.0.1", 0),  # trusted proxy peer
            )

        response = await attempt("203.0.113.7, 10.0.0.1")
        assert response.status == 401
        assert slept[-1] == 0.01  # first failure for 10.0.0.1
        response = await attempt("198.51.100.9, 10.0.0.2")
        assert response.status == 401
        assert slept[-1] == 0.01  # distinct rightmost ⇒ independent counter
        response = await attempt("198.51.100.9, 10.0.0.1")
        assert response.status == 401
        assert slept[-1] == 0.02  # same rightmost as the first ⇒ doubling
    await app.state.engine.dispose()


# ---------------------------------------------------------------------------
# Trusted forwarded metadata (ADR §6.3/S4, app half)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_forwarded_host_mismatch_is_rejected_from_trusted_peer(
    migrated_url: str,
) -> None:
    app = _build_app(migrated_url, trusted="127.0.0.1/32")
    async with _running(app):
        response = await call_app(
            app,
            "GET",
            "/api/v1/health",
            headers={"x-forwarded-host": "attacker.example"},
            client=("127.0.0.1", 0),
        )
        assert response.status == 400
        assert _error(response)["code"] == "request_rejected"
    await app.state.engine.dispose()


@pytest.mark.anyio
async def test_forwarded_host_mismatch_is_ignored_from_untrusted_peer(
    migrated_url: str,
) -> None:
    app = _build_app(migrated_url, trusted="127.0.0.1/32")
    async with _running(app):
        response = await call_app(
            app,
            "GET",
            "/api/v1/health",
            headers={"x-forwarded-host": "attacker.example"},
            client=("10.9.9.9", 0),  # not in MAI_TRUSTED_PROXIES
        )
        assert response.status == 200  # forwarded headers ignored entirely
    await app.state.engine.dispose()


@pytest.mark.anyio
async def test_matching_forwarded_host_passes_the_trusted_proxy(
    migrated_url: str,
) -> None:
    app = _build_app(migrated_url, trusted="127.0.0.1/32")
    async with _running(app):
        response = await call_app(
            app,
            "GET",
            "/api/v1/health",
            headers={"x-forwarded-host": "fixture", "x-forwarded-for": "203.0.113.7"},
            client=("127.0.0.1", 0),
        )
        assert response.status == 200
    await app.state.engine.dispose()


# ---------------------------------------------------------------------------
# Envelope + secret hygiene across every captured failure
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_error_bodies_never_carry_secret_material(auth_app) -> None:
    await bootstrap_via_endpoint(auth_app, FIXTURE_PASSWORD)
    jar = await session_cookies(auth_app)
    captured = [
        await login_request(auth_app, "not-the-fixture-password"),
        await login_request(auth_app, FIXTURE_PASSWORD, origin=EVIL_ORIGIN),
        await _logout(auth_app, jar, origin=EVIL_ORIGIN),
        await call_app(auth_app, "GET", "/api/v1/nope"),
    ]
    for response in captured:
        text = response.body.decode()
        assert FIXTURE_PASSWORD not in text
        assert SESSION_COOKIE not in text  # token lives in Set-Cookie only
        assert "Traceback" not in text
        body = _error(response)
        assert body["request_id"] == response.header("x-request-id")
