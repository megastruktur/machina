"""Focused tests for the Service Map, chat context tools and the two new
agent capabilities (campaign ``mechanicus-ui`` card ``02-backend-map-and-chat-tools``).

Coverage:
- ``ServiceMapStore``: the 8 default categories + custom category CRUD,
  per-service entries (deterministic UUID5 identity, revision bumps),
  case-insensitive tag resolution and the deterministic classifier.
- ``mai.api.chat``: General Consult sentinel submits, immutable
  ``service_tags`` on the submission record + ConversationView, the
  agent-task enrichment, and the all-server conversation listing.
- ``mai.api.applications``: manual-origin adoption straight into the
  Service Map, custom categories riding the adopt request, and the
  merged taxonomy + manual entries on the list page.
- Capabilities: ``app.upsert_map_entry`` and ``session.read``
  descriptors validate through the reviewed ``CapabilityRegistry`` and
  execute through the reviewed ``CapabilityDispatcher`` /
  ``CapabilityToolBridge``.

Boundary discipline (test policy): the HTTP claims run through the real
ASGI app (``create_app`` + lifespan + middleware + handlers) against a
real disposable Postgres (fixture ``migrated_url``, the established
``MAI_TEST_DATABASE_URL`` convention); store/capability unit claims are
pure and need no database. No live provider, no real SSH, no browser.
"""

from __future__ import annotations

import asyncio
import fcntl
import gc
import json
import multiprocessing
import os
import threading
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
from psycopg.rows import dict_row

from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE
from mai.applications.service_map import (
    DEFAULT_SERVICE_CATEGORIES,
    MAP_ORIGINS,
    ServiceMapError,
    ServiceMapErrorCode,
    ServiceMapStore,
    ServiceMapUpsertCapability,
    classify_service,
    upsert_map_descriptor,
)
from mai.capabilities.chat_read import (
    chat_read_descriptor,
)
from mai.capabilities.dispatch import CapabilityCall, CapabilityDispatcher
from mai.agents.tools import CapabilityToolBridge, tool_name_for
from mai.credentials.env import EnvResolver
from mai.database import create_app_engine
from mai.plugins.registry import CapabilityRegistry
from mai.policy.approvals import ApprovalStore
from mai.policy.evaluate import ExecutionContext, Policy, PolicyMode

BACKEND_ROOT = Path(__file__).resolve().parents[1]

#: The suites run async tests through the anyio pytest plugin (the repo's
#: async test convention); sync helpers stay unmarked and pass through.
pytestmark = pytest.mark.anyio

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

#: Synthetic fixture credential (NOT a secret; disposable fixture DB only).
FIXTURE_PASSWORD = "fixture-password-mui-02-only"
ORIGIN = "http://127.0.0.1:8443"  # loopback allowlist entry

SERVER_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
SERVER_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
GENERAL_CONSULT = uuid.UUID(int=0)

UPSERT_CAPABILITY = "app.upsert_map_entry"
CHAT_READ_CAPABILITY = "session.read"


# ---------------------------------------------------------------------------
# ServiceMapStore units (no database)
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> ServiceMapStore:
    return ServiceMapStore(tmp_path / "service_map")


def test_default_categories_are_exactly_the_frozen_eight(tmp_path: Path) -> None:
    ids = [category.machine_id for category in DEFAULT_SERVICE_CATEGORIES]
    assert ids == [
        "games",
        "ai",
        "agent",
        "system",
        "dev",
        "infrastructure",
        "media",
        "observability",
    ]
    assert all(category.built_in for category in DEFAULT_SERVICE_CATEGORIES)
    store = _store(tmp_path)
    assert [c.machine_id for c in store.list_categories()] == ids
    games = next(c for c in store.list_categories() if c.machine_id == "games")
    assert "Enshrouded" in games.description


def test_custom_category_upsert_persists_and_merges(tmp_path: Path) -> None:
    store = _store(tmp_path)
    created = store.upsert_category(
        "home_automation", label="Home Automation", description="zigbee etc"
    )
    assert created.built_in is False
    categories = store.list_categories()
    assert len(categories) == 9
    custom = next(c for c in categories if c.machine_id == "home_automation")
    assert custom.label == "Home Automation"

    # Editing converges on one record (not a duplicate).
    store.upsert_category("home_automation", label="Home", description="")
    store2 = _store(tmp_path)  # re-read from disk
    customs = [c for c in store2.list_categories() if not c.built_in]
    assert [c.machine_id for c in customs] == ["home_automation"]
    assert customs[0].label == "Home"


def test_custom_category_rejects_bad_slug(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ServiceMapError):
        store.upsert_category("Bad Slug!", label="nope")
    with pytest.raises(ServiceMapError):
        store.upsert_category("", label="nope")


def test_entry_upsert_is_deterministic_and_revision_bumped(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = store.upsert_entry(
        SERVER_A,
        name="Jellyfin",
        category="media",
        description="media server",
        instructions={"backup": "restic backup /srv/jellyfin"},
    )
    second = store.upsert_entry(SERVER_A, name="jellyfin", category="media")
    assert first.application_id == second.application_id  # UUID5 stable
    assert second.revision == 2
    assert second.name == "jellyfin"  # upsert replaces the record
    assert second.instructions.backup == ""  # omitted slots reset

    reread = _store(tmp_path).get_entry(SERVER_A, uuid.UUID(first.application_id))
    assert reread is not None and reread.revision == 2


def test_entry_upsert_validates_origin_and_category(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ServiceMapError):
        store.upsert_entry(SERVER_A, name="x", origin="not-an-origin")
    assert MAP_ORIGINS == frozenset(
        {"compose", "systemd_unit", "docker_container", "manual"}
    )
    with pytest.raises(ServiceMapError):
        store.upsert_entry(SERVER_A, name="x", category="Bad Slug")


def test_find_entries_matches_case_insensitively(tmp_path: Path) -> None:
    store = _store(tmp_path)
    entry = store.upsert_entry(
        SERVER_A,
        name="Enshrouded Dedicated",
        category="games",
        unit_name="enshrouded.service",
        compose_project="enshrouded",
    )
    by_name = store.find_entries(SERVER_A, ["enshrouded dedicated"])
    by_unit = store.find_entries(SERVER_A, ["ENSHROUDED.SERVICE"])
    by_project = store.find_entries(SERVER_A, ["Enshrouded"])
    by_id = store.find_entries(SERVER_A, [entry.application_id.upper()])
    assert [e.application_id for e in by_name] == [entry.application_id]
    assert [e.application_id for e in by_unit] == [entry.application_id]
    assert [e.application_id for e in by_project] == [entry.application_id]
    assert [e.application_id for e in by_id] == [entry.application_id]
    assert store.find_entries(SERVER_A, ["nothing-matches"]) == ()
    assert store.find_entries(SERVER_A, []) == ()


def test_classify_service_is_deterministic() -> None:
    assert classify_service("jellyfin") == "media"
    assert classify_service("my-ollama") == "ai"
    assert classify_service("enshrouded-dedicated") == "games"
    assert classify_service("forgejo") == "dev"
    assert classify_service("caddy") == "infrastructure"
    assert classify_service("prometheus") == "observability"
    assert classify_service("hermes-agent") == "agent"
    assert classify_service("something-unknown") == "system"
    assert classify_service("") == "system"


def test_malformed_entry_file_surfaces(tmp_path: Path) -> None:
    root = tmp_path / "service_map"
    servers = root / "servers"
    servers.mkdir(parents=True)
    (servers / f"{SERVER_A}.json").write_text('{"not": "a list"}')
    store = ServiceMapStore(root)
    with pytest.raises(ServiceMapError):
        store.list_entries(SERVER_A)


# ---------------------------------------------------------------------------
# Capability validation through the reviewed registry + bridge execution
# ---------------------------------------------------------------------------


def _registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register(upsert_map_descriptor())
    registry.register(chat_read_descriptor())
    return registry


def test_descriptors_validate_through_capability_registry() -> None:
    registry = _registry()
    identifiers = [d.identifier for d in registry.list_capabilities()]
    assert identifiers == sorted([UPSERT_CAPABILITY, CHAT_READ_CAPABILITY])
    upsert = next(
        d for d in registry.list_capabilities() if d.identifier == UPSERT_CAPABILITY
    )
    assert upsert.version == "1.0.0"
    assert upsert.effect_class == "mutate"
    assert upsert.resource_scope == ("service_map",)
    read = next(
        d
        for d in registry.list_capabilities()
        if d.identifier == CHAT_READ_CAPABILITY
    )
    assert read.version == "1.0.0"
    assert read.effect_class == "read"
    assert read.resource_scope == ("chat.session",)
    assert read.required_grants == (CHAT_READ_CAPABILITY,)


def _unused_approvals() -> ApprovalStore:
    """A real store over a lazily-connecting engine (never dialed)."""
    _, sessions = create_app_engine(
        "postgresql://unused:unused@127.0.0.1:1/unused"
    )
    return ApprovalStore(sessions)


def _dispatcher(
    registry: CapabilityRegistry,
    executor: Any,
    grants: Any = None,
) -> CapabilityDispatcher:
    if grants is None:
        grants = lambda: {  # noqa: E731
            descriptor.identifier: {SERVER_A}
            for descriptor in registry.list_capabilities()
        }
    env_file = Path(os.environ.get("MAI_ENV_FILE", "/tmp/mai-fxt-mui-02.env"))
    env_file.write_text("MAI_FIXTURE=value\n")
    return CapabilityDispatcher(
        registry=registry,
        approvals=_unused_approvals(),
        grants=grants,
        policy=lambda: Policy(revision=1, denied_capabilities=frozenset()),
        adopted=lambda server_id: True,
        credentials=lambda: EnvResolver(env_file),
        executor=executor,
    )


def _context(
    allowed: tuple[str, ...], *, server_id: uuid.UUID = SERVER_A
) -> ExecutionContext:
    return ExecutionContext(
        principal="principal",
        server_id=server_id,
        run_id=uuid.uuid4(),
        parent_scope=None,
        profile_revision="1",
        prompt_revision="1",
        credential_references=(),
        allowed_capabilities=allowed,
        mode=PolicyMode.DELEGATED,
    )


async def test_upsert_map_capability_executes_through_the_tool_bridge(
    tmp_path: Path,
) -> None:
    registry = _registry()
    store = _store(tmp_path)
    bridge = CapabilityToolBridge(
        registry=registry,
        dispatcher=_dispatcher(registry, ServiceMapUpsertCapability(store)),
        context=_context((UPSERT_CAPABILITY,)),
    )
    tool = next(
        t for t in bridge.tools if t.name == tool_name_for(UPSERT_CAPABILITY)
    )
    result = json.loads(
        await tool.function(
            name="Jellyfin",
            category="media",
            description="the media server",
            instructions={"backup": "restic backup"},
        )
    )
    assert result["status"] == "executed"
    assert result["allowed"] is True
    assert result["effect"] == "executed"
    summary = json.loads(result["output"]["text"])
    assert summary["name"] == "Jellyfin"
    assert summary["category"] == "media"
    assert summary["created"] is True
    entry = store.get_entry(SERVER_A, uuid.UUID(summary["application_id"]))
    assert entry is not None
    assert entry.instructions.backup == "restic backup"

    # A context scoped to another server is denied at the dispatch seam:
    # the effect never runs (the store stays untouched).
    denied_bridge = CapabilityToolBridge(
        registry=registry,
        dispatcher=_dispatcher(registry, ServiceMapUpsertCapability(store)),
        context=_context((UPSERT_CAPABILITY,), server_id=SERVER_B),
    )
    denied_tool = next(
        t
        for t in denied_bridge.tools
        if t.name == tool_name_for(UPSERT_CAPABILITY)
    )
    denied = json.loads(await denied_tool.function(name="Intruder"))
    assert denied["status"] == "denied"
    assert denied["allowed"] is False
    assert denied["effect"] == "not_attempted"
    assert store.find_entries(SERVER_A, ["Intruder"]) == ()


async def test_upsert_map_capability_rejects_invalid_payload(
    tmp_path: Path,
) -> None:
    registry = _registry()
    store = _store(tmp_path)
    bridge = CapabilityToolBridge(
        registry=registry,
        dispatcher=_dispatcher(registry, ServiceMapUpsertCapability(store)),
        context=_context((UPSERT_CAPABILITY,)),
    )
    tool = next(
        t for t in bridge.tools if t.name == tool_name_for(UPSERT_CAPABILITY)
    )
    # Schema-valid but store-invalid (a non-canonical application_id):
    # the executor's structured rejection maps to an honest
    # unknown-effect receipt, never a silent no-op.
    result = json.loads(
        await tool.function(name="x", application_id="not-a-canonical-uuid")
    )
    assert result["status"] == "unknown_effect"
    assert result["allowed"] is False
    assert result["effect"] == "unknown"
    assert store.find_entries(SERVER_A, ["x"]) == ()


# ---------------------------------------------------------------------------
# live fixtures: real disposable Postgres + the real ASGI app
# ---------------------------------------------------------------------------


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(os.path.join(BACKEND_ROOT, "alembic.ini"))


def _drop_app_tables(url: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``head``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the mui-02 HTTP surface claims "
            "need a real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        _drop_app_tables(url)
        command.upgrade(_alembic_config(), "head")
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


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[FastAPI]:
    async with app.router.lifespan_context(app):
        yield app


async def _truncate(url: str) -> None:
    """Reset the facade tables between tests (the session-scoped DB stays)."""
    from sqlalchemy import text

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
async def live_app(bootstrapped_url: str, tmp_path: Path):
    """App on the real fixture DB with fresh confined roots."""
    await _truncate(bootstrapped_url)
    app = create_app(
        env={
            "MAI_DATABASE_URL": bootstrapped_url,
            "MAI_PUBLIC_ORIGINS": ORIGIN,
            "MAI_DATA_ROOT": str(tmp_path / "data-root"),
            "MAI_ARTIFACTS_ROOT": str(tmp_path / "artifacts"),
        }
    )
    async with _running(app):
        yield app
    if app.state.engine is not None:
        await app.state.engine.dispose()


# ---------------------------------------------------------------------------
# ASGI harness (no test-client dependency)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HttpResponse:
    status: int | None = None
    headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    body: bytes = b""

    def header_all(self, name: str) -> list[str]:
        return [
            value.decode("ascii")
            for key, value in self.headers
            if key.decode("ascii").lower() == name
        ]

    def json(self) -> Any:
        return json.loads(self.body)


def _scope(method: str, path: str, headers: Mapping[str, str]) -> dict:
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
    headers: Mapping[str, str] | None = None,
    cookies: Mapping[str, str] | None = None,
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

    await app(_scope(method, path, flat_headers), receive, send)
    return response


def _set_cookies(response: HttpResponse) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw in response.header_all("set-cookie"):
        name, _, value = raw.partition("=")
        parsed[name] = value.split(";", 1)[0]
    return parsed


def _mutation_headers(jar: Mapping[str, str]) -> dict[str, str]:
    return {"origin": ORIGIN, "x-csrf-token": jar[CSRF_COOKIE]}


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


async def create_profile(app: FastAPI, jar: dict[str, str], name: str) -> uuid.UUID:
    """Mint one canonical server profile through the real HTTP surface."""
    response = await call_app(
        app,
        "POST",
        "/api/v1/servers",
        json_body={
            "name": name,
            "prompt_revision": 7,
            "environment": "linux_ssh",
            "ssh": {
                "host": "fixture.internal",
                "port": 22,
                "user": "fixture",
                "host_key": "SHA256:6VhOyUC6fvXW0aQ9WNe7Kx3nZZf+zxgupEWLoZXoTMU",
                "credential_ref": "MAI_SSH_KEY_FIXTURE",
            },
            "privilege": "none",
            "capabilities": [],
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert response.status == 201, response.body
    return uuid.UUID(response.json()["server_id"])


@pytest.fixture
async def jar(live_app: FastAPI) -> dict[str, str]:
    return await login_session(live_app)


@pytest.fixture
async def server_a(live_app: FastAPI, jar: dict[str, str]) -> uuid.UUID:
    return await create_profile(live_app, jar, "mui-02-a")


def _apps_root(server_id: uuid.UUID) -> str:
    return f"/api/v1/servers/{server_id}/applications"


async def submit_chat(
    app: FastAPI,
    jar: dict[str, str],
    run_id: uuid.UUID,
    server_id: str,
    *,
    message: str = "investigate the fixture service",
    profile: str = "fast",
    service_tags: list[str] | None = None,
    attachments: list[dict] | None = None,
) -> HttpResponse:
    body: dict[str, Any] = {
        "run_id": str(run_id),
        "server_id": server_id,
        "message": message,
        "profile": profile,
    }
    if service_tags is not None:
        body["service_tags"] = service_tags
    if attachments is not None:
        body["attachments"] = attachments
    return await call_app(
        app,
        "POST",
        "/api/v1/chat/messages",
        json_body=body,
        headers=_mutation_headers(jar),
        cookies=jar,
    )


def read_events_direct(url: str, run_id: uuid.UUID) -> list[dict[str, Any]]:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT seq, type, v, data FROM job_events WHERE run_id = %s "
            "ORDER BY seq",
            (run_id,),
        ).fetchall()
    for row in rows:
        if isinstance(row["data"], str):
            row["data"] = json.loads(row["data"])
    return rows


# ---------------------------------------------------------------------------
# HTTP: applications surface (Service Map integration)
# ---------------------------------------------------------------------------


async def test_manual_adoption_persists_map_entry_categories_and_doc(
    live_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
) -> None:
    application_id = uuid.uuid4()
    body = {
        "application_id": str(application_id),
        "name": "Jellyfin",
        "origin": "manual",
        "category": "media",
        "description": "The household media server.",
        "instructions": {"backup": "restic backup /srv/jellyfin"},
        "upsert_categories": [
            {
                "machine_id": "home_automation",
                "label": "Home Automation",
                "description": "Zigbee and friends",
            }
        ],
    }
    created = await call_app(
        live_app,
        "POST",
        _apps_root(server_a),
        json_body=body,
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert created.status == 201, created.body
    record = created.json()
    assert record["outcome"] == "reconciled"
    view = record["application"]
    assert view["application_id"] == str(application_id)
    assert view["origin"] == "manual"
    assert view["category"] == "media"
    assert view["description"] == "The household media server."
    assert view["instructions"]["backup"] == "restic backup /srv/jellyfin"
    assert record["record_doc"]["status"] == "documented"

    # Idempotent re-submit converges on the same entry (revision bump).
    again = await call_app(
        live_app,
        "POST",
        _apps_root(server_a),
        json_body=body,
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert again.status == 201, again.body
    assert again.json()["application"]["revision"] == 2

    listing = await call_app(
        live_app, "GET", _apps_root(server_a), cookies=jar
    )
    assert listing.status == 200, listing.body
    page = listing.json()
    manual = next(a for a in page["applications"] if a["application_id"] == str(application_id))
    assert manual["category"] == "media"
    assert manual["instructions"]["backup"] == "restic backup /srv/jellyfin"
    category_ids = {c["machine_id"] for c in page["categories"]}
    assert {
        "games", "ai", "agent", "system", "dev",
        "infrastructure", "media", "observability",
    } <= category_ids
    assert "home_automation" in category_ids
    custom = next(c for c in page["categories"] if c["machine_id"] == "home_automation")
    assert custom["built_in"] is False
    assert all(
        c["built_in"] for c in page["categories"] if c["machine_id"] != "home_automation"
    )


async def test_manual_adoption_rejects_bad_category_slug(
    live_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
) -> None:
    response = await call_app(
        live_app,
        "POST",
        _apps_root(server_a),
        json_body={
            "application_id": str(uuid.uuid4()),
            "name": "Nope",
            "origin": "manual",
            "category": "Bad Slug",
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert response.status == 422


async def test_compose_adoption_persists_map_metadata(
    live_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
) -> None:
    application_id = uuid.uuid4()
    created = await call_app(
        live_app,
        "POST",
        _apps_root(server_a),
        json_body={
            "application_id": str(application_id),
            "name": "fixture-app",
            "origin": "compose",
            "compose_project": "fixture-project",
            "working_dir": "/srv/fixture",
            "category": "dev",
            "description": "fixture compose service",
            "instructions": {"update": "docker compose pull && up -d"},
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert created.status == 201, created.body
    assert created.json()["application"]["category"] == "dev"
    listing = await call_app(live_app, "GET", _apps_root(server_a), cookies=jar)
    adopted = next(
        a for a in listing.json()["applications"]
        if a["application_id"] == str(application_id)
    )
    assert adopted["origin"] == "compose"
    assert adopted["category"] == "dev"
    assert adopted["instructions"]["update"] == "docker compose pull && up -d"


# ---------------------------------------------------------------------------
# HTTP: chat surface (General Consult, service tags, all-server history)
# ---------------------------------------------------------------------------


def test_internal_api_tool_enforces_endpoint_and_field_descriptions() -> None:
    from pydantic import BaseModel, Field

    from mai.capabilities.internal_api import (
        INTERNAL_API_CAPABILITY_IDS,
        assert_agent_ready_descriptor,
        build_internal_api_capabilities,
        internal_api_tool,
    )

    class UndocumentedPayload(BaseModel):
        model_config = {"extra": "forbid"}
        name: str

    # 1. Missing/short endpoint description is rejected at decoration time.
    with pytest.raises(ValueError, match="tool description must be a non-empty string"):
        @internal_api_tool(
            capability_id="app.bad_desc",
            version="1.0.0",
            effect_class="read",
            resource_scope=["applications"],
            environments=["controller"],
            payload_model=UndocumentedPayload,
            description="Too short",
        )
        async def _bad_desc(call: Any, payload: Any) -> Any:
            return {}

    # 2. Missing Field(description=...) on a parameter is rejected at decoration time.
    with pytest.raises(ValueError, match="must declare Field\\(description=...\\)"):
        @internal_api_tool(
            capability_id="app.bad_param",
            version="1.0.0",
            effect_class="read",
            resource_scope=["applications"],
            environments=["controller"],
            payload_model=UndocumentedPayload,
            description="A sufficiently detailed endpoint description for the model.",
        )
        async def _bad_param(call: Any, payload: Any) -> Any:
            return {}

    # 3. Nested BaseModel field missing description is rejected recursively.
    class BadNestedItem(BaseModel):
        model_config = {"extra": "forbid"}
        key: str

    class NestedPayload(BaseModel):
        model_config = {"extra": "forbid"}
        nested: BadNestedItem = Field(
            description="Nested configuration object for the operation."
        )

    with pytest.raises(ValueError, match="parameter 'nested.key'"):
        @internal_api_tool(
            capability_id="app.bad_nested",
            version="1.0.0",
            effect_class="read",
            resource_scope=["applications"],
            environments=["controller"],
            payload_model=NestedPayload,
            description="A sufficiently detailed endpoint description for the model.",
        )
        async def _bad_nested(call: Any, payload: Any) -> Any:
            return {}

    # 4. Every composed internal API capability passes assert_agent_ready_descriptor
    # and projects nested enums and nested object schemas cleanly.
    comp = build_internal_api_capabilities(
        applications=None,
        adoption=None,
        docs=None,
        service_map=None,
    )
    by_id: dict[str, Any] = {d.identifier: d for d in comp.descriptors}
    for cap_id in (
        "app.discover",
        "app.list_adopted",
        "app.adopt",
        "app.unadopt",
        "app.delete_map_entry",
        "app.list_catalog",
        "app.upsert_catalog_runbook",
        "app.delete_catalog_runbook",
        "docs.list",
        "docs.read",
        "docs.upsert",
        "docs.delete",
    ):
        desc = assert_agent_ready_descriptor(by_id[cap_id])
        assert len(str(desc.input_schema["description"])) >= 30
    adopt_props: Any = by_id["app.adopt"].input_schema["properties"]
    assert adopt_props["origin"]["enum"] == ["manual", "compose", "systemd_unit"]
    assert "install" in adopt_props["instructions"]["properties"]
    assert "custom_instructions" in adopt_props
    assert "installed" in adopt_props
    assert "machine_id" in adopt_props["upsert_categories"]["items"]["properties"]
    discover_props: Any = by_id["app.discover"].input_schema["properties"]
    assert "limit" in discover_props and "offset" in discover_props
    list_adopted_props: Any = by_id["app.list_adopted"].input_schema["properties"]
    assert "limit" in list_adopted_props and "offset" in list_adopted_props
    list_catalog_props: Any = by_id["app.list_catalog"].input_schema["properties"]
    assert "limit" in list_catalog_props and "offset" in list_catalog_props
    docs_list_props: Any = by_id["docs.list"].input_schema["properties"]
    assert "limit" in docs_list_props and "offset" in docs_list_props
    upsert_catalog_props: Any = by_id["app.upsert_catalog_runbook"].input_schema["properties"]
    assert "install" in upsert_catalog_props["instructions"]["properties"]
    # FRR T5: server.profile.update joined the auto-grant vocabulary (forced
    # captain approval at the bridge seam).
    assert len(INTERNAL_API_CAPABILITY_IDS) == 15


async def test_catalog_tools_and_adopt_manual_inherits_catalog_unit(
    tmp_path: Path, migrated_url: str
) -> None:
    """Unit test verifying:
    - @internal_api_tool catalog capabilities: app.upsert_catalog_runbook,
      app.list_catalog, app.delete_catalog_runbook
    - app.adopt inheriting category, description, and 5-slot instructions from
      the global catalog while persisting server-specific custom_instructions
      and installed status.

    The writers are fail-closed on the metadata coordinator, so the unit
    composition wires a REAL session factory (the fixture DB) — the lease
    service is auto-derived from its engine URL.
    """
    from unittest.mock import MagicMock
    from mai.capabilities.internal_api import build_internal_api_capabilities
    from mai.jobs.resource_locks import LOCK_TABLE

    engine, session_factory = create_app_engine(migrated_url)
    map_store = _store(tmp_path)
    composition = build_internal_api_capabilities(
        applications=MagicMock(),
        adoption=MagicMock(),
        docs=MagicMock(),
        service_map=map_store,
        data_root=tmp_path,
        session_factory=session_factory,
    )
    desc_by_id = {d.identifier: d for d in composition.descriptors}

    def _call(
        cap_id: str, payload: Mapping[str, Any], ctx: ExecutionContext
    ) -> CapabilityCall:
        return CapabilityCall(
            descriptor=desc_by_id[cap_id],
            payload=dict(payload),
            context=ctx,
            credentials={},
            op_id=uuid.uuid4(),
        )

    global_ctx = _context(
        ("app.list_catalog", "app.upsert_catalog_runbook", "app.delete_catalog_runbook"),
        server_id=GENERAL_CONSULT,
    )

    # 1. Add Forgejo to global catalog via app.upsert_catalog_runbook
    upsert_exec = composition.executors["app.upsert_catalog_runbook"]
    res1 = await upsert_exec(
        _call(
            "app.upsert_catalog_runbook",
            {
                "name": "Forgejo",
                "category": "dev",
                "description": "Self-hosted lightweight software forge.",
                "instructions": {
                    "install": "docker compose up -d forgejo",
                    "update": "docker compose pull forgejo && docker compose up -d",
                    "uninstall": "docker compose down",
                    "backup": "forgejo dump -c /data/gitea/conf/app.ini",
                    "verify": "curl -fsS http://127.0.0.1:3000/api/v1/version",
                },
            },
            global_ctx,
        )
    )
    assert res1.ok is True, res1.output
    cat_data = json.loads(res1.output)
    runbook_id = cat_data["runbook_id"]
    assert cat_data["category"] == "dev"
    assert cat_data["revision"] == 1

    # 2. List catalog via app.list_catalog
    list_exec = composition.executors["app.list_catalog"]
    res_list = await list_exec(
        _call(
            "app.list_catalog",
            {"query": "forgejo", "category": "dev", "limit": 10, "offset": 0},
            global_ctx,
        )
    )
    assert res_list.ok is True, res_list.output
    list_data = json.loads(res_list.output)
    assert list_data["total"] == 1
    assert list_data["runbooks"][0]["runbook_id"] == runbook_id

    # 3. Adopt Forgejo on SERVER_A with server-specific custom_instructions
    # and installed=True (omitting category/description/instructions -> inherited)
    server_ctx = _context(("app.adopt",), server_id=SERVER_A)
    adopt_exec = composition.executors["app.adopt"]
    res_adopt = await adopt_exec(
        _call(
            "app.adopt",
            {
                "name": "Forgejo",
                "origin": "manual",
                "installed": True,
                "custom_instructions": "Listening on 127.0.0.1:3000 behind Caddy git.internal; repos on /mnt/nas/git.",
            },
            server_ctx,
        )
    )
    assert res_adopt.ok is True, res_adopt.output
    adopt_data = json.loads(res_adopt.output)
    assert adopt_data["category"] == "dev"
    assert adopt_data["description"] == "Self-hosted lightweight software forge."
    assert adopt_data["instructions"]["backup"] == "forgejo dump -c /data/gitea/conf/app.ini"
    assert adopt_data["custom_instructions"] == (
        "Listening on 127.0.0.1:3000 behind Caddy git.internal; repos on /mnt/nas/git."
    )
    assert adopt_data["installed"] is True

    # 4. Update server-specific custom_instructions via app.adopt without wiping catalog fields
    res_adopt2 = await adopt_exec(
        _call(
            "app.adopt",
            {
                "name": "Forgejo",
                "custom_instructions": "Listening on 127.0.0.1:3000; SSH port 2222; repos on /mnt/nas/git.",
            },
            server_ctx,
        )
    )
    assert res_adopt2.ok is True, res_adopt2.output
    adopt_data2 = json.loads(res_adopt2.output)
    assert adopt_data2["revision"] == 2
    assert adopt_data2["installed"] is True
    assert adopt_data2["instructions"]["verify"] == "curl -fsS http://127.0.0.1:3000/api/v1/version"
    assert "SSH port 2222" in adopt_data2["custom_instructions"]

    # 5. Also verify origin="compose" adoption inheriting from catalog and updating
    # custom_instructions without passing expected_revision or application_id.
    from datetime import datetime, timezone
    from mai.applications.adopt import AdoptedApplication, AdoptionResult

    stored_records: dict[tuple[uuid.UUID, uuid.UUID], AdoptedApplication] = {}

    class _FakeAdoption:
        async def list_adopted(self, sid: uuid.UUID) -> list[AdoptedApplication]:
            return [r for (s, _), r in stored_records.items() if s == sid]

        async def read(
            self, sid: uuid.UUID, aid: uuid.UUID
        ) -> AdoptedApplication | None:
            return stored_records.get((sid, aid))

        async def adopt(self, *, store: Any, request: Any) -> AdoptionResult:
            del store
            key = (request.server_id, request.application_id)
            prev = stored_records.get(key)
            expected = 0 if prev is None else prev.revision
            assert request.expected_revision == expected, (
                f"expected_revision mismatch: got {request.expected_revision}, expected {expected}"
            )
            new_rev = expected + 1
            now = datetime.now(timezone.utc)
            rec = AdoptedApplication(
                server_id=request.server_id,
                application_id=request.application_id,
                name=request.name,
                origin=request.origin,
                compose_project=request.compose_project,
                working_dir=request.working_dir,
                unit_name=request.unit_name,
                external_working_dir=request.external_working_dir,
                external_config_files=request.external_config_files,
                external_environment_files=request.external_environment_files,
                revision=new_rev,
                schema_revision=1,
                sha256="deadbeef" * 8,
                adopted_at=now,
                reconciled_at=now,
                stale=False,
            )
            stored_records[key] = rec
            return AdoptionResult(
                intent_id=uuid.uuid4(),
                server_id=request.server_id,
                application_id=request.application_id,
                op="upsert",
                outcome="adopted" if expected == 0 else "updated",
                revision=new_rev,
                sha256=rec.sha256,
            )

    comp_compose = build_internal_api_capabilities(
        applications=MagicMock(),
        adoption=_FakeAdoption(),  # type: ignore[arg-type]
        docs=MagicMock(),
        service_map=map_store,
        data_root=tmp_path,
        session_factory=session_factory,
    )
    compose_adopt_exec = comp_compose.executors["app.adopt"]
    res_comp1 = await compose_adopt_exec(
        _call(
            "app.adopt",
            {
                "name": "Forgejo",
                "origin": "compose",
                "compose_project": "forgejo",
                "working_dir": "/srv/forgejo",
                "installed": True,
                "custom_instructions": "Compose stack at /srv/forgejo; port 3000.",
            },
            _context(("app.adopt",), server_id=SERVER_B),
        )
    )
    assert res_comp1.ok is True, res_comp1.output
    comp1_data = json.loads(res_comp1.output)
    assert comp1_data["origin"] == "compose"
    assert comp1_data["category"] == "dev"
    assert comp1_data["instructions"]["install"] == "docker compose up -d forgejo"
    assert comp1_data["custom_instructions"] == "Compose stack at /srv/forgejo; port 3000."
    assert comp1_data["installed"] is True

    # Re-calling app.adopt on SERVER_B without expected_revision or origin updates in place
    res_comp2 = await compose_adopt_exec(
        _call(
            "app.adopt",
            {
                "name": "Forgejo",
                "custom_instructions": "Compose stack at /srv/forgejo; port 3000; runner enabled.",
                "installed": True,
            },
            _context(("app.adopt",), server_id=SERVER_B),
        )
    )
    assert res_comp2.ok is True, res_comp2.output
    comp2_data = json.loads(res_comp2.output)
    assert comp2_data["application_id"] == comp1_data["application_id"]
    assert comp2_data["origin"] == "compose"
    assert comp2_data["revision"] == 2
    assert comp2_data["custom_instructions"] == (
        "Compose stack at /srv/forgejo; port 3000; runner enabled."
    )
    assert comp2_data["instructions"]["backup"] == "forgejo dump -c /data/gitea/conf/app.ini"

    # 6. Delete from global catalog via app.delete_catalog_runbook
    del_exec = composition.executors["app.delete_catalog_runbook"]
    res_del = await del_exec(
        _call(
            "app.delete_catalog_runbook",
            {"runbook_id": runbook_id},
            global_ctx,
        )
    )
    assert res_del.ok is True, res_del.output
    assert json.loads(res_del.output)["removed"] is True

    # Lease + engine cleanup for the coordinator wired above.
    with psycopg.connect(migrated_url, autocommit=True) as conn:
        conn.execute(f"DELETE FROM {LOCK_TABLE}")
    await engine.dispose()


# ---------------------------------------------------------------------------
# app.list_adopted (agent tool): the SAME membership rule as the HTTP list
# ---------------------------------------------------------------------------


LIST_ADOPTED_CAPABILITY = "app.list_adopted"

#: Every non-manual Service Map origin — all of them must be hidden when
#: their canonical adoption record is gone.
NONMANUAL_ORIGINS = ("compose", "systemd_unit", "docker_container")


def _internal_api_bridge(
    app: FastAPI,
    server_ids: tuple[uuid.UUID, ...],
) -> CapabilityToolBridge:
    """The REAL internal-API capability surface behind the agent tool.

    The composition is the production one: the very same
    ``build_internal_api_capabilities`` builder (with the app's own
    adoption service + Service Map store) drives the descriptors and the
    executors, and the tool call travels through the reviewed
    ``CapabilityToolBridge`` → ``CapabilityDispatcher`` seam.
    """
    from mai.capabilities.internal_api import build_internal_api_capabilities

    composition = build_internal_api_capabilities(
        applications=app.state.applications,
        adoption=app.state.app_adoption,
        docs=app.state.app_docs,
        service_map=app.state.service_map,
        session_factory=app.state.session_factory,
    )
    registry = CapabilityRegistry()
    for descriptor in composition.descriptors:
        registry.register(descriptor)
    env_file = Path(os.environ.get("MAI_ENV_FILE", "/tmp/mai-fxt-mui-02.env"))
    env_file.write_text("MAI_FIXTURE=value\n")
    scope = frozenset(server_ids)
    dispatcher = CapabilityDispatcher(
        registry=registry,
        approvals=_unused_approvals(),
        grants=lambda: {LIST_ADOPTED_CAPABILITY: scope},
        policy=lambda: Policy(revision=1, denied_capabilities=frozenset()),
        adopted=lambda server_id: True,
        credentials=lambda: EnvResolver(env_file),
        executor=composition.executors[LIST_ADOPTED_CAPABILITY],
    )
    return CapabilityToolBridge(
        registry=registry,
        dispatcher=dispatcher,
        context=_context((LIST_ADOPTED_CAPABILITY,), server_id=server_ids[0]),
    )


async def _tool_list_adopted(
    bridge: CapabilityToolBridge, server_id: uuid.UUID, **payload: Any
) -> dict[str, Any]:
    """One agent-tool listing call; returns the executor's envelope."""
    tool = next(
        t
        for t in bridge.tools
        if t.name == tool_name_for(LIST_ADOPTED_CAPABILITY)
    )
    result = json.loads(
        await tool.function(server_id=str(server_id), **payload)
    )
    assert result["status"] == "executed", result
    assert result["allowed"] is True, result
    return json.loads(result["output"]["text"])


async def test_agent_tool_list_adopted_matches_http_membership(
    live_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
) -> None:
    server_b = await create_profile(live_app, jar, "mui-02-b")

    # A live adopted compose application (canonical adoption row).
    live_id = uuid.uuid4()
    adopted = await call_app(
        live_app,
        "POST",
        _apps_root(server_a),
        json_body={
            "application_id": str(live_id),
            "name": "live-app",
            "origin": "compose",
            "compose_project": "live-project",
            "working_dir": "/srv/live",
            "category": "dev",
            "description": "the live service",
            "expected_revision": 0,
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert adopted.status == 201, adopted.body
    revision = adopted.json()["revision"]

    # A legitimate manual map-only entry (no canonical adoption row).
    manual_id = uuid.uuid4()
    manual = await call_app(
        live_app,
        "POST",
        _apps_root(server_a),
        json_body={
            "application_id": str(manual_id),
            "name": "manual-app",
            "origin": "manual",
            "category": "media",
            "description": "the manual service",
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert manual.status == 201, manual.body

    # Orphan sidecars: Service Map metadata whose canonical adoption
    # record is gone (or never existed). Every non-manual origin is
    # covered.
    store = live_app.state.service_map
    orphan_ids: set[str] = set()
    for origin in NONMANUAL_ORIGINS:
        orphan_id = uuid.uuid4()
        orphan_ids.add(str(orphan_id))
        store.upsert_entry(
            server_a,
            name=f"orphan-{origin}",
            origin=origin,
            application_id=orphan_id,
            compose_project=f"orphan-{origin}-project",
        )

    # Another server's own manual entry (target-server isolation).
    other_manual_id = uuid.uuid4()
    other = await call_app(
        live_app,
        "POST",
        _apps_root(server_b),
        json_body={
            "application_id": str(other_manual_id),
            "name": "other-manual-app",
            "origin": "manual",
            "category": "media",
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert other.status == 201, other.body

    bridge = _internal_api_bridge(live_app, (server_a, server_b))

    # 1. The tool and the HTTP list agree on MEMBERSHIP and total.
    tool_page = await _tool_list_adopted(bridge, server_a)
    http_response = await call_app(
        live_app, "GET", _apps_root(server_a), cookies=jar
    )
    assert http_response.status == 200, http_response.body
    http_page = http_response.json()
    tool_ids = [a["application_id"] for a in tool_page["applications"]]
    http_ids = [a["application_id"] for a in http_page["applications"]]
    assert tool_page["total"] == http_page["total"]
    assert set(tool_ids) == set(http_ids)
    assert set(tool_ids) == {str(live_id), str(manual_id)}
    assert orphan_ids.isdisjoint(tool_ids)
    assert str(other_manual_id) not in tool_ids

    # The canonical record keeps its Service Map metadata (enrichment).
    live_view = next(
        a for a in tool_page["applications"] if a["application_id"] == str(live_id)
    )
    assert live_view["category"] == "dev"
    assert live_view["description"] == "the live service"
    assert live_view["origin"] == "compose"

    # 2. Revoke the live application: its orphaned sidecar disappears
    #    from BOTH listings, the manual entry stays visible, and the
    #    sidecar itself is retained on disk (non-destructive).
    revoked = await call_app(
        live_app,
        "DELETE",
        f"{_apps_root(server_a)}/{live_id}",
        json_body={"expected_revision": revision},
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert revoked.status == 204, revoked.body
    tool_page = await _tool_list_adopted(bridge, server_a)
    tool_ids = [a["application_id"] for a in tool_page["applications"]]
    assert tool_page["total"] == 1
    assert tool_ids == [str(manual_id)]
    http_response = await call_app(
        live_app, "GET", _apps_root(server_a), cookies=jar
    )
    http_page = http_response.json()
    assert http_page["total"] == 1
    assert [a["application_id"] for a in http_page["applications"]] == [
        str(manual_id)
    ]
    stored_ids = {
        entry.application_id for entry in store.list_entries(server_a)
    }
    assert str(live_id) in stored_ids
    assert str(manual_id) in stored_ids

    # 3. Repeated calls are stable (no mutation, no drift).
    again = await _tool_list_adopted(bridge, server_a)
    assert again["total"] == tool_page["total"]
    assert [a["application_id"] for a in again["applications"]] == tool_ids

    # 4. The other server sees ONLY its own entries (target isolation).
    other_page = await _tool_list_adopted(bridge, server_b)
    assert other_page["total"] == 1
    assert [a["application_id"] for a in other_page["applications"]] == [
        str(other_manual_id)
    ]


async def test_agent_tool_list_adopted_pagination_counts_filtered_set(
    live_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
) -> None:
    manual_ids: list[str] = []
    for index in range(3):
        application_id = uuid.uuid4()
        manual_ids.append(str(application_id))
        created = await call_app(
            live_app,
            "POST",
            _apps_root(server_a),
            json_body={
                "application_id": str(application_id),
                "name": f"manual-{index}",
                "origin": "manual",
                "category": "media",
            },
            headers=_mutation_headers(jar),
            cookies=jar,
        )
        assert created.status == 201, created.body

    store = live_app.state.service_map
    for index in range(3):
        store.upsert_entry(
            server_a,
            name=f"orphan-{index}",
            origin=NONMANUAL_ORIGINS[index % len(NONMANUAL_ORIGINS)],
            application_id=uuid.uuid4(),
            compose_project=f"orphan-project-{index}",
        )

    bridge = _internal_api_bridge(live_app, (server_a,))

    first = await _tool_list_adopted(bridge, server_a, limit=2, offset=0)
    assert first["total"] == 3
    assert first["limit"] == 2
    assert first["offset"] == 0
    first_ids = [a["application_id"] for a in first["applications"]]
    assert len(first_ids) == 2

    second = await _tool_list_adopted(bridge, server_a, limit=2, offset=2)
    assert second["total"] == 3
    assert second["limit"] == 2
    assert second["offset"] == 2
    second_ids = [a["application_id"] for a in second["applications"]]
    assert len(second_ids) == 1

    # Deterministic repeated pages, and the pages cover exactly the
    # manual entries (the orphans are filtered out).
    again = await _tool_list_adopted(bridge, server_a, limit=2, offset=0)
    assert [a["application_id"] for a in again["applications"]] == first_ids
    assert set(first_ids + second_ids) == set(manual_ids)

    # has_more reflects the FILTERED set: a page that consumes the whole
    # filtered set reports no more, a partial page reports more.
    full = await _tool_list_adopted(bridge, server_a, limit=3, offset=0)
    assert full["total"] == 3
    assert len(full["applications"]) == 3
    assert full["has_more"] is False
    partial = await _tool_list_adopted(bridge, server_a, limit=2, offset=0)
    assert partial["has_more"] is True


async def test_catalog_tools_and_adopt_inherits_catalog_with_custom_instructions(
    live_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
) -> None:
    """End-to-end two-tier workflow:
    1. Agent adds an app to the global Runbook Catalog via app.upsert_catalog_runbook
       (works in General Consult without a server_id).
    2. Agent lists the catalog via app.list_catalog.
    3. Agent adopts the app on server_a via app.adopt with server-specific
       custom_instructions and installed=True, automatically inheriting the
       catalog's category, description, and 5-slot runbook instructions.
    4. Agent updates custom_instructions/installed via a second app.adopt call
       without passing expected_revision or application_id.
    5. Deleting the catalog recipe via app.delete_catalog_runbook leaves the
       server adoption intact.
    """
    from mai.capabilities.internal_api import build_internal_api_capabilities

    del jar
    composition = build_internal_api_capabilities(
        applications=live_app.state.applications,
        adoption=live_app.state.app_adoption,
        docs=live_app.state.app_docs,
        service_map=live_app.state.service_map,
        session_factory=live_app.state.session_factory,
    )
    desc_by_id = {d.identifier: d for d in composition.descriptors}

    def _call(
        cap_id: str, payload: Mapping[str, Any], ctx: ExecutionContext
    ) -> CapabilityCall:
        return CapabilityCall(
            descriptor=desc_by_id[cap_id],
            payload=dict(payload),
            context=ctx,
            credentials={},
            op_id=uuid.uuid4(),
        )

    global_ctx = _context(
        ("app.list_catalog", "app.upsert_catalog_runbook", "app.delete_catalog_runbook"),
        server_id=uuid.UUID(int=0),
    )

    # 1. Upsert into global catalog from General Consult (server_id = 0).
    upsert_exec = composition.executors["app.upsert_catalog_runbook"]
    upsert_res = await upsert_exec(
        _call(
            "app.upsert_catalog_runbook",
            {
                "name": "Immich",
                "category": "media",
                "description": "Self-hosted photo and video management platform.",
                "instructions": {
                    "install": "docker compose up -d",
                    "update": "docker compose pull && docker compose up -d",
                    "uninstall": "docker compose down",
                    "backup": "pg_dumpall > dump.sql",
                    "verify": "curl -fsS http://127.0.0.1:2283/api/server/ping",
                },
            },
            global_ctx,
        )
    )
    assert upsert_res.ok is True, upsert_res.output
    upsert_env = json.loads(upsert_res.output)
    assert upsert_env["status"] == "ok"
    runbook_id = upsert_env["runbook_id"]
    assert upsert_env["category"] == "media"
    assert upsert_env["revision"] == 1

    # 2. List global catalog with query filter.
    list_exec = composition.executors["app.list_catalog"]
    list_res = await list_exec(
        _call(
            "app.list_catalog",
            {"query": "immich", "limit": 10, "offset": 0},
            global_ctx,
        )
    )
    assert list_res.ok is True, list_res.output
    list_env = json.loads(list_res.output)
    assert list_env["total"] == 1
    assert list_env["runbooks"][0]["runbook_id"] == runbook_id
    assert list_env["runbooks"][0]["instructions"]["verify"].startswith("curl")

    # 3. Adopt on server_a with compose selectors + server-specific custom_instructions
    # while omitting category, description, and instructions (must inherit from catalog).
    server_ctx = _context(("app.adopt", "app.list_adopted"), server_id=server_a)
    adopt_exec = composition.executors["app.adopt"]
    adopt_res = await adopt_exec(
        _call(
            "app.adopt",
            {
                "name": "Immich",
                "origin": "compose",
                "compose_project": "immich",
                "working_dir": "/opt/immich",
                "installed": True,
                "custom_instructions": "Compose dir /opt/immich; bound to 127.0.0.1:2283; library at /mnt/nas/photos.",
            },
            server_ctx,
        )
    )
    assert adopt_res.ok is True, adopt_res.output
    adopt_env = json.loads(adopt_res.output)
    assert adopt_env["status"] == "ok"
    assert adopt_env["category"] == "media"
    assert adopt_env["description"] == "Self-hosted photo and video management platform."
    assert adopt_env["instructions"]["install"] == "docker compose up -d"
    assert adopt_env["instructions"]["verify"] == "curl -fsS http://127.0.0.1:2283/api/server/ping"
    assert adopt_env["custom_instructions"] == (
        "Compose dir /opt/immich; bound to 127.0.0.1:2283; library at /mnt/nas/photos."
    )
    assert adopt_env["installed"] is True

    # 4. Call app.adopt again to update custom_instructions without expected_revision
    # or origin/selectors — must auto-resolve revision and preserve compose selectors + catalog instructions.
    adopt_update = await adopt_exec(
        _call(
            "app.adopt",
            {
                "name": "Immich",
                "custom_instructions": "Compose dir /opt/immich; upgraded to v1.120; backup target /mnt/nas/backups/immich.",
                "installed": True,
            },
            server_ctx,
        )
    )
    assert adopt_update.ok is True, adopt_update.output
    update_env = json.loads(adopt_update.output)
    assert update_env["application_id"] == adopt_env["application_id"]
    assert update_env["origin"] == "compose"
    assert update_env["revision"] == 2
    assert update_env["custom_instructions"] == (
        "Compose dir /opt/immich; upgraded to v1.120; backup target /mnt/nas/backups/immich."
    )
    assert update_env["instructions"]["backup"] == "pg_dumpall > dump.sql"

    # 5. Delete from catalog.json; server adoption remains untouched.
    del_exec = composition.executors["app.delete_catalog_runbook"]
    del_res = await del_exec(
        _call(
            "app.delete_catalog_runbook",
            {"runbook_id": runbook_id},
            global_ctx,
        )
    )
    assert del_res.ok is True, del_res.output
    assert json.loads(del_res.output)["removed"] is True
    stored_entry = live_app.state.service_map.get_entry(
        server_a, adopt_env["application_id"]
    )
    assert stored_entry is not None
    assert stored_entry.custom_instructions.startswith("Compose dir /opt/immich; upgraded")


async def test_docs_agent_tools_crud_pagination_and_title_upsert(
    tmp_path: Path,
) -> None:
    """Verify docs.upsert, docs.list, docs.read, and docs.delete agent tools."""
    from unittest.mock import MagicMock
    from mai.applications.document import ApplicationDocService
    from mai.capabilities.internal_api import build_internal_api_capabilities
    from mai.knowledge.documents import DocumentStore

    doc_store = DocumentStore(tmp_path)
    composition = build_internal_api_capabilities(
        applications=MagicMock(),
        adoption=MagicMock(),
        docs=ApplicationDocService(doc_store),
        service_map=_store(tmp_path),
        data_root=tmp_path,
    )
    desc_by_id = {d.identifier: d for d in composition.descriptors}

    def _call(
        cap_id: str, payload: Mapping[str, Any], ctx: ExecutionContext
    ) -> CapabilityCall:
        return CapabilityCall(
            descriptor=desc_by_id[cap_id],
            payload=dict(payload),
            context=ctx,
            credentials={},
            op_id=uuid.uuid4(),
        )

    ctx = _context(
        ("docs.list", "docs.read", "docs.upsert", "docs.delete"),
        server_id=SERVER_A,
    )
    upsert_exec = composition.executors["docs.upsert"]
    list_exec = composition.executors["docs.list"]
    read_exec = composition.executors["docs.read"]
    delete_exec = composition.executors["docs.delete"]

    # 1. Create a document in 'docs' namespace with title + content
    res1 = await upsert_exec(
        _call(
            "docs.upsert",
            {
                "title": "PostgreSQL Backup Architecture",
                "content": "Nightly pg_dumpall to /mnt/nas/backups/postgres.",
                "namespace": "docs",
                "verification": "verified",
            },
            ctx,
        )
    )
    assert res1.ok is True, res1.output
    doc1 = json.loads(res1.output)
    assert doc1["status"] == "ok"
    assert doc1["revision"] == 1
    assert doc1["namespace"] == "docs"
    assert doc1["verification"] == "verified"
    assert doc1["title"] == "PostgreSQL Backup Architecture"
    assert doc1["citation"] == f"docs:{doc1['doc_id']}#r1"
    doc1_id = doc1["doc_id"]

    # 2. Upsert again with the SAME title and no doc_id -> updates revision to 2
    res1_update = await upsert_exec(
        _call(
            "docs.upsert",
            {
                "title": "PostgreSQL Backup Architecture",
                "content": "Nightly pg_dumpall + ZFS snapshot at 03:00 UTC.",
                "namespace": "docs",
                "verification": "verified",
            },
            ctx,
        )
    )
    assert res1_update.ok is True, res1_update.output
    doc1_v2 = json.loads(res1_update.output)
    assert doc1_v2["doc_id"] == doc1_id
    assert doc1_v2["revision"] == 2

    # 3. Create an operational runbook in 'runbooks' namespace
    res2 = await upsert_exec(
        _call(
            "docs.upsert",
            {
                "title": "Rotate WireGuard Keys",
                "content": "1. Generate keypair\n2. Restart wg-quick@wg0",
                "namespace": "runbooks",
            },
            ctx,
        )
    )
    assert res2.ok is True, res2.output
    doc2 = json.loads(res2.output)
    assert doc2["namespace"] == "runbooks"
    assert doc2["verification"] == "unverified"

    # 4. List documents with pagination and filters
    list_all = json.loads(
        (await list_exec(_call("docs.list", {"limit": 1, "offset": 0}, ctx))).output
    )
    assert list_all["status"] == "ok"
    assert list_all["total"] == 2
    assert list_all["limit"] == 1
    assert list_all["offset"] == 0
    assert list_all["has_more"] is True
    assert len(list_all["documents"]) == 1

    list_filtered = json.loads(
        (
            await list_exec(
                _call("docs.list", {"query": "zfs snapshot", "namespace": "docs"}, ctx)
            )
        ).output
    )
    assert list_filtered["total"] == 1
    assert list_filtered["documents"][0]["doc_id"] == doc1_id
    assert list_filtered["documents"][0]["revision"] == 2

    # 5. Read full document via docs.read
    read_res = await read_exec(
        _call("docs.read", {"doc_id": doc1_id, "namespace": "docs"}, ctx)
    )
    assert read_res.ok is True, read_res.output
    read_env = json.loads(read_res.output)
    assert read_env["doc_id"] == doc1_id
    assert read_env["revision"] == 2
    assert "# PostgreSQL Backup Architecture" in read_env["content"]
    assert "ZFS snapshot" in read_env["content"]
    assert read_env["truncated"] is False

    # 6. Delete document via docs.delete and confirm removal
    del_res = await delete_exec(
        _call("docs.delete", {"doc_id": doc1_id, "namespace": "docs"}, ctx)
    )
    assert del_res.ok is True, del_res.output
    del_env = json.loads(del_res.output)
    assert del_env["deleted"] is True
    assert del_env["revision"] == 2

    list_after = json.loads(
        (await list_exec(_call("docs.list", {}, ctx))).output
    )
    assert list_after["total"] == 1
    assert list_after["documents"][0]["doc_id"] == doc2["doc_id"]





# ---------------------------------------------------------------------------
# ask_15-map-file-lock: cross-process locking, atomic rename, revision CAS
# (pure filesystem; workers use the fork start method — Linux CI contract)
# ---------------------------------------------------------------------------


def _map_worker_upsert_distinct(root: str, server_id: str, worker: int) -> None:
    """One OS process inserting five distinct services (cross-process
    write contention on the same server map)."""
    store = ServiceMapStore(root)
    sid = uuid.UUID(server_id)
    for index in range(5):
        store.upsert_entry(
            sid,
            name=f"svc-{worker}-{index}",
            category="system",
            description=f"inserted by worker {worker}",
        )


def _map_worker_update_same(root: str, server_id: str, name: str, worker: int) -> None:
    """One OS process updating the SAME service once (revision race)."""
    store = ServiceMapStore(root)
    store.upsert_entry(
        uuid.UUID(server_id), name=name, category="system", description=f"update-{worker}"
    )


def _map_probe_lock(root: str, server_id: str, results) -> None:  # type: ignore[no-untyped-def]
    """In a CHILD process: try a NON-BLOCKING exclusive flock on the
    server lock file. Puts 0 when correctly blocked (EWOULDBLOCK), 1 when
    wrongly acquired, 2 on any other failure."""
    try:
        path = Path(root) / "servers" / f"{server_id}.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            results.put(1)
        except BlockingIOError:
            results.put(0)
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001 — the exit code IS the result channel
        results.put(2)


def test_cross_process_distinct_upserts_zero_lost_updates(tmp_path: Path) -> None:
    root = tmp_path / "service_map"
    root.mkdir(parents=True)
    ctx = multiprocessing.get_context("fork")
    processes = [
        ctx.Process(target=_map_worker_upsert_distinct, args=(str(root), str(SERVER_A), w))
        for w in range(8)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(120)
    for process in processes:
        if process.is_alive():  # never hang pytest's atexit on a straggler
            process.terminate()
            process.join(5)
    assert all(process.exitcode == 0 for process in processes)
    entries = ServiceMapStore(root).list_entries(SERVER_A)
    assert len(entries) == 40  # 8 processes x 5 services, zero lost updates
    assert {e.name for e in entries} == {
        f"svc-{w}-{i}" for w in range(8) for i in range(5)
    }
    assert all(e.revision == 1 for e in entries)


def test_cross_process_same_service_updates_monotonic(tmp_path: Path) -> None:
    root = tmp_path / "service_map"
    root.mkdir(parents=True)
    ctx = multiprocessing.get_context("fork")
    processes = [
        ctx.Process(target=_map_worker_update_same, args=(str(root), str(SERVER_A), "traefik", w))
        for w in range(12)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(120)
    for process in processes:
        if process.is_alive():  # never hang pytest's atexit on a straggler
            process.terminate()
            process.join(5)
    assert all(process.exitcode == 0 for process in processes)
    final = ServiceMapStore(root).get_entry(
        SERVER_A, uuid.uuid5(SERVER_A, "service-map:traefik")
    )
    assert final is not None
    assert final.revision == 12  # 1..12, no lost update
    assert final.description.startswith("update-")


def test_concurrent_reader_never_observes_torn_json(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_entry(SERVER_A, name="base", category="system")
    stop = threading.Event()
    errors: list[Exception] = []

    def writer(worker: int) -> None:
        writer_store = ServiceMapStore(store.root)
        try:
            for index in range(40):
                writer_store.upsert_entry(
                    SERVER_A,
                    name=f"svc-{worker}-{index}",
                    category="system",
                    description=f"from writer {worker}",
                )
        except Exception as exc:  # noqa: BLE001 — collected for the main thread
            errors.append(exc)

    def reader() -> None:
        reader_store = ServiceMapStore(store.root)
        reads = 0
        try:
            while reads < 120 and not stop.is_set():
                entries = reader_store.list_entries(SERVER_A)
                if not entries or entries[0].category != "system":
                    errors.append(AssertionError("torn or empty read observed"))
                    break
                if reader_store.get_entry(SERVER_A, uuid.UUID(entries[0].application_id)) is None:
                    errors.append(AssertionError("entry vanished mid-read"))
                reads += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    writers = [threading.Thread(target=writer, args=(w,)) for w in range(4)]
    readers = [threading.Thread(target=reader) for _ in range(2)]
    for thread in (*writers, *readers):
        thread.start()
    for thread in writers:
        thread.join(120)
    stop.set()
    for thread in readers:
        thread.join(120)
    assert errors == []
    assert len(store.list_entries(SERVER_A)) == 1 + 4 * 40


def test_expected_revision_cas_upsert_and_delete(tmp_path: Path) -> None:
    store = _store(tmp_path)
    aid = uuid.uuid5(SERVER_A, "service-map:cas")

    created = store.upsert_entry(
        SERVER_A, name="cas", application_id=aid, expected_revision=0
    )
    assert created.revision == 1

    with pytest.raises(ServiceMapError) as create_conflict:
        store.upsert_entry(
            SERVER_A, name="cas", application_id=aid, expected_revision=0
        )
    assert create_conflict.value.code is ServiceMapErrorCode.REVISION_CONFLICT

    updated = store.upsert_entry(
        SERVER_A, name="cas", application_id=aid, description="v2", expected_revision=1
    )
    assert updated.revision == 2

    with pytest.raises(ServiceMapError) as stale_update:
        store.upsert_entry(
            SERVER_A, name="cas", application_id=aid, description="v3", expected_revision=1
        )
    assert stale_update.value.code is ServiceMapErrorCode.REVISION_CONFLICT
    assert store.get_entry(SERVER_A, aid) is not None
    assert store.get_entry(SERVER_A, aid).revision == 2  # untouched by the refusal

    with pytest.raises(ServiceMapError) as stale_delete:
        store.delete_entry(SERVER_A, aid, expected_revision=1)
    assert stale_delete.value.code is ServiceMapErrorCode.REVISION_CONFLICT
    assert store.get_entry(SERVER_A, aid) is not None  # NOT deleted

    assert store.delete_entry(SERVER_A, aid, expected_revision=2) is True
    assert store.get_entry(SERVER_A, aid) is None

    # Type validation: bool and negative integers are INVALID_REQUEST.
    with pytest.raises(ServiceMapError) as bad_type:
        store.upsert_entry(SERVER_A, name="cas2", expected_revision=True)
    assert bad_type.value.code is ServiceMapErrorCode.INVALID_REQUEST
    with pytest.raises(ServiceMapError) as negative:
        store.delete_entry(SERVER_A, uuid.uuid4(), expected_revision=-1)
    assert negative.value.code is ServiceMapErrorCode.INVALID_REQUEST


def test_server_lock_reentrant_and_blocks_other_processes(tmp_path: Path) -> None:
    root = tmp_path / "service_map"
    store = ServiceMapStore(root)
    lock_path = root / "servers" / f"{SERVER_A}.lock"
    ctx = multiprocessing.get_context("fork")

    with store.server_lock(SERVER_A):
        assert lock_path.exists()  # lock file materialized while held
        # Nested re-entrant acquisition + a real mutation inside the held
        # lock: must NOT deadlock (RLock re-entry, no second flock).
        with store.server_lock(SERVER_A):
            entry = store.upsert_entry(SERVER_A, name="locked", category="system")
        assert entry.revision == 1

        # Another PROCESS attempting a non-blocking exclusive flock on the
        # SAME lock file gets BlockingIOError (EWOULDBLOCK).
        results = ctx.SimpleQueue()
        probe = ctx.Process(
            target=_map_probe_lock, args=(str(root), str(SERVER_A), results)
        )
        probe.start()
        probe.join(60)
        if probe.is_alive():
            probe.terminate()
            probe.join(5)
        assert probe.exitcode == 0
        assert results.get() == 0

    # The lock file is never unlinked on release...
    assert lock_path.exists()
    # ...and a fresh probe CAN acquire it once the holder released.
    results_after = ctx.SimpleQueue()
    probe_after = ctx.Process(
        target=_map_probe_lock, args=(str(root), str(SERVER_A), results_after)
    )
    probe_after.start()
    probe_after.join(60)
    if probe_after.is_alive():
        probe_after.terminate()
        probe_after.join(5)
    assert probe_after.exitcode == 0
    assert results_after.get() == 1


def test_server_lock_acquire_failure_rolls_back_depth_and_rlock(
    tmp_path: Path,
) -> None:
    """Fix 1 regression: an unusable root (``servers`` exists as a plain
    file, so ``_ensure_root`` cannot create the lock file's parent) makes
    ``acquire`` fail — and the failure MUST roll back ``_depth`` and
    release the RLock, leaving the lock usable for later threads."""
    from mai.applications import service_map as service_map_module

    root = tmp_path / "service_map"
    root.mkdir(parents=True)
    (root / "servers").write_text("not a directory")
    store = ServiceMapStore(root)
    lock_path = (root / "servers" / f"{SERVER_A}.lock").resolve(strict=False)

    with pytest.raises(OSError):  # FileExistsError from _ensure_root
        with store.server_lock(SERVER_A):
            pytest.fail("the lock must not be acquirable on an unusable root")

    locked = service_map_module._FILE_LOCKS[str(lock_path)]
    assert locked._depth == 0  # fully rolled back
    assert locked._rlock.acquire(blocking=False) is True  # RLock is FREE
    locked._rlock.release()

    # The store remains usable for the happy path afterwards (a sibling
    # root still works, proving the failure poisoned nothing).
    healthy = ServiceMapStore(tmp_path / "healthy_map")
    with healthy.server_lock(SERVER_A):
        assert healthy.upsert_entry(SERVER_A, name="after", category="system")


# ---------------------------------------------------------------------------
# ask_17-agent-writer-retrofit: the internal writer executors route their
# mutation sections through the slice-16 coordinator guard (C4 lease set +
# tombstone-before-removal) when the lease service is wired into the
# composition; without it the legacy path stays byte-identical.
# ---------------------------------------------------------------------------


async def test_internal_api_writer_guard_tombstones_before_removal(
    live_app: FastAPI,
    bootstrapped_url: str,
    jar: dict[str, str],
    server_a: uuid.UUID,
) -> None:
    from mai.capabilities.internal_api import build_internal_api_capabilities
    from mai.jobs.resource_locks import ResourceLockService

    del jar
    coordinator_locks = ResourceLockService(bootstrapped_url)
    coordinator_locks.ensure_schema()
    composition = build_internal_api_capabilities(
        applications=live_app.state.applications,
        adoption=live_app.state.app_adoption,
        docs=live_app.state.app_docs,
        service_map=live_app.state.service_map,
        session_factory=live_app.state.session_factory,
        locks=coordinator_locks,
    )
    desc_by_id = {
        descriptor.identifier: descriptor for descriptor in composition.descriptors
    }
    recipe_locks = ResourceLockService(bootstrapped_url, boot_id=str(uuid.uuid4()))
    recipe_locks.ensure_schema()

    def _call(
        cap_id: str, payload: Mapping[str, Any], ctx: ExecutionContext
    ) -> CapabilityCall:
        return CapabilityCall(
            descriptor=desc_by_id[cap_id],
            payload=dict(payload),
            context=ctx,
            credentials={},
            op_id=uuid.uuid4(),
        )

    ctx = _context(("app.adopt", "app.unadopt"), server_id=server_a)
    tag = uuid.uuid4().hex[:8]
    aid = uuid.uuid5(server_a, f"service-map:guard-{tag}")
    app_key = f"application:{aid}"

    adopted = await composition.executors["app.adopt"](
        _call(
            "app.adopt",
            {
                "name": f"guard-{tag}",
                "origin": "compose",
                "compose_project": f"grd-{tag}",
                "working_dir": f"/srv/grd-{tag}",
                "custom_instructions": "guard notes",
            },
            ctx,
        )
    )
    assert adopted.ok is True, adopted.output
    assert json.loads(adopted.output)["revision"] == 1
    # Both coordinator-owned leases are released after the adoption.
    assert coordinator_locks.inspect(str(server_a), app_key) is None
    assert coordinator_locks.inspect(str(server_a), "service-metadata") is None

    # A canonically adopted entry is refused by app.delete_map_entry
    # (use app.unadopt instead); nothing is deleted or tombstoned.
    del_refused = await composition.executors["app.delete_map_entry"](
        _call("app.delete_map_entry", {"application_id": str(aid)}, ctx)
    )
    assert del_refused.ok is False
    assert json.loads(del_refused.output)["code"] == "conflict"
    assert live_app.state.service_map.get_entry(server_a, aid) is not None

    # Contention: another boot holding the metadata lease refuses the
    # unadopt BEFORE any effect (no fence bypass, no tombstone written).
    holder = recipe_locks.acquire(str(server_a), "service-metadata", owner_ref="other")
    held = await composition.executors["app.unadopt"](
        _call("app.unadopt", {"application_id": str(aid)}, ctx)
    )
    assert held.ok is False
    assert json.loads(held.output)["code"] == "lease_conflict"
    assert live_app.state.service_map.get_entry(server_a, aid) is not None
    record = await live_app.state.app_adoption.read(server_a, aid)
    assert record is not None
    with psycopg.connect(bootstrapped_url) as conn:
        absent = conn.execute(
            "SELECT COUNT(*) FROM service_knowledge_identity WHERE "
            "server_id = %s AND application_id = %s",
            (str(server_a), str(aid)),
        ).fetchone()
    assert absent is not None and absent[0] == 0
    recipe_locks.release(holder)

    # Healthy unadopt: the tombstone is persisted BEFORE the revoke and
    # the map deletion; the response contract is unchanged.
    removed = await composition.executors["app.unadopt"](
        _call("app.unadopt", {"application_id": str(aid)}, ctx)
    )
    assert removed.ok is True, removed.output
    payload_doc = json.loads(removed.output)
    assert payload_doc["outcome"] == "applied"
    assert live_app.state.service_map.get_entry(server_a, aid) is None
    record = await live_app.state.app_adoption.read(server_a, aid)
    assert record is None
    with psycopg.connect(bootstrapped_url) as conn:
        tombstone = conn.execute(
            "SELECT auto_adopt_suppressed, generation FROM "
            "service_knowledge_identity WHERE server_id = %s AND "
            "application_id = %s",
            (str(server_a), str(aid)),
        ).fetchone()
    assert tombstone is not None
    assert tombstone[0] is True and tombstone[1] == 1
    assert coordinator_locks.inspect(str(server_a), app_key) is None
    assert coordinator_locks.inspect(str(server_a), "service-metadata") is None

    # An explicit re-adopt RE-ADOPTS the tombstoned identity: suppression
    # cleared, generation bumped 1 -> 2, entry and projection rebuilt.
    re_adopted = await composition.executors["app.adopt"](
        _call(
            "app.adopt",
            {
                "name": f"guard-{tag}",
                "origin": "compose",
                "compose_project": f"grd-{tag}",
                "working_dir": f"/srv/grd-{tag}",
            },
            ctx,
        )
    )
    assert re_adopted.ok is True, re_adopted.output
    assert json.loads(re_adopted.output)["revision"] == 1  # fresh projection
    assert live_app.state.service_map.get_entry(server_a, aid) is not None
    with psycopg.connect(bootstrapped_url) as conn:
        readopt_row = conn.execute(
            "SELECT auto_adopt_suppressed, generation FROM "
            "service_knowledge_identity WHERE server_id = %s AND "
            "application_id = %s",
            (str(server_a), str(aid)),
        ).fetchone()
    assert readopt_row is not None
    assert readopt_row[0] is False and readopt_row[1] == 2
    assert coordinator_locks.inspect(str(server_a), "service-metadata") is None


async def test_internal_api_delete_map_entry_guard_releases_leases(
    live_app: FastAPI,
    bootstrapped_url: str,
    jar: dict[str, str],
    server_a: uuid.UUID,
) -> None:
    from mai.capabilities.internal_api import build_internal_api_capabilities
    from mai.jobs.resource_locks import ResourceLockService

    del jar
    coordinator_locks = ResourceLockService(bootstrapped_url)
    coordinator_locks.ensure_schema()
    composition = build_internal_api_capabilities(
        applications=live_app.state.applications,
        adoption=live_app.state.app_adoption,
        docs=live_app.state.app_docs,
        service_map=live_app.state.service_map,
        session_factory=live_app.state.session_factory,
        locks=coordinator_locks,
    )
    desc_by_id = {
        descriptor.identifier: descriptor for descriptor in composition.descriptors
    }

    def _call(
        cap_id: str, payload: Mapping[str, Any], ctx: ExecutionContext
    ) -> CapabilityCall:
        return CapabilityCall(
            descriptor=desc_by_id[cap_id],
            payload=dict(payload),
            context=ctx,
            credentials={},
            op_id=uuid.uuid4(),
        )

    ctx = _context(("app.adopt", "app.delete_map_entry"), server_id=server_a)
    tag = uuid.uuid4().hex[:8]
    manual = await composition.executors["app.adopt"](
        _call(
            "app.adopt",
            {
                "name": f"manual-{tag}",
                "origin": "manual",
                "custom_instructions": "manual notes",
            },
            ctx,
        )
    )
    assert manual.ok is True, manual.output
    aid = uuid.uuid5(server_a, f"service-map:manual-{tag}")

    delete_exec = composition.executors["app.delete_map_entry"]
    removed = await delete_exec(
        _call("app.delete_map_entry", {"application_id": str(aid)}, ctx)
    )
    assert removed.ok is True, removed.output
    assert json.loads(removed.output)["removed"] is True
    assert coordinator_locks.inspect(str(server_a), f"application:{aid}") is None
    assert coordinator_locks.inspect(str(server_a), "service-metadata") is None

    # The manual delete TOMBSTONED the identity before removal (D4).
    with psycopg.connect(bootstrapped_url) as conn:
        tombstone = conn.execute(
            "SELECT auto_adopt_suppressed, generation FROM "
            "service_knowledge_identity WHERE server_id = %s AND "
            "application_id = %s",
            (str(server_a), str(aid)),
        ).fetchone()
    assert tombstone is not None
    assert tombstone[0] is True and tombstone[1] == 1

    # The second delete keeps the not_found contract and still releases.
    missing = await delete_exec(
        _call("app.delete_map_entry", {"application_id": str(aid)}, ctx)
    )
    assert missing.ok is False
    assert json.loads(missing.output)["code"] == "not_found"
    assert coordinator_locks.inspect(str(server_a), "service-metadata") is None

    # An explicit re-adopt of the same manual service clears the tombstone
    # and bumps the generation 1 -> 2; the entry is rebuilt at revision 1.
    re_manual = await composition.executors["app.adopt"](
        _call(
            "app.adopt",
            {
                "name": f"manual-{tag}",
                "origin": "manual",
                "custom_instructions": "manual notes",
            },
            ctx,
        )
    )
    assert re_manual.ok is True, re_manual.output
    assert json.loads(re_manual.output)["revision"] == 1
    with psycopg.connect(bootstrapped_url) as conn:
        readopt_row = conn.execute(
            "SELECT auto_adopt_suppressed, generation FROM "
            "service_knowledge_identity WHERE server_id = %s AND "
            "application_id = %s",
            (str(server_a), str(aid)),
        ).fetchone()
    assert readopt_row is not None
    assert readopt_row[0] is False and readopt_row[1] == 2


async def test_internal_api_writers_fail_closed_when_coordinator_dependencies_absent(
    tmp_path: Path,
) -> None:
    """AC1 fail-closed discipline: with NO session factory and NO lease
    service the coordinator cannot exist, and the three writer executors
    refuse with ``unavailable`` — there is NO unguarded mutation path."""
    from unittest.mock import MagicMock

    from mai.capabilities.internal_api import build_internal_api_capabilities

    map_store = ServiceMapStore(tmp_path / "service_map")
    composition = build_internal_api_capabilities(
        applications=MagicMock(),
        adoption=MagicMock(),
        docs=MagicMock(),
        service_map=map_store,
        session_factory=None,
        locks=None,
    )
    desc_by_id = {d.identifier: d for d in composition.descriptors}
    ctx = _context(
        ("app.adopt", "app.unadopt", "app.delete_map_entry"), server_id=SERVER_A
    )

    def _call(
        cap_id: str, payload: Mapping[str, Any], ctx: ExecutionContext
    ) -> CapabilityCall:
        return CapabilityCall(
            descriptor=desc_by_id[cap_id],
            payload=dict(payload),
            context=ctx,
            credentials={},
            op_id=uuid.uuid4(),
        )

    for capability, payload in (
        ("app.adopt", {"name": "whatever"}),
        ("app.unadopt", {"application_id": str(uuid.uuid4())}),
        ("app.delete_map_entry", {"application_id": str(uuid.uuid4())}),
    ):
        refused = await composition.executors[capability](
            _call(capability, payload, ctx)
        )
        assert refused.ok is False, refused.output
        assert json.loads(refused.output)["code"] == "unavailable"
        assert (
            json.loads(refused.output)["message"]
            == "service metadata coordinator is unavailable"
        )
    # Not a single byte was written to the map.
    assert map_store.list_entries(SERVER_A) == ()


async def test_internal_api_delete_map_entry_fails_closed_on_adoption_read_error(
    live_app: FastAPI,
    bootstrapped_url: str,
    jar: dict[str, str],
    server_a: uuid.UUID,
) -> None:
    """An UNHEALTHY adoption store must refuse the deletion (fail-closed):
    a failed ``adoption.read`` is never treated as "no adoption record"."""
    from mai.capabilities.internal_api import build_internal_api_capabilities
    from mai.jobs.resource_locks import ResourceLockService

    coordinator_locks = ResourceLockService(bootstrapped_url)
    coordinator_locks.ensure_schema()
    composition = build_internal_api_capabilities(
        applications=live_app.state.applications,
        adoption=live_app.state.app_adoption,
        docs=live_app.state.app_docs,
        service_map=live_app.state.service_map,
        session_factory=live_app.state.session_factory,
        locks=coordinator_locks,
    )

    def _call(
        cap_id: str, payload: Mapping[str, Any], ctx: ExecutionContext
    ) -> CapabilityCall:
        return CapabilityCall(
            descriptor=desc_by_id[cap_id],
            payload=dict(payload),
            context=ctx,
            credentials={},
            op_id=uuid.uuid4(),
        )

    desc_by_id = {d.identifier: d for d in composition.descriptors}
    ctx = _context(("app.adopt", "app.delete_map_entry"), server_id=server_a)
    tag = uuid.uuid4().hex[:8]
    manual = await composition.executors["app.adopt"](
        _call(
            "app.adopt",
            {
                "name": f"failclosed-{tag}",
                "origin": "manual",
                "custom_instructions": "notes",
            },
            ctx,
        )
    )
    assert manual.ok is True, manual.output
    aid = uuid.uuid5(server_a, f"service-map:failclosed-{tag}")

    original_read = live_app.state.app_adoption.read

    async def _failing_read(server_id: uuid.UUID, application_id: uuid.UUID):
        raise RuntimeError("adoption store unavailable")

    live_app.state.app_adoption.read = _failing_read  # type: ignore[method-assign]
    try:
        refused = await composition.executors["app.delete_map_entry"](
            _call("app.delete_map_entry", {"application_id": str(aid)}, ctx)
        )
    finally:
        live_app.state.app_adoption.read = original_read  # type: ignore[method-assign]

    assert refused.ok is False
    envelope = json.loads(refused.output)
    assert envelope["code"] == "unavailable"
    assert "adoption state could not be verified" in envelope["message"]

    # Fail-closed: the entry is untouched and NO tombstone was fabricated.
    assert live_app.state.service_map.get_entry(server_a, aid) is not None
    with psycopg.connect(bootstrapped_url) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM service_knowledge_identity WHERE "
            "server_id = %s AND application_id = %s",
            (str(server_a), str(aid)),
        ).fetchone()
    assert row is not None and row[0] == 0
    assert coordinator_locks.inspect(str(server_a), "service-metadata") is None

    # With the store healthy again the same delete succeeds honestly.
    removed = await composition.executors["app.delete_map_entry"](
        _call("app.delete_map_entry", {"application_id": str(aid)}, ctx)
    )
    assert removed.ok is True, removed.output
    assert json.loads(removed.output)["removed"] is True
    assert coordinator_locks.inspect(str(server_a), "service-metadata") is None


async def test_internal_api_injected_metadata_coordinator_is_honored(
    live_app: FastAPI,
    bootstrapped_url: str,
    jar: dict[str, str],
    server_a: uuid.UUID,
) -> None:
    """An explicitly injected ``metadata_coordinator`` is honored EVEN with
    ``locks=None``, and its own lock service performs the releases."""
    from mai.applications.learning_operations import LearningOperationStore
    from mai.applications.metadata_mutations import MetadataMutationCoordinator
    from mai.capabilities.internal_api import build_internal_api_capabilities
    from mai.jobs.resource_locks import ResourceLockService

    coordinator_locks = ResourceLockService(bootstrapped_url)
    coordinator_locks.ensure_schema()
    injected = MetadataMutationCoordinator(
        locks=coordinator_locks,
        map_store=live_app.state.service_map,
        learning=LearningOperationStore(live_app.state.session_factory),
        adoption=live_app.state.app_adoption,
        sessions=live_app.state.session_factory,
    )
    composition = build_internal_api_capabilities(
        applications=live_app.state.applications,
        adoption=live_app.state.app_adoption,
        docs=live_app.state.app_docs,
        service_map=live_app.state.service_map,
        session_factory=live_app.state.session_factory,
        locks=None,
        metadata_coordinator=injected,
    )

    def _call(
        cap_id: str, payload: Mapping[str, Any], ctx: ExecutionContext
    ) -> CapabilityCall:
        return CapabilityCall(
            descriptor=desc_by_id[cap_id],
            payload=dict(payload),
            context=ctx,
            credentials={},
            op_id=uuid.uuid4(),
        )

    desc_by_id = {d.identifier: d for d in composition.descriptors}
    ctx = _context(("app.adopt",), server_id=server_a)
    tag = uuid.uuid4().hex[:8]
    adopted = await composition.executors["app.adopt"](
        _call(
            "app.adopt",
            {
                "name": f"injected-{tag}",
                "origin": "manual",
                "custom_instructions": "injected notes",
            },
            ctx,
        )
    )
    assert adopted.ok is True, adopted.output
    aid = uuid.uuid5(server_a, f"service-map:injected-{tag}")
    assert live_app.state.service_map.get_entry(server_a, aid) is not None
    # The leases were taken AND released through the injected
    # coordinator's own lock service.
    assert coordinator_locks.inspect(str(server_a), f"application:{aid}") is None
    assert coordinator_locks.inspect(str(server_a), "service-metadata") is None


# ---------------------------------------------------------------------------
# ask_19-map-capability-retrofit: app.upsert_map_entry delegates through
# the metadata coordinator (C4 lease order + tombstone guard) WITHOUT
# recursively acquiring the store's internal file lock.
# ---------------------------------------------------------------------------


def _derived_locks(session_factory: Any):
    from mai.jobs.resource_locks import ResourceLockService as _RLS

    bind = getattr(session_factory, "kw", {}).get("bind")
    bind_url = getattr(bind, "url")
    if hasattr(bind_url, "set"):
        dsn = bind_url.set(drivername="postgresql").render_as_string(
            hide_password=False
        )
    else:
        dsn = (
            str(bind_url)
            .replace("postgresql+asyncpg://", "postgresql://")
            .replace("postgres+asyncpg://", "postgresql://")
        )
    locks = _RLS(dsn)
    locks.ensure_schema()
    return locks


async def test_upsert_map_capability_delegates_to_coordinator_without_recursive_store_lock(
    tmp_path: Path, migrated_url: str
) -> None:
    store = _store(tmp_path)
    engine, session_factory = create_app_engine(migrated_url)
    try:
        my_locks = _derived_locks(session_factory)
        capability = ServiceMapUpsertCapability(
            store, session_factory=session_factory, locks=my_locks
        )
        coordinator = capability._coordinator
        original = coordinator.upsert_map_entry
        lock_key = str(
            (store.root / "servers" / f"{SERVER_A}.lock").resolve(strict=False)
        )
        calls: list[tuple[str, str]] = []
        depths_at_entry: list[int | None] = []

        async def spy(server_id: uuid.UUID, application_id: uuid.UUID, **kwargs: Any):
            # Delegation entry point: the store's internal file lock must
            # NOT be held here (it is acquired only inside upsert_entry).
            from mai.applications.service_map import _FILE_LOCKS

            lock = _FILE_LOCKS.get(lock_key)
            depths_at_entry.append(None if lock is None else lock._depth)
            calls.append((str(server_id), str(application_id)))
            return await original(server_id, application_id, **kwargs)

        coordinator.upsert_map_entry = spy  # type: ignore[method-assign]

        call = CapabilityCall(
            descriptor=upsert_map_descriptor(),
            payload={"name": "delegated"},
            context=_context((UPSERT_CAPABILITY,), server_id=SERVER_A),
            credentials={},
            op_id=uuid.uuid4(),
        )
        result = await asyncio.wait_for(capability(call), 30)
        assert result.ok is True, result.output
        aid = uuid.uuid5(SERVER_A, "service-map:delegated")
        assert calls == [(str(SERVER_A), str(aid))]  # delegation proven
        assert depths_at_entry == [None]  # store lock NOT held at entry

        entry = store.get_entry(SERVER_A, aid)
        assert entry is not None and entry.revision == 1
        # Both C4 leases were acquired and RELEASED through the wired
        # lock service.
        assert my_locks.inspect(str(SERVER_A), f"application:{aid}") is None
        assert my_locks.inspect(str(SERVER_A), "service-metadata") is None
    finally:
        await engine.dispose()


async def test_upsert_map_capability_respects_deletion_tombstone_and_readopt(
    tmp_path: Path, migrated_url: str
) -> None:
    from mai.applications.learning_operations import LearningOperationStore

    store = _store(tmp_path)
    engine, session_factory = create_app_engine(migrated_url)
    try:
        my_locks = _derived_locks(session_factory)
        capability = ServiceMapUpsertCapability(
            store, session_factory=session_factory, locks=my_locks
        )
        learning = LearningOperationStore(session_factory)
        aid = uuid.uuid4()

        async def upsert(**overrides: Any) -> Any:
            from mai.capabilities.dispatch import CapabilityCall

            call = CapabilityCall(
                descriptor=upsert_map_descriptor(),
                payload={
                    "name": "tombstoned",
                    "application_id": str(aid),
                    **overrides,
                },
                context=_context((UPSERT_CAPABILITY,), server_id=SERVER_A),
                credentials={},
                op_id=uuid.uuid4(),
            )
            return await capability(call)

        created = await asyncio.wait_for(
            upsert(custom_instructions="first notes"), 30
        )
        assert created.ok is True, created.output

        # Unadopt/delete equivalent: tombstone + full map removal.
        await learning.suppress_for_unadopt(SERVER_A, aid)
        store.delete_entry(SERVER_A, aid)

        refused = await asyncio.wait_for(upsert(), 30)
        assert refused.ok is False
        assert "auto_adopt_suppressed" in refused.output
        # The deleted service was NOT recreated by the refused call.
        assert store.get_entry(SERVER_A, aid) is None

        # An operator-initiated write passes through the tombstone...
        operator = ServiceMapUpsertCapability(
            store,
            session_factory=session_factory,
            locks=my_locks,
            explicit_operator_write=True,
        )
        operator_call = CapabilityCall(
            descriptor=upsert_map_descriptor(),
            payload={
                "name": "tombstoned",
                "application_id": str(aid),
                "custom_instructions": "operator notes",
            },
            context=_context((UPSERT_CAPABILITY,), server_id=SERVER_A),
            credentials={},
            op_id=uuid.uuid4(),
        )
        operator_result = await asyncio.wait_for(operator(operator_call), 30)
        assert operator_result.ok is True, operator_result.output
        assert store.get_entry(SERVER_A, aid) is not None

        # ...and the explicit readopt clears the tombstone for background
        # writers again.
        await learning.readopt_service_identity(SERVER_A, aid, manual_identity=True)

        background = ServiceMapUpsertCapability(
            store, session_factory=session_factory, locks=my_locks
        )
        background_call = CapabilityCall(
            descriptor=upsert_map_descriptor(),
            payload={"name": "tombstoned", "application_id": str(aid)},
            context=_context((UPSERT_CAPABILITY,), server_id=SERVER_A),
            credentials={},
            op_id=uuid.uuid4(),
        )
        background_result = await asyncio.wait_for(background(background_call), 30)
        assert background_result.ok is True, background_result.output
    finally:
        await engine.dispose()


async def test_upsert_map_capability_refuses_under_lease_contention_and_concurrent_http_ops(
    live_app: FastAPI,
    bootstrapped_url: str,
    jar: dict[str, str],
    server_a: uuid.UUID,
) -> None:
    from mai.capabilities.dispatch import CapabilityCall, ExecutorResult
    from mai.jobs.resource_locks import ResourceLockService

    store = live_app.state.service_map
    my_locks = ResourceLockService(bootstrapped_url)
    my_locks.ensure_schema()
    capability = ServiceMapUpsertCapability(
        store=store, session_factory=live_app.state.session_factory
    )

    async def upsert_call(aid: uuid.UUID, name: str):
        call = CapabilityCall(
            descriptor=upsert_map_descriptor(),
            payload={"name": name, "application_id": str(aid), "origin": "manual"},
            context=_context((UPSERT_CAPABILITY,), server_id=server_a),
            credentials={},
            op_id=uuid.uuid4(),
        )
        return await capability(call)

    # Contention: a foreign boot holding service-metadata refuses the
    # capability WITHOUT modifying the store; the lease is released in
    # finally so nothing leaks into the session-scoped fixture DB.
    holder = ResourceLockService(bootstrapped_url, boot_id=str(uuid.uuid4()))
    holder.ensure_schema()
    lease = holder.acquire(str(server_a), "service-metadata", owner_ref="test")
    try:
        refused = await upsert_call(uuid.uuid4(), "contended")
        assert refused.ok is False
        assert refused.error_code == "lease_conflict"
        envelope = json.loads(refused.output)
        assert envelope["code"] == "lease_conflict"
        assert store.list_entries(server_a) == ()
    finally:
        holder.release(lease)

    # Concurrent HTTP writer: adopt a compose service, then run the
    # capability upserts (distinct services) alongside the HTTP DELETE of
    # the adopted entry — everything serializes through the metadata
    # lease with clean release.
    compose_id = uuid.uuid4()
    tag = uuid.uuid4().hex[:8]
    created = await call_app(
        live_app,
        "POST",
        _apps_root(server_a),
        json_body={
            "application_id": str(compose_id),
            "name": f"cap-compose-{tag}",
            "origin": "compose",
            "expected_revision": 0,
            "compose_project": f"cap-{tag}",
            "working_dir": f"/srv/cap-{tag}",
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert created.status == 201, created.body

    x_id, y_id = uuid.uuid4(), uuid.uuid4()

    # The lease discipline is FAIL-FAST (never waits): concurrent
    # contenders refuse with lease_conflict. Bounded retries settle every
    # operation while keeping the concurrency genuine.
    async def upsert_with_retry(aid: uuid.UUID, name: str):
        for _ in range(20):
            result = await upsert_call(aid, name)
            if result.ok:
                return result
            await asyncio.sleep(0.05)
        return result

    async def delete_with_retry():
        for _ in range(20):
            response = await call_app(
                live_app,
                "DELETE",
                f"{_apps_root(server_a)}/{compose_id}",
                json_body={"expected_revision": 1},
                headers=_mutation_headers(jar),
                cookies=jar,
            )
            if response.status in (200, 201, 204):
                return response
            await asyncio.sleep(0.05)
        return response

    results = await asyncio.gather(
        upsert_with_retry(x_id, f"cap-x-{tag}"),
        upsert_with_retry(y_id, f"cap-y-{tag}"),
        delete_with_retry(),
    )
    for result in results:
        if isinstance(result, ExecutorResult):
            assert result.ok is True, result.output
        else:  # HttpResponse
            assert result.status in (200, 201, 204), result.body

    entries = {e.application_id: e for e in store.list_entries(server_a)}
    assert entries[str(x_id)].revision == 1  # serialized, no lost update
    assert entries[str(y_id)].revision == 1
    assert entries[str(compose_id)].revision == 1  # sidecar retained
    record = await live_app.state.app_adoption.read(server_a, compose_id)
    assert record is None  # the concurrent HTTP revoke applied
    assert my_locks.inspect(str(server_a), "service-metadata") is None
    for application_id in (x_id, y_id, compose_id):
        assert (
            my_locks.inspect(str(server_a), f"application:{application_id}")
            is None
        )


async def test_upsert_map_capability_fails_closed_when_unguarded_by_default(
    tmp_path: Path, migrated_url: str
) -> None:
    """AC 1 fail-closed: with no wiring at all, ``app.upsert_map_entry``
    refuses ``unavailable`` and writes NOTHING; a coordinator bound on
    the store AFTER capability construction dynamically enables it."""
    from mai.applications.learning_operations import LearningOperationStore
    from mai.applications.metadata_mutations import MetadataMutationCoordinator

    store = _store(tmp_path)
    capability = ServiceMapUpsertCapability(store, allow_unguarded=False)
    engine, session_factory = create_app_engine(migrated_url)
    try:
        my_locks = _derived_locks(session_factory)
        call = CapabilityCall(
            descriptor=upsert_map_descriptor(),
            payload={"name": "late-bound"},
            context=_context((UPSERT_CAPABILITY,), server_id=SERVER_A),
            credentials={},
            op_id=uuid.uuid4(),
        )
        refused = await asyncio.wait_for(capability(call), 30)
        assert refused.ok is False
        assert refused.error_code == "unavailable"
        envelope = json.loads(refused.output)
        assert envelope["code"] == "unavailable"
        assert "bind a coordinator" in envelope["message"]
        assert store.list_entries(SERVER_A) == ()  # zero writes

        # A LATER bind_coordinator on the SAME capability/store enables it.
        coordinator = MetadataMutationCoordinator(
            locks=my_locks,
            map_store=store,
            learning=LearningOperationStore(session_factory),
            sessions=session_factory,
        )
        store.bind_coordinator(coordinator)
        accepted = await asyncio.wait_for(capability(call), 30)
        assert accepted.ok is True, accepted.output
        aid = uuid.uuid5(SERVER_A, "service-map:late-bound")
        entry = store.get_entry(SERVER_A, aid)
        assert entry is not None and entry.revision == 1
        # Released through the bound coordinator's own lock service.
        assert my_locks.inspect(str(SERVER_A), f"application:{aid}") is None
        assert my_locks.inspect(str(SERVER_A), "service-metadata") is None

        # bind_coordinator validation: garbage is refused, None unbinds.
        with pytest.raises(ServiceMapError):
            store.bind_coordinator("not a coordinator")

        # Unbind AND drop the test's own reference: discovery finds
        # nothing anymore, so the capability fails closed again.
        del coordinator
        gc.collect()
        store.bind_coordinator(None)
        assert store.metadata_coordinator is None
        refused_again = await asyncio.wait_for(capability(call), 30)
        assert refused_again.ok is False
        assert refused_again.error_code == "unavailable"
    finally:
        await engine.dispose()


async def test_worker_style_positional_upsert_capability_discovers_coordinator_and_enforces_tombstone(
    tmp_path: Path, migrated_url: str
) -> None:
    """The EXACT worker.py:345->370 composition pattern: a coordinator
    composed beside the store is auto-DISCOVERED by a positional-only
    ``ServiceMapUpsertCapability(store)``, the write lands under the C4
    leases, and the deletion tombstone is enforced on the same lane."""
    from mai.applications.learning_operations import LearningOperationStore

    from unittest.mock import MagicMock

    from mai.capabilities.internal_api import build_internal_api_capabilities

    store = _store(tmp_path)
    engine, session_factory = create_app_engine(migrated_url)
    try:
        # worker.py:345 — the composition constructs a coordinator whose
        # ``_map_store`` IS this store (nothing bound explicitly here).
        # The composition reference MUST stay alive for the whole test:
        # its closures hold that coordinator, backing the gc-based
        # discovery below (worker.py retains it the same way, on the
        # app/worker module).
        composition = build_internal_api_capabilities(
            applications=MagicMock(),
            adoption=MagicMock(),
            docs=MagicMock(),
            service_map=store,
            session_factory=session_factory,
        )
        assert composition.executors  # liveness proof (kept uncollected)
        # worker.py:370 — POSITIONAL-ONLY construction (no kwargs).
        capability = ServiceMapUpsertCapability(store)
        print("PROBE noop fallback:", capability._using_noop_fallback)

        async def upsert(aid: uuid.UUID, name: str):
            call = CapabilityCall(
                descriptor=upsert_map_descriptor(),
                payload={
                    "name": name,
                    "application_id": str(aid),
                    "origin": "manual",
                    "custom_instructions": "worker notes",
                },
                context=_context((UPSERT_CAPABILITY,), server_id=SERVER_A),
                credentials={},
                op_id=uuid.uuid4(),
            )
            return await capability(call)

        target_aid = uuid.uuid4()
        accepted = await upsert(target_aid, "worker-style")
        assert accepted.ok is True, accepted.output
        # The coordinator was auto-discovered and BOUND on the store.
        assert store.metadata_coordinator is not None
        bound_locks = getattr(store.metadata_coordinator, "_locks")
        aid = target_aid
        entry = store.get_entry(SERVER_A, aid)
        assert entry is not None and entry.revision == 1
        # Both C4 leases released through the discovered coordinator's
        # own lock service.
        assert bound_locks.inspect(str(SERVER_A), f"application:{aid}") is None
        assert bound_locks.inspect(str(SERVER_A), "service-metadata") is None

        # Tombstone + remove: the same positional-only lane must now
        # REFUSE — deletion suppression enforced through the discovered
        # coordinator's identity seam; the deleted service is NOT
        # recreated.
        learning = LearningOperationStore(session_factory)
        await learning.suppress_for_unadopt(SERVER_A, aid)
        store.delete_entry(SERVER_A, aid)
        refused = await upsert(target_aid, "worker-style")
        assert refused.ok is False
        assert "auto_adopt_suppressed" in refused.output
        assert store.get_entry(SERVER_A, aid) is None
        assert bound_locks.inspect(str(SERVER_A), "service-metadata") is None
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# ask_23: service_knowledge capability composition (internal-API lane)
# ---------------------------------------------------------------------------

_KNOWLEDGE_CAPABILITY_IDS = (
    "app.resolve_service",
    "app.get_service_knowledge",
    "app.get_learning_status",
    "app.learn_service",
)


async def test_knowledge_capabilities_compose_identically_and_never_autogrant() -> None:
    """HTTP-style and worker-style compositions expose IDENTICAL
    descriptor id sets (both call the ONE shared builder); the four
    service-knowledge capabilities are registered + executor-backed in
    both; every composed descriptor passes the fail-closed gate; the
    ids stay OUT of the frozen grant vocabulary; and double
    registration of the same capability line is refused."""
    from mai.capabilities.internal_api import (
        INTERNAL_API_CAPABILITY_IDS,
        assert_agent_ready_descriptor,
        build_internal_api_capabilities,
    )
    from mai.capabilities.service_knowledge import (
        get_learning_status_descriptor,
        get_service_knowledge_descriptor,
        learn_service_descriptor,
        resolve_service_descriptor,
    )
    from mai.plugins.registry import CapabilityError

    http_comp = build_internal_api_capabilities(
        applications=None, adoption=None, docs=None, service_map=None
    )
    worker_comp = build_internal_api_capabilities(
        applications=None, adoption=None, docs=None, service_map=None
    )
    http_ids = {d.identifier for d in http_comp.descriptors}
    worker_ids = {d.identifier for d in worker_comp.descriptors}
    assert http_ids == worker_ids

    for cap_id in _KNOWLEDGE_CAPABILITY_IDS:
        assert cap_id in http_ids
        assert cap_id in http_comp.executors
        assert cap_id in worker_comp.executors
        # Registered, NEVER auto-granted.
        assert cap_id not in INTERNAL_API_CAPABILITY_IDS
    for desc in http_comp.descriptors:
        assert_agent_ready_descriptor(desc)
    # The rich service_knowledge descriptors pass the SAME gate.
    for desc in (
        resolve_service_descriptor(),
        get_service_knowledge_descriptor(),
        get_learning_status_descriptor(),
        learn_service_descriptor(),
    ):
        assert_agent_ready_descriptor(desc)

    # Worker-side registration discipline (worker.py): every composed
    # descriptor registers fail-closed; the SAME capability line cannot
    # register twice (the internal-API projection and the native
    # service_knowledge descriptor share identifier+version).
    registry = CapabilityRegistry()
    for desc in worker_comp.descriptors:
        registry.register(desc)
    with pytest.raises(CapabilityError):
        registry.register(learn_service_descriptor())


async def test_absent_knowledge_dependencies_answer_unavailable() -> None:
    """With no knowledge_read/session_factory/learning store/enqueue
    seam, all four executors answer {"status": "unavailable", ...} —
    no store touch, no fake success."""
    from mai.capabilities.internal_api import build_internal_api_capabilities

    comp = build_internal_api_capabilities(
        applications=None, adoption=None, docs=None, service_map=None
    )
    by_id = {d.identifier: d for d in comp.descriptors}
    valid_payloads = {
        "app.resolve_service": {},
        "app.get_service_knowledge": {"application_id": str(uuid.uuid4())},
        "app.get_learning_status": {"application_id": str(uuid.uuid4())},
        "app.learn_service": {
            "application_id": str(uuid.uuid4()),
            "facts": [
                {
                    "category": "deployment",
                    "key": "health-path",
                    "statement": "health lives at /healthz",
                    "source_kind": "tool_result",
                    "source_refs": [
                        "00000000-0000-4000-8000-000000000001"
                    ],
                }
            ],
            "expected_generation": 1,
        },
    }
    for cap_id in _KNOWLEDGE_CAPABILITY_IDS:
        call = CapabilityCall(
            descriptor=by_id[cap_id],
            payload=dict(valid_payloads[cap_id]),
            context=_context((cap_id,)),
            credentials={},
            op_id=uuid.uuid4(),
        )
        result = await comp.executors[cap_id](call)
        envelope = json.loads(result.output)
        assert envelope["status"] == "unavailable", envelope
        assert envelope["code"] == "unavailable"


async def test_composed_knowledge_reads_execute_over_session_factory(
    tmp_path: Path, migrated_url: str
) -> None:
    """The derived-from-session_factory read store executes real
    bounded reads through the internal-API lane (empty DB: learning
    status answers found=false), while the learn admission stays
    honestly unavailable without an engine enqueue seam."""
    from mai.capabilities.internal_api import build_internal_api_capabilities

    engine, session_factory = create_app_engine(migrated_url)
    try:
        comp = build_internal_api_capabilities(
            applications=None,
            adoption=None,
            docs=None,
            service_map=_store(tmp_path),
            session_factory=session_factory,
        )
        by_id = {d.identifier: d for d in comp.descriptors}

        status_call = CapabilityCall(
            descriptor=by_id["app.get_learning_status"],
            payload={"application_id": str(uuid.uuid4())},
            context=_context(("app.get_learning_status",)),
            credentials={},
            op_id=uuid.uuid4(),
        )
        status_env = json.loads(
            (
                await comp.executors["app.get_learning_status"](status_call)
            ).output
        )
        assert status_env["status"] == "ok"
        assert status_env["found"] is False

        resolve_call = CapabilityCall(
            descriptor=by_id["app.resolve_service"],
            payload={"name_query": "nothing-matches"},
            context=_context(("app.resolve_service",)),
            credentials={},
            op_id=uuid.uuid4(),
        )
        resolve_env = json.loads(
            (await comp.executors["app.resolve_service"](resolve_call)).output
        )
        assert resolve_env["status"] == "ok"
        assert resolve_env["total"] == 0

        learn_call = CapabilityCall(
            descriptor=by_id["app.learn_service"],
            payload={
                "application_id": str(uuid.uuid4()),
                "facts": [
                    {
                        "category": "deployment",
                        "key": "health-path",
                        "statement": "health lives at /healthz",
                        "source_kind": "tool_result",
                        "source_refs": [
                            "00000000-0000-4000-8000-000000000001"
                        ],
                    }
                ],
                "expected_generation": 1,
            },
            context=_context(("app.learn_service",)),
            credentials={},
            op_id=uuid.uuid4(),
        )
        learn_env = json.loads(
            (await comp.executors["app.learn_service"](learn_call)).output
        )
        assert learn_env["status"] == "unavailable"
        assert learn_env["code"] == "unavailable"
    finally:
        await engine.dispose()
