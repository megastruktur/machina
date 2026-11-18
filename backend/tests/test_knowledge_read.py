"""Focused tests for the paged service-knowledge reads (ask_11).

Real PostgreSQL (the campaign disposable fixture) + a real confined
``ServiceMapStore``: same-name services on two servers never leak across
scopes; resolution prefers exact id/selector over the ambiguous name
page; the knowledge read filters ACTIVE facts of the CURRENT generation
only (conflicts/retracted/superseded and stale generations excluded)
with operator notes and the catalog runbook untouched; fact history and
operation receipts paginate deterministically under the 64 KiB envelope
bound. No mocks of the code under test.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import async_sessionmaker

from mai.applications.knowledge_contracts import (
    FactStatus,
    MAX_PAGE_LIMIT,
    OperationStatus,
    SubEffectStatus,
    enforce_envelope_bound,
)
from mai.applications.knowledge_read import (
    KnowledgeReadStore,
    ServiceKnowledgeNotFoundError,
)
from mai.applications.service_map import ServiceMapStore
from mai.applications.knowledge_contracts import ScopeMismatchError
from mai.database import create_app_engine

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

SERVER_A = uuid.UUID("5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60")
SERVER_B = uuid.UUID("9a8b7c6d-1e2f-4a3b-8c5d-6e7f8a9b0c1d")
APP_MAIN_ID = uuid.UUID("0aaaaaaa-0000-4000-8000-000000000001")
_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)

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


def _set_migration_env(url: str) -> dict[str, str | None]:
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url
    return {"previous": previous}


def _restore_migration_env(saved: dict[str, str | None]) -> None:
    if saved["previous"] is None:
        os.environ.pop("MAI_DATABASE_URL", None)
    else:
        os.environ["MAI_DATABASE_URL"] = str(saved["previous"])


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database upgraded from zero to the current head."""
    url = os.environ.get(LIVE_URL_VAR, "")
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the read claims need a real "
            "disposable Postgres fixture"
        )
    saved = _set_migration_env(url)
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
            conn.execute("CREATE SCHEMA public")
        command.upgrade(_alembic_config(), "head")
    finally:
        _restore_migration_env(saved)
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
# seeding helpers
# ---------------------------------------------------------------------------


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
    sessions,
    *,
    server_id,
    application_id,
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
                "knowledge_revision) VALUES (:s, :a, :g, :sr, :kr)"
            ),
            {"s": server_id, "a": application_id, "g": generation,
             "sr": service_revision, "kr": knowledge_revision},
        )
        await session.commit()


async def _seed_fact(
    sessions,
    *,
    fact_id: uuid.UUID,
    server_id,
    application_id,
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
                "f": fact_id, "s": server_id, "a": application_id,
                "g": generation, "c": category, "k": key, "st": statement,
                "r": revision, "status": status, "sk": "tool_result",
                "ref": source_ref, "run": uuid.uuid4(),
                "v": created_at, "created": created_at,
            },
        )
        await session.commit()


async def _seed_revision(
    sessions,
    *,
    revision_id: uuid.UUID,
    fact_id: uuid.UUID,
    application_id: uuid.UUID,
    revision: int,
    statement: str,
    changed_at: datetime,
) -> None:
    import sqlalchemy as sa

    async with sessions() as session:
        await session.execute(
            sa.text(
                "INSERT INTO service_learned_fact_revision (revision_id, "
                "fact_id, server_id, application_id, revision, statement, "
                "status, source_kind, source_ref, changed_at) VALUES (:rid, "
                ":f, :s, :a, :r, :st, 'active', 'tool_result', 'r', :at)"
            ),
            {
                "rid": revision_id, "f": fact_id, "s": SERVER_A,
                "a": application_id, "r": revision, "st": statement,
                "at": changed_at,
            },
        )
        await session.commit()


async def _seed_operation(
    sessions,
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
                "o": operation_id, "s": SERVER_A, "a": application_id,
                "digest": uuid.uuid4().hex + uuid.uuid4().hex,
                "wf": workflow_id, "status": status.value,
                "effects": json.dumps(sub_effects or {}),
                "ck": f"coalesce-{operation_id}", "run": uuid.uuid4(),
                "call": f"call-{operation_id}", "at": accepted_at,
            },
        )
        await session.commit()




# ---------------------------------------------------------------------------
# resolution: same-server isolation, priority, ambiguity
# ---------------------------------------------------------------------------


def test_resolution_never_leaks_across_servers(
    migrated_url: str, map_store: ServiceMapStore, factory
) -> None:
    mapping = _seed_map_entries(map_store)
    store = KnowledgeReadStore(factory, map_store)
    result = _run(
        store.resolve_service(server_id=SERVER_A, query="traefik")
    )
    assert result.total == 1
    assert result.candidates[0].server_id == SERVER_A
    assert result.candidates[0].application_id == uuid.UUID(
        mapping["traefik:" + str(SERVER_A)]
    )
    postgres = _run(
        store.resolve_service(server_id=SERVER_A, query="postgres")
    )
    assert postgres.total == 1
    assert postgres.candidates[0].server_id == SERVER_A
    with pytest.raises(ScopeMismatchError):
        _run(
            store.resolve_service(
                server_id=SERVER_A,
                query="traefik",
                target_server_id=SERVER_B,
            )
        )
    with pytest.raises(ScopeMismatchError):
        _run(
            store.get_service_knowledge(
                server_id=SERVER_A,
                application_id=uuid.UUID(mapping["app-main"]),
                target_server_id=SERVER_B,
            )
        )


def test_resolution_priority_exact_id_then_selector_then_query(
    migrated_url: str, map_store: ServiceMapStore, factory
) -> None:
    from mai.applications.knowledge_contracts import ServiceSelector

    mapping = _seed_map_entries(map_store)
    store = KnowledgeReadStore(factory, map_store)
    app_main = uuid.UUID(mapping["app-main"])

    exact_id = _run(
        store.resolve_service(server_id=SERVER_A, application_id=app_main)
    )
    assert exact_id.exact_match is True
    assert exact_id.match_kind == "exact_application_id"
    assert exact_id.total == 1
    assert exact_id.candidates[0].application_id == app_main
    assert exact_id.candidates[0].map_revision == 1
    assert exact_id.candidates[0].generation == 1

    compose_selector = ServiceSelector(
        compose_project="edge", compose_directory="/srv/edge"
    )
    exact_selector = _run(
        store.resolve_service(server_id=SERVER_A, selector=compose_selector)
    )
    assert exact_selector.exact_match is True
    assert exact_selector.match_kind == "exact_selector"
    assert exact_selector.candidates[0].name == "traefik"

    unit_selector = ServiceSelector(systemd_unit="postgres@14.service")
    exact_unit = _run(
        store.resolve_service(server_id=SERVER_A, selector=unit_selector)
    )
    assert exact_unit.exact_match is True
    assert exact_unit.candidates[0].name == "postgres"


def test_ambiguous_query_paginates_deterministically(
    migrated_url: str, map_store: ServiceMapStore, factory
) -> None:
    _seed_map_entries(map_store)
    store = KnowledgeReadStore(factory, map_store)
    first = _run(
        store.resolve_service(server_id=SERVER_A, query="redis", limit=2, offset=0)
    )
    assert first.ambiguous is True
    assert first.exact_match is False
    assert first.total == 3
    assert [c.name for c in first.candidates] == ["redis-cache", "redis-queue"]
    assert first.has_more is True
    second = _run(
        store.resolve_service(server_id=SERVER_A, query="redis", limit=2, offset=2)
    )
    assert [c.name for c in second.candidates] == ["redis-session"]
    assert second.has_more is False
    assert second.total == 3


# ---------------------------------------------------------------------------
# knowledge read: generation/status filtering + operator separation
# ---------------------------------------------------------------------------


def test_knowledge_read_filters_generation_and_status(
    migrated_url: str, map_store: ServiceMapStore, factory
) -> None:
    mapping = _seed_map_entries(map_store)
    app_main = uuid.UUID(mapping["app-main"])
    _run(_seed_identity(factory, server_id=SERVER_A, application_id=app_main))
    # Stale generation + non-active statuses MUST be excluded.
    _run(_seed_fact(
        factory, fact_id=uuid.UUID(int=0x601), server_id=SERVER_A,
        application_id=app_main, generation=1, status="active",
        key="stale-generation-key", created_at=_EPOCH,
    ))
    _run(_seed_fact(
        factory, fact_id=uuid.UUID(int=0x602), server_id=SERVER_A,
        application_id=app_main, generation=2, status="conflict",
        created_at=_EPOCH, key="conflict-key",
        statement="conflicts with operator notes",
    ))
    _run(_seed_fact(
        factory, fact_id=uuid.UUID(int=0x603), server_id=SERVER_A,
        application_id=app_main, generation=2, status="retracted",
        created_at=_EPOCH, key="retracted-key",
    ))
    _run(_seed_fact(
        factory, fact_id=uuid.UUID(int=0x604), server_id=SERVER_A,
        application_id=app_main, generation=2, status="superseded",
        created_at=_EPOCH, key="superseded-key",
    ))
    for index in range(1, 16):
        _run(_seed_fact(
            factory,
            fact_id=uuid.UUID(int=0x700 + index),
            server_id=SERVER_A,
            application_id=app_main,
            generation=2,
            status="active",
            key=f"active-key-{index:02d}",
            statement=f"active fact {index}",
            created_at=_EPOCH + timedelta(minutes=index),
        ))

    store = KnowledgeReadStore(factory, map_store)
    first = _run(
        store.get_service_knowledge(
            server_id=SERVER_A, application_id=app_main, limit=10, offset=0
        )
    )
    assert first.generation == 2
    assert first.service_revision == 3
    assert first.knowledge_revision == 5
    assert first.found_in_map is True
    assert first.custom_instructions == "operator notes for app-main"
    assert first.instructions is not None
    assert first.catalog_runbook is not None
    assert first.catalog_runbook["description"] == "catalog runbook for app-main"
    assert first.conflict_count == 1
    assert first.facts.total == 15
    assert len(first.facts.items) == 10
    assert first.facts.has_more is True
    # Only generation-2 ACTIVE facts, no duplicates or gaps.
    fact_ids = [fact.fact_id.int for fact in first.facts.items]
    assert fact_ids == [0x701 + index for index in range(10)]
    second = _run(
        store.get_service_knowledge(
            server_id=SERVER_A, application_id=app_main, limit=10, offset=10
        )
    )
    assert len(second.facts.items) == 5
    assert second.facts.has_more is False
    assert [fact.fact_id.int for fact in second.facts.items] == [
        0x70B + index for index in range(5)
    ]
    all_ids = [fact.fact_id.int for fact in first.facts.items] + [
        fact.fact_id.int for fact in second.facts.items
    ]
    assert all_ids == [0x701 + index for index in range(15)]
    enforce_envelope_bound(first)
    enforce_envelope_bound(first.facts)
    # include_conflicts only populates the CONFLICT list: facts stays
    # strictly ACTIVE (AC1).
    with_conflicts = _run(
        store.get_service_knowledge(
            server_id=SERVER_A,
            application_id=app_main,
            include_conflicts=True,
        )
    )
    assert with_conflicts.facts.total == 15
    assert all(fact.status is FactStatus.ACTIVE for fact in with_conflicts.facts.items)
    assert with_conflicts.conflict_count == 1
    assert len(with_conflicts.conflicts) == 1
    assert with_conflicts.conflicts[0].status is FactStatus.CONFLICT
    assert with_conflicts.conflicts[0].key == "conflict-key"


def test_knowledge_read_not_found(migrated_url: str, map_store: ServiceMapStore, factory) -> None:
    _seed_map_entries(map_store)
    store = KnowledgeReadStore(factory, map_store)
    unknown = uuid.UUID(int=0xDEAD)
    with pytest.raises(ServiceKnowledgeNotFoundError) as excinfo:
        _run(
            store.get_service_knowledge(
                server_id=SERVER_A, application_id=unknown
            )
        )
    assert excinfo.value.code == "not_found"


def test_knowledge_read_map_entry_without_identity_row_uses_frozen_defaults(
    migrated_url: str, map_store: ServiceMapStore, factory
) -> None:
    """Regression (OCR fix1 critical): a map entry WITHOUT a
    ``service_knowledge_identity`` row uses the frozen defaults
    (generation 1, knowledge revision 0, compose origin is NOT a manual
    identity) — and operator notes plus the catalog runbook still come
    back."""
    mapping = _seed_map_entries(map_store)
    app_main = uuid.UUID(mapping["app-main"])
    with psycopg.connect(migrated_url) as conn:
        identity_rows = conn.execute(
            "SELECT count(*) FROM service_knowledge_identity WHERE "
            "application_id = %s",
            (str(app_main),),
        ).fetchone()[0]
    assert identity_rows == 0

    store = KnowledgeReadStore(factory, map_store)
    read = _run(
        store.get_service_knowledge(
            server_id=SERVER_A, application_id=app_main, limit=5
        )
    )
    assert read.found_in_map is True
    assert read.generation == 1
    assert read.service_revision == read.map_revision
    assert read.knowledge_revision == 0
    assert read.manual_identity is False
    assert read.auto_adopt_suppressed is False
    assert read.facts.total == 0
    assert read.facts.items == []
    assert read.custom_instructions == "operator notes for app-main"
    assert read.catalog_runbook is not None
    assert read.catalog_runbook["name"] == "app-main"


# ---------------------------------------------------------------------------
# fact history + operation status pagination
# ---------------------------------------------------------------------------


def test_fact_history_paginates_newest_first(
    migrated_url: str, map_store: ServiceMapStore, factory
) -> None:
    mapping = _seed_map_entries(map_store)
    app_main = uuid.UUID(mapping["app-main"])
    fact_id = uuid.UUID(int=0xB00)
    _run(_seed_fact(
        factory, fact_id=fact_id, server_id=SERVER_A,
        application_id=app_main, generation=2, status="superseded",
        statement="final superseded statement", created_at=_EPOCH,
    ))
    for index in range(1, 14):
        _run(_seed_revision(
            factory,
            revision_id=uuid.UUID(int=0xB00 + index),
            fact_id=fact_id,
            application_id=app_main,
            revision=index,
            statement=f"history revision {index}",
            changed_at=_EPOCH + timedelta(minutes=index),
        ))

    store = KnowledgeReadStore(factory, map_store)
    first = _run(
        store.list_fact_history(
            server_id=SERVER_A, application_id=app_main,
            fact_id=fact_id, limit=5, offset=0,
        )
    )
    assert first.total == 13
    assert len(first.items) == 5
    assert first.has_more is True
    assert [row.revision for row in first.items] == [13, 12, 11, 10, 9]
    assert first.items[0].category == "deployment"
    assert first.items[0].generation == 2
    second = _run(
        store.list_fact_history(
            server_id=SERVER_A, application_id=app_main,
            fact_id=fact_id, limit=5, offset=5,
        )
    )
    assert [row.revision for row in second.items] == [8, 7, 6, 5, 4]
    third = _run(
        store.list_fact_history(
            server_id=SERVER_A, application_id=app_main,
            fact_id=fact_id, limit=5, offset=10,
        )
    )
    assert [row.revision for row in third.items] == [3, 2, 1]
    assert third.has_more is False
    for page in (first, second, third):
        enforce_envelope_bound(page)


def test_operation_status_paginates_and_filters(
    migrated_url: str, map_store: ServiceMapStore, factory
) -> None:
    mapping = _seed_map_entries(map_store)
    app_main = uuid.UUID(mapping["app-main"])
    cycle = [
        OperationStatus.ACCEPTED,
        OperationStatus.APPLIED,
        OperationStatus.FAILED,
        OperationStatus.PARTIAL,
        OperationStatus.UNKNOWN,
    ]
    seeded_unknown = 0
    for index in range(1, 14):
        status = cycle[index % len(cycle)]
        if status is OperationStatus.UNKNOWN:
            seeded_unknown += 1
        _run(_seed_operation(
            factory,
            operation_id=uuid.UUID(int=0xC00 + index),
            workflow_id=f"wf-{index:03d}",
            status=status,
            accepted_at=_EPOCH + timedelta(minutes=index),
            application_id=app_main,
            sub_effects={"facts": SubEffectStatus.APPLIED.value},
        ))

    store = KnowledgeReadStore(factory, map_store)
    first = _run(
        store.list_learning_operations(
            server_id=SERVER_A, application_id=app_main, limit=5, offset=0
        )
    )
    assert first.total == 13
    assert len(first.items) == 5
    assert first.has_more is True
    accepted_times = [item.accepted_at for item in first.items]
    assert accepted_times == sorted(accepted_times, reverse=True)
    assert first.items[0].workflow_id == "wf-013"
    assert all(
        item.payload_digest and len(item.payload_digest) == 64
        for item in first.items
    )
    second = _run(
        store.list_learning_operations(
            server_id=SERVER_A, application_id=app_main, limit=5, offset=5
        )
    )
    assert [item.workflow_id for item in second.items] == [
        "wf-008", "wf-007", "wf-006", "wf-005", "wf-004",
    ]
    third = _run(
        store.list_learning_operations(
            server_id=SERVER_A, application_id=app_main, limit=5, offset=10
        )
    )
    assert [item.workflow_id for item in third.items] == [
        "wf-003", "wf-002", "wf-001",
    ]
    assert third.has_more is False
    for page in (first, second, third):
        enforce_envelope_bound(page)

    unknown_page = _run(
        store.list_learning_operations(
            server_id=SERVER_A, application_id=app_main,
            status=OperationStatus.UNKNOWN,
        )
    )
    assert unknown_page.total == seeded_unknown == 2
    assert all(
        item.status is OperationStatus.UNKNOWN for item in unknown_page.items
    )
    latest = _run(
        store.get_learning_status(
            server_id=SERVER_A, application_id=app_main
        )
    )
    assert latest is not None and latest.workflow_id == "wf-013"
    by_id = _run(
        store.get_learning_status(
            server_id=SERVER_A, operation_id=uuid.UUID(int=0xC03)
        )
    )
    assert by_id is not None and by_id.status is OperationStatus.PARTIAL
    assert by_id.sub_effects == {"facts": SubEffectStatus.APPLIED}


# ---------------------------------------------------------------------------
# paging validation (fail-closed, AGENTS.md invariant)
# ---------------------------------------------------------------------------


def test_paging_bounds_fail_closed(
    migrated_url: str, map_store: ServiceMapStore
) -> None:
    """Paging validation fails closed BEFORE any session touch: an
    unbound store proves no query runs on rejected paths."""
    unbound = KnowledgeReadStore(_NullFactory(), map_store)
    app_main = uuid.UUID(int=0x1)
    for limit in (0, MAX_PAGE_LIMIT + 1, -5):
        with pytest.raises(ValueError):
            _run(
                unbound.resolve_service(
                    server_id=SERVER_A, application_id=app_main, limit=limit
                )
            )
    with pytest.raises(ValueError):
        _run(
            unbound.resolve_service(
                server_id=SERVER_A, application_id=app_main, offset=-1
            )
        )
    with pytest.raises(ValueError):
        _run(
            unbound.get_service_knowledge(
                server_id=SERVER_A, application_id=app_main, limit=0
            )
        )
    with pytest.raises(ValueError):
        _run(
            unbound.list_fact_history(
                server_id=SERVER_A, application_id=app_main, offset=-1
            )
        )
    with pytest.raises(ValueError):
        _run(
            unbound.list_learning_operations(
                server_id=SERVER_A, limit=MAX_PAGE_LIMIT + 1
            )
        )


class _NullFactory:
    """A stand-in session factory: paging validation fails closed BEFORE
    any session touch, so no query ever runs on these paths."""

    def __call__(self):  # pragma: no cover - never reached
        raise AssertionError("no session may be touched on validated paths")
