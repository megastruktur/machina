"""ASGI tests for the guided-onboarding HTTP surface (so-01 + so-02).

Acceptance-critical cases only: boundary layers, real public-key export,
real discovery + pinned test against the disposable ``mai-fxt-so-01-ssh-arch``
sshd, structured negative controls, side-effect-free data root, the
confirmed-enrollment seam (trust gate, real enroll → read-back → pinned
probe, stale/auth/privilege negative controls, projection-failure honesty),
route order and OpenAPI additions. Harness mirrors
``tests/test_servers_http.py`` (real ASGI app + real disposable Postgres,
no mocks); the sshd fixture and its ``mai-fxt-so-*`` cleanup are imported
from ``tests/test_onboarding.py``.
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
    FALLBACK_CREDENTIAL_REF,
    FIXTURE_CREDENTIAL_REF,
    FIXTURE_LIMITED_REF,
    _generate_key,
    _write_env_file,
    so_sshd,
)
from mai.execution.onboarding import durable_known_hosts_path
from mai.execution.ssh import SshIdentity, probe_environment
from mai.api.terminal import _known_hosts_path
from mai.profiles.projection import (
    ProfileProjection,
    ProjectionError,
    ProjectionErrorCode,
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
# Confirmed enrollment (so-02): boundary, trust gate, persistence, honesty
# ---------------------------------------------------------------------------


def _enroll_body(
    port: int,
    host_key: str,
    *,
    privilege: str = "sudo",
    confirmed: bool | None = True,
    user: str = "root",
    name: str = "enrolled-fixture",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": name,
        "privilege": privilege,
        "capabilities": ["monitoring.read"],
        "ssh": {"host": "127.0.0.1", "port": port, "user": user},
        "host_key": host_key,
        "timeout_seconds": 15,
    }
    if confirmed is not None:
        body["host_key_confirmed"] = confirmed
    return body


@pytest.mark.anyio
async def test_enroll_is_never_anonymous(onboarding_app) -> None:
    response = await call_app(
        onboarding_app, "POST", ENROLL_PATH,
        json_body=_enroll_body(22, "SHA256:" + "a" * 43),
    )
    assert response.status == 401
    assert _error(response)["code"] == "unauthenticated"


@pytest.mark.anyio
async def test_enroll_requires_origin_and_csrf(onboarding_app) -> None:
    jar = await login_session(onboarding_app)
    missing_origin = await call_app(
        onboarding_app, "POST", ENROLL_PATH,
        json_body=_enroll_body(22, "SHA256:" + "a" * 43),
        headers={"x-csrf-token": jar[CSRF_COOKIE]}, cookies=jar,
    )
    assert missing_origin.status == 403
    assert _error(missing_origin)["code"] == "origin_rejected"
    missing_csrf = await call_app(
        onboarding_app, "POST", ENROLL_PATH,
        json_body=_enroll_body(22, "SHA256:" + "a" * 43),
        headers={"origin": ORIGIN}, cookies=jar,
    )
    assert missing_csrf.status == 403
    assert _error(missing_csrf)["code"] == "csrf_rejected"


@pytest.mark.anyio
async def test_unconfirmed_trust_rejected_before_any_effect(
    onboarding_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent/false consent: 422 host_key_confirmation_required, zero effects.

    The app has NO credential source and every subprocess raises — neither
    a keyscan nor a credential resolution may happen for an unconfirmed
    request, and the data root must stay untouched.
    """
    jar = await login_session(onboarding_app)

    def no_subprocess(*_a: object, **_k: object) -> object:
        raise AssertionError("no subprocess may run without explicit trust")

    monkeypatch.setattr(subprocess, "run", no_subprocess)
    data_root: Path = onboarding_app.state.profiles.root
    before = _tree(data_root)
    for body in (
        _enroll_body(22, "SHA256:" + "a" * 43, confirmed=None),
        _enroll_body(22, "SHA256:" + "a" * 43, confirmed=False),
    ):
        response = await call_app(
            onboarding_app, "POST", ENROLL_PATH,
            json_body=body, headers=_mutation_headers(jar), cookies=jar,
        )
        assert response.status == 422, response.body
        assert _error(response)["code"] == "host_key_confirmation_required"
    assert _tree(data_root) == before


@pytest.mark.anyio
async def test_enroll_rejects_malformed_requests(onboarding_app) -> None:
    jar = await login_session(onboarding_app)
    bad_pin = await call_app(
        onboarding_app, "POST", ENROLL_PATH,
        json_body=_enroll_body(22, "not-a-fingerprint"),
        headers=_mutation_headers(jar), cookies=jar,
    )
    assert bad_pin.status == 422
    bad_timeout = await call_app(
        onboarding_app, "POST", ENROLL_PATH,
        json_body=_enroll_body(22, "SHA256:" + "a" * 43) | {"timeout_seconds": 2},
        headers=_mutation_headers(jar), cookies=jar,
    )
    assert bad_timeout.status == 422


@pytest.mark.anyio
async def test_enroll_with_real_sshd_persists_profile_and_pin(
    sshd_app, so_sshd
) -> None:
    """Success: 201 → canonical read-back → durable pin read-back → probe."""
    jar = await login_session(sshd_app)
    data_root: Path = sshd_app.state.profiles.root
    pin = so_sshd.identity.host_key

    response = await call_app(
        sshd_app, "POST", ENROLL_PATH,
        json_body=_enroll_body(so_sshd.port, pin),
        headers=_mutation_headers(jar), cookies=jar,
    )
    assert response.status == 201, response.body
    mutation = response.json()
    assert set(mutation) == {"server_id", "revision", "schema_revision", "sha256"}
    assert mutation["revision"] == 1
    server_id = uuid.UUID(mutation["server_id"])

    # Canonical read-back: the stored profile carries the observed host key
    # and the SERVER-side credential reference (override configured here).
    read = await call_app(
        sshd_app, "GET", f"/api/v1/servers/{server_id}", cookies=jar
    )
    assert read.status == 200, read.body
    profile = read.json()
    assert profile["ssh"]["host_key"] == pin
    assert profile["ssh"]["credential_ref"] == FIXTURE_CREDENTIAL_REF
    assert profile["ssh"]["host"] == "127.0.0.1"
    assert profile["ssh"]["port"] == so_sshd.port
    assert profile["privilege"] == "sudo"
    assert profile["projection"]["stale"] is False
    assert profile["revision"] == mutation["revision"]
    # No credential material or path ever reaches the browser.
    assert str(so_sshd.key_path) not in read.body.decode()
    assert "PRIVATE" not in read.body.decode()

    # Durable pin read-back: the ONE canonical pin, at exactly the path
    # terminal._known_hosts_path resolves for this server.
    pin_path = _known_hosts_path(sshd_app.state.profiles, server_id)
    assert pin_path == durable_known_hosts_path(data_root, server_id)
    assert pin_path == data_root / "servers" / str(server_id) / "known_hosts"
    assert pin_path.is_file()
    assert (os.stat(pin_path).st_mode & 0o777) == 0o600
    assert (os.stat(pin_path.parent).st_mode & 0o777) == 0o700
    pin_lines = pin_path.read_text(encoding="utf-8").splitlines()
    assert len(pin_lines) == 1
    assert pin_lines[0].split()[1] == "ssh-ed25519"
    # Enrollment created NO root-level pin directory.
    assert not (data_root / "known_hosts").exists()

    # The existing pinned probe_environment works immediately: no second
    # trust step, the persisted pin is the only trust state.
    observed = probe_environment(
        SshIdentity(
            host=profile["ssh"]["host"],
            port=profile["ssh"]["port"],
            user=profile["ssh"]["user"],
            host_key=profile["ssh"]["host_key"],
            credential_ref=profile["ssh"]["credential_ref"],
        ),
        key_path=so_sshd.key_path,
        known_hosts_path=pin_path,
    )
    assert observed.kernel_name == "Linux"
    assert observed.os_id == "arch"
    assert observed.boot_id
    # Exactly one canonical server directory (holding the pin + profile)
    # and no root-level pin directory exist.
    assert sorted(
        path.name for path in (data_root / "servers").iterdir()
    ) == [str(server_id)]
    assert not (data_root / "known_hosts").exists()


@pytest.mark.anyio
async def test_enroll_stores_fallback_credential_ref(
    migrated_url: str, tmp_path: Path, so_sshd
) -> None:
    """No ``MAI_SSH_CREDENTIAL_REF`` override: the installer fallback wins."""
    _truncate(migrated_url)
    env_file = _write_env_file(
        tmp_path / "fallback.env", {"MAI_SSH_KEY_MAI": str(so_sshd.key_path)}
    ).path
    app = _build_app(migrated_url, tmp_path / "data-root", env_file=env_file)
    async with _running(app):
        jar = await login_session(app)
        response = await call_app(
            app, "POST", ENROLL_PATH,
            json_body=_enroll_body(so_sshd.port, so_sshd.identity.host_key),
            headers=_mutation_headers(jar), cookies=jar,
        )
        assert response.status == 201, response.body
        server_id = response.json()["server_id"]
        read = await call_app(app, "GET", f"/api/v1/servers/{server_id}",
                              cookies=jar)
    assert read.status == 200, read.body
    assert read.json()["ssh"]["credential_ref"] == FALLBACK_CREDENTIAL_REF


@pytest.mark.anyio
async def test_stale_fingerprint_enroll_writes_nothing(sshd_app, so_sshd) -> None:
    """A key the target no longer presents: 409, zero canonical/pin writes."""
    jar = await login_session(sshd_app)
    data_root: Path = sshd_app.state.profiles.root
    before = _tree(data_root)
    stranger_pin = "SHA256:" + "z" * 43
    response = await call_app(
        sshd_app, "POST", ENROLL_PATH,
        json_body=_enroll_body(so_sshd.port, stranger_pin),
        headers=_mutation_headers(jar), cookies=jar,
    )
    assert response.status == 409
    assert _error(response)["code"] == "host_key_mismatch"
    assert _tree(data_root) == before
    listing = await call_app(sshd_app, "GET", "/api/v1/servers", cookies=jar)
    assert listing.json()["servers"] == []


@pytest.mark.anyio
async def test_auth_failure_enroll_writes_nothing(sshd_app, so_sshd) -> None:
    """An unauthorized identity key: 503 auth_failed, zero writes."""
    jar = await login_session(sshd_app)
    stranger_key = so_sshd.scratch / "enroll_stranger_key"
    _generate_key(stranger_key)
    stranger_env = _write_env_file(
        so_sshd.scratch / "enroll_stranger.env",
        {FIXTURE_CREDENTIAL_REF: str(stranger_key)},
    ).path
    bad_app = _build_app(
        sshd_app.state.env["MAI_DATABASE_URL"],
        sshd_app.state.profiles.root.parent / "enroll-bad-key-root",
        env_file=stranger_env,
        extra_env={DEFAULT_CREDENTIAL_REF_VAR: FIXTURE_CREDENTIAL_REF},
    )
    async with _running(bad_app):
        response = await call_app(
            bad_app, "POST", ENROLL_PATH,
            json_body=_enroll_body(
                so_sshd.port, so_sshd.identity.host_key, privilege="none"
            ),
            headers=_mutation_headers(jar), cookies=jar,
        )
        bad_root: Path = bad_app.state.profiles.root
    assert response.status == 503
    assert _error(response)["code"] == "auth_failed"
    assert _tree(bad_root) == []


@pytest.mark.anyio
async def test_privilege_failure_enroll_writes_nothing(
    migrated_url: str, tmp_path: Path, so_sshd
) -> None:
    """Password-required sudo for the limited user: privilege_unavailable."""
    _truncate(migrated_url)
    env_file = _write_env_file(
        tmp_path / "limited.env", {FIXTURE_LIMITED_REF: str(so_sshd.limited_key_path)}
    ).path
    app = _build_app(
        migrated_url,
        tmp_path / "data-root",
        env_file=env_file,
        extra_env={DEFAULT_CREDENTIAL_REF_VAR: FIXTURE_LIMITED_REF},
    )
    async with _running(app):
        jar = await login_session(app)
        response = await call_app(
            app, "POST", ENROLL_PATH,
            json_body=_enroll_body(
                so_sshd.port, so_sshd.identity.host_key,
                privilege="sudo", user="limited",
            ),
            headers=_mutation_headers(jar), cookies=jar,
        )
        data_root: Path = app.state.profiles.root
    assert response.status == 503
    assert _error(response)["code"] == "privilege_unavailable"
    assert _tree(data_root) == []


@pytest.mark.anyio
async def test_projection_failure_is_no_success_and_pin_is_cleaned(
    sshd_app, so_sshd, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Canonical failure after the pin: 409, orphan pin removed, no profile."""
    jar = await login_session(sshd_app)
    data_root: Path = sshd_app.state.profiles.root

    async def failing_upsert(*_a: object, **_k: object) -> object:
        raise ProjectionError(
            ProjectionErrorCode.INTENT_PENDING, "injected for the failure test"
        )

    monkeypatch.setattr(ProfileProjection, "apply_upsert", failing_upsert)
    response = await call_app(
        sshd_app, "POST", ENROLL_PATH,
        json_body=_enroll_body(so_sshd.port, so_sshd.identity.host_key),
        headers=_mutation_headers(jar), cookies=jar,
    )
    assert response.status == 409, response.body
    assert _error(response)["code"] == "conflict"
    # The pin created moments earlier was cleaned; no canonical profile and
    # no root-level pin directory anywhere.
    assert not (data_root / "known_hosts").exists()
    assert list(data_root.glob("servers/*/known_hosts")) == []
    listing = await call_app(sshd_app, "GET", "/api/v1/servers", cookies=jar)
    assert listing.json()["servers"] == []


@pytest.mark.anyio
async def test_unremovable_orphan_pin_is_reported_honestly(
    sshd_app, so_sshd, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When even the cleanup fails, the 409 says an orphan pin remains."""
    jar = await login_session(sshd_app)
    data_root: Path = sshd_app.state.profiles.root

    async def failing_upsert(*_a: object, **_k: object) -> object:
        raise ProjectionError(
            ProjectionErrorCode.INTENT_PENDING, "injected for the failure test"
        )

    real_unlink = Path.unlink

    def failing_unlink(self: Path, missing_ok: bool = False) -> None:
        # Break ONLY the canonical-pin cleanup: the temporary known-hosts
        # file of the verification step (system temp) must still clean up,
        # otherwise the injected OSError would mask the flow under test.
        if self.name == "known_hosts" and data_root in self.parents:
            raise OSError("injected unlink failure")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(ProfileProjection, "apply_upsert", failing_upsert)
    monkeypatch.setattr(Path, "unlink", failing_unlink)
    response = await call_app(
        sshd_app, "POST", ENROLL_PATH,
        json_body=_enroll_body(so_sshd.port, so_sshd.identity.host_key),
        headers=_mutation_headers(jar), cookies=jar,
    )
    assert response.status == 409
    error = _error(response)
    assert error["code"] == "conflict"
    assert "orphan pin" in error["message"]
    # Honest remainder: the orphan pin IS still on disk (no fake rollback).
    leftovers = list(data_root.glob("servers/*/known_hosts"))
    assert len(leftovers) == 1
    listing = await call_app(sshd_app, "GET", "/api/v1/servers", cookies=jar)
    assert listing.json()["servers"] == []
    os.unlink(leftovers[0])  # report-only leftover; leave no scratch behind


@pytest.mark.anyio
async def test_second_enrollment_separates_pins_and_refuses_stale_keys(
    sshd_app, so_sshd
) -> None:
    """Enrolling twice: two identities, two pins; stale keys re-trust nothing."""
    jar = await login_session(sshd_app)
    data_root: Path = sshd_app.state.profiles.root
    pin = so_sshd.identity.host_key
    first = await call_app(
        sshd_app, "POST", ENROLL_PATH,
        json_body=_enroll_body(so_sshd.port, pin, name="first"),
        headers=_mutation_headers(jar), cookies=jar,
    )
    assert first.status == 201, first.body
    first_id = uuid.UUID(first.json()["server_id"])
    first_pin = durable_known_hosts_path(data_root, first_id).read_bytes()

    stale = await call_app(
        sshd_app, "POST", ENROLL_PATH,
        json_body=_enroll_body(so_sshd.port, "SHA256:" + "z" * 43, name="stale"),
        headers=_mutation_headers(jar), cookies=jar,
    )
    assert stale.status == 409
    assert _error(stale)["code"] == "host_key_mismatch"
    # The stale refusal touched the existing pin nowhere.
    assert durable_known_hosts_path(data_root, first_id).read_bytes() == first_pin

    second = await call_app(
        sshd_app, "POST", ENROLL_PATH,
        json_body=_enroll_body(so_sshd.port, pin, name="second"),
        headers=_mutation_headers(jar), cookies=jar,
    )
    assert second.status == 201, second.body
    second_id = uuid.UUID(second.json()["server_id"])
    assert second_id != first_id
    assert (
        durable_known_hosts_path(data_root, second_id).read_bytes() == first_pin
    )
    assert sorted(
        path.name for path in (data_root / "servers").iterdir()
    ) == sorted({str(first_id), str(second_id)})
    assert not (data_root / "known_hosts").exists()


# ---------------------------------------------------------------------------
# Registration order and OpenAPI (so-01 + so-02 surface)
# ---------------------------------------------------------------------------


def _api_routes(app: FastAPI) -> list[APIRoute]:
    routes: list[APIRoute] = []
    for included in app.routes:
        original = getattr(included, "original_router", None)
        candidates = original.routes if original is not None else [included]
        routes.extend(route for route in candidates if isinstance(route, APIRoute))
    return routes


def test_onboarding_routes_precede_server_id_and_extend_openapi() -> None:
    app = create_app(env={})
    paths_in_order = [route.path for route in _api_routes(app)]
    for onboarding_path in (KEY_PATH, TEST_PATH, ENROLL_PATH):
        assert paths_in_order.index(onboarding_path) < paths_in_order.index(
            "/api/v1/servers/{server_id}"
        )
    schema = app.openapi()
    key_responses = set(schema["paths"][KEY_PATH]["get"]["responses"])
    assert key_responses == {"200", "401", "422", "503"}
    test_responses = set(schema["paths"][TEST_PATH]["post"]["responses"])
    assert test_responses == {"200", "401", "403", "409", "422", "503"}
    enroll = schema["paths"][ENROLL_PATH]["post"]
    assert set(enroll["responses"]) == {"201", "401", "403", "409", "422", "503"}
    assert enroll["responses"]["201"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ProfileMutationResult"
    }
    enroll_fields = set(
        schema["components"]["schemas"]["OnboardingEnrollRequest"]["properties"]
    )
    assert enroll_fields == {
        "name",
        "prompt_revision",
        "prompt_text",
        "environment",
        "privilege",
        "capabilities",
        "ssh",
        "host_key",
        "host_key_confirmed",
        "timeout_seconds",
    }
    # The complete typed enroll contract for task 03: the browser never
    # names a credential reference anywhere in the enroll request schema.
    assert "credential_ref" not in enroll_fields
    component_names = set(schema["components"]["schemas"])
    assert {
        "ControllerKeyResponse",
        "OnboardingTestRequest",
        "OnboardingDiscoveryResponse",
        "OnboardingTestResult",
        "OnboardingEnrollRequest",
        "ProfileMutationResult",
    } <= component_names
