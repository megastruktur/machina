"""Focused tests for the plugin configuration/enablement HTTP API (card 09-plugin-api).

Card ``09-plugin-api`` against contracts.md **C03** (rule 1 identifier
vocabulary, rule 2/6 credential references only, rule 3/7 grants are
explicit admin actions — never auto-granted, discovery never enables),
**C01** (envelope, opaque identity, revision conflicts), **C02** (canonical
store, revision triple) and **C08 rule 2** (session → origin → CSRF on
every mutation). The storage semantics underneath are covered by
``tests/test_plugin_config.py`` (09-plugin-store), the native runtime by
``tests/test_native_plugins.py`` (09-native-plugin), the MCP bridge by
``tests/test_mcp.py`` (09-mcp) and the skill loader by
``tests/test_skills.py`` (09-skills); this suite proves the HTTP surface
joining them.

Boundary discipline: every claim runs through the real ASGI app
(``create_app`` + lifespan + middleware + handlers) against a real
disposable Postgres (fixture ``migrated_url``) and a real confined data
root on the local filesystem — never mocks. The MCP discovery claims run
against a **real stdio MCP server subprocess** (the reviewed 09-mcp
fixture pattern): spawn, initialize, tools/list, teardown — availability
and grant separation is observed at the real transport boundary.

Registered P09 scenario fragments owned here (writer half; assembly is
``09-verify``): *the actual API configures a fixture native/MCP plugin,
rejects schema violations/secret leaks and changes a grant; the registered
tool surface follows the grant on the real transport.* The live model tool
list under policy is assembled by 09-verify from these fragments.

Secret hygiene: fixture credentials are inert constants for the disposable
fixture DB; the fixture credential *value* below is never sent anywhere —
only its reference name travels; session/CSRF tokens are minted at runtime
and never printed into assertion messages.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI

from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE
from mai.plugins.mcp import SERVER_CONFIG_SCHEMA
from mai.plugins.native import CREDENTIAL_DECLARATION, ENTRY_POINT_GROUP

BACKEND_ROOT = Path(__file__).resolve().parents[1]

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: Synthetic fixture credential (NOT a secret; disposable fixture DB only).
FIXTURE_PASSWORD = "fixture-password-09-plugin-api-only"
ORIGIN = "http://127.0.0.1:8443"  # loopback allowlist entry
EVIL_ORIGIN = "https://attacker.example"

#: Fixture identities (C01 rule 1: opaque).
SERVER_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")

#: Native fixture plugin: name == capability identifier (the 09-plugin-store
#: probe convention); declares a config schema and a credential reference
#: *name*. The value below is never transmitted by any test — it exists so
#: sweeps can prove no resolved material ever appears.
NATIVE_PLUGIN = "fxtapi.compile"
NATIVE_IDENTIFIER = "fxtapi.compile"
CREDENTIAL_NAME = "FXTAPI_TOKEN"
CREDENTIAL_VALUE = "s3cr3t-fixture-value-never-sent"
NATIVE_CONFIG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"region": {"type": "string", "minLength": 1}},
    "required": ["region"],
    "additionalProperties": False,
}
BROKEN_PLUGIN = "fxtapi.broken"
GHOST_PLUGIN = "ghost.plugin"

#: MCP fixture plugin backed by a real stdio server subprocess.
MCP_PLUGIN = "fxtapi.tools"
MCP_TOOL = "echo"
MCP_CAPABILITY = f"mcp.{MCP_PLUGIN}.{MCP_TOOL}"
UNOFFERED_CAPABILITY = f"mcp.{MCP_PLUGIN}.absent"

#: Built-in vault plugin (real installed distribution of this package) and
#: its service-account reference name. The fixture token value below is a
#: disposable marker, never a secret: sweeps prove it never travels.
PLUGIN_ONEPASSWORD = "onepassword"
OP_TOKEN = "OP_SERVICE_ACCOUNT_TOKEN"
OP_BADGE_VALUE = "op-fixture-value-never-echoed"
OP_CONFIG: dict[str, Any] = {"default_vault": "Dev", "allowed_vaults": ["Dev"]}

#: Skill fixture (09-skills frozen format).
SKILL_NAME = "deploy-notes"
SKILL_OTHER = "patch-notes"
SKILL_BROKEN = "broken-notes"
SKILL_REQUIRES = [NATIVE_IDENTIFIER, MCP_CAPABILITY]

_CANARY = "canary-inline-secret-value"


# ---------------------------------------------------------------------------
# Fixtures: real disposable Postgres + real app through lifespan
# ---------------------------------------------------------------------------


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(str(BACKEND_ROOT / "alembic.ini"))


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``head``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the plugin HTTP surface claims "
            "need a real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        with psycopg.connect(url) as conn:
            # Disposable fixture: reset the whole public schema (the
            # migration chain owns every table in it; the vector extension
            # is database-level and IF NOT EXISTS-safe across resets).
            conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
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
async def plugins_app(migrated_url: str, tmp_path: Path):
    """App on the real fixture DB with a fresh confined data root."""
    _truncate(migrated_url)
    app = _build_app(migrated_url, tmp_path / "data-root")
    async with _running(app):
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()


@pytest.fixture
def data_root(plugins_app: FastAPI, tmp_path: Path) -> Path:
    """The app's data root (skills fixtures stage files here)."""
    return Path(str(plugins_app.state.env["MAI_DATA_ROOT"]))


# ---------------------------------------------------------------------------
# Native fixture distribution (real installed shape; 09-native-plugin pattern)
# ---------------------------------------------------------------------------


_NATIVE_CAPABILITY_DOC: dict[str, Any] = {
    "identifier": NATIVE_IDENTIFIER,
    "version": "1.0.0",
    "config_schema": NATIVE_CONFIG_SCHEMA,
    "input_schema": {"type": "object"},
    "output_schema": {"type": "object"},
    "execution_environments": ["controller"],
    "effect_class": "read",
    "resource_scope": [],
    "required_grants": [NATIVE_IDENTIFIER],
}

_NATIVE_MODULE = (
    f"CAPABILITY = {_NATIVE_CAPABILITY_DOC!r}\n"
    f"{CREDENTIAL_DECLARATION} = [{CREDENTIAL_NAME!r}]\n\n\n"
    "def execute(call):\n"
    "    raise AssertionError('the HTTP surface never executes plugins')\n"
)

_BROKEN_MODULE = "raise RuntimeError('broken fixture plugin')\n"


def _install_plugins(root: Path, monkeypatch: Any) -> Path:
    """Install a real native distribution and put it on ``sys.path``.

    Exact on-disk shape ``importlib.metadata`` discovers (dist-info with
    ``entry_points.txt`` in group ``mai.capabilities``) — no installer
    involved. Entry-point *names* may carry dots (plugin-name slug
    charset); the *values* are dot-free module stems — a dotted value is
    a package path, not a module file. Fixture modules are purged from
    ``sys.modules`` first so the app's discovery imports what this test
    installed.
    """
    root.mkdir(parents=True, exist_ok=True)
    modules: dict[str, tuple[str, str]] = {
        NATIVE_PLUGIN: ("fxtapi_compile_mod", _NATIVE_MODULE),
        BROKEN_PLUGIN: ("fxtapi_broken_mod", _BROKEN_MODULE),
    }
    lines = [f"[{ENTRY_POINT_GROUP}]"]
    for name, (stem, source) in modules.items():
        sys.modules.pop(stem, None)
        (root / f"{stem}.py").write_text(source, encoding="utf-8")
        lines.append(f"{name} = {stem}")
    dist_info = root / "fxtapi-plugins-1.0.0.dist-info"
    dist_info.mkdir(exist_ok=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: fxtapi-plugins\nVersion: 1.0.0\n",
        encoding="utf-8",
    )
    (dist_info / "entry_points.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(root))
    importlib.invalidate_caches()
    return root


@pytest.fixture
def native_dist(tmp_path: Path, monkeypatch: Any) -> Path:
    return _install_plugins(tmp_path / "dist", monkeypatch)


# ---------------------------------------------------------------------------
# MCP stdio server fixture (real transport; reviewed 09-mcp pattern)
# ---------------------------------------------------------------------------

_MCP_SERVER = '''\
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fxtapi")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the text back."""
    return "echo:" + text


mcp.run("stdio")
'''


@pytest.fixture
def mcp_server_script(tmp_path: Path) -> Path:
    path = tmp_path / "fxtapi_mcp_server.py"
    path.write_text(_MCP_SERVER, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Skill package fixtures (09-skills frozen format, staged on the data root)
# ---------------------------------------------------------------------------


def _stage_skill(
    root: Path,
    name: str,
    *,
    requires: list[str],
    instructions: str = "# Steps\nDo it safely.\n",
    references: dict[str, str] | None = None,
    title: str | None = None,
    unknown_field: bool = False,
) -> None:
    package = root / "shared" / "skills" / name
    package.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "skill": name,
        "requires_capabilities": list(requires),
        "references": list(references or {}),
    }
    if title is not None:
        manifest["title"] = title
    if unknown_field:
        manifest["tools"] = ["definitely-not-allowed"]
    lines = [f"{key}: {json.dumps(value)}" for key, value in manifest.items()]
    (package / "skill.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (package / "instructions.md").write_text(instructions, encoding="utf-8")
    for relative, text in (references or {}).items():
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


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
    path, _, query = path.partition("?")
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


@pytest.fixture
async def jar(plugins_app: FastAPI) -> dict[str, str]:
    """Bootstrap once and return the authenticated cookie jar."""
    bootstrap = await call_app(
        plugins_app,
        "POST",
        "/api/v1/auth/bootstrap",
        json_body={"password": FIXTURE_PASSWORD},
        headers={"origin": ORIGIN},
    )
    assert bootstrap.status == 201, bootstrap.body
    return await _login(plugins_app)


def _mutation_headers(jar_: Mapping[str, str]) -> dict[str, str]:
    return {
        "origin": ORIGIN,
        "x-csrf-token": jar_[CSRF_COOKIE],
    }


# ---------------------------------------------------------------------------
# Plugin-surface request helpers
# ---------------------------------------------------------------------------


def _config_body(
    expected_revision: int,
    *,
    enabled: bool = True,
    refs: list[str] | None = None,
    config: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "expected_revision": expected_revision,
        "enabled": enabled,
        "credential_refs": list(refs or ()),
        "config": dict(config or {}),
    }
    body.update(extra)
    return body


async def _get(app: FastAPI, jar_: Mapping[str, str], path: str) -> HttpResponse:
    return await call_app(app, "GET", path, cookies=jar_)


async def _mutate(
    app: FastAPI,
    jar_: Mapping[str, str],
    method: str,
    path: str,
    body: Any = None,
) -> HttpResponse:
    return await call_app(
        app,
        method,
        path,
        json_body=body,
        headers=_mutation_headers(jar_),
        cookies=jar_,
    )


async def _put_config(
    app: FastAPI, jar_: Mapping[str, str], plugin: str, body: Mapping[str, Any]
) -> HttpResponse:
    return await _mutate(app, jar_, "PUT", f"/api/v1/plugins/{plugin}/config", body)


async def _put_binding(
    app: FastAPI,
    jar_: Mapping[str, str],
    plugin: str,
    server_id: uuid.UUID,
    expected_revision: int,
    capabilities: list[str],
) -> HttpResponse:
    return await _mutate(
        app,
        jar_,
        "PUT",
        f"/api/v1/plugins/{plugin}/bindings/{server_id}",
        {"expected_revision": expected_revision, "capabilities": capabilities},
    )


def _delete(path: str, expected_revision: int) -> str:
    return f"{path}?expected_revision={expected_revision}"


def _sweep(value: object, *forbidden: str) -> None:
    """No forbidden string anywhere in a JSON-safe structure."""
    encoded = json.dumps(value, default=str)
    for needle in forbidden:
        assert needle not in encoded, f"leaked {needle!r}"


# ---------------------------------------------------------------------------
# C08 boundary: session → origin → CSRF on every route
# ---------------------------------------------------------------------------


#: The full route matrix (C08 rule 2: every route answers 401 unauthenticated).
Routes = [
    ("GET", "/api/v1/plugins", None),
    ("GET", f"/api/v1/plugins/{NATIVE_PLUGIN}/config", None),
    ("PUT", f"/api/v1/plugins/{NATIVE_PLUGIN}/config", _config_body(0)),
    ("DELETE", _delete(f"/api/v1/plugins/{NATIVE_PLUGIN}/config", 0), None),
    ("GET", f"/api/v1/plugins/{NATIVE_PLUGIN}/schema", None),
    ("GET", f"/api/v1/plugins/{NATIVE_PLUGIN}/bindings", None),
    (
        "PUT",
        f"/api/v1/plugins/{NATIVE_PLUGIN}/bindings/{SERVER_A}",
        {"expected_revision": 0, "capabilities": [NATIVE_IDENTIFIER]},
    ),
    (
        "DELETE",
        _delete(f"/api/v1/plugins/{NATIVE_PLUGIN}/bindings/{SERVER_A}", 0),
        None,
    ),
    ("POST", f"/api/v1/plugins/{NATIVE_PLUGIN}/discovery", {}),
    ("POST", f"/api/v1/plugins/{NATIVE_PLUGIN}/probe", {}),
    ("GET", "/api/v1/skills", None),
    ("GET", f"/api/v1/skills/{SKILL_NAME}", None),
]


class TestBoundary:
    """C08 rule 2: the whole surface sits behind the three layers."""

    @pytest.mark.anyio
    @pytest.mark.parametrize("method,path,body", Routes)
    async def test_every_route_requires_a_session(
        self, plugins_app: FastAPI, method: str, path: str, body: Any
    ) -> None:
        """No route answers without a valid session (401, no state)."""
        response = await call_app(plugins_app, method, path, json_body=body)
        assert response.status == 401, response.body
        error = _error(response)
        assert error["code"] == "unauthenticated"
        assert error["fields"] == {}

    @pytest.mark.anyio
    async def test_mutations_require_origin_then_csrf(
        self, plugins_app: FastAPI, jar: dict[str, str]
    ) -> None:
        """Origin precedes CSRF precedes any effect; denials mutate nothing."""
        # PUT config: no Origin at all.
        response = await call_app(
            plugins_app,
            "PUT",
            f"/api/v1/plugins/{NATIVE_PLUGIN}/config",
            json_body=_config_body(0),
            cookies=jar,
        )
        assert response.status == 403
        assert _error(response)["code"] == "origin_rejected"
        # PUT config: evil origin.
        response = await call_app(
            plugins_app,
            "PUT",
            f"/api/v1/plugins/{NATIVE_PLUGIN}/config",
            json_body=_config_body(0),
            headers={"origin": EVIL_ORIGIN, "x-csrf-token": jar[CSRF_COOKIE]},
            cookies=jar,
        )
        assert response.status == 403
        assert _error(response)["code"] == "origin_rejected"
        # PUT config: right origin, missing CSRF token.
        response = await call_app(
            plugins_app,
            "PUT",
            f"/api/v1/plugins/{NATIVE_PLUGIN}/config",
            json_body=_config_body(0),
            headers={"origin": ORIGIN},
            cookies=jar,
        )
        assert response.status == 403
        assert _error(response)["code"] == "csrf_rejected"
        # DELETE binding and POST discovery enforce the same layers.
        for method, path, body in (
            ("DELETE", _delete(f"/api/v1/plugins/{NATIVE_PLUGIN}/bindings/{SERVER_A}", 0), None),
            ("POST", f"/api/v1/plugins/{NATIVE_PLUGIN}/discovery", {}),
        ):
            for headers in ({}, {"origin": EVIL_ORIGIN}, {"origin": ORIGIN}):
                response = await call_app(
                    plugins_app,
                    method,
                    path,
                    json_body=body,
                    headers=headers,
                    cookies=jar,
                )
                assert response.status == 403, (method, headers, response.body)
                assert _error(response)["code"] in {
                    "origin_rejected",
                    "csrf_rejected",
                }
        # No write ever landed: the store has no config for the plugin.
        response = await _get(plugins_app, jar, f"/api/v1/plugins/{NATIVE_PLUGIN}/config")
        assert response.status == 404

    @pytest.mark.anyio
    async def test_malformed_plugin_name_is_a_structured_422(
        self, plugins_app: FastAPI, jar: dict[str, str]
    ) -> None:
        """The slug charset is enforced at transport (C03 rule 1)."""
        response = await _get(plugins_app, jar, "/api/v1/plugins/BadName/config")
        assert response.status == 422
        error = _error(response)
        assert error["code"] == "validation_failed"
        assert list(error["fields"].values()) == ["invalid"]
        _sweep(error, "BadName")  # value-free: the value is never echoed


# ---------------------------------------------------------------------------
# Catalog: availability separate from grants (AC2)
# ---------------------------------------------------------------------------


class TestCatalog:
    @pytest.mark.anyio
    async def test_empty_root_reports_empty_catalog(
        self, plugins_app: FastAPI, jar: dict[str, str]
    ) -> None:
        response = await _get(plugins_app, jar, "/api/v1/plugins")
        assert response.status == 200
        catalog = response.json()
        assert catalog["configured"] == []
        assert isinstance(catalog["installed"], list)

    @pytest.mark.anyio
    async def test_configured_plugin_shows_state_and_bindings_separately(
        self,
        plugins_app: FastAPI,
        jar: dict[str, str],
        native_dist: Path,
    ) -> None:
        assert (await _put_config(
            plugins_app, jar, NATIVE_PLUGIN, _config_body(0, refs=[CREDENTIAL_NAME], config={"region": "eu"})
        )).status == 200
        assert (
            await _put_binding(
                plugins_app, jar, NATIVE_PLUGIN, SERVER_A, 0, [NATIVE_IDENTIFIER]
            )
        ).status == 200
        response = await _get(plugins_app, jar, "/api/v1/plugins")
        assert response.status == 200
        catalog = response.json()
        assert NATIVE_PLUGIN in catalog["installed"]
        assert len(catalog["configured"]) == 1
        view = catalog["configured"][0]
        assert view["plugin"] == NATIVE_PLUGIN
        assert view["enabled"] is True
        assert view["kind"] == "native"
        assert view["credential_refs"] == [CREDENTIAL_NAME]
        assert view["config"] == {"region": "eu"}
        assert view["revision"] == 1 and view["schema_revision"] == 1
        assert len(view["sha256"]) == 64
        # The grant section is separate data, not a flag on availability.
        assert view["bindings"] == [
            {
                "server_id": str(SERVER_A),
                "capabilities": [NATIVE_IDENTIFIER],
                "revision": 1,
                "schema_revision": 1,
                "sha256": view["bindings"][0]["sha256"],
            }
        ]
        _sweep(catalog, CREDENTIAL_VALUE)

    @pytest.mark.anyio
    async def test_configured_but_uninstalled_plugin_is_surfaced(
        self, plugins_app: FastAPI, jar: dict[str, str]
    ) -> None:
        """A configured plugin with no installed code is visible (C02 W4)."""
        assert (
            await _put_config(plugins_app, jar, GHOST_PLUGIN, _config_body(0, enabled=False))
        ).status == 200
        catalog = (await _get(plugins_app, jar, "/api/v1/plugins")).json()
        assert catalog["configured"][0]["kind"] == "uninstalled"
        assert catalog["configured"][0]["enabled"] is False
        assert catalog["configured"][0]["bindings"] == []

    @pytest.mark.anyio
    async def test_transport_config_classifies_as_mcp(
        self, plugins_app: FastAPI, jar: dict[str, str], mcp_server_script: Path
    ) -> None:
        body = _config_body(
            0,
            config={
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(mcp_server_script)],
            },
        )
        assert (await _put_config(plugins_app, jar, MCP_PLUGIN, body)).status == 200
        catalog = (await _get(plugins_app, jar, "/api/v1/plugins")).json()
        assert catalog["configured"][0]["kind"] == "mcp"

    @pytest.mark.anyio
    async def test_inline_credential_key_is_rejected_and_never_persisted(
        self, plugins_app: FastAPI, jar: dict[str, str]
    ) -> None:
        """C03 rule 6 at the HTTP boundary: no credential-shaped config key."""
        response = await _put_config(
            plugins_app,
            jar,
            NATIVE_PLUGIN,
            _config_body(0, config={"api_key": _CANARY}),
        )
        assert response.status == 422
        error = _error(response)
        assert error["fields"] == {"document": "inline_credential_forbidden"}
        _sweep(error, _CANARY)  # value-free: the canary is never echoed
        # Negative observable: nothing was stored.
        assert (
            await _get(plugins_app, jar, f"/api/v1/plugins/{NATIVE_PLUGIN}/config")
        ).status == 404
        catalog = (await _get(plugins_app, jar, "/api/v1/plugins")).json()
        assert catalog["configured"] == []
        _sweep(catalog, _CANARY)

    @pytest.mark.anyio
    async def test_catalog_pagination_envelope_and_defaults(
        self,
        plugins_app: FastAPI,
        jar: dict[str, str],
        native_dist: Path,
    ) -> None:
        """``configured`` is one deterministic bounded page (AGENTS §1)."""
        assert (
            await _put_config(plugins_app, jar, NATIVE_PLUGIN, _config_body(0, config={"region": "eu"}))
        ).status == 200
        assert (
            await _put_config(plugins_app, jar, GHOST_PLUGIN, _config_body(0, enabled=False))
        ).status == 200
        default = (await _get(plugins_app, jar, "/api/v1/plugins")).json()
        assert default["limit"] == 50 and default["offset"] == 0
        assert default["total"] == 2
        assert len(default["configured"]) == 2
        first = (await _get(plugins_app, jar, "/api/v1/plugins?limit=1&offset=0")).json()
        assert first["limit"] == 1 and first["offset"] == 0 and first["total"] == 2
        assert len(first["configured"]) == 1
        second = (await _get(plugins_app, jar, "/api/v1/plugins?limit=1&offset=1")).json()
        assert second["offset"] == 1 and second["total"] == 2
        assert len(second["configured"]) == 1
        assert second["configured"][0]["plugin"] != first["configured"][0]["plugin"]
        # Beyond the end: an empty page, but the total stays honest.
        beyond = (await _get(plugins_app, jar, "/api/v1/plugins?limit=1&offset=9")).json()
        assert beyond["configured"] == [] and beyond["total"] == 2
        # Deterministic order: pages tile the sorted plugin list.
        names = [first["configured"][0]["plugin"], second["configured"][0]["plugin"]]
        assert names == sorted(names)

    @pytest.mark.anyio
    async def test_catalog_limit_offset_bounds_are_transport_enforced(
        self, plugins_app: FastAPI, jar: dict[str, str]
    ) -> None:
        for query in ("limit=0", "limit=201", "offset=-1", "limit=bad"):
            response = await _get(plugins_app, jar, f"/api/v1/plugins?{query}")
            assert response.status == 422, query
            assert _error(response)["code"] == "validation_failed"
        # The documented upper bound itself answers 200.
        assert (
            await _get(plugins_app, jar, "/api/v1/plugins?limit=200")
        ).status == 200

    @pytest.mark.anyio
    async def test_installed_plugins_enrichment_includes_builtins(
        self,
        plugins_app: FastAPI,
        jar: dict[str, str],
        native_dist: Path,
    ) -> None:
        """The rich catalog covers built-in and third-party loaded plugins."""
        catalog = (await _get(plugins_app, jar, "/api/v1/plugins")).json()
        by_plugin = {view["plugin"]: view for view in catalog["installed_plugins"]}
        assert NATIVE_PLUGIN in by_plugin
        native = by_plugin[NATIVE_PLUGIN]
        assert native["title"] == NATIVE_PLUGIN  # no PLUGIN_METADATA declared
        assert native["description"] == ""
        assert native["capabilities"] == [NATIVE_IDENTIFIER]
        assert native["required_credentials"] == [CREDENTIAL_NAME]
        assert native["configured_credentials"] == []
        assert native["config_schema"] == NATIVE_CONFIG_SCHEMA
        # The built-in vault plugin (include_builtins runtime).
        assert PLUGIN_ONEPASSWORD in by_plugin
        op = by_plugin[PLUGIN_ONEPASSWORD]
        assert op["title"] == "1Password Vault"
        assert "1Password" in op["description"]
        assert op["required_credentials"] == [OP_TOKEN]
        assert len(op["capabilities"]) == 3
        assert all(cap.startswith("secret.") for cap in op["capabilities"])
        assert op["config_schema"] is not None
        assert op["config_schema"]["required"] == ["default_vault", "allowed_vaults"]
        assert op["configured_credentials"] == []
        _sweep(catalog, CREDENTIAL_VALUE)

    @pytest.mark.anyio
    async def test_configured_credentials_badge_reflects_runtime_keys(
        self,
        plugins_app: FastAPI,
        jar: dict[str, str],
        native_dist: Path,
        data_root: Path,
    ) -> None:
        """The badge is the configured subset of the declared references."""
        badge_value = "badge-fixture-value-never-echoed"
        assert (
            await _put_config(
                plugins_app,
                jar,
                NATIVE_PLUGIN,
                _config_body(
                    0,
                    refs=[CREDENTIAL_NAME],
                    config={"region": "eu"},
                    credential_values={CREDENTIAL_NAME: badge_value},
                ),
            )
        ).status == 200
        # A configured plugin whose references have no values: empty badge.
        assert (
            await _put_config(
                plugins_app, jar, GHOST_PLUGIN, _config_body(0, enabled=False, refs=[CREDENTIAL_NAME])
            )
        ).status == 200
        catalog = (await _get(plugins_app, jar, "/api/v1/plugins")).json()
        views = {view["plugin"]: view for view in catalog["configured"]}
        assert views[NATIVE_PLUGIN]["configured_credentials"] == [CREDENTIAL_NAME]
        # The ghost plugin declares the same reference name; its badge is
        # honest about the runtime key set (the value is configured).
        assert views[GHOST_PLUGIN]["configured_credentials"] == [CREDENTIAL_NAME]
        # The stored-document read carries the same badge.
        view = (
            await _get(plugins_app, jar, f"/api/v1/plugins/{NATIVE_PLUGIN}/config")
        ).json()
        assert view["configured_credentials"] == [CREDENTIAL_NAME]
        # The 0600 store exists below the data root; the value never travels.
        assert (data_root / "credentials.env").is_file()
        _sweep(catalog, badge_value, CREDENTIAL_VALUE)
        _sweep(view, badge_value)


# ---------------------------------------------------------------------------
# Config lifecycle: versioned edits, schema-strict writes (AC1)
# ---------------------------------------------------------------------------


class TestConfigLifecycle:
    @pytest.mark.anyio
    async def test_create_read_update_cycle(
        self,
        plugins_app: FastAPI,
        jar: dict[str, str],
        native_dist: Path,
    ) -> None:
        created = await _put_config(
            plugins_app, jar, NATIVE_PLUGIN, _config_body(0, refs=[CREDENTIAL_NAME], config={"region": "eu"})
        )
        assert created.status == 200
        triple = created.json()
        assert triple["revision"] == 1
        assert triple["schema_revision"] == 1
        assert len(triple["sha256"]) == 64
        view = (
            await _get(plugins_app, jar, f"/api/v1/plugins/{NATIVE_PLUGIN}/config")
        ).json()
        assert view["plugin"] == NATIVE_PLUGIN
        assert view["enabled"] is True
        assert view["credential_refs"] == [CREDENTIAL_NAME]
        assert view["config"] == {"region": "eu"}
        assert view["revision"] == 1
        updated = await _put_config(
            plugins_app,
            jar,
            NATIVE_PLUGIN,
            _config_body(1, enabled=False, config={"region": "eu"}),
        )
        assert updated.status == 200
        assert updated.json()["revision"] == 2
        view = (
            await _get(plugins_app, jar, f"/api/v1/plugins/{NATIVE_PLUGIN}/config")
        ).json()
        assert view["enabled"] is False and view["revision"] == 2

    @pytest.mark.anyio
    async def test_stale_revision_rejected_before_any_effect(
        self,
        plugins_app: FastAPI,
        jar: dict[str, str],
        native_dist: Path,
    ) -> None:
        assert (
            await _put_config(plugins_app, jar, NATIVE_PLUGIN, _config_body(0, config={"region": "eu"}))
        ).status == 200
        stale = await _put_config(
            plugins_app, jar, NATIVE_PLUGIN, _config_body(0, config={"region": "us"})
        )
        assert stale.status == 409
        assert _error(stale)["code"] == "conflict"
        view = (
            await _get(plugins_app, jar, f"/api/v1/plugins/{NATIVE_PLUGIN}/config")
        ).json()
        assert view["config"] == {"region": "eu"}  # no clobber
        assert view["revision"] == 1

    @pytest.mark.anyio
    async def test_config_values_validate_against_declared_schema(
        self,
        plugins_app: FastAPI,
        jar: dict[str, str],
        native_dist: Path,
    ) -> None:
        """Schema-strict: the installed descriptor's schema governs writes."""
        for bad in ({"region": 7}, {"region": ""}, {"region": "eu", "extra": 1}, {}):
            response = await _put_config(
                plugins_app, jar, NATIVE_PLUGIN, _config_body(0, config=bad)
            )
            assert response.status == 422, bad
            error = _error(response)
            assert error["fields"] == {"document": "config_schema_invalid"}
        # Nothing stored after the rejections.
        assert (
            await _get(plugins_app, jar, f"/api/v1/plugins/{NATIVE_PLUGIN}/config")
        ).status == 404

    @pytest.mark.anyio
    async def test_unknown_plugin_rejects_nonempty_config_fail_closed(
        self, plugins_app: FastAPI, jar: dict[str, str]
    ) -> None:
        """No declared schema ⇒ any non-empty config is a rejection."""
        response = await _put_config(
            plugins_app, jar, GHOST_PLUGIN, _config_body(0, config={"a": 1})
        )
        assert response.status == 422
        assert _error(response)["fields"] == {"document": "config_schema_invalid"}

    @pytest.mark.anyio
    async def test_delete_refused_while_bindings_exist_then_ordering_holds(
        self,
        plugins_app: FastAPI,
        jar: dict[str, str],
        native_dist: Path,
    ) -> None:
        assert (
            await _put_config(plugins_app, jar, NATIVE_PLUGIN, _config_body(0, config={"region": "eu"}))
        ).status == 200
        assert (
            await _put_binding(
                plugins_app, jar, NATIVE_PLUGIN, SERVER_A, 0, [NATIVE_IDENTIFIER]
            )
        ).status == 200
        refused = await _mutate(
            plugins_app,
            jar,
            "DELETE",
            _delete(f"/api/v1/plugins/{NATIVE_PLUGIN}/config", 1),
        )
        assert refused.status == 409
        error = _error(refused)
        assert error["fields"] == {"binding": "bindings_present"}
        # The grant survived the refused delete (no partial effect).
        bindings = (
            await _get(plugins_app, jar, f"/api/v1/plugins/{NATIVE_PLUGIN}/bindings")
        ).json()
        assert len(bindings) == 1
        # Bindings first, then the config goes.
        assert (
            await _mutate(
                plugins_app,
                jar,
                "DELETE",
                _delete(f"/api/v1/plugins/{NATIVE_PLUGIN}/bindings/{SERVER_A}", 1),
            )
        ).status == 204
        assert (
            await _mutate(
                plugins_app,
                jar,
                "DELETE",
                _delete(f"/api/v1/plugins/{NATIVE_PLUGIN}/config", 1),
            )
        ).status == 204
        assert (
            await _get(plugins_app, jar, f"/api/v1/plugins/{NATIVE_PLUGIN}/config")
        ).status == 404

    @pytest.mark.anyio
    async def test_delete_missing_config_is_404(
        self, plugins_app: FastAPI, jar: dict[str, str]
    ) -> None:
        response = await _mutate(
            plugins_app,
            jar,
            "DELETE",
            _delete(f"/api/v1/plugins/{NATIVE_PLUGIN}/config", 0),
        )
        assert response.status == 404
        assert _error(response)["code"] == "not_found"

    @pytest.mark.anyio
    async def test_credential_value_rejection_leaves_config_untouched(
        self,
        plugins_app: FastAPI,
        jar: dict[str, str],
        native_dist: Path,
        data_root: Path,
    ) -> None:
        """Credential-first ordering: an invalid value fails the whole edit.

        The credential store write (and its validation) happens before the
        canonical config write, so a rejected value never leaves a
        half-applied edit behind.
        """
        response = await _put_config(
            plugins_app,
            jar,
            NATIVE_PLUGIN,
            _config_body(
                0,
                refs=[CREDENTIAL_NAME],
                config={"region": "eu"},
                credential_values={CREDENTIAL_NAME: ""},
            ),
        )
        assert response.status == 422
        error = _error(response)
        assert error["code"] == "validation_error"
        assert error["fields"] == {"credential_values": "empty_value"}
        _sweep(error, CREDENTIAL_VALUE)
        # Nothing was stored and no credentials.env was created.
        assert (
            await _get(plugins_app, jar, f"/api/v1/plugins/{NATIVE_PLUGIN}/config")
        ).status == 404
        assert not (data_root / "credentials.env").exists()
        # Undeclared keys are refused at the same boundary, before any write.
        undeclared = await _put_config(
            plugins_app,
            jar,
            NATIVE_PLUGIN,
            _config_body(
                0,
                config={"region": "eu"},
                credential_values={"UNDECLARED_TOKEN": "x"},
            ),
        )
        assert undeclared.status == 422
        assert _error(undeclared)["fields"] == {
            "credential_values": "undeclared_credential_ref"
        }
        assert not (data_root / "credentials.env").exists()

    @pytest.mark.anyio
    async def test_onepassword_enable_requires_a_configured_token(
        self,
        plugins_app: FastAPI,
        jar: dict[str, str],
        data_root: Path,
    ) -> None:
        """``enabled: true`` is refused while the token has no value.

        The gate answers from the runtime key set *after* upserting the
        request's ``credential_values`` and *before* the config write.
        References with externally mounted values (no credential_values in
        the request) stay valid for every other plugin — only the built-in
        vault plugin is gated.
        """
        gated = await _put_config(
            plugins_app,
            jar,
            PLUGIN_ONEPASSWORD,
            _config_body(0, refs=[OP_TOKEN], config=dict(OP_CONFIG)),
        )
        assert gated.status == 422
        error = _error(gated)
        assert error["code"] == "validation_error"
        assert error["fields"] == {"credential_refs": "missing_credential_value"}
        # The refusal precedes the config write: nothing stored.
        assert (
            await _get(plugins_app, jar, f"/api/v1/plugins/{PLUGIN_ONEPASSWORD}/config")
        ).status == 404
        assert not (data_root / "credentials.env").exists()
        # Supplying the value in the same request passes the gate.
        enabled = await _put_config(
            plugins_app,
            jar,
            PLUGIN_ONEPASSWORD,
            _config_body(
                0,
                refs=[OP_TOKEN],
                config=dict(OP_CONFIG),
                credential_values={OP_TOKEN: OP_BADGE_VALUE},
            ),
        )
        assert enabled.status == 200, enabled.body
        assert (data_root / "credentials.env").is_file()
        view = (
            await _get(plugins_app, jar, f"/api/v1/plugins/{PLUGIN_ONEPASSWORD}/config")
        ).json()
        assert view["enabled"] is True
        assert view["credential_refs"] == [OP_TOKEN]
        assert view["configured_credentials"] == [OP_TOKEN]
        _sweep(view, OP_BADGE_VALUE)


# ---------------------------------------------------------------------------
# Declared schemas (the schema-generated-forms input, IDEA §13)
# ---------------------------------------------------------------------------


class TestSchemaEndpoint:
    @pytest.mark.anyio
    async def test_native_plugin_declares_its_schema(
        self, plugins_app: FastAPI, jar: dict[str, str], native_dist: Path
    ) -> None:
        response = await _get(
            plugins_app, jar, f"/api/v1/plugins/{NATIVE_PLUGIN}/schema"
        )
        assert response.status == 200
        view = response.json()
        assert view["plugin"] == NATIVE_PLUGIN
        assert view["kind"] == "native"
        assert view["config_schema"] == NATIVE_CONFIG_SCHEMA
        assert view["load_rejection"] is None

    @pytest.mark.anyio
    async def test_mcp_plugin_declares_the_frozen_server_schema(
        self, plugins_app: FastAPI, jar: dict[str, str], mcp_server_script: Path
    ) -> None:
        body = _config_body(
            0,
            config={
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(mcp_server_script)],
            },
        )
        assert (await _put_config(plugins_app, jar, MCP_PLUGIN, body)).status == 200
        view = (
            await _get(plugins_app, jar, f"/api/v1/plugins/{MCP_PLUGIN}/schema")
        ).json()
        assert view["kind"] == "mcp"
        assert view["config_schema"] == SERVER_CONFIG_SCHEMA

    @pytest.mark.anyio
    async def test_uninstalled_configured_plugin_reports_null_schema(
        self, plugins_app: FastAPI, jar: dict[str, str]
    ) -> None:
        assert (
            await _put_config(plugins_app, jar, GHOST_PLUGIN, _config_body(0))
        ).status == 200
        view = (
            await _get(plugins_app, jar, f"/api/v1/plugins/{GHOST_PLUGIN}/schema")
        ).json()
        assert view["kind"] == "uninstalled"
        assert view["config_schema"] is None

    @pytest.mark.anyio
    async def test_unknown_plugin_is_404(
        self, plugins_app: FastAPI, jar: dict[str, str]
    ) -> None:
        response = await _get(
            plugins_app, jar, "/api/v1/plugins/never.heard-of/schema"
        )
        assert response.status == 404

    @pytest.mark.anyio
    async def test_broken_plugin_reports_value_free_rejection(
        self, plugins_app: FastAPI, jar: dict[str, str], native_dist: Path
    ) -> None:
        """A load rejection surfaces by stable code; no schema invented."""
        view = (
            await _get(plugins_app, jar, f"/api/v1/plugins/{BROKEN_PLUGIN}/schema")
        ).json()
        assert view["kind"] == "native"
        assert view["config_schema"] is None
        assert view["load_rejection"] == "import_failed"
        _sweep(view, "RuntimeError", "broken fixture plugin")


# ---------------------------------------------------------------------------
# Server grant bindings (explicit admin grants; C03 rule 3/7)
# ---------------------------------------------------------------------------


class TestBindings:
    @pytest.mark.anyio
    async def test_binding_requires_connection_config(
        self, plugins_app: FastAPI, jar: dict[str, str], native_dist: Path
    ) -> None:
        """No auto-grant: the store refuses to invent a config (409)."""
        response = await _put_binding(
            plugins_app, jar, NATIVE_PLUGIN, SERVER_A, 0, [NATIVE_IDENTIFIER]
        )
        assert response.status == 409
        error = _error(response)
        assert error["code"] == "conflict"
        assert error["fields"] == {"binding": "connection_config_missing"}
        bindings = (
            await _get(plugins_app, jar, f"/api/v1/plugins/{NATIVE_PLUGIN}/bindings")
        ).json()
        assert bindings == []

    @pytest.mark.anyio
    async def test_binding_write_read_revoke_cycle(
        self,
        plugins_app: FastAPI,
        jar: dict[str, str],
        native_dist: Path,
    ) -> None:
        assert (
            await _put_config(plugins_app, jar, NATIVE_PLUGIN, _config_body(0, config={"region": "eu"}))
        ).status == 200
        created = await _put_binding(
            plugins_app, jar, NATIVE_PLUGIN, SERVER_A, 0, [NATIVE_IDENTIFIER]
        )
        assert created.status == 200
        assert created.json()["revision"] == 1
        updated = await _put_binding(
            plugins_app, jar, NATIVE_PLUGIN, SERVER_A, 1, [NATIVE_IDENTIFIER]
        )
        assert updated.status == 200
        assert updated.json()["revision"] == 2
        bindings = (
            await _get(plugins_app, jar, f"/api/v1/plugins/{NATIVE_PLUGIN}/bindings")
        ).json()
        assert bindings == [
            {
                "server_id": str(SERVER_A),
                "capabilities": [NATIVE_IDENTIFIER],
                "revision": 2,
                "schema_revision": 1,
                "sha256": bindings[0]["sha256"],
            }
        ]
        stale = await _mutate(
            plugins_app,
            jar,
            "DELETE",
            _delete(f"/api/v1/plugins/{NATIVE_PLUGIN}/bindings/{SERVER_A}", 1),
        )
        assert stale.status == 409
        assert (
            await _mutate(
                plugins_app,
                jar,
                "DELETE",
                _delete(f"/api/v1/plugins/{NATIVE_PLUGIN}/bindings/{SERVER_A}", 2),
            )
        ).status == 204
        bindings = (
            await _get(plugins_app, jar, f"/api/v1/plugins/{NATIVE_PLUGIN}/bindings")
        ).json()
        assert bindings == []
        # Revoking the grant never deletes the connection config.
        assert (
            await _get(plugins_app, jar, f"/api/v1/plugins/{NATIVE_PLUGIN}/config")
        ).status == 200

    @pytest.mark.anyio
    async def test_binding_rejects_duplicate_capabilities(
        self,
        plugins_app: FastAPI,
        jar: dict[str, str],
        native_dist: Path,
    ) -> None:
        assert (
            await _put_config(plugins_app, jar, NATIVE_PLUGIN, _config_body(0, config={"region": "eu"}))
        ).status == 200
        response = await _put_binding(
            plugins_app,
            jar,
            NATIVE_PLUGIN,
            SERVER_A,
            0,
            [NATIVE_IDENTIFIER, NATIVE_IDENTIFIER],
        )
        assert response.status == 422
        assert _error(response)["fields"] == {"document": "field_type_invalid"}
        # Empty capability lists are a transport rejection (min_length 1).
        response = await _put_binding(
            plugins_app, jar, NATIVE_PLUGIN, SERVER_A, 0, []
        )
        assert response.status == 422


# ---------------------------------------------------------------------------
# Bounded MCP discovery on the real transport (09-mcp producer)
# ---------------------------------------------------------------------------


async def _configure_mcp(
    plugins_app: FastAPI,
    jar: dict[str, str],
    script: Path,
    *,
    enabled: bool = True,
    command: str | None = None,
    args: list[str] | None = None,
) -> None:
    """Store a stdio MCP connection config through the real surface."""
    config: dict[str, Any] = {
        "transport": "stdio",
        "command": command or sys.executable,
        "args": args or [str(script)],
    }
    body = _config_body(0, enabled=enabled, config=config)
    response = await _put_config(plugins_app, jar, MCP_PLUGIN, body)
    assert response.status == 200, response.body


class TestMcpDiscovery:
    @pytest.mark.anyio
    async def test_discovery_without_grant_registers_nothing(
        self,
        plugins_app: FastAPI,
        jar: dict[str, str],
        mcp_server_script: Path,
    ) -> None:
        """Availability ≠ grant, observed on the real transport (AC2)."""
        await _configure_mcp(plugins_app, jar, mcp_server_script)
        response = await _mutate(
            plugins_app, jar, "POST", f"/api/v1/plugins/{MCP_PLUGIN}/discovery", {}
        )
        assert response.status == 200, response.body
        report = response.json()
        assert report["plugin"] == MCP_PLUGIN
        assert report["transport"] == "stdio"
        assert report["tool_count"] == 1
        assert report["registered"] == []
        assert report["ungranted"] == [MCP_TOOL]
        assert report["stale_grants"] == []
        assert report["limit_exceeded"] is False

    @pytest.mark.anyio
    async def test_grant_change_changes_the_registered_surface(
        self,
        plugins_app: FastAPI,
        jar: dict[str, str],
        mcp_server_script: Path,
    ) -> None:
        """Grant in → registered; unoffered grant → stale; never auto-grant."""
        await _configure_mcp(plugins_app, jar, mcp_server_script)
        assert (
            await _put_binding(
                plugins_app, jar, MCP_PLUGIN, SERVER_A, 0, [MCP_CAPABILITY]
            )
        ).status == 200
        report = (
            await _mutate(
                plugins_app, jar, "POST", f"/api/v1/plugins/{MCP_PLUGIN}/discovery", {}
            )
        ).json()
        assert report["registered"] == [MCP_CAPABILITY]
        assert report["ungranted"] == []
        assert report["stale_grants"] == []
        # A binding naming a capability the server does not offer is
        # surfaced as stale — never silently ignored.
        assert (
            await _put_binding(
                plugins_app,
                jar,
                MCP_PLUGIN,
                SERVER_A,
                1,
                [MCP_CAPABILITY, UNOFFERED_CAPABILITY],
            )
        ).status == 200
        report = (
            await _mutate(
                plugins_app, jar, "POST", f"/api/v1/plugins/{MCP_PLUGIN}/discovery", {}
            )
        ).json()
        assert report["registered"] == [MCP_CAPABILITY]
        assert report["stale_grants"] == [UNOFFERED_CAPABILITY]

    @pytest.mark.anyio
    async def test_discovery_of_non_mcp_plugin_is_conflict(
        self,
        plugins_app: FastAPI,
        jar: dict[str, str],
        native_dist: Path,
    ) -> None:
        assert (
            await _put_config(
                plugins_app, jar, NATIVE_PLUGIN, _config_body(0, config={"region": "eu"})
            )
        ).status == 200
        response = await _mutate(
            plugins_app, jar, "POST", f"/api/v1/plugins/{NATIVE_PLUGIN}/discovery", {}
        )
        assert response.status == 409
        assert _error(response)["code"] == "conflict"

    @pytest.mark.anyio
    async def test_discovery_of_disabled_server_is_conflict(
        self,
        plugins_app: FastAPI,
        jar: dict[str, str],
        mcp_server_script: Path,
    ) -> None:
        await _configure_mcp(
            plugins_app, jar, mcp_server_script, enabled=False
        )
        response = await _mutate(
            plugins_app, jar, "POST", f"/api/v1/plugins/{MCP_PLUGIN}/discovery", {}
        )
        assert response.status == 409
        assert _error(response)["fields"] == {"document": "invalid_config"}

    @pytest.mark.anyio
    async def test_discovery_of_unconfigured_plugin_is_404(
        self, plugins_app: FastAPI, jar: dict[str, str]
    ) -> None:
        response = await _mutate(
            plugins_app, jar, "POST", f"/api/v1/plugins/{MCP_PLUGIN}/discovery", {}
        )
        assert response.status == 404
        assert _error(response)["code"] == "not_found"

    @pytest.mark.anyio
    async def test_discovery_failure_is_bounded_and_value_free(
        self,
        plugins_app: FastAPI,
        jar: dict[str, str],
        tmp_path: Path,
    ) -> None:
        """A dead server answers the stable 502 code, no server text."""
        missing = tmp_path / "no-such-server.py"
        await _configure_mcp(
            plugins_app,
            jar,
            missing,
            args=[str(missing)],
        )
        response = await _mutate(
            plugins_app, jar, "POST", f"/api/v1/plugins/{MCP_PLUGIN}/discovery", {}
        )
        assert response.status == 502
        error = _error(response)
        assert error["code"] == "discovery_failed"
        assert error["fields"] == {}
        _sweep(error, str(missing), "Traceback")


# ---------------------------------------------------------------------------
# Readiness probe (POST /probe; value-free provider classification)
# ---------------------------------------------------------------------------

_FAKE_OP = """\
#!/bin/sh
case "$1" in
  --version)
    echo "2.18.0"
    exit 0
    ;;
  vault)
    case "$2" in
      get) echo "{}" ; exit 0 ;;
      list) echo "[]" ; exit 0 ;;
      *) exit 1 ;;
    esac
    ;;
  *)
    exit 1
    ;;
esac
"""


@pytest.fixture
def fake_op(tmp_path: Path, monkeypatch: Any) -> Path:
    """A fake ``op`` CLI on PATH answering version and vault reads."""
    bin_dir = tmp_path / "op-bin"
    bin_dir.mkdir()
    binary = bin_dir / "op"
    binary.write_text(_FAKE_OP, encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return binary


class TestProbe:
    @pytest.mark.anyio
    async def test_unknown_plugin_is_404(
        self, plugins_app: FastAPI, jar: dict[str, str]
    ) -> None:
        response = await _mutate(
            plugins_app, jar, "POST", "/api/v1/plugins/never.heard-of/probe", {}
        )
        assert response.status == 404
        assert _error(response)["code"] == "not_found"

    @pytest.mark.anyio
    async def test_installed_plugin_without_probe_reports_unsupported(
        self, plugins_app: FastAPI, jar: dict[str, str], native_dist: Path
    ) -> None:
        """A structured no-probe classification, never invented readiness."""
        response = await _mutate(
            plugins_app, jar, "POST", f"/api/v1/plugins/{NATIVE_PLUGIN}/probe", {}
        )
        assert response.status == 200
        view = response.json()
        assert view["plugin"] == NATIVE_PLUGIN
        assert view["available"] is False
        assert view["code"] == "unsupported"
        assert view["checked_vault"] is None

    @pytest.mark.anyio
    async def test_onepassword_without_token_reports_token_missing(
        self, plugins_app: FastAPI, jar: dict[str, str]
    ) -> None:
        """No configured value for the reference ⇒ structured unavailability."""
        response = await _mutate(
            plugins_app, jar, "POST", f"/api/v1/plugins/{PLUGIN_ONEPASSWORD}/probe", {}
        )
        assert response.status == 200
        view = response.json()
        assert view["plugin"] == PLUGIN_ONEPASSWORD
        assert view["available"] is False
        assert view["code"] == "token_missing"
        assert view["checked_vault"] is None

    @pytest.mark.anyio
    async def test_onepassword_probe_ready_with_configured_token(
        self,
        plugins_app: FastAPI,
        jar: dict[str, str],
        fake_op: Path,
    ) -> None:
        """Real resolver lane + real CLI subprocess; vault resolution order."""
        assert (
            await _put_config(
                plugins_app,
                jar,
                PLUGIN_ONEPASSWORD,
                _config_body(
                    0,
                    refs=[OP_TOKEN],
                    config=dict(OP_CONFIG),
                    credential_values={OP_TOKEN: OP_BADGE_VALUE},
                ),
            )
        ).status == 200
        # No explicit vault: the stored config's default_vault is checked.
        unscoped = await _mutate(
            plugins_app, jar, "POST", f"/api/v1/plugins/{PLUGIN_ONEPASSWORD}/probe", {}
        )
        assert unscoped.status == 200, unscoped.body
        view = unscoped.json()
        assert view["available"] is True
        assert view["code"] == "ready"
        assert view["checked_vault"] == "Dev"
        # An explicit body vault wins over the stored default.
        scoped = await _mutate(
            plugins_app,
            jar,
            "POST",
            f"/api/v1/plugins/{PLUGIN_ONEPASSWORD}/probe",
            {"vault": "Prod"},
        )
        assert scoped.status == 200
        view = scoped.json()
        assert view["available"] is True
        assert view["checked_vault"] == "Prod"
        # A malformed vault segment is a structured, value-free rejection.
        malformed = await _mutate(
            plugins_app,
            jar,
            "POST",
            f"/api/v1/plugins/{PLUGIN_ONEPASSWORD}/probe",
            {"vault": "../evil"},
        )
        assert malformed.status == 200
        view = malformed.json()
        assert view["available"] is False
        assert view["code"] == "invalid_reference"
        _sweep(view, OP_BADGE_VALUE)


# ---------------------------------------------------------------------------
# Skills inspection (read-only; prerequisite binding report)
# ---------------------------------------------------------------------------


class TestSkills:
    @pytest.fixture
    def staged_skills(self, data_root: Path) -> Path:
        _stage_skill(
            data_root,
            SKILL_NAME,
            requires=SKILL_REQUIRES,
            title="Deploy notes",
            references={"checklist.md": "- build\n- test\n"},
        )
        _stage_skill(data_root, SKILL_OTHER, requires=[], title="Patch notes")
        _stage_skill(data_root, SKILL_BROKEN, requires=[], unknown_field=True)
        return data_root

    @pytest.mark.anyio
    async def test_listing_reports_packages_and_value_free_rejections(
        self,
        plugins_app: FastAPI,
        jar: dict[str, str],
        staged_skills: Path,
    ) -> None:
        response = await _get(plugins_app, jar, "/api/v1/skills")
        assert response.status == 200
        catalog = response.json()
        names = [skill["skill"] for skill in catalog["skills"]]
        assert names == [SKILL_NAME, SKILL_OTHER]  # sorted; broken not silent
        deploy = catalog["skills"][0]
        assert deploy["title"] == "Deploy notes"
        assert deploy["requires_capabilities"] == SKILL_REQUIRES
        assert deploy["references"] == ["checklist.md"]
        assert len(deploy["package_sha256"]) == 64
        assert deploy["total_bytes"] > 0
        assert catalog["rejections"] == [
            {"skill": SKILL_BROKEN, "code": "manifest_field_unknown"}
        ]

    @pytest.mark.anyio
    async def test_detail_returns_package_and_prerequisite_report(
        self,
        plugins_app: FastAPI,
        jar: dict[str, str],
        staged_skills: Path,
        native_dist: Path,
    ) -> None:
        """The binding report compares declarations against stored grants."""
        # One prerequisite granted (native), one not (the MCP tool).
        assert (
            await _put_config(
                plugins_app, jar, NATIVE_PLUGIN, _config_body(0, config={"region": "eu"})
            )
        ).status == 200
        assert (
            await _put_binding(
                plugins_app, jar, NATIVE_PLUGIN, SERVER_A, 0, [NATIVE_IDENTIFIER]
            )
        ).status == 200
        response = await _get(plugins_app, jar, f"/api/v1/skills/{SKILL_NAME}")
        assert response.status == 200
        view = response.json()
        assert view["skill"] == SKILL_NAME
        assert view["requires_capabilities"] == SKILL_REQUIRES
        assert view["instructions"]["name"] == "instructions.md"
        assert "Do it safely" in view["instructions"]["text"]
        assert len(view["instructions"]["sha256"]) == 64
        assert [doc["name"] for doc in view["documents"]] == ["checklist.md"]
        assert "- build" in view["documents"][0]["text"]
        package_dir = staged_skills / "shared" / "skills" / SKILL_NAME
        expected_total = sum(
            path.stat().st_size for path in package_dir.iterdir() if path.is_file()
        )
        assert view["total_bytes"] == expected_total
        assert NATIVE_IDENTIFIER in view["granted_capability_pool"]
        assert view["missing_from_granted_pool"] == [MCP_CAPABILITY]

    @pytest.mark.anyio
    async def test_unknown_skill_is_404(
        self, plugins_app: FastAPI, jar: dict[str, str], data_root: Path
    ) -> None:
        response = await _get(plugins_app, jar, "/api/v1/skills/no-such-skill")
        assert response.status == 404
        assert _error(response)["code"] == "not_found"

    @pytest.mark.anyio
    async def test_malformed_skill_name_is_structured_422(
        self, plugins_app: FastAPI, jar: dict[str, str], data_root: Path
    ) -> None:
        response = await _get(plugins_app, jar, "/api/v1/skills/BadSkill")
        assert response.status == 422
        error = _error(response)
        assert error["code"] == "validation_failed"
        assert list(error["fields"].values()) == ["invalid"]
        _sweep(error, "BadSkill")


# ---------------------------------------------------------------------------
# OpenAPI contract (AC3: real registration, generated schema only)
# ---------------------------------------------------------------------------


class TestOpenApiContract:
    def test_plugins_and_skills_surfaces_register_exactly(
        self,
    ) -> None:
        """The implemented routes are the entire registered surface."""
        app = create_app(env={})
        schema = app.openapi()
        paths = schema["paths"]
        expected: dict[str, set[str]] = {
            "/api/v1/plugins": {"get"},
            "/api/v1/plugins/{plugin}/config": {"get", "put", "delete"},
            "/api/v1/plugins/{plugin}/schema": {"get"},
            "/api/v1/plugins/{plugin}/bindings": {"get"},
            "/api/v1/plugins/{plugin}/bindings/{server_id}": {"put", "delete"},
            "/api/v1/plugins/{plugin}/discovery": {"post"},
            "/api/v1/plugins/{plugin}/probe": {"post"},
            "/api/v1/skills": {"get"},
            "/api/v1/skills/{skill}": {"get"},
        }
        for path, methods in expected.items():
            assert path in paths, path
            assert set(paths[path]) == methods, path
        # No installation endpoint on THIS surface (AC2 negative): plugins
        # arrive as installed distributions, skills as staged files. (The
        # C09 application installer under /servers is a different, jobs-
        # facade surface owned by another card.)
        assert not [
            path
            for path in paths
            if path.startswith("/api/v1/plugins")
            or path.startswith("/api/v1/skills")
            if "install" in path or "package" in path
        ]
        # Skills are read-only: no write method exists at all (AC2).
        for path, methods in paths.items():
            if path.startswith("/api/v1/skills"):
                assert set(methods) == {"get"}, path
        # G1: no public docs/OpenAPI listeners.
        assert "/docs" not in paths and "/openapi.json" not in paths

    def test_mutations_document_the_boundary_and_request_bodies(self) -> None:
        """C08 layers and typed bodies are in the generated contract."""
        schema = create_app(env={}).openapi()
        put_config = schema["paths"]["/api/v1/plugins/{plugin}/config"]["put"]
        assert "PluginConfigEditBody" in json.dumps(put_config["requestBody"])
        for method in ("put", "delete"):
            operation = schema["paths"]["/api/v1/plugins/{plugin}/config"][method]
            documented = json.dumps(operation)
            assert "401" in documented and "403" in documented
        discovery = schema["paths"]["/api/v1/plugins/{plugin}/discovery"]["post"]
        assert "McpDiscoveryReportView" in json.dumps(discovery)
        assert "502" in json.dumps(discovery)
