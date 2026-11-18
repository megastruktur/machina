"""Focused tests for the shared service-metadata mutation coordinator
(ask_16, campaign D4, C4/C5).

Real PostgreSQL leases (``ResourceLockService`` over the disposable
fixture, runtime table ``job_resource_locks``) + the real locked Service
Map filesystem (slice 15) + the real identity/preservation seam
(slice 14) + the real canonical adoption seam (manifest store +
projection):

- C4 lock order: a recipe-held ``application:<aid>`` lease is BORROWED
  (never reacquired, never released); the coordinator-owned
  ``service-metadata`` lease is taken through ``acquire_all`` (canonical
  sorted order, all-or-nothing) and released afterwards;
- contention refuses BEFORE any effect (no fence bypass, no deadlock);
- unadopt suppresses auto-adopt and cancels stale pending preservation
  BEFORE the map entry is deleted; explicit readopt bumps the
  generation;
- metadata updates go through the slice-15 ``expected_revision`` CAS and
  never wipe operator notes the caller did not target;
- canonical adoption is pure delegation (manifest + projection), never a
  map-only substitute, with no standalone-to-manual fallback.
"""

from __future__ import annotations

import asyncio
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

from mai.applications.adopt import (
    ABSENT_REVISION,
    AdoptionError,
    AdoptionErrorCode,
    AdoptionRequest,
    ApplicationAdoption,
    ApplicationManifestStore,
)
from mai.applications.discover import ApplicationOrigin
from mai.applications.knowledge_contracts import (
    LearnRequest,
    ProposedFactDelta,
    TrustedSourceContext,
)
from mai.applications.learning_operations import (
    LearningOperationStore,
    OperationAdmissionError,
    ServiceAutoAdoptSuppressedError,
)
from mai.applications.metadata_mutations import (
    APPLICATION_LEASE_PREFIX,
    METADATA_LEASE_RESOURCE,
    MetadataMutationCoordinator,
    MetadataMutationError,
    MetadataMutationErrorCode,
)
from mai.applications.service_map import (
    ServiceMapError,
    ServiceMapErrorCode,
    ServiceMapStore,
)
from mai.database import create_app_engine
from mai.jobs.resource_locks import LOCK_TABLE, ResourceLockService

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

SERVER_A = uuid.UUID("5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60")
APP_MAIN_ID = uuid.UUID("0aaaaaaa-0000-4000-8000-000000000001")

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
    return AlembicConfig(str(ALEMBIC_INI))


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database upgraded from zero to the current head."""
    url = os.environ.get(LIVE_URL_VAR, "")
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the coordinator claims need a real "
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
def factory(migrated_url: str) -> async_sessionmaker:
    engine, sessions = create_app_engine(migrated_url)
    # Per-test isolation: clean the knowledge tables AND the runtime-owned
    # lease table (job_resource_locks lives outside the Alembic chain and
    # is recreated by ResourceLockService.ensure_schema on first use).
    with psycopg.connect(migrated_url, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE TABLE service_learned_fact_revision, "
            "service_learned_fact, service_learning_operation, "
            "service_knowledge_identity CASCADE"
        )
        conn.execute(f"DELETE FROM {LOCK_TABLE}")
    yield sessions
    _run(engine.dispose())


@pytest.fixture
def locks(migrated_url: str) -> ResourceLockService:
    """The coordinator's lease service (one controller boot)."""
    service = ResourceLockService(migrated_url)
    service.ensure_schema()
    return service


@pytest.fixture
def recipe_locks(migrated_url: str) -> ResourceLockService:
    """A SECOND controller boot holding leases on the coordinator's
    behalf (the contending recipe/execution owner)."""
    service = ResourceLockService(migrated_url, boot_id=str(uuid.uuid4()))
    service.ensure_schema()
    return service


# ---------------------------------------------------------------------------
# fakes + builders
# ---------------------------------------------------------------------------


class _FailingEngine:
    """Engine seam fake that never acknowledges (rows stay queued)."""

    async def __call__(self, workflow_id: str) -> None:
        raise RuntimeError("simulated engine outage: no acknowledgment")


class _RecordingEngine:
    """Engine seam fake that acknowledges every enqueue once."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, workflow_id: str) -> None:
        self.calls.append(workflow_id)


def _coordinator(
    locks: ResourceLockService,
    factory: async_sessionmaker,
    map_store: ServiceMapStore,
    *,
    with_adoption: bool = False,
) -> MetadataMutationCoordinator:
    return MetadataMutationCoordinator(
        locks=locks,
        map_store=map_store,
        learning=LearningOperationStore(factory),
        adoption=ApplicationAdoption(factory) if with_adoption else None,
    )


def _seed_entry(map_root: Path) -> ServiceMapStore:
    """One mapped service at revision 1 carrying operator notes."""
    store = ServiceMapStore(map_root)
    store.upsert_entry(
        SERVER_A,
        name="box",
        category="system",
        description="old",
        application_id=APP_MAIN_ID,
        custom_instructions="operator notes",
    )
    return store


def _queued_learning_op(factory: async_sessionmaker) -> uuid.UUID:
    """Leave one durable queued preservation op for (SERVER_A, APP_MAIN_ID)
    via the failing engine; returns the deterministic operation id."""
    learning = LearningOperationStore(factory)
    run_id = uuid.uuid4()
    trusted = TrustedSourceContext(
        server_id=SERVER_A,
        run_id=run_id,
        session_id=None,
        tool_call_id="call-unadopt",
        actor="agent:mai",
        grants=["knowledge.learn"],
        context_at=datetime.now(tz=timezone.utc),
    )
    request = LearnRequest(
        facts=[
            ProposedFactDelta(
                category="runtime",
                key="k",
                statement="queued before unadopt",
                source_kind="tool_result",
                source_refs=["00000000-0000-4000-8000-000000000001"],
            )
        ],
        expected_generation=1,
    )

    async def go() -> None:
        with pytest.raises(OperationAdmissionError) as err:
            await learning.admit(
                request=request,
                trusted=trusted,
                application_id=APP_MAIN_ID,
                engine_enqueue=_FailingEngine(),
            )
        assert err.value.code == "enqueue_failed"

    _run(go())
    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"mai:learning-operation:{run_id}:call-unadopt"
    )


def _identity_row(migrated_url: str) -> dict[str, object] | None:
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute(
            "SELECT generation, auto_adopt_suppressed FROM "
            "service_knowledge_identity WHERE server_id = %s AND "
            "application_id = %s",
            (str(SERVER_A), str(APP_MAIN_ID)),
        ).fetchone()
    if row is None:
        return None
    return {"generation": row[0], "auto_adopt_suppressed": row[1]}


def _op_row(migrated_url: str, operation_id: uuid.UUID) -> dict[str, object]:
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute(
            "SELECT status, error_code FROM service_learning_operation "
            "WHERE operation_id = %s",
            (str(operation_id),),
        ).fetchone()
    assert row is not None, "expected the queued preservation row"
    return {"status": row[0], "error_code": row[1]}


# ---------------------------------------------------------------------------
# 1. metadata updates: CAS + operator-note preservation + lease release
# ---------------------------------------------------------------------------


def test_update_metadata_cas_preserves_operator_notes(
    tmp_path: Path, locks: ResourceLockService, factory: async_sessionmaker
) -> None:
    map_store = _seed_entry(tmp_path / "service_map")
    coordinator = _coordinator(locks, factory, map_store)

    outcome = _run(
        coordinator.update_entry_metadata(
            SERVER_A,
            APP_MAIN_ID,
            expected_revision=1,
            description="new",
            instructions={"backup": "restic backup /srv/box"},
        )
    )
    assert outcome.operation == "update_entry_metadata"
    assert outcome.map_entry_revision == 2
    assert outcome.previous_revision == 1

    entry = map_store.get_entry(SERVER_A, APP_MAIN_ID)
    assert entry is not None and entry.revision == 2
    assert entry.description == "new"
    assert entry.instructions.backup == "restic backup /srv/box"
    # Operator notes the caller did NOT target are preserved.
    assert entry.custom_instructions == "operator notes"
    assert entry.installed is True

    with pytest.raises(ServiceMapError) as stale:
        _run(
            coordinator.update_entry_metadata(
                SERVER_A, APP_MAIN_ID, expected_revision=1, description="v3"
            )
        )
    assert stale.value.code is ServiceMapErrorCode.REVISION_CONFLICT
    entry = map_store.get_entry(SERVER_A, APP_MAIN_ID)
    assert entry is not None and entry.revision == 2  # untouched refusal

    # Both coordinator-owned leases are released after the call.
    assert (
        locks.inspect(str(SERVER_A), f"{APPLICATION_LEASE_PREFIX}{APP_MAIN_ID}")
        is None
    )
    assert locks.inspect(str(SERVER_A), METADATA_LEASE_RESOURCE) is None


# ---------------------------------------------------------------------------
# 2. borrowed application lease: reused, never reacquired, never released
# ---------------------------------------------------------------------------


def test_borrowed_application_lease_reused_not_released(
    tmp_path: Path,
    locks: ResourceLockService,
    factory: async_sessionmaker,
    recipe_locks: ResourceLockService,
) -> None:
    map_store = _seed_entry(tmp_path / "service_map")
    coordinator = _coordinator(locks, factory, map_store)
    app_key = f"{APPLICATION_LEASE_PREFIX}{APP_MAIN_ID}"

    recipe = recipe_locks.acquire(str(SERVER_A), app_key, owner_ref="recipe-run")
    outcome = _run(
        coordinator.update_entry_metadata(
            SERVER_A,
            APP_MAIN_ID,
            expected_revision=1,
            description="coordinated",
            application_lease=recipe,
        )
    )
    assert outcome.map_entry_revision == 2  # no HELD refusal: not reacquired

    snapshot = locks.inspect(str(SERVER_A), app_key)
    assert snapshot is not None and snapshot.state == "held"  # NOT released
    assert locks.inspect(str(SERVER_A), METADATA_LEASE_RESOURCE) is None

    # The recipe boot is still the owner and can release its own lease.
    recipe_locks.release(recipe)
    assert locks.inspect(str(SERVER_A), app_key) is None

    # A lease for a DIFFERENT key is rejected as invalid, never used.
    wrong = recipe_locks.acquire(
        str(SERVER_A), f"{APPLICATION_LEASE_PREFIX}{uuid.uuid4()}", owner_ref="x"
    )
    with pytest.raises(MetadataMutationError) as err:
        _run(
            coordinator.update_entry_metadata(
                SERVER_A,
                APP_MAIN_ID,
                expected_revision=2,
                application_lease=wrong,
            )
        )
    assert err.value.code is MetadataMutationErrorCode.INVALID_REQUEST
    recipe_locks.release(wrong)


# ---------------------------------------------------------------------------
# 3. contention refuses before any effect (no fence bypass, no deadlock)
# ---------------------------------------------------------------------------


def test_metadata_lease_contention_refuses_without_effects(
    tmp_path: Path,
    migrated_url: str,
    locks: ResourceLockService,
    factory: async_sessionmaker,
    recipe_locks: ResourceLockService,
) -> None:
    map_store = _seed_entry(tmp_path / "service_map")
    coordinator = _coordinator(locks, factory, map_store)
    holder = recipe_locks.acquire(
        str(SERVER_A), METADATA_LEASE_RESOURCE, owner_ref="other-boot"
    )

    with pytest.raises(MetadataMutationError) as update_err:
        _run(
            coordinator.update_entry_metadata(
                SERVER_A, APP_MAIN_ID, expected_revision=1, description="v3"
            )
        )
    assert update_err.value.code is MetadataMutationErrorCode.LEASE_CONFLICT
    entry = map_store.get_entry(SERVER_A, APP_MAIN_ID)
    assert entry is not None and entry.revision == 1 and entry.description == "old"

    with pytest.raises(MetadataMutationError) as unadopt_err:
        _run(coordinator.unadopt(SERVER_A, APP_MAIN_ID))
    assert unadopt_err.value.code is MetadataMutationErrorCode.LEASE_CONFLICT
    # No fence bypass: the refused unadopt wrote NOTHING.
    assert _identity_row(migrated_url) is None
    entry = map_store.get_entry(SERVER_A, APP_MAIN_ID)
    assert entry is not None

    # Releasing the contender unblocks the SAME call (no deadlock, no steal).
    recipe_locks.release(holder)
    outcome = _run(coordinator.unadopt(SERVER_A, APP_MAIN_ID, expected_revision=1))
    assert outcome.map_entry_deleted is True
    assert outcome.auto_adopt_suppressed is True


# ---------------------------------------------------------------------------
# 4. unadopt: suppression BEFORE map deletion, stale pending cancelled
# ---------------------------------------------------------------------------


def test_unadopt_suppresses_before_map_delete_cancels_pending(
    tmp_path: Path,
    migrated_url: str,
    locks: ResourceLockService,
    factory: async_sessionmaker,
) -> None:
    map_store = _seed_entry(tmp_path / "service_map")
    coordinator = _coordinator(locks, factory, map_store)
    operation_id = _queued_learning_op(factory)

    outcome = _run(coordinator.unadopt(SERVER_A, APP_MAIN_ID, expected_revision=1))
    assert outcome.operation == "unadopt"
    assert outcome.map_entry_deleted is True
    assert outcome.auto_adopt_suppressed is True
    assert outcome.identity_generation == 1

    # The map entry is gone...
    assert map_store.get_entry(SERVER_A, APP_MAIN_ID) is None
    # ...the stale pending preservation was cancelled by the suppression...
    row = _op_row(migrated_url, operation_id)
    assert row["status"] == "failed"
    assert row["error_code"] == "generation_superseded"
    # ...and the tombstone OUTLIVES the deletion: no background admission.
    learning = LearningOperationStore(factory)
    trusted = TrustedSourceContext(
        server_id=SERVER_A,
        run_id=uuid.uuid4(),
        session_id=None,
        tool_call_id="call-after",
        actor="agent:mai",
        grants=["knowledge.learn"],
        context_at=datetime.now(tz=timezone.utc),
    )
    request = LearnRequest(
        facts=[
            ProposedFactDelta(
                category="runtime",
                key="k",
                statement="after unadopt",
                source_kind="tool_result",
                source_refs=["00000000-0000-4000-8000-000000000001"],
            )
        ],
        expected_generation=1,
    )
    with pytest.raises(ServiceAutoAdoptSuppressedError):
        _run(
            learning.admit(
                request=request,
                trusted=trusted,
                application_id=APP_MAIN_ID,
                engine_enqueue=_RecordingEngine(),
            )
        )
    assert _identity_row(migrated_url)["auto_adopt_suppressed"] is True  # type: ignore[index]


# ---------------------------------------------------------------------------
# 5. explicit readopt bumps the generation and reopens admission
# ---------------------------------------------------------------------------


def test_readopt_bumps_generation_and_reopens_admission(
    tmp_path: Path,
    migrated_url: str,
    locks: ResourceLockService,
    factory: async_sessionmaker,
) -> None:
    map_store = ServiceMapStore(tmp_path / "service_map")
    coordinator = _coordinator(locks, factory, map_store)

    suppressed = _run(coordinator.unadopt(SERVER_A, APP_MAIN_ID))
    assert suppressed.auto_adopt_suppressed is True
    assert suppressed.map_entry_deleted is False  # nothing mapped: no-op delete

    readopted = _run(coordinator.readopt(SERVER_A, APP_MAIN_ID))
    assert readopted.operation == "readopt"
    assert readopted.identity_generation == 2  # explicit readopt bumps
    assert readopted.auto_adopt_suppressed is False

    identity = _identity_row(migrated_url)
    assert identity is not None and identity["generation"] == 2

    # Admission works again at the NEW generation (background seam).
    learning = LearningOperationStore(factory)
    trusted = TrustedSourceContext(
        server_id=SERVER_A,
        run_id=uuid.uuid4(),
        session_id=None,
        tool_call_id="call-readopt",
        actor="agent:mai",
        grants=["knowledge.learn"],
        context_at=datetime.now(tz=timezone.utc),
    )
    request = LearnRequest(
        facts=[
            ProposedFactDelta(
                category="runtime",
                key="k",
                statement="after readopt",
                source_kind="tool_result",
                source_refs=["00000000-0000-4000-8000-000000000001"],
            )
        ],
        expected_generation=2,
    )
    engine = _RecordingEngine()
    admitted = _run(
        learning.admit(
            request=request,
            trusted=trusted,
            application_id=APP_MAIN_ID,
            engine_enqueue=engine,
        )
    )
    assert admitted.receipt.status.value == "accepted"
    assert engine.calls != []


# ---------------------------------------------------------------------------
# 6. canonical adoption delegation (never a map-only substitute)
# ---------------------------------------------------------------------------


def test_adopt_delegation_is_canonical_with_no_fallback(
    tmp_path: Path, locks: ResourceLockService, factory: async_sessionmaker
) -> None:
    map_store = ServiceMapStore(tmp_path / "service_map")
    coordinator = _coordinator(locks, factory, map_store, with_adoption=True)
    manifest_store = ApplicationManifestStore(tmp_path / "manifests")
    app_key = f"{APPLICATION_LEASE_PREFIX}{APP_MAIN_ID}"

    request = AdoptionRequest(
        server_id=SERVER_A,
        application_id=APP_MAIN_ID,
        name="box",
        origin=ApplicationOrigin.COMPOSE,
        compose_project="box",
        working_dir="/srv/box",
        expected_revision=ABSENT_REVISION,
    )
    result = _run(coordinator.adopt(manifest_store, request))
    assert result.op == "upsert"
    assert result.outcome == "applied"
    assert result.revision == 1
    # Canonical adoption NEVER writes the Service Map itself.
    assert map_store.get_entry(SERVER_A, APP_MAIN_ID) is None
    # Both coordinator-owned leases are released.
    assert locks.inspect(str(SERVER_A), app_key) is None
    assert locks.inspect(str(SERVER_A), METADATA_LEASE_RESOURCE) is None

    # No standalone-to-manual fallback: the canonical request vocabulary
    # STRUCTURALLY refuses standalone containers at request construction
    # (before any lease is taken or any effect possible) — the
    # coordinator can never paper one over into a manual entry.
    with pytest.raises(AdoptionError) as unsupported:
        AdoptionRequest(
            server_id=SERVER_A,
            application_id=uuid.uuid4(),
            name="standalone",
            origin=ApplicationOrigin.DOCKER_CONTAINER,
            expected_revision=ABSENT_REVISION,
        )
    assert unsupported.value.code is AdoptionErrorCode.UNSUPPORTED_ORIGIN
    assert locks.inspect(str(SERVER_A), METADATA_LEASE_RESOURCE) is None
    assert locks.inspect(str(SERVER_A), f"{APPLICATION_LEASE_PREFIX}{APP_MAIN_ID}") is None
