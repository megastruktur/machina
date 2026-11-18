"""Focused tests for the agent read tools over KnowledgeReadStore
(ask_20): descriptors are agent-ready with independent ids; every
capability confirms the C3 scope (payload server_id may only CONFIRM the
context scope), maps store rejections to stable refusal codes, and
envelope-checks every success against the 64 KiB dispatcher cap (an
overflow is a structured refusal, never sliced JSON). Real PostgreSQL
(the campaign disposable fixture); no mocks of the code under test.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import async_sessionmaker

from mai.applications.knowledge_contracts import (
    MAX_PAGE_LIMIT,
    OperationStatus,
)
from mai.applications.knowledge_read import KnowledgeReadStore
from mai.applications.learning_operations import LearningOperationStore
from mai.applications.service_map import (
    CAPABILITY_ID as UPSERT_MAP_CAPABILITY,
)
from mai.capabilities.service_knowledge import (
    GET_LEARNING_STATUS_CAPABILITY_ID,
    GET_SERVICE_KNOWLEDGE_CAPABILITY_ID,
    LEARN_SERVICE_CAPABILITY_ID,
    GetLearningStatusCapability,
    GetServiceKnowledgeCapability,
    LearnServiceCapability,
    RESOLVE_SERVICE_CAPABILITY_ID,
    ResolveServiceCapability,
    get_learning_status_descriptor,
    get_service_knowledge_descriptor,
    learn_service_descriptor,
    resolve_service_descriptor,
)
from mai.applications.service_map import ServiceMapStore
from mai.database import create_app_engine

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

SERVER_A = uuid.UUID("5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60")
SERVER_B = uuid.UUID("9a8b7c6d-1e2f-4a3b-8c5d-6e7f8a9b0c1d")
APP_MAIN_ID = uuid.UUID("0aaaaaaa-0000-4000-8000-000000000001")
MAX_STATEMENT_CHARS = 800

_TEST_LOOP: asyncio.AbstractEventLoop | None = None
_TEST_LOOP_LOCK = threading.Lock()


def _run(coro: object) -> object:
    """Run one coroutine on the persistent test loop; block for the result."""
    global _TEST_LOOP
    with _TEST_LOOP_LOCK:
        if _TEST_LOOP is None:
            _TEST_LOOP = asyncio.new_event_loop()
            threading.Thread(target=_TEST_LOOP.run_forever, daemon=True).start()
    assert _TEST_LOOP is not None
    return asyncio.run_coroutine_threadsafe(coro, _TEST_LOOP).result()


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database upgraded from zero to the current head."""
    url = os.environ.get(LIVE_URL_VAR, "")
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the read-tool claims need a real "
            "disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
            conn.execute("CREATE SCHEMA public")
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    return url


@pytest.fixture
def factory(
    migrated_url: str,
) -> async_sessionmaker:
    engine, sessions = create_app_engine(migrated_url)
    # Per-test SQL isolation: every test starts with clean knowledge
    # tables (the map store is file-scoped per test and unaffected).
    with psycopg.connect(migrated_url, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE TABLE service_learned_fact_revision, "
            "service_learned_fact, service_learning_operation, "
            "service_knowledge_identity CASCADE"
        )
    yield sessions
    _run(engine.dispose())


@pytest.fixture
def map_store(tmp_path: Path) -> ServiceMapStore:
    return ServiceMapStore(tmp_path / "service_map")


# ---------------------------------------------------------------------------
# capability + payload helpers
# ---------------------------------------------------------------------------


def _descriptors() -> dict[str, object]:
    return {
        RESOLVE_SERVICE_CAPABILITY_ID: resolve_service_descriptor(),
        GET_SERVICE_KNOWLEDGE_CAPABILITY_ID: get_service_knowledge_descriptor(),
        GET_LEARNING_STATUS_CAPABILITY_ID: get_learning_status_descriptor(),
    }


def _resolve_capability(
    factory: async_sessionmaker, map_store: ServiceMapStore
) -> ResolveServiceCapability:
    return ResolveServiceCapability(KnowledgeReadStore(factory, map_store))


def _knowledge_capability(
    factory: async_sessionmaker, map_store: ServiceMapStore
) -> GetServiceKnowledgeCapability:
    return GetServiceKnowledgeCapability(KnowledgeReadStore(factory, map_store))


def _status_capability(
    factory: async_sessionmaker, map_store: ServiceMapStore
) -> GetLearningStatusCapability:
    return GetLearningStatusCapability(KnowledgeReadStore(factory, map_store))


def _call(
    descriptor: object,
    payload: dict[str, object],
    server_id: uuid.UUID = SERVER_A,
):
    from mai.capabilities.dispatch import CapabilityCall
    from mai.policy.evaluate import ExecutionContext, PolicyMode

    return CapabilityCall(
        descriptor=descriptor,
        payload=dict(payload),
        context=ExecutionContext(
            principal="principal",
            server_id=server_id,
            run_id=uuid.uuid4(),
            parent_scope=None,
            profile_revision="1",
            prompt_revision="1",
            credential_references=(),
            allowed_capabilities=(),
            mode=PolicyMode.DELEGATED,
        ),
        credentials={},
        op_id=uuid.uuid4(),
    )


def _seed_map_entries(store: ServiceMapStore) -> dict[str, str]:
    """Same service names on BOTH servers plus the ambiguous redis trio
    and the knowledge target on SERVER_A. Returns name -> application_id."""
    mapping: dict[str, str] = {}
    for server_id in (SERVER_A, SERVER_B):
        mapping[f"traefik:{server_id}"] = str(
            store.upsert_entry(
                server_id,
                name="traefik",
                category="proxy",
                origin="compose",
                compose_project="edge",
                working_dir=(
                    "/srv/edge" if server_id == SERVER_A else "/srv/b-edge"
                ),
                custom_instructions="operator notes for traefik",
            ).application_id
        )
        mapping[f"postgres:{server_id}"] = str(
            store.upsert_entry(
                server_id,
                name="postgres",
                category="database",
                origin="systemd_unit",
                unit_name="postgres@14.service",
            ).application_id
        )
    for name in ("redis-cache", "redis-queue", "redis-session"):
        mapping[name] = str(
            store.upsert_entry(
                SERVER_A,
                name=name,
                category="cache",
                origin="compose",
                compose_project="redis",
                working_dir=f"/srv/{name}",
            ).application_id
        )
    app_main = store.upsert_entry(
        SERVER_A,
        name="app-main",
        category="application",
        origin="compose",
        compose_project="app-main",
        working_dir="/srv/app-main",
        custom_instructions="operator notes for app-main",
    )
    mapping["app-main"] = str(app_main.application_id)
    store.upsert_catalog_runbook(
        name="app-main",
        category="application",
        description="catalog runbook for app-main",
        instructions={
            "install": "docker compose up -d",
            "verify": "systemctl is-active app-main",
        },
    )
    return mapping


async def _seed_identity(
    sessions: async_sessionmaker,
    *,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    generation: int = 2,
    service_revision: int = 3,
    knowledge_revision: int = 5,
) -> None:
    import sqlalchemy as sa

    async with sessions() as session:
        await session.execute(
            sa.text(
                "INSERT INTO service_knowledge_identity (server_id, "
                "application_id, generation, service_revision, "
                "knowledge_revision) VALUES (:s, :a, :g, :sr, :kr) "
                "ON CONFLICT (server_id, application_id) DO NOTHING"
            ),
            {"s": str(server_id), "a": str(application_id), "g": generation,
             "sr": service_revision, "kr": knowledge_revision},
        )
        await session.commit()


async def _seed_fact(
    sessions: async_sessionmaker,
    *,
    fact_id: uuid.UUID,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    generation: int,
    status: str = "active",
    category: str = "deployment",
    key: str = "health-path",
    statement: str = "health lives at /healthz",
    revision: int = 1,
    created_at: datetime,
    source_ref: str = "run:1:step:2",
) -> None:
    import sqlalchemy as sa

    async with sessions() as session:
        await session.execute(
            sa.text(
                "INSERT INTO service_learned_fact (fact_id, server_id, "
                "application_id, generation, category, key, statement, "
                "revision, status, source_kind, source_ref, source_run_id, "
                "verified_at, created_at) VALUES (:f, :s, :a, :g, :c, :k, "
                ":st, :r, :status, :sk, :ref, :run, :v, :created)"
            ),
            {
                "f": str(fact_id), "s": str(server_id), "a": str(application_id),
                "g": generation, "c": category, "k": key, "st": statement,
                "r": revision, "status": status, "sk": "tool_result",
                "ref": source_ref, "run": uuid.uuid4(),
                "v": created_at, "created": created_at,
            },
        )
        await session.commit()


async def _seed_operation(
    sessions: async_sessionmaker,
    *,
    operation_id: uuid.UUID,
    workflow_id: str,
    status: OperationStatus,
    accepted_at: datetime,
    application_id: uuid.UUID | None = APP_MAIN_ID,
    sub_effects: dict | None = None,
) -> None:
    import sqlalchemy as sa

    async with sessions() as session:
        await session.execute(
            sa.text(
                "INSERT INTO service_learning_operation (operation_id, "
                "server_id, application_id, payload_digest, workflow_id, "
                "status, sub_effects, coalesce_key, source_run_id, "
                "source_tool_call_id, actor, accepted_at) VALUES (:o, :s, "
                ":a, :digest, :wf, :status, CAST(:effects AS jsonb), :ck, "
                ":run, :call, 'runtime', :at)"
            ),
            {
                "o": str(operation_id), "s": str(SERVER_A), "a": str(application_id),
                "digest": uuid.uuid4().hex + uuid.uuid4().hex,
                "wf": workflow_id, "status": status.value,
                "effects": json.dumps(sub_effects or {}),
                "ck": f"coalesce-{operation_id}", "run": uuid.uuid4(),
                "call": f"call-{operation_id}", "at": accepted_at,
            },
        )
        await session.commit()


def _assert_page_invariants(envelope: dict) -> None:
    """The frozen paging invariants on every collection result."""
    for collection in ("candidates", "facts"):
        page = envelope.get(collection)
        if not isinstance(page, dict):
            continue
        assert {"items", "limit", "offset", "total", "has_more"} <= set(page)
        assert page["has_more"] is (
            page["total"] > page["offset"] + len(page["items"])
        )


# ---------------------------------------------------------------------------
# 1. descriptors
# ---------------------------------------------------------------------------


def test_descriptors_are_agent_ready_and_ids_independent() -> None:
    descriptors = _descriptors()
    from mai.capabilities.internal_api import assert_agent_ready_descriptor

    for descriptor in descriptors.values():
        assert_agent_ready_descriptor(descriptor)
    ids = {d.identifier for d in descriptors.values()}
    assert ids == {
        RESOLVE_SERVICE_CAPABILITY_ID,
        GET_SERVICE_KNOWLEDGE_CAPABILITY_ID,
        GET_LEARNING_STATUS_CAPABILITY_ID,
    }
    # Independent from the writer capability's id (never reuse it).
    assert UPSERT_MAP_CAPABILITY not in ids


# ---------------------------------------------------------------------------
# 2. resolve_service via capability
# ---------------------------------------------------------------------------


def test_resolve_capability_exact_and_ambiguous_pages(
    tmp_path: Path, migrated_url: str, factory: async_sessionmaker,
    map_store: ServiceMapStore,
) -> None:
    _seed_map_entries(map_store)

    entries = {e.name: e for e in map_store.list_entries(SERVER_A)}
    traefik = entries["traefik"]

    # Exact application id hit.
    exact = json.loads(
        _resolve_call(
            factory, map_store, {"application_id": traefik.application_id}
        ).output
    )
    assert exact["status"] == "ok"
    assert exact["exact_match"] is True
    assert exact["match_kind"] == "exact_application_id"
    assert len(exact["candidates"]) == 1
    assert exact["candidates"][0]["application_id"] == traefik.application_id
    _assert_page_invariants(exact)

    # Ambiguous name page (redis trio) with honest has_more.
    ambiguous = json.loads(
        _resolve_call(factory, map_store, {"name_query": "redis"}).output
    )
    assert ambiguous["status"] == "ok"
    assert ambiguous["exact_match"] is False
    assert ambiguous["ambiguous"] is True
    assert ambiguous["total"] == 3
    assert len(ambiguous["candidates"]) == 3
    assert ambiguous["has_more"] is False
    _assert_page_invariants(ambiguous)


def _seed_map_entries2(tmp_path: Path) -> ServiceMapStore:
    return ServiceMapStore(tmp_path / "service_map")


def _resolve_call(
    factory: async_sessionmaker,
    map_store: ServiceMapStore,
    payload: dict[str, object],
    *,
    context_server_id: uuid.UUID = SERVER_A,
):
    capability = _resolve_capability(factory, map_store)
    descriptor = resolve_service_descriptor()
    return _run(
        capability.__call__(
            _call(descriptor, payload, context_server_id)
        )
    )


# ---------------------------------------------------------------------------
# 3. pagination: limit respected
# ---------------------------------------------------------------------------


def test_resolve_capability_pagination_limit_one(
    tmp_path: Path, migrated_url: str, factory: async_sessionmaker,
    map_store: ServiceMapStore,
) -> None:
    for name in ("page-a", "page-b", "page-c"):
        map_store.upsert_entry(SERVER_A, name=name, category="system", origin="manual")

    page = json.loads(
        _resolve_call(factory, map_store, {"name_query": "page-", "limit": 1}).output
    )
    assert page["total"] == 3
    assert len(page["candidates"]) == 1
    assert page["has_more"] is True
    assert page["limit"] == 1
    assert page["offset"] == 0
    _assert_page_invariants(page)

    page2 = json.loads(
        _resolve_call(
            factory, map_store, {"name_query": "page-", "limit": 1, "offset": 1}
        ).output
    )
    assert page2["offset"] == 1
    assert len(page2["candidates"]) == 1
    _assert_page_invariants(page2)


# ---------------------------------------------------------------------------
# 4. scope confirmation / mismatch
# ---------------------------------------------------------------------------


def test_scope_confirmation_mismatch_and_absent(
    tmp_path: Path, migrated_url: str, factory: async_sessionmaker,
    map_store: ServiceMapStore,
) -> None:
    _seed_map_entries(map_store)
    # No server_id in payload: the context server is used.
    ok = json.loads(
        _resolve_call(factory, map_store, {"name_query": "traefik"}).output
    )
    assert ok["status"] == "ok"
    assert ok["candidates"][0]["server_id"] == str(SERVER_A)

    # Payload server_id CONFIRMING the context scope: works.
    confirmed = json.loads(
        _resolve_call(
            factory,
            map_store,
            {"name_query": "traefik", "server_id": str(SERVER_A)},
        ).output
    )
    assert confirmed["status"] == "ok"

    # Payload server_id naming a DIFFERENT server: refused, zero leakage.
    mismatch = _resolve_call(
        factory,
        map_store,
        {"name_query": "traefik", "server_id": str(SERVER_B)},
        context_server_id=SERVER_A,
    )
    envelope = json.loads(mismatch.output)
    assert mismatch.ok is False
    assert envelope["code"] == "scope_mismatch"
    assert envelope["status"] == "refused"

    # Malformed UUID: invalid_request.
    malformed = _resolve_call(
        factory,
        map_store,
        {"name_query": "traefik", "server_id": "not-a-uuid"},
    )
    envelope = json.loads(malformed.output)
    assert malformed.ok is False
    assert envelope["code"] == "invalid_request"


# ---------------------------------------------------------------------------
# 5. get_service_knowledge via capability
# ---------------------------------------------------------------------------


def test_knowledge_capability_success_not_found_and_envelope_bytes(
    tmp_path: Path,
    migrated_url: str,
    factory: async_sessionmaker,
    map_store: ServiceMapStore,
) -> None:
    _seed_map_entries(map_store)
    _run(
        _seed_identity(
            factory, server_id=SERVER_A, application_id=APP_MAIN_ID
        )
    )
    # The knowledge target: an operator-curated entry under APP_MAIN_ID
    # (its name matches the seeded catalog runbook "app-main").
    map_store.upsert_entry(
        SERVER_A,
        name="app-main",
        origin="manual",
        application_id=APP_MAIN_ID,
        category="application",
        description="the knowledge target",
        custom_instructions="operator notes for app-main",
    )
    created_at = datetime(2026, 3, 1, tzinfo=timezone.utc)
    _run(
        _seed_fact(
            factory,
            fact_id=uuid.uuid4(),
            server_id=SERVER_A,
            application_id=APP_MAIN_ID,
            generation=2,
            created_at=created_at,
        )
    )

    capability = _knowledge_capability(factory, map_store)
    descriptor = get_service_knowledge_descriptor()
    result = _run(
        capability.__call__(
            _call(descriptor, {"application_id": str(APP_MAIN_ID)})
        )
    )
    assert result.ok is True, result.output
    envelope = json.loads(result.output)
    assert envelope["status"] == "ok"
    assert envelope["found_in_map"] is True
    assert envelope["generation"] == 2
    assert envelope["custom_instructions"] == "operator notes for app-main"
    assert envelope["catalog_runbook"]["name"] == "app-main"
    assert envelope["facts"]["total"] == 1
    assert len(result.output.encode("utf-8")) < 65536
    _assert_page_invariants(envelope)

    # Not-found application: structured 404-style refusal.
    missing = _run(
        capability.__call__(
            _call(descriptor, {"application_id": str(uuid.uuid4())})
        )
    )
    assert missing.ok is False
    assert json.loads(missing.output)["code"] == "not_found"


# ---------------------------------------------------------------------------
# 6. get_learning_status via capability
# ---------------------------------------------------------------------------


def test_learning_status_found_filters_and_honest_empty(
    tmp_path: Path,
    migrated_url: str,
    factory: async_sessionmaker,
    map_store: ServiceMapStore,
) -> None:
    capability = _status_capability(factory, map_store)
    descriptor = get_learning_status_descriptor()
    older = datetime(2026, 3, 1, tzinfo=timezone.utc)
    newer = datetime(2026, 3, 2, tzinfo=timezone.utc)
    op_old = uuid.uuid4()
    op_new = uuid.uuid4()
    _run(_seed_operation(factory, operation_id=op_old, workflow_id="wf-old",
                         status=OperationStatus.ACCEPTED, accepted_at=older))
    _run(_seed_operation(factory, operation_id=op_new, workflow_id="wf-new",
                         status=OperationStatus.APPLIED, accepted_at=newer))

    # Latest wins for the application filter.
    latest = json.loads(
        _run(
            capability.__call__(
                _call(descriptor, {"application_id": str(APP_MAIN_ID)}, )
            )
        ).output
    )
    assert latest["found"] is True
    assert latest["operation_id"] == str(op_new)
    # The receipt's own status travels as receipt_status; the envelope
    # status stays "ok" (fix 2: no key collision).
    assert latest["status"] == "ok"
    assert latest["receipt_status"] == OperationStatus.APPLIED.value
    assert latest["payload_digest"]
    assert isinstance(latest["sub_effects"], dict)
    assert latest["accepted_at"]
    assert latest["workflow_id"]

    # Exact operation filter.
    by_op = json.loads(
        _run(
            capability.__call__(
                _call(descriptor, {"operation_id": str(op_old)})
            )
        ).output
    )
    assert by_op["found"] is True
    assert by_op["operation_id"] == str(op_old)

    # Source-run filter over an unknown run: honest empty.
    empty = json.loads(
        _run(
            capability.__call__(
                _call(descriptor, {"source_run_id": str(uuid.uuid4())})
            )
        ).output
    )
    assert empty == {"status": "ok", "found": False}

    # Unknown application: honest empty too.
    empty2 = json.loads(
        _run(
            capability.__call__(
                _call(descriptor, {"application_id": str(uuid.uuid4())})
            )
        ).output
    )
    assert empty2["found"] is False


# ---------------------------------------------------------------------------
# 7. pagination invariants on collection results (covered in 2/3/5) and
# 8. envelope overflow refusal
# ---------------------------------------------------------------------------


def test_envelope_overflow_refused(
    tmp_path: Path,
    migrated_url: str,
    factory: async_sessionmaker,
    map_store: ServiceMapStore,
) -> None:
    # No map entry for APP_MAIN_ID: the knowledge read resolves through
    # the SQL identity row seeded below (not_found guard satisfied).
    _run(
        _seed_identity(
            factory, server_id=SERVER_A, application_id=APP_MAIN_ID
        )
    )
    created_at = datetime(2026, 3, 1, tzinfo=timezone.utc)
    huge = "x" * MAX_STATEMENT_CHARS
    for index in range(MAX_PAGE_LIMIT):
        _run(
            _seed_fact(
                factory,
                fact_id=uuid.uuid4(),
                server_id=SERVER_A,
                application_id=APP_MAIN_ID,
                generation=2,
                status="active",
                key=f"active-{index}",
                statement=huge,
                created_at=created_at,
            )
        )
        _run(
            _seed_fact(
                factory,
                fact_id=uuid.uuid4(),
                server_id=SERVER_A,
                application_id=APP_MAIN_ID,
                generation=2,
                status="conflict",
                key=f"conflict-{index}",
                statement=huge,
                created_at=created_at,
            )
        )

    capability = _knowledge_capability(factory, map_store)
    descriptor = get_service_knowledge_descriptor()
    result = _run(
        capability.__call__(
            _call(
                descriptor,
                {
                    "application_id": str(APP_MAIN_ID),
                    "include_conflicts": True,
                    "limit": MAX_PAGE_LIMIT,
                },
            )
        )
    )
    # The refusal IS the required behavior: never sliced JSON, never an
    # oversized success.
    assert result.ok is False, result.output[:200]
    envelope = json.loads(result.output)
    assert envelope["code"] == "envelope_overflow"
    assert len(result.output.encode("utf-8")) < 65536
    # Read-only honesty: the store holds ONLY the seeded facts (2 x
    # MAX_PAGE_LIMIT) and no writer touched the map in this test.
    with psycopg.connect(migrated_url, autocommit=True) as conn:
        fact_count = conn.execute(
            "SELECT COUNT(*) FROM service_learned_fact WHERE "
            "application_id = %s",
            (str(APP_MAIN_ID),),
        ).fetchone()[0]
    assert fact_count == 2 * MAX_PAGE_LIMIT
    assert map_store.list_entries(SERVER_A) == ()  # read-only: map untouched


def test_resolve_selector_vocabulary_and_disambiguation(
    tmp_path: Path, migrated_url: str, factory: async_sessionmaker,
    map_store: ServiceMapStore,
) -> None:
    """Fix 5a/5b: the selector vocabulary is compose_directory /
    systemd_unit (the store's canonical selector attributes), and a
    compose selector disambiguates by working directory."""
    unit_entry = map_store.upsert_entry(
        SERVER_A,
        name="nginx",
        category="proxy",
        origin="systemd_unit",
        unit_name="nginx.service",
    )
    compose_a = map_store.upsert_entry(
        SERVER_A,
        name="web-a",
        category="web",
        origin="compose",
        compose_project="web",
        working_dir="/srv/web-a",
    )
    compose_b = map_store.upsert_entry(
        SERVER_A,
        name="web-b",
        category="web",
        origin="compose",
        compose_project="web",
        working_dir="/srv/web-b",
    )
    descriptor = resolve_service_descriptor()
    capability = _resolve_capability(factory, map_store)

    def resolve(payload: dict[str, object]):
        return json.loads(
            _run(capability.__call__(_call(descriptor, payload))).output
        )

    # unit_name-only exact selector resolution on the systemd entry.
    unit_resolution = resolve({"unit_name": "nginx.service"})
    assert unit_resolution["exact_match"] is True
    assert unit_resolution["match_kind"] == "exact_selector"
    assert unit_resolution["total"] == 1
    assert (
        unit_resolution["candidates"][0]["application_id"]
        == unit_entry.application_id
    )

    # compose_project + working_dir disambiguates across two entries
    # sharing the same compose project.
    compose_resolution = resolve(
        {"compose_project": "web", "working_dir": "/srv/web-a"}
    )
    assert compose_resolution["exact_match"] is True
    assert compose_resolution["match_kind"] == "exact_selector"
    assert compose_resolution["total"] == 1
    assert (
        compose_resolution["candidates"][0]["application_id"]
        == compose_a.application_id
    )
    assert (
        compose_resolution["candidates"][0]["application_id"]
        != compose_b.application_id
    )

    # A NON-matching working_dir against single compose entries is NOT
    # an exact selector hit (the store falls back to the empty
    # candidate page, total 0).
    non_matching = resolve(
        {"compose_project": "web", "working_dir": "/srv/web-zzz"}
    )
    assert non_matching["exact_match"] is False
    assert non_matching["total"] == 0


def test_learning_status_envelope_matches_descriptor_keys(
    tmp_path: Path, migrated_url: str, factory: async_sessionmaker,
    map_store: ServiceMapStore,
) -> None:
    """Fix 5c: every emitted key of every successful envelope is declared
    in its descriptor's output_schema properties and includes all
    required keys."""
    from mai.capabilities.dispatch import CapabilityCall
    from mai.policy.evaluate import ExecutionContext, PolicyMode

    _seed_map_entries(map_store)
    created_at = datetime(2026, 3, 1, tzinfo=timezone.utc)
    _run(
        _seed_identity(factory, server_id=SERVER_A, application_id=APP_MAIN_ID)
    )
    _run(
        _seed_fact(
            factory,
            fact_id=uuid.uuid4(),
            server_id=SERVER_A,
            application_id=APP_MAIN_ID,
            generation=2,
            created_at=created_at,
        )
    )
    op_accepted = uuid.uuid4()
    _run(_seed_operation(factory, operation_id=op_accepted,
                         workflow_id="wf-acc",
                         status=OperationStatus.ACCEPTED, accepted_at=created_at))

    descriptors = _descriptors()

    def invoke(descriptor, payload):
        call = CapabilityCall(
            descriptor=descriptor,
            payload=dict(payload),
            context=ExecutionContext(
                principal="principal",
                server_id=SERVER_A,
                run_id=uuid.uuid4(),
                parent_scope=None,
                profile_revision="1",
                prompt_revision="1",
                credential_references=(),
                allowed_capabilities=(),
                mode=PolicyMode.DELEGATED,
            ),
            credentials={},
            op_id=uuid.uuid4(),
        )
        executors = {
            RESOLVE_SERVICE_CAPABILITY_ID: _resolve_capability(factory, map_store),
            GET_SERVICE_KNOWLEDGE_CAPABILITY_ID: _knowledge_capability(factory, map_store),
            GET_LEARNING_STATUS_CAPABILITY_ID: _status_capability(factory, map_store),
        }
        return _run(executors[descriptor.identifier](call))

    resolve_env = json.loads(invoke(
        descriptors[RESOLVE_SERVICE_CAPABILITY_ID], {"name_query": "traefik"}
    ).output)
    knowledge_env = json.loads(invoke(
        descriptors[GET_SERVICE_KNOWLEDGE_CAPABILITY_ID],
        {"application_id": str(APP_MAIN_ID)},
    ).output)
    status_env = json.loads(invoke(
        descriptors[GET_LEARNING_STATUS_CAPABILITY_ID],
        {"application_id": str(APP_MAIN_ID)},
    ).output)
    # found=False (unknown application): the honest-empty envelope emits
    # ONLY status + found — the receipt fields stay optional.
    empty_env = json.loads(invoke(
        descriptors[GET_LEARNING_STATUS_CAPABILITY_ID],
        {"application_id": str(uuid.uuid4())},
    ).output)
    assert empty_env["found"] is False

    assert status_env["status"] == "ok"
    assert status_env["found"] is True
    assert status_env["receipt_status"] == OperationStatus.ACCEPTED.value
    assert status_env["operation_id"]
    assert status_env["payload_digest"]
    assert isinstance(status_env["sub_effects"], dict)
    assert status_env["accepted_at"]
    assert status_env["workflow_id"]

    schema = descriptors[GET_LEARNING_STATUS_CAPABILITY_ID].output_schema
    declared = set(schema["properties"])
    required = set(schema["required"])
    # found=True: the full receipt envelope — exactly the declared keys,
    # including every required one.
    assert set(status_env) == declared
    assert required <= set(status_env)
    # found=False: the honest-empty envelope still satisfies
    # required <= emitted <= declared.
    assert required <= set(empty_env) <= declared

    for envelope, descriptor_id in (
        (resolve_env, RESOLVE_SERVICE_CAPABILITY_ID),
        (knowledge_env, GET_SERVICE_KNOWLEDGE_CAPABILITY_ID),
    ):
        schema = descriptors[descriptor_id].output_schema
        declared = set(schema["properties"])
        required = set(schema["required"])
        emitted = set(envelope)
        assert emitted <= declared, (
            f"{descriptor_id}: undeclared keys {emitted - declared}"
        )
        assert required <= emitted, (
            f"{descriptor_id}: missing required keys {required - emitted}"
        )


# ---------------------------------------------------------------------------
# ask_21: app.learn_service — the authorized durable admission tool
# ---------------------------------------------------------------------------

_LEARN_REF = "00000000-0000-4000-8000-000000000001"

_LEARN_FACTS = [
    {
        "category": "deployment",
        "key": "health-path",
        "statement": "health lives at /healthz",
        "source_kind": "tool_result",
        "source_refs": [_LEARN_REF],
    }
]


class _RecordingEngine:
    """Engine seam fake that acknowledges every enqueue once."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, workflow_id: str) -> None:
        self.calls.append(workflow_id)


class _FailingEngine:
    """Engine seam fake that never acknowledges an enqueue."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, workflow_id: str) -> None:
        raise RuntimeError("simulated engine outage: no acknowledgment")


_LEARN_DESCRIPTORS = {
    descriptor.identifier: descriptor
    for descriptor in (
        resolve_service_descriptor(),
        get_service_knowledge_descriptor(),
        get_learning_status_descriptor(),
        learn_service_descriptor(),
    )
}


def _learn_capability(
    factory: async_sessionmaker, engine: object
) -> LearnServiceCapability:
    return LearnServiceCapability(
        LearningOperationStore(factory), engine_enqueue=engine
    )


def _learn_call(
    payload: dict[str, object],
    *,
    op_id: uuid.UUID,
    run_id: uuid.UUID | None = None,
    server_id: uuid.UUID = SERVER_A,
):
    from mai.capabilities.dispatch import CapabilityCall
    from mai.policy.evaluate import ExecutionContext, PolicyMode

    return CapabilityCall(
        descriptor=learn_service_descriptor(),
        payload=dict(payload),
        context=ExecutionContext(
            principal="principal",
            server_id=server_id,
            run_id=run_id if run_id is not None else uuid.uuid4(),
            parent_scope=None,
            profile_revision="1",
            prompt_revision="1",
            credential_references=(),
            allowed_capabilities=("app.learn_service",),
            mode=PolicyMode.DELEGATED,
        ),
        credentials={},
        op_id=op_id,
    )


def _learning_op_row(migrated_url: str, operation_id: uuid.UUID) -> dict | None:
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute(
            "SELECT operation_id, status, source_tool_call_id, workflow_id "
            "FROM service_learning_operation WHERE operation_id = %s",
            (str(operation_id),),
        ).fetchone()
    if row is None:
        return None
    return {
        "operation_id": row[0],
        "status": row[1],
        "source_tool_call_id": row[2],
        "workflow_id": row[3],
    }


def _op_rows_for_run(migrated_url: str, run_id: uuid.UUID) -> list[dict]:
    with psycopg.connect(migrated_url) as conn:
        rows = conn.execute(
            "SELECT operation_id, status FROM service_learning_operation "
            "WHERE source_run_id = %s",
            (str(run_id),),
        ).fetchall()
    return [{"operation_id": r[0], "status": r[1]} for r in rows]


def test_learn_descriptor_ready_and_ids_distinct() -> None:
    from mai.applications.service_map import CAPABILITY_ID as UPSERT_ID
    from mai.capabilities.internal_api import assert_agent_ready_descriptor

    descriptor = learn_service_descriptor()
    assert_agent_ready_descriptor(descriptor)
    assert descriptor.identifier == LEARN_SERVICE_CAPABILITY_ID
    assert descriptor.effect_class == "mutate"
    ids = set(_LEARN_DESCRIPTORS)
    assert len(ids) == 4
    assert UPSERT_ID not in ids
    assert LEARN_SERVICE_CAPABILITY_ID != UPSERT_ID


def test_learn_happy_path_admission_and_db_row(
    tmp_path: Path, migrated_url: str, factory: async_sessionmaker
) -> None:
    engine = _RecordingEngine()
    capability = _learn_capability(factory, engine)
    run_id = uuid.uuid4()
    op_id = uuid.uuid4()

    result = _run(capability.__call__(_learn_call(
        {
            "application_id": str(APP_MAIN_ID),
            "facts": _LEARN_FACTS,
            "expected_generation": 1,
        },
        op_id=op_id,
        run_id=run_id,
    )))
    assert result.ok is True, result.output
    envelope = json.loads(result.output)
    operation_id = uuid.UUID(envelope["operation_id"])
    assert envelope["status"] == "ok"
    assert envelope["receipt_status"] == "accepted"
    assert envelope["workflow_id"] == f"service.learn:{operation_id}"
    assert envelope["replayed"] is False
    assert envelope["coalesced"] is False
    assert envelope["budget_used"] == 1
    assert envelope["budget_limit"] == 16
    assert envelope["job_run_id"] is None  # honest: DBOS binding is slice 22/36
    assert envelope["poll_operation_id"] == str(operation_id)
    assert engine.calls == [envelope["workflow_id"]]

    row = _learning_op_row(migrated_url, operation_id)
    assert row is not None
    assert row["status"] == "accepted"
    assert row["source_tool_call_id"] == str(op_id)
    assert row["workflow_id"] == envelope["workflow_id"]


def test_learn_duplicate_same_call_replays_same_receipt(
    tmp_path: Path, migrated_url: str, factory: async_sessionmaker
) -> None:
    engine = _RecordingEngine()
    capability = _learn_capability(factory, engine)
    run_id = uuid.uuid4()
    op_id = uuid.uuid4()
    payload = {"facts": _LEARN_FACTS, "expected_generation": 1}

    first = _run(capability.__call__(_learn_call(
        payload, op_id=op_id, run_id=run_id
    )))
    assert first.ok is True
    first_env = json.loads(first.output)

    replay = _run(capability.__call__(_learn_call(
        payload, op_id=op_id, run_id=run_id
    )))
    assert replay.ok is True, replay.output
    replay_env = json.loads(replay.output)
    assert replay_env["replayed"] is True
    assert replay_env["coalesced"] is False
    assert replay_env["operation_id"] == first_env["operation_id"]
    assert replay_env["budget_used"] == 1  # duplicate never wastes budget
    assert engine.calls == [first_env["workflow_id"]]  # enqueued only once


def test_learn_changed_same_call_payload_conflicts(
    tmp_path: Path, migrated_url: str, factory: async_sessionmaker
) -> None:
    capability = _learn_capability(factory, _RecordingEngine())
    run_id = uuid.uuid4()
    op_id = uuid.uuid4()

    first = _run(capability.__call__(_learn_call(
        {"facts": _LEARN_FACTS, "expected_generation": 1},
        op_id=op_id, run_id=run_id,
    )))
    assert first.ok is True

    changed = [dict(_LEARN_FACTS[0])]
    changed[0]["statement"] = "health moved to /health"
    conflict = _run(capability.__call__(_learn_call(
        {"facts": changed, "expected_generation": 1},
        op_id=op_id, run_id=run_id,
    )))
    assert conflict.ok is False
    assert json.loads(conflict.output)["code"] == "payload_conflict"
    assert len(_op_rows_for_run(migrated_url, run_id)) == 1  # no new row


def test_learn_budget_exhaustion_refuses_seventeenth(
    tmp_path: Path, migrated_url: str, factory: async_sessionmaker
) -> None:
    capability = _learn_capability(factory, _RecordingEngine())
    run_id = uuid.uuid4()

    budget_used_seen: list[int] = []
    for index in range(16):
        result = _run(capability.__call__(_learn_call(
            {
                "facts": [{
                    "category": "budget",
                    "key": f"k{index}",
                    "statement": f"statement {index}",
                    "source_kind": "tool_result",
                    "source_refs": [_LEARN_REF],
                }],
                "expected_generation": 1,
            },
            op_id=uuid.uuid4(),
            run_id=run_id,
        )))
        assert result.ok is True, result.output
        budget_used_seen.append(json.loads(result.output)["budget_used"])
    assert budget_used_seen == list(range(1, 17))

    seventeenth = _run(capability.__call__(_learn_call(
        {
            "facts": [{
                "category": "budget",
                "key": "k-overflow",
                "statement": "one too many",
                "source_kind": "tool_result",
                "source_refs": [_LEARN_REF],
            }],
            "expected_generation": 1,
        },
        op_id=uuid.uuid4(),
        run_id=run_id,
    )))
    assert seventeenth.ok is False
    envelope = json.loads(seventeenth.output)
    assert envelope["code"] == "operation_budget_exceeded"
    assert "16" in envelope["message"]


def test_learn_enqueue_failure_queues_then_replay_reconciles(
    tmp_path: Path, migrated_url: str, factory: async_sessionmaker
) -> None:
    run_id = uuid.uuid4()
    op_id = uuid.uuid4()
    payload = {
        "facts": [{
            "category": "queue",
            "key": "k1",
            "statement": "queued statement",
            "source_kind": "tool_result",
            "source_refs": [_LEARN_REF],
        }],
        "expected_generation": 1,
    }
    operation_id = uuid.uuid5(
        uuid.NAMESPACE_URL, f"mai:learning-operation:{run_id}:{op_id}"
    )

    failing = _learn_capability(factory, _FailingEngine())
    refused = _run(failing.__call__(_learn_call(
        payload, op_id=op_id, run_id=run_id
    )))
    assert refused.ok is False
    assert json.loads(refused.output)["code"] == "enqueue_failed"

    row = _learning_op_row(migrated_url, operation_id)
    assert row is not None
    assert row["status"] == "queued"  # honest durable request, not accepted

    healthy = _RecordingEngine()
    recovered = _run(_learn_capability(factory, healthy).__call__(_learn_call(
        payload, op_id=op_id, run_id=run_id
    )))
    assert recovered.ok is True, recovered.output
    envelope = json.loads(recovered.output)
    assert envelope["replayed"] is True
    assert envelope["receipt_status"] == "accepted"
    assert healthy.calls == [f"service.learn:{operation_id}"]
    assert _learning_op_row(migrated_url, operation_id)["status"] == "accepted"


def test_learn_scope_confirm_mismatch_absent(
    tmp_path: Path, migrated_url: str, factory: async_sessionmaker
) -> None:
    capability = _learn_capability(factory, _RecordingEngine())

    confirmed = _run(capability.__call__(_learn_call(
        {"facts": _LEARN_FACTS, "expected_generation": 1,
         "server_id": str(SERVER_A)},
        op_id=uuid.uuid4(),
    )))
    assert confirmed.ok is True, confirmed.output

    mismatched = _run(capability.__call__(_learn_call(
        {"facts": _LEARN_FACTS, "expected_generation": 1,
         "server_id": str(SERVER_B)},
        op_id=uuid.uuid4(),
    )))
    assert mismatched.ok is False
    assert json.loads(mismatched.output)["code"] == "scope_mismatch"

    absent = _run(capability.__call__(_learn_call(
        {"facts": _LEARN_FACTS, "expected_generation": 1},
        op_id=uuid.uuid4(),
    )))
    assert absent.ok is True, absent.output
    assert json.loads(absent.output)["budget_used"] == 1


def test_learn_payload_validation_refusals(
    tmp_path: Path, migrated_url: str, factory: async_sessionmaker
) -> None:
    capability = _learn_capability(factory, _RecordingEngine())

    invalid_payloads = [
        {"facts": [], "expected_generation": 1},  # empty facts
        {
            "facts": [
                {
                    "category": "c",
                    "key": f"k{i}",
                    "statement": "s",
                    "source_kind": "tool_result",
                    "source_refs": [_LEARN_REF],
                }
                for i in range(9)
            ],  # 9 facts
            "expected_generation": 1,
        },
        {
            "facts": [{
                "category": "c",
                "key": "k",
                "statement": "s",
                "source_kind": "tool_result",
                "source_refs": [_LEARN_REF] * 5,  # 5 source refs
            }],
            "expected_generation": 1,
        },
        {
            "facts": [{
                "category": "c",
                "key": "k",
                "statement": "x" * 801,  # statement too long
                "source_kind": "tool_result",
                "source_refs": [_LEARN_REF],
            }],
            "expected_generation": 1,
        },
        {
            "facts": _LEARN_FACTS,
            "expected_generation": 1,
            "unexpected_extra": True,  # extra field
        },
    ]
    for payload in invalid_payloads:
        result = _run(capability.__call__(_learn_call(
            payload, op_id=uuid.uuid4()
        )))
        assert result.ok is False, result.output
        assert json.loads(result.output)["code"] == "invalid_request"


def test_learn_missing_engine_fails_closed_at_construction(
    tmp_path: Path, migrated_url: str, factory: async_sessionmaker
) -> None:
    store = LearningOperationStore(factory)
    with pytest.raises(ValueError):
        LearnServiceCapability(store)
    with pytest.raises(ValueError):
        LearnServiceCapability(store, engine_enqueue=None)
    with pytest.raises(ValueError):
        LearnServiceCapability(object(), engine_enqueue=_RecordingEngine())


def test_learn_envelope_honesty_and_bound(
    tmp_path: Path, migrated_url: str, factory: async_sessionmaker
) -> None:
    descriptor = learn_service_descriptor()
    capability = _learn_capability(factory, _RecordingEngine())

    result = _run(capability.__call__(_learn_call(
        {"facts": _LEARN_FACTS, "expected_generation": 1},
        op_id=uuid.uuid4(),
    )))
    assert result.ok is True, result.output
    envelope = json.loads(result.output)
    assert len(result.output.encode("utf-8")) < 65536

    schema = descriptor.output_schema
    declared = set(schema["properties"])
    required = set(schema["required"])
    assert required <= set(envelope)
    assert set(envelope) <= declared
