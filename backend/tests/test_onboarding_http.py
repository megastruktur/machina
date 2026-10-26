"""ASGI tests for the guided-onboarding HTTP surface (so-01).

Acceptance-critical cases only (so_01-onboarding-probe.md): boundary layers,
real public-key export, real discovery + pinned test against the disposable
``mai-fxt-so-01-ssh-arch`` sshd, structured negative controls, side-effect-free
data root, 404 enroll route, route order and OpenAPI additions. Harness
mirrors ``tests/test_servers_http.py`` (real ASGI app + real disposable
Postgres, no mocks); the sshd fixture and its ``mai-fxt-so-*`` cleanup are
imported from ``tests/test_onboarding.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from fastapi.routing import APIRoute

from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE

from test_onboarding import (  # noqa: F401 - so_sshd is a pytest fixture
    DEFAULT_CREDENTIAL_REF_VAR,
    FIXTURE_CREDENTIAL_REF,
    _generate_key,
    _write_env_file,
    so_sshd,
)

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: Synthetic fixture credential (NOT a secret; disposable fixture DB only).
FIXTURE_PASSWORD = "fixture-password-so-01-onboarding-only"
ORIGIN = "http://127.0.0.1:8443"  # loopback allowlist entry

KEY_PATH = "/api/v1/servers/onboarding/controller-key"
TEST_PATH = "/api/v1/servers/onboarding/test"
ENROLL_PATH = "/api/v1/servers/onboarding/enroll"


# ---------------------------------------------------------------------------
# Disposable Postgres fixtures (same pattern as tests/test_servers_http.py)
# ---------------------------------------------------------------------------


def _reset_fixture_db(url: str) -> None:
    """Reset the DISPOSABLE fixture database (only ``mai_so_campaign``).

    The campaign database may carry tables from earlier disposable runs
    beyond this migration chain (observed: agent-task/conversation tables),
    so the reset enumerates the actual ``public`` tables of THIS database
    and drops each by name — no wildcard, no CASCADE, no other database is
    touched — leaving the exact pre-bootstrap state for ``upgrade head``.
    """
    with psycopg.connect(url) as conn:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "ORDER BY tablename"
            ).fetchall()
        ]
        if names:
            # One statement: FK constraints WITHIN the dropped set are fine.
            list_sql = ", ".join(f'"{name}"' for name in names)
            conn.execute(f"DROP TABLE IF EXISTS {list_sql}")
        conn.execute("DROP SEQUENCE IF EXISTS server_observations_seq")
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> Iterator[str]:
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the HTTP surface claims need a "
            "real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url
    try:
        _reset_fixture_db(url)
        command.upgrade(AlembicConfig(os.path.join(BACKEND_ROOT, "alembic.ini")), "head")
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


# ---------------------------------------------------------------------------
# Minimal ASGI harness (bodies + cookies; same as test_servers_http.py)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HttpResponse:
    status: int | None = None
    headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    body: bytes = b""

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
    except Exception:  # noqa: BLE001 - middleware re-raises after reply
        pass
    return response


def _set_cookies(response: HttpResponse) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw in response.headers:
        name, value = raw[0], raw[1]
        if name == b"set-cookie":
            key, _, rest = value.decode("ascii").partition("=")
            parsed[key] = rest.split(";", 1)[0]
    return parsed


def _error(response: HttpResponse) -> dict[str, Any]:
    envelope = response.json()
    assert set(envelope) == {"error"}
    body = envelope["error"]
    assert set(body) == {"code", "message", "request_id", "fields"}
    return body


async def login_session(app: FastAPI) -> dict[str, str]:
    bootstrap = await call_app(
        app, "POST", "/api/v1/auth/bootstrap",
        json_body={"password": FIXTURE_PASSWORD}, headers={"origin": ORIGIN},
    )
    assert bootstrap.status == 201, bootstrap.body
    login = await call_app(
        app, "POST", "/api/v1/auth/login",
        json_body={"password": FIXTURE_PASSWORD}, headers={"origin": ORIGIN},
    )
    assert login.status == 204, login.body
    jar = _set_cookies(login)
    assert SESSION_COOKIE in jar and CSRF_COOKIE in jar
    return jar


def _mutation_headers(jar: Mapping[str, str]) -> dict[str, str]:
    return {"origin": ORIGIN, "x-csrf-token": jar[CSRF_COOKIE]}


# ---------------------------------------------------------------------------
# App fixtures
# ---------------------------------------------------------------------------


def _build_app(
    url: str,
    data_root: Path,
    *,
    env_file: Path | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> FastAPI:
    env: dict[str, str] = {
        "MAI_DATABASE_URL": url,
        "MAI_PUBLIC_ORIGINS": ORIGIN,
        "MAI_DATA_ROOT": str(data_root),
    }
    if env_file is not None:
        env["MAI_ENV_FILE"] = str(env_file)
    env.update(extra_env or {})
    return create_app(env=env)


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[FastAPI]:
    async with app.router.lifespan_context(app):
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()


@pytest.fixture
async def key_app(migrated_url: str, tmp_path: Path) -> AsyncIterator[FastAPI]:
    """App whose default reference resolves to a REAL synthetic keypair."""
    _truncate(migrated_url)
    key_path = tmp_path / "controller_key"
    _generate_key(key_path)
    env_file = _write_env_file(
        tmp_path / "credentials.env", {"MAI_SSH_KEY_MAI": str(key_path)}
    ).path
    app = _build_app(migrated_url, tmp_path / "data-root", env_file=env_file)
    async with _running(app):
        yield app


@pytest.fixture
async def onboarding_app(migrated_url: str, tmp_path: Path) -> AsyncIterator[FastAPI]:
    """App with NO credential source (fail-closed controller-key default)."""
    _truncate(migrated_url)
    app = _build_app(migrated_url, tmp_path / "data-root")
    async with _running(app):
        yield app


@pytest.fixture
async def sshd_app(migrated_url: str, tmp_path: Path, so_sshd) -> AsyncIterator[FastAPI]:
    """App wired to the real disposable sshd fixture's synthetic credential."""
    _truncate(migrated_url)
    app = _build_app(
        migrated_url,
        tmp_path / "data-root",
        env_file=so_sshd.env_resolver().path,
        extra_env={DEFAULT_CREDENTIAL_REF_VAR: FIXTURE_CREDENTIAL_REF},
    )
    async with _running(app):
        yield app


def _tree(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def _test_body(port: int, *, host_key: str | None, privilege: str = "none") -> dict[str, Any]:
    return {
        "ssh": {"host": "127.0.0.1", "port": port, "user": "root"},
        "privilege": privilege,
        "host_key": host_key,
        "timeout_seconds": 15,
    }


# ---------------------------------------------------------------------------
# Controller public key
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_controller_key_is_never_anonymous(onboarding_app) -> None:
    response = await call_app(onboarding_app, "GET", KEY_PATH)
    assert response.status == 401
    assert _error(response)["code"] == "unauthenticated"


@pytest.mark.anyio
async def test_controller_key_exports_the_real_public_half(key_app) -> None:
    jar = await login_session(key_app)
    response = await call_app(key_app, "GET", KEY_PATH, cookies=jar)
    assert response.status == 200, response.body
    body = response.json()
    assert set(body) == {"public_key", "key_type", "fingerprint"}
    assert body["key_type"] == "ssh-ed25519"
    assert body["fingerprint"].startswith("SHA256:") and len(body["fingerprint"]) == 50
    # One OpenSSH public line; no private material, no path, no PEM body.
    assert len(body["public_key"].splitlines()) == 1
    assert body["public_key"].split()[0] == "ssh-ed25519"
    assert "PRIVATE" not in body["public_key"]
    assert "-----BEGIN" not in body["public_key"]
    env_path = str(key_app.state.env["MAI_ENV_FILE"])
    assert env_path not in response.body.decode()
    data_root: Path = key_app.state.profiles.root
    assert _tree(data_root) == []


@pytest.mark.anyio
async def test_controller_key_fails_closed_without_credential_source(
    onboarding_app,
) -> None:
    jar = await login_session(onboarding_app)
    response = await call_app(onboarding_app, "GET", KEY_PATH, cookies=jar)
    assert response.status == 503
    assert _error(response)["code"] == "unavailable"


@pytest.mark.anyio
async def test_controller_key_503_when_reference_missing(
    migrated_url: str, tmp_path: Path
) -> None:
    _truncate(migrated_url)
    env_file = _write_env_file(tmp_path / "empty.env", {"OTHER_REF": "/nowhere"}).path
    app = _build_app(migrated_url, tmp_path / "data-root", env_file=env_file)
    async with _running(app):
        jar = await login_session(app)
        response = await call_app(app, "GET", KEY_PATH, cookies=jar)
    assert response.status == 503
    assert _error(response)["code"] == "unavailable"
    # The resolver source path must never reach the browser.
    assert str(env_file) not in response.body.decode()


# ---------------------------------------------------------------------------
# Boundary layers on the mutating test endpoint
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_test_endpoint_is_never_anonymous(onboarding_app) -> None:
    response = await call_app(
        onboarding_app, "POST", TEST_PATH, json_body=_test_body(22, host_key=None)
    )
    assert response.status == 401
    assert _error(response)["code"] == "unauthenticated"


@pytest.mark.anyio
async def test_test_endpoint_requires_origin_and_csrf(onboarding_app) -> None:
    jar = await login_session(onboarding_app)
    missing_origin = await call_app(
        onboarding_app, "POST", TEST_PATH,
        json_body=_test_body(22, host_key=None),
        headers={"x-csrf-token": jar[CSRF_COOKIE]}, cookies=jar,
    )
    assert missing_origin.status == 403
    assert _error(missing_origin)["code"] == "origin_rejected"
    missing_csrf = await call_app(
        onboarding_app, "POST", TEST_PATH,
        json_body=_test_body(22, host_key=None),
        headers={"origin": ORIGIN}, cookies=jar,
    )
    assert missing_csrf.status == 403
    assert _error(missing_csrf)["code"] == "csrf_rejected"


@pytest.mark.anyio
async def test_test_endpoint_rejects_malformed_requests(onboarding_app) -> None:
    jar = await login_session(onboarding_app)
    bad_pin = await call_app(
        onboarding_app, "POST", TEST_PATH,
        json_body=_test_body(22, host_key="not-a-fingerprint"),
        headers=_mutation_headers(jar), cookies=jar,
    )
    assert bad_pin.status == 422
    bad_timeout = await call_app(
        onboarding_app, "POST", TEST_PATH,
        json_body=_test_body(22, host_key=None) | {"timeout_seconds": 2},
        headers=_mutation_headers(jar), cookies=jar,
    )
    assert bad_timeout.status == 422
    bad_privilege = await call_app(
        onboarding_app, "POST", TEST_PATH,
        json_body=_test_body(22, host_key=None) | {"privilege": "wheel"},
        headers=_mutation_headers(jar), cookies=jar,
    )
    assert bad_privilege.status == 422


@pytest.mark.anyio
async def test_dash_prefixed_identity_never_reaches_a_subprocess(
    onboarding_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer control: option-injection values die at validation.

    A user/host beginning with ``-`` would reach the ``ssh``/``ssh-keyscan``
    argv as a LOCAL option (``-oProxyCommand=…``); the wire patterns must
    reject it before any subprocess exists.
    """
    jar = await login_session(onboarding_app)

    def no_subprocess(*_a: object, **_k: object) -> object:
        raise AssertionError("no subprocess may run for an invalid identity")

    monkeypatch.setattr(subprocess, "run", no_subprocess)
    for field_updates in (
        {"user": "-oProxyCommand=evil"},
        {"host": "-FourOhFour"},
        {"user": "bad user"},
        {"user": "a@b"},
        {"host": ""},
    ):
        body = _test_body(22, host_key=None)
        body["ssh"].update(field_updates)
        response = await call_app(
            onboarding_app, "POST", TEST_PATH,
            json_body=body, headers=_mutation_headers(jar), cookies=jar,
        )
        assert response.status == 422, (field_updates, response.body)
        assert _error(response)["code"] == "validation_failed"


# ---------------------------------------------------------------------------
# Real sshd: discovery, pinned test, negative controls, side-effect-freedom
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_discovery_then_pinned_test_with_real_sshd(sshd_app, so_sshd) -> None:
    jar = await login_session(sshd_app)
    data_root: Path = sshd_app.state.profiles.root
    before = _tree(data_root)

    discovery = await call_app(
        sshd_app, "POST", TEST_PATH,
        json_body=_test_body(so_sshd.port, host_key=None),
        headers=_mutation_headers(jar), cookies=jar,
    )
    assert discovery.status == 200, discovery.body
    candidates = discovery.json()["candidates"]
    types = {candidate["key_type"] for candidate in candidates}
    assert {"ssh-ed25519", "ssh-rsa"} <= types  # nothing collapsed away
    assert "authentication" not in discovery.json()  # no authentication claim
    selected = next(
        c for c in candidates if c["key_type"] == "ssh-ed25519"
    )
    assert selected["host_key"] == so_sshd.identity.host_key
    assert _tree(data_root) == before  # discovery wrote nothing

    pinned = await call_app(
        sshd_app, "POST", TEST_PATH,
        json_body=_test_body(so_sshd.port, host_key=selected["host_key"],
                             privilege="sudo"),
        headers=_mutation_headers(jar), cookies=jar,
    )
    assert pinned.status == 200, pinned.body
    body = pinned.json()
    assert body["authentication"] == "ok"
    assert body["host_key"] == selected["host_key"]
    assert body["key_type"] == "ssh-ed25519"
    assert body["privilege"] == "sudo"
    identity = body["probe_environment"]
    assert identity["os_id"] == "arch"
    assert identity["kernel"].startswith("Linux")
    assert identity["hostname"] and identity["boot_id"]
    # The whole flow stayed side-effect-free at the canonical data root.
    assert _tree(data_root) == before


@pytest.mark.anyio
async def test_wrong_pin_conflicts_and_bad_key_is_auth_failure(
    sshd_app, so_sshd
) -> None:
    jar = await login_session(sshd_app)
    stranger_pin = "SHA256:" + "z" * 43
    wrong = await call_app(
        sshd_app, "POST", TEST_PATH,
        json_body=_test_body(so_sshd.port, host_key=stranger_pin),
        headers=_mutation_headers(jar), cookies=jar,
    )
    assert wrong.status == 409
    assert _error(wrong)["code"] == "host_key_mismatch"

    # Point the app at a keypair the target does not authorize.
    stranger_key = so_sshd.scratch / "http_stranger_key"
    _generate_key(stranger_key)
    stranger_env = _write_env_file(
        so_sshd.scratch / "http_stranger.env",
        {FIXTURE_CREDENTIAL_REF: str(stranger_key)},
    ).path
    bad_app = _build_app(
        sshd_app.state.env["MAI_DATABASE_URL"],
        sshd_app.state.profiles.root.parent / "bad-key-root",
        env_file=stranger_env,
        extra_env={DEFAULT_CREDENTIAL_REF_VAR: FIXTURE_CREDENTIAL_REF},
    )
    async with _running(bad_app):
        # Same disposable DB: the sshd_app session is already valid here.
        response = await call_app(
            bad_app, "POST", TEST_PATH,
            json_body=_test_body(so_sshd.port, host_key=so_sshd.identity.host_key),
            headers=_mutation_headers(jar), cookies=jar,
        )
    assert response.status == 503
    assert _error(response)["code"] == "auth_failed"


@pytest.mark.anyio
async def test_unreachable_target_maps_to_unreachable(onboarding_app) -> None:
    jar = await login_session(onboarding_app)
    probe = __import__("socket").socket()
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    probe.close()
    response = await call_app(
        onboarding_app, "POST", TEST_PATH,
        json_body=_test_body(closed_port, host_key=None),
        headers=_mutation_headers(jar), cookies=jar,
    )
    assert response.status == 503
    assert _error(response)["code"] == "unreachable"


# ---------------------------------------------------------------------------
# Enrollment stays absent until task 02; registration order and OpenAPI
# ---------------------------------------------------------------------------


def _api_routes(app: FastAPI) -> list[APIRoute]:
    routes: list[APIRoute] = []
    for included in app.routes:
        original = getattr(included, "original_router", None)
        candidates = original.routes if original is not None else [included]
        routes.extend(route for route in candidates if isinstance(route, APIRoute))
    return routes


def test_enroll_route_does_not_exist_yet() -> None:
    app = create_app(env={})
    assert ENROLL_PATH not in {route.path for route in _api_routes(app)}


@pytest.mark.anyio
async def test_enroll_route_answers_404(onboarding_app) -> None:
    jar = await login_session(onboarding_app)
    response = await call_app(
        onboarding_app, "POST", ENROLL_PATH,
        json_body={"name": "stub"},
        headers=_mutation_headers(jar), cookies=jar,
    )
    assert response.status == 404


def test_onboarding_routes_precede_server_id_and_extend_openapi() -> None:
    app = create_app(env={})
    paths_in_order = [route.path for route in _api_routes(app)]
    for onboarding_path in (KEY_PATH, TEST_PATH):
        assert paths_in_order.index(onboarding_path) < paths_in_order.index(
            "/api/v1/servers/{server_id}"
        )
    schema = app.openapi()
    key_responses = set(schema["paths"][KEY_PATH]["get"]["responses"])
    assert key_responses == {"200", "401", "422", "503"}
    test_responses = set(schema["paths"][TEST_PATH]["post"]["responses"])
    assert test_responses == {"200", "401", "403", "409", "422", "503"}
    assert ENROLL_PATH not in schema["paths"]
    component_names = set(schema["components"]["schemas"])
    assert {
        "ControllerKeyResponse",
        "OnboardingTestRequest",
        "OnboardingDiscoveryResponse",
        "OnboardingTestResult",
    } <= component_names
