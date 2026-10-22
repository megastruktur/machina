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
- Capabilities: ``app.upsert_map_entry`` and ``chat.read_session``
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

import json
import os
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

from mai.agents import run as agent_run
from mai.api.app import create_app
from mai.api.auth import CSRF_COOKIE, SESSION_COOKIE
from mai.api.chat import (
    GLOBAL_CONSULT_SERVER_ID,
    ChatSubmitBody,
    _agent_document,
    _submission_document,
)
from mai.applications.service_map import (
    DEFAULT_SERVICE_CATEGORIES,
    MAP_ORIGINS,
    ServiceMapError,
    ServiceMapStore,
    ServiceMapUpsertCapability,
    classify_service,
    upsert_map_descriptor,
)
from mai.capabilities.chat_read import (
    ChatReadSessionCapability,
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
CHAT_READ_CAPABILITY = "chat.read_session"


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
# Chat document units: tags in the digest + task enrichment (no database)
# ---------------------------------------------------------------------------


def _body(**overrides: Any) -> ChatSubmitBody:
    values: dict[str, Any] = {
        "run_id": str(uuid.uuid4()),
        "server_id": str(SERVER_A),
        "message": "check the tagged service",
        "profile": "fast",
    }
    values.update(overrides)
    return ChatSubmitBody(**values)


def test_submission_document_omits_empty_service_tags() -> None:
    without = _submission_document(_body())
    with_tags = _submission_document(_body(service_tags=["jellyfin"]))
    assert "service_tags" not in without
    assert with_tags["service_tags"] == ["jellyfin"]


def test_agent_document_appends_service_map_context(tmp_path: Path) -> None:
    store = _store(tmp_path)
    entry = store.upsert_entry(
        SERVER_A,
        name="Jellyfin",
        category="media",
        description="The media server.",
        instructions={"backup": "restic backup", "verify": "curl healthcheck"},
    )
    body = _body(service_tags=["jellyfin"])
    context = ExecutionContext(
        principal="principal",
        server_id=SERVER_A,
        run_id=uuid.UUID(body.run_id),
        parent_scope=None,
        profile_revision="1",
        prompt_revision="1",
        credential_references=(),
        allowed_capabilities=(),
        mode=PolicyMode.ASK,
    )
    resolved = store.find_entries(SERVER_A, body.service_tags)
    assert [item.application_id for item in resolved] == [entry.application_id]
    document = _agent_document(
        body=body,
        run_id=body.run_id,
        server_id=str(SERVER_A),
        context=context,
        service_details=list(resolved),
    )
    assert document["task"].startswith("check the tagged service")
    assert "## Service map context (@service tags)" in document["task"]
    assert "Jellyfin" in document["task"]
    assert "restic backup" in document["task"]
    # The producer's task bound is respected.
    assert len(document["task"]) <= agent_run.MAX_TASK_CHARS

    plain = _agent_document(
        body=body, run_id=body.run_id, server_id=str(SERVER_A), context=context
    )
    assert plain["task"] == "check the tagged service"


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


async def test_general_consult_submit_needs_no_profile(
    live_app: FastAPI, jar: dict[str, str]
) -> None:
    run_id = uuid.uuid4()
    response = await submit_chat(
        live_app, jar, run_id, GLOBAL_CONSULT_SERVER_ID
    )
    assert response.status == 201, response.body
    view = response.json()["conversation"]
    assert view["server_id"] == GLOBAL_CONSULT_SERVER_ID
    assert view["service_tags"] == []
    assert view["depth"] == 0
    assert view["profile_revision"] == 1
    assert view["prompt_revision"] == 1


async def test_general_consult_rejects_host_scoped_attachments(
    live_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
) -> None:
    response = await submit_chat(
        live_app,
        jar,
        uuid.uuid4(),
        GLOBAL_CONSULT_SERVER_ID,
        attachments=[{"server_id": str(server_a), "op_id": str(uuid.uuid4())}],
    )
    assert response.status == 422
    envelope = response.json()
    assert set(envelope["error"]["fields"]) == {"attachments"}
    assert "general consult" in envelope["error"]["fields"]["attachments"]


async def test_service_tags_record_on_submission_and_view(
    live_app: FastAPI,
    jar: dict[str, str],
    server_a: uuid.UUID,
    bootstrapped_url: str,
) -> None:
    # Seed a map entry the tags resolve against.
    adopted = await call_app(
        live_app,
        "POST",
        _apps_root(server_a),
        json_body={
            "application_id": str(uuid.uuid4()),
            "name": "Jellyfin",
            "origin": "manual",
            "category": "media",
            "instructions": {"verify": "curl -fsS localhost:8096/health"},
        },
        headers=_mutation_headers(jar),
        cookies=jar,
    )
    assert adopted.status == 201, adopted.body

    run_id = uuid.uuid4()
    response = await submit_chat(
        live_app, jar, run_id, str(server_a), service_tags=["JELLYFIN"]
    )
    assert response.status == 201, response.body
    view = response.json()["conversation"]
    assert view["service_tags"] == ["JELLYFIN"]

    events = read_events_direct(bootstrapped_url, run_id)
    submitted = next(e for e in events if e["type"] == "chat.submitted")
    assert submitted["data"]["service_tags"] == ["JELLYFIN"]
    assert submitted["data"]["handler"] == "agent.run"
    assert submitted["data"]["children"] == 0

    # Immutable per run: the identical submission attaches (the C05 r5
    # resume); any tag difference is a NEW submission → 409.
    duplicate = await submit_chat(
        live_app, jar, run_id, str(server_a), service_tags=["JELLYFIN"]
    )
    assert duplicate.status == 200
    assert duplicate.json()["deduplicated"] is True
    conflicting = await submit_chat(
        live_app, jar, run_id, str(server_a), service_tags=["other"]
    )
    assert conflicting.status == 409

    # The detail view replays the tags for the Floating Chat History.
    detail = await call_app(
        live_app,
        "GET",
        f"/api/v1/chat/runs/{run_id}",
        cookies=jar,
    )
    assert detail.status == 200
    assert detail.json()["service_tags"] == ["JELLYFIN"]


async def test_chat_runs_lists_across_all_servers_and_general_consult(
    live_app: FastAPI, jar: dict[str, str], server_a: uuid.UUID
) -> None:
    other = await create_profile(live_app, jar, "mui-02-b")
    run_a = uuid.uuid4()
    run_b = uuid.uuid4()
    run_global = uuid.uuid4()
    for run_id, scope in (
        (run_a, str(server_a)),
        (run_b, str(other)),
        (run_global, GLOBAL_CONSULT_SERVER_ID),
    ):
        response = await submit_chat(live_app, jar, run_id, scope)
        assert response.status == 201, response.body

    all_page = await call_app(
        live_app, "GET", "/api/v1/chat/runs?limit=50", cookies=jar
    )
    assert all_page.status == 200, all_page.body
    page = all_page.json()
    assert page["total"] == 3
    listed = {c["run_id"]: c["server_id"] for c in page["conversations"]}
    assert listed[str(run_a)] == str(server_a)
    assert listed[str(run_b)] == str(other)
    assert listed[str(run_global)] == GLOBAL_CONSULT_SERVER_ID

    scoped = await call_app(
        live_app,
        "GET",
        f"/api/v1/chat/runs?server_id={server_a}",
        cookies=jar,
    )
    assert scoped.status == 200
    scoped_page = scoped.json()
    assert scoped_page["total"] == 1
    assert scoped_page["conversations"][0]["run_id"] == str(run_a)


# ---------------------------------------------------------------------------
# chat.read_session capability against the real facade tables
# ---------------------------------------------------------------------------


async def test_chat_read_session_capability_replays_a_conversation(
    live_app: FastAPI,
    jar: dict[str, str],
    server_a: uuid.UUID,
    bootstrapped_url: str,
) -> None:
    run_id = uuid.uuid4()
    response = await submit_chat(
        live_app, jar, run_id, str(server_a), service_tags=["jellyfin"]
    )
    assert response.status == 201, response.body

    capability = ChatReadSessionCapability(live_app.state.session_factory)
    context = _context((CHAT_READ_CAPABILITY,))
    call = CapabilityCall(
        descriptor=chat_read_descriptor(),
        payload={"session_id": str(run_id)},
        context=context,
        credentials={},
        op_id=uuid.uuid4(),
    )
    result = await capability(call)
    assert result.ok is True
    summary = json.loads(result.output)
    assert summary["session_id"] == str(run_id)
    assert summary["server_id"] == str(server_a)
    assert summary["state"] == "queued"
    assert summary["model_profile"] == "fast"
    assert summary["service_tags"] == ["jellyfin"]
    assert summary["event_count"] >= 2
    assert summary["events_summary"][0]["type"] == "run.submitted"

    # Unknown sessions are honest not_found rejections.
    missing = CapabilityCall(
        descriptor=chat_read_descriptor(),
        payload={"session_id": str(uuid.uuid4())},
        context=context,
        credentials={},
        op_id=uuid.uuid4(),
    )
    from mai.capabilities.chat_read import ChatReadError

    with pytest.raises(ChatReadError):
        await capability(missing)


async def test_chat_read_session_executes_through_the_tool_bridge(
    live_app: FastAPI,
    jar: dict[str, str],
    server_a: uuid.UUID,
) -> None:
    run_id = uuid.uuid4()
    response = await submit_chat(
        live_app, jar, run_id, str(server_a), service_tags=["jellyfin"]
    )
    assert response.status == 201, response.body

    registry = _registry()
    bridge = CapabilityToolBridge(
        registry=registry,
        dispatcher=_dispatcher(
            registry, ChatReadSessionCapability(live_app.state.session_factory)
        ),
        context=_context((CHAT_READ_CAPABILITY,)),
    )
    tool = next(
        t for t in bridge.tools if t.name == tool_name_for(CHAT_READ_CAPABILITY)
    )
    result = json.loads(await tool.function(session_id=str(run_id)))
    assert result["status"] == "executed"
    assert result["allowed"] is True
    summary = json.loads(result["output"]["text"])
    assert summary["session_id"] == str(run_id)
    assert summary["service_tags"] == ["jellyfin"]
    assert summary["state"] == "queued"


# ---------------------------------------------------------------------------
# Fail-closed documentation enforcement on @internal_api_tool
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
    for cap_id in ("app.discover", "app.list_adopted", "app.adopt", "app.unadopt", "app.delete_map_entry"):
        desc = assert_agent_ready_descriptor(by_id[cap_id])
        assert len(str(desc.input_schema["description"])) >= 30
    adopt_props: Any = by_id["app.adopt"].input_schema["properties"]
    assert adopt_props["origin"]["enum"] == ["manual", "compose", "systemd_unit"]
    assert "install" in adopt_props["instructions"]["properties"]
    assert "machine_id" in adopt_props["upsert_categories"]["items"]["properties"]
    discover_props: Any = by_id["app.discover"].input_schema["properties"]
    assert "limit" in discover_props and "offset" in discover_props
    list_adopted_props: Any = by_id["app.list_adopted"].input_schema["properties"]
    assert "limit" in list_adopted_props and "offset" in list_adopted_props
    assert len(INTERNAL_API_CAPABILITY_IDS) == 7


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

