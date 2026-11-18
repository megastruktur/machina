"""ask_22: DBOS ``service.learn`` background-handler tests (D4/D5).

Real disposable fixture PostgreSQL (``MAI_TEST_DATABASE_URL``) plus the
REAL DBOS worker (``mai.jobs.engine.launch_worker`` /
``EngineClient.enqueue_run`` / ``await_run``): admissions flow through
``LearningOperationStore.admit`` with the real engine seam, the worker
executes the registered ``service.learn`` handler, and every durable
claim is read back from ``service_learning_operation`` /
``service_learned_fact`` / ``application_projection`` on a separate
connection. Crash boundaries (b)-(d) are exercised through direct
in-process handler invocations around deliberately injected failures so
the reconciliation is deterministic.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import threading
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.session_store import acreate_session
from mai.applications.adopt import (
    ApplicationAdoption,
    ApplicationManifestStore,
)
from mai.applications.knowledge_contracts import (
    LearnRequest,
    ProposedFactDelta,
    TrustedSourceContext,
)
from mai.applications.knowledge_write import KnowledgeWriteStore
from mai.applications.learning_operations import (
    LearningOperationStore,
    OperationAdmissionError,
)
from mai.applications.metadata_mutations import MetadataMutationCoordinator
from mai.applications.service_map import ServiceMapStore
from mai.contracts.sessions import SessionSourceKind
from mai.database import create_app_engine
from mai.jobs import engine
from mai.jobs.engine import EngineClient
from mai.jobs.resource_locks import ResourceLockService
from mai.jobs.service_learning import (
    ServiceLearningHandler,
    ServiceLearningPayload,
    AdoptionProposal,
    build_engine_enqueue,
    register_service_learning_handler,
)

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

SERVER_A = uuid.UUID("5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60")
APP_MAIN_ID = uuid.UUID("0aaaaaaa-0000-4000-8000-000000000001")

_TEST_LOOP: asyncio.AbstractEventLoop | None = None
_TEST_LOOP_LOCK = threading.Lock()


def _run(coro: Any) -> Any:
    """Run one coroutine on the persistent test loop (W3 pattern)."""
    global _TEST_LOOP
    with _TEST_LOOP_LOCK:
        if _TEST_LOOP is None or _TEST_LOOP.is_closed():
            _TEST_LOOP = asyncio.new_event_loop()
            threading.Thread(
                target=_TEST_LOOP.run_forever, daemon=True
            ).start()
    return asyncio.run_coroutine_threadsafe(coro, _TEST_LOOP).result()


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


def _drop_schemas(url: str) -> None:
    """Whole-database reset: the dbos schema is engine-owned and is
    (re)created by the worker launch."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")


@pytest.fixture(scope="session")
def worker_url() -> Iterator[str]:
    """Migrate the disposable DB, launch the REAL DBOS worker, and
    restore pre-bootstrap state afterwards."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the durable service-learning "
            "handler claims need a real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        _drop_schemas(url)
        command.upgrade(_alembic_config(), "head")
    finally:
        if previous is None:
            os.environ.pop("MAI_DATABASE_URL", None)
        else:
            os.environ["MAI_DATABASE_URL"] = previous
    engine.launch_worker(url)  # engine-owned dbos schema + queue claim
    try:
        yield url
    finally:
        engine.shutdown()
        _drop_schemas(url)


@pytest.fixture
def factory(worker_url: str) -> Iterator[async_sessionmaker[AsyncSession]]:
    app_engine, sessions = create_app_engine(worker_url)
    try:
        yield sessions
    finally:
        _run(app_engine.dispose())


@pytest.fixture(autouse=True)
def _clean_tables(worker_url: str) -> Iterator[None]:
    """Per-test SQL isolation across the knowledge, adoption, session
    and lease tables (the DBOS system tables stay engine-owned)."""
    with psycopg.connect(worker_url, autocommit=True) as conn:
        # job_resource_locks is lazily created by the first
        # ResourceLockService boot; truncate it only once it exists.
        locks_table = conn.execute(
            "SELECT to_regclass('job_resource_locks')"
        ).fetchone()
        targets = [
            "service_learned_fact_revision",
            "service_learned_fact",
            "service_learning_operation",
            "service_knowledge_identity",
            "application_projection",
            "application_projection_journal",
            "session_runs",
            "sessions",
        ]
        if locks_table is not None and locks_table[0] is not None:
            targets.append("job_resource_locks")
        conn.execute(
            "TRUNCATE TABLE " + ", ".join(targets) + " CASCADE"
        )
    yield


# ---------------------------------------------------------------------------
# wiring + admission helpers
# ---------------------------------------------------------------------------

_FACT = {
    "category": "deployment",
    "key": "health-path",
    "statement": "health lives at /healthz",
    "source_kind": "tool_result",
    "source_refs": ["00000000-0000-4000-8000-000000000001"],
}

_ADOPTION = AdoptionProposal(
    name="web",
    origin="compose",
    compose_project="web",
    working_dir="/srv/web",
    description="web stack",
)


class _DeferredEngine:
    """Enqueue seam fake that records WITHOUT acknowledging (it raises,
    like an unacknowledged engine): the admission row stays honestly
    ``queued`` until a later real re-enqueue."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, workflow_id: str) -> None:
        self.calls.append(workflow_id)
        raise RuntimeError("simulated outage: enqueue never acknowledged")


class _CrashingWriteBefore(KnowledgeWriteStore):
    """Simulates a worker crash BEFORE the fact write commits."""

    async def apply_facts(self, **kwargs: Any) -> Any:
        raise RuntimeError("simulated crash before the fact write")


def _seed_foreground_run(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
) -> uuid.UUID:
    """Seed one real foreground session run + a successful tool-result
    entry (the provenance a learn request must reference). Returns the
    provenance entry identity."""
    session_row = _run(
        acreate_session(
            factory, server_id=SERVER_A, source=SessionSourceKind.INTERACTIVE
        )
    )
    ref = uuid.uuid4()
    with psycopg.connect(worker_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO session_runs (run_id, session_id, server_id, "
            "state, model_profile, client_request_id) VALUES "
            "(%s, %s, %s, 'running', 'test-profile', %s)",
            (
                str(run_id),
                str(session_row.session_id),
                str(SERVER_A),
                str(uuid.uuid4()),
            ),
        )
        conn.execute(
            "INSERT INTO session_entries (entry_id, session_id, run_id, "
            "seq, kind, payload, durable_source_id) VALUES "
            "(%s, %s, %s, 1, 'tool_result', %s::jsonb, %s)",
            (
                str(ref),
                str(session_row.session_id),
                str(run_id),
                json.dumps({"status": "executed", "output": "ok"}),
                str(uuid.uuid4()),
            ),
        )
    return ref


class _CrashingWriteAfter(KnowledgeWriteStore):
    """Simulates a worker crash AFTER the fact write committed but
    before the executor checkpointed it."""

    def __init__(self, inner: KnowledgeWriteStore) -> None:
        self._inner = inner

    async def apply_facts(self, **kwargs: Any) -> Any:
        await self._inner.apply_facts(**kwargs)
        raise RuntimeError("simulated crash after the fact commit")


def _wire(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
    *,
    knowledge_write: KnowledgeWriteStore | None = None,
    docs_indexer: Any = None,
    run_stopped_checker: Any = None,
    current_grants_resolver: Any = None,
    operations: LearningOperationStore | None = None,
) -> tuple[
    ServiceLearningHandler,
    LearningOperationStore,
    ServiceMapStore,
    KnowledgeWriteStore,
]:
    # A DBOS recovery re-executes through the SAME registered handler
    # instance, so the admission context registry (memory) survives;
    # replay wiring reuses the store instance to mirror that.
    operations = operations or LearningOperationStore(factory)
    map_store = ServiceMapStore(tmp_path / "service_map")
    locks = ResourceLockService(worker_url)
    coordinator = MetadataMutationCoordinator(
        locks=locks,
        map_store=map_store,
        learning=operations,
        adoption=ApplicationAdoption(factory),
        sessions=factory,
    )
    write = knowledge_write or KnowledgeWriteStore(factory)
    manifest_store = ApplicationManifestStore(tmp_path / "manifests")
    handler = ServiceLearningHandler(
        operations=operations,
        coordinator=coordinator,
        knowledge_write=write,
        map_store=map_store,
        sessions=factory,
        manifest_store=manifest_store,
        docs_indexer=docs_indexer,
        run_stopped_checker=run_stopped_checker,
        current_grants_resolver=current_grants_resolver,
    )
    return handler, operations, map_store, write, coordinator


class _AdmittedOutcome:
    """Uniform admission handle: the deferred-engine path raises
    ``enqueue_failed`` inside ``admit`` (the row stays honestly queued)
    — the operation identity is reconstructed from the store's frozen
    derivation."""

    def __init__(self, operation_id: uuid.UUID) -> None:
        self.operation_id = operation_id
        self.workflow_id = f"service.learn:{operation_id}"
        self.replayed = False
        self.coalesced = False
        self.receipt = SimpleNamespace(
            operation_id=operation_id, workflow_id=self.workflow_id
        )


def _admit(
    operations: LearningOperationStore,
    engine_enqueue: Any,
    *,
    run_id: uuid.UUID,
    ref: uuid.UUID | None = None,
    facts: list[dict[str, object]] | None = None,
    application_id: uuid.UUID | None = APP_MAIN_ID,
    grants: list[str] | None = None,
    expected_generation: int = 1,
) -> _AdmittedOutcome:
    fact = dict(_FACT)
    if facts is not None:
        fact = dict(facts[0])
    fact["source_refs"] = [str(ref or uuid.uuid4())]
    tool_call_id = str(uuid.uuid4())
    trusted = TrustedSourceContext(
        server_id=SERVER_A,
        run_id=run_id,
        session_id=None,
        tool_call_id=tool_call_id,
        actor="principal",
        grants=grants if grants is not None else ["app.learn_service"],
        context_at=datetime.now(tz=timezone.utc),
    )
    request = LearnRequest(
        facts=[
            ProposedFactDelta(
                category=str(fact["category"]),
                key=str(fact["key"]),
                statement=str(fact["statement"]),
                source_kind="tool_result",
                source_refs=[str(r) for r in fact["source_refs"]],
            )
        ],
        expected_generation=expected_generation,
    )
    operation_id = uuid.uuid5(
        uuid.NAMESPACE_URL, f"mai:learning-operation:{run_id}:{tool_call_id}"
    )
    try:
        _run(
            operations.admit(
                request=request,
                trusted=trusted,
                application_id=application_id,
                engine_enqueue=engine_enqueue,
            )
        )
    except OperationAdmissionError as error:
        # Deferred/unacknowledged engine: the row stays queued.
        assert error.code == "enqueue_failed", error.code
    return _AdmittedOutcome(operation_id)


def _row(url: str, operation_id: uuid.UUID) -> dict[str, Any]:
    with psycopg.connect(url) as conn:
        row = conn.execute(
            "SELECT status, sub_effects, error_code FROM "
            "service_learning_operation WHERE operation_id = %s",
            (str(operation_id),),
        ).fetchone()
    assert row is not None
    return {
        "status": row[0],
        "sub_effects": row[1] or {},
        "error_code": row[2],
    }


def _fact_count(url: str, statement: str | None = None) -> int:
    with psycopg.connect(url) as conn:
        if statement is None:
            row = conn.execute(
                "SELECT COUNT(*) FROM service_learned_fact WHERE "
                "status = 'active'"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM service_learned_fact WHERE "
                "status = 'active' AND statement = %s",
                (statement,),
            ).fetchone()
    return int(row[0])


def _fact_revision(url: str) -> int:
    with psycopg.connect(url) as conn:
        revisions = conn.execute(
            "SELECT revision FROM service_learned_fact WHERE "
            "status = 'active'"
        ).fetchall()
    assert len(revisions) == 1
    return int(revisions[0][0])


def _revision_history_count(url: str) -> int:
    with psycopg.connect(url) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM service_learned_fact_revision"
        ).fetchone()
    return int(row[0])


def _projection_revision(url: str) -> int:
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT revision FROM application_projection WHERE "
            "application_id = %s",
            (str(APP_MAIN_ID),),
        ).fetchall()
    assert len(rows) == 1
    return int(rows[0][0])


# ---------------------------------------------------------------------------
# 1. foreground completion + real DBOS worker end-to-end
# ---------------------------------------------------------------------------


def test_foreground_completion_end_to_end(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    run_id = uuid.uuid4()

    def _checker(server_id: uuid.UUID, source_run_id: uuid.UUID) -> bool:
        # A production checker reads the durable session-run state:
        # only an explicit operator stop cancels; ordinary foreground
        # completion (succeeded/running) never does.
        with psycopg.connect(worker_url) as conn:
            row = conn.execute(
                "SELECT state FROM session_runs WHERE run_id = %s",
                (str(source_run_id),),
            ).fetchone()
        return bool(row and row[0] in ("cancelled", "outcome_unknown"))

    handler, operations, _map_store, _write, _coordinator = _wire(
        worker_url, factory, tmp_path, run_stopped_checker=_checker
    )
    register_service_learning_handler(handler)

    ref = _seed_foreground_run(worker_url, factory, run_id)

    with EngineClient(worker_url) as client:
        outcome = _admit(
            operations, build_engine_enqueue(client), run_id=run_id, ref=ref
        )
        operation_id = outcome.receipt.operation_id
        assert outcome.replayed is False
        # The foreground run COMPLETES normally mid-flight — the source
        # run row settles to 'succeeded' while the workflow is live.
        with psycopg.connect(worker_url, autocommit=True) as conn:
            conn.execute(
                "UPDATE session_runs SET state = 'succeeded', "
                "completed_at = now() WHERE run_id = %s",
                (str(run_id),),
            )

        receipt = client.await_run(str(operation_id))
        assert isinstance(receipt, dict)
        assert receipt["status"] == "applied", (
            receipt.get("error_code"), receipt,
        )
        assert receipt["sub_effects"]["facts"] == "applied"
        assert receipt["error_code"] is None

    durable = _row(worker_url, operation_id)
    assert durable["status"] == "applied"
    assert durable["sub_effects"]["facts"] == "applied"
    assert _fact_count(worker_url, str(_FACT["statement"])) == 1


# ---------------------------------------------------------------------------
# 2. adoption through the coordinator: reuse vs selector conflict
# ---------------------------------------------------------------------------


def test_adoption_replay_reuses_and_mismatch_conflicts(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    handler, operations, map_store, _write, _coordinator = _wire(
        worker_url, factory, tmp_path
    )
    register_service_learning_handler(handler)

    def _resolver(operation_id: uuid.UUID) -> ServiceLearningPayload:
        return ServiceLearningPayload(
            operation_id=operation_id,
            server_id=SERVER_A,
            application_id=APP_MAIN_ID,
            adoption=_ADOPTION,
        )

    with EngineClient(worker_url) as client:
        enqueue = build_engine_enqueue(client, payload_resolver=_resolver)
        # Run 1: canonical adoption + facts, all through the coordinator
        # (C4 lease set) — projection, map entry and facts all land.
        ref1 = _seed_foreground_run(worker_url, factory, uuid.uuid4())
        first = _admit(
            operations,
            enqueue,
            run_id=uuid.uuid4(),
            ref=ref1,
            grants=["app.learn_service", "app.adopt"],
        )
        receipt = client.await_run(str(first.receipt.operation_id))
        assert receipt["status"] == "applied", (
            receipt.get("error_code"), receipt,
        )
        assert receipt["sub_effects"]["adoption"] == "applied"
        assert receipt["sub_effects"]["facts"] == "applied"
        assert _projection_revision(worker_url) == 1
        entry = map_store.get_entry(SERVER_A, APP_MAIN_ID)
        assert entry is not None
        assert entry.origin == "compose"
        assert entry.compose_project == "web"
        assert entry.revision == 1
        assert _fact_count(worker_url, str(_FACT["statement"])) == 1

        # Run 2: matching selector replay — reconciled, no map revision
        # bump, facts idempotent.
        ref2 = _seed_foreground_run(worker_url, factory, uuid.uuid4())
        second = _admit(
            operations,
            enqueue,
            run_id=uuid.uuid4(),
            ref=ref2,
            grants=["app.learn_service", "app.adopt"],
        )
        receipt = client.await_run(str(second.receipt.operation_id))
        assert receipt["status"] == "applied", (
            receipt.get("error_code"), receipt,
        )
        assert receipt["sub_effects"]["adoption"] == "applied"
        assert receipt["sub_effects"]["facts"] == "applied"
        assert map_store.get_entry(SERVER_A, APP_MAIN_ID).revision == 1
        assert _projection_revision(worker_url) == 1
        assert _fact_count(worker_url, str(_FACT["statement"])) == 1

        # Run 3: MISMATCHED selector — explicit conflict; nothing is
        # overwritten and the not-attempted facts are refused.
        mismatched = AdoptionProposal(
            name="other",
            origin="compose",
            compose_project="other",
            working_dir="/srv/other",
            description="different stack",
        )

        def _mismatch_resolver(operation_id: uuid.UUID) -> ServiceLearningPayload:
            return ServiceLearningPayload(
                operation_id=operation_id,
                server_id=SERVER_A,
                application_id=APP_MAIN_ID,
                adoption=mismatched,
            )

        third = _admit(
            operations,
            build_engine_enqueue(client, payload_resolver=_mismatch_resolver),
            run_id=uuid.uuid4(),
            ref=uuid.uuid4(),  # never validated: the facts are skipped
            facts=[
                {**_FACT, "statement": "conflicting claim that must be skipped"}
            ],
            grants=["app.learn_service", "app.adopt"],
        )
        receipt = client.await_run(str(third.receipt.operation_id))
        assert receipt["status"] == "failed", (
            receipt.get("error_code"), receipt,
        )
        assert receipt["error_code"] == "selector_conflict"
        assert receipt["sub_effects"]["adoption"] == "failed"
        assert receipt["sub_effects"]["facts"] == "failed"
        # The stored adoption and facts are untouched.
        assert map_store.get_entry(SERVER_A, APP_MAIN_ID).revision == 1
        assert _projection_revision(worker_url) == 1
        assert (
            _fact_count(worker_url, "conflicting claim that must be skipped")
            == 0
        )


# ---------------------------------------------------------------------------
# 3. execution-time authority, tombstone suppression, operator stop
# ---------------------------------------------------------------------------


def test_authority_revocation_fails_before_effects(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    handler, operations, _map_store, _write, _coordinator = _wire(
        worker_url,
        factory,
        tmp_path,
        current_grants_resolver=lambda server_id, actor: set(),
    )
    register_service_learning_handler(handler)

    deferred = _DeferredEngine()
    outcome = _admit(operations, deferred, run_id=uuid.uuid4())
    operation_id = outcome.receipt.operation_id
    assert _row(worker_url, operation_id)["status"] == "queued"

    with EngineClient(worker_url) as client:
        _run(
            operations.recover_pending_batch(
                SERVER_A, engine_enqueue=build_engine_enqueue(client)
            )
        )
        receipt = client.await_run(str(operation_id))

    assert receipt["status"] == "failed", (
            receipt.get("error_code"), receipt,
        )
    assert receipt["error_code"] == "authority_revoked"
    assert _fact_count(worker_url) == 0


def test_tombstone_suppresses_pending_effects(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    handler, operations, _map_store, _write, _coordinator = _wire(
        worker_url, factory, tmp_path
    )
    register_service_learning_handler(handler)

    deferred = _DeferredEngine()
    outcome = _admit(operations, deferred, run_id=uuid.uuid4())
    operation_id = outcome.receipt.operation_id

    # D4: the un-adopt tombstone fences BEFORE any effect and cancels
    # the pending preservation operation as generation_superseded.
    _run(operations.suppress_for_unadopt(SERVER_A, APP_MAIN_ID))
    durable = _row(worker_url, operation_id)
    assert durable["status"] == "failed"
    assert durable["error_code"] == "generation_superseded"

    with EngineClient(worker_url) as client:
        # The durable row is already terminal (cancelled by the
        # tombstone); enqueue the deterministic workflow and let the
        # handler reconcile it without touching any store.
        _run(build_engine_enqueue(client)(outcome.workflow_id))
        receipt = client.await_run(str(operation_id))
    assert receipt["status"] == "failed", (
        receipt.get("error_code"), receipt,
    )
    assert receipt["error_code"] == "generation_superseded"
    assert receipt["sub_effects"] == {}
    assert _fact_count(worker_url) == 0


def test_operator_stop_cancels_pending_operation(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    handler, operations, _map_store, _write, _coordinator = _wire(
        worker_url, factory, tmp_path, run_stopped_checker=lambda s, r: True
    )
    register_service_learning_handler(handler)

    deferred = _DeferredEngine()
    outcome = _admit(operations, deferred, run_id=uuid.uuid4())
    operation_id = outcome.receipt.operation_id

    with EngineClient(worker_url) as client:
        _run(
            operations.recover_pending_batch(
                SERVER_A, engine_enqueue=build_engine_enqueue(client)
            )
        )
        receipt = client.await_run(str(operation_id))

    assert receipt["status"] == "failed", (
            receipt.get("error_code"), receipt,
        )
    assert receipt["error_code"] == "source_cancelled"
    assert _fact_count(worker_url) == 0


# ---------------------------------------------------------------------------
# 4. crash/restart recovery across the four boundaries
# ---------------------------------------------------------------------------


def test_recovery_reenqueues_queued_operation(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    handler, operations, _map_store, _write, _coordinator = _wire(
        worker_url, factory, tmp_path
    )
    register_service_learning_handler(handler)

    # Boundary (a): admitted but never acknowledged (process died
    # before the enqueue ack) — the row stays honestly queued.
    run_id = uuid.uuid4()
    ref = _seed_foreground_run(worker_url, factory, run_id)
    deferred = _DeferredEngine()
    outcome = _admit(operations, deferred, run_id=run_id, ref=ref)
    operation_id = outcome.receipt.operation_id
    assert _row(worker_url, operation_id)["status"] == "queued"
    assert deferred.calls == [outcome.receipt.workflow_id]

    with EngineClient(worker_url) as client:
        result = _run(
            operations.recover_pending_batch(
                SERVER_A, engine_enqueue=build_engine_enqueue(client)
            )
        )
        assert result.total >= 1
        receipt = client.await_run(str(operation_id))

    assert receipt["status"] == "applied", (
            receipt.get("error_code"), receipt,
        )
    assert receipt["sub_effects"]["facts"] == "applied"
    assert _fact_count(worker_url, str(_FACT["statement"])) == 1


def test_post_adopt_crash_replay_reconciles(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    # Boundary (b): crash right after the adoption checkpoint. The
    # first run commits the adoption/map effect through the coordinator
    # and dies before the fact write.
    handler, operations, map_store, _write, _coordinator = _wire(
        worker_url,
        factory,
        tmp_path,
        knowledge_write=_CrashingWriteBefore(factory),
    )
    run_id = uuid.uuid4()
    ref = _seed_foreground_run(worker_url, factory, run_id)
    deferred = _DeferredEngine()
    outcome = _admit(
        operations,
        deferred,
        run_id=run_id,
        ref=ref,
        grants=["app.learn_service", "app.adopt"],
    )
    operation_id = outcome.receipt.operation_id
    payload_json = ServiceLearningPayload(
        operation_id=operation_id,
        server_id=SERVER_A,
        application_id=APP_MAIN_ID,
        adoption=_ADOPTION,
    ).model_dump_json()

    with pytest.raises(RuntimeError):
        _run(handler(payload_json))

    durable = _row(worker_url, operation_id)
    assert durable["status"] == "applying"
    assert durable["sub_effects"]["adoption"] == "applied"
    assert durable["sub_effects"]["facts"] == "pending"
    entry = map_store.get_entry(SERVER_A, APP_MAIN_ID)
    assert entry is not None and entry.revision == 1

    # Replay (the recovered worker re-executes the SAME workflow):
    # the adoption reconciles from the checkpoint WITHOUT re-running
    # it, the facts apply exactly once, and the operation finalizes.
    healthy, _operations, _map_store, _write, _coordinator = _wire(
        worker_url, factory, tmp_path, operations=operations
    )
    receipt = _run(healthy(payload_json))
    assert receipt["status"] == "applied", (
            receipt.get("error_code"), receipt,
        )
    assert receipt["sub_effects"]["adoption"] == "applied"
    assert receipt["sub_effects"]["facts"] == "applied"
    assert map_store.get_entry(SERVER_A, APP_MAIN_ID).revision == 1  # no re-run
    assert _fact_count(worker_url, str(_FACT["statement"])) == 1
    assert _fact_revision(worker_url) == 1


def test_docs_failure_after_facts_is_partial(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    # Boundary (c): the docs/index sync fails AFTER the facts committed
    # — the receipt records partial with exact per-effect statuses and
    # the committed facts are never lost.
    async def _failing_docs(
        server_id: uuid.UUID,
        application_id: uuid.UUID | None,
        applied_fact_ids: tuple[uuid.UUID, ...],
    ) -> None:
        raise RuntimeError("docs index unavailable")

    handler, operations, _map_store, _write, _coordinator = _wire(
        worker_url, factory, tmp_path, docs_indexer=_failing_docs
    )
    run_id = uuid.uuid4()
    ref = _seed_foreground_run(worker_url, factory, run_id)
    deferred = _DeferredEngine()
    outcome = _admit(operations, deferred, run_id=run_id, ref=ref)
    operation_id = outcome.receipt.operation_id
    payload_json = ServiceLearningPayload(
        operation_id=operation_id,
        server_id=SERVER_A,
        application_id=APP_MAIN_ID,
    ).model_dump_json()

    receipt = _run(handler(payload_json))
    assert receipt["status"] == "partial", (
            receipt.get("error_code"), receipt,
        )
    assert receipt["error_code"] == "docs_sync_failed"
    assert receipt["sub_effects"]["facts"] == "applied"
    assert receipt["sub_effects"]["docs"] == "failed"
    assert _fact_count(worker_url, str(_FACT["statement"])) == 1


def test_post_fact_crash_replay_is_idempotent(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    # Boundary (d): the fact write COMMITTED but the executor crashed
    # before checkpointing/finalizing. The replay reconciles through
    # the idempotent write seam WITHOUT bumping the fact revision.
    handler, operations, _map_store, write, _coordinator = _wire(
        worker_url, factory, tmp_path
    )
    crashy = ServiceLearningHandler(
        operations=operations,
        coordinator=_coordinator,
        knowledge_write=_CrashingWriteAfter(write),
        map_store=_map_store,
        sessions=factory,
    )
    run_id = uuid.uuid4()
    ref = _seed_foreground_run(worker_url, factory, run_id)
    deferred = _DeferredEngine()
    outcome = _admit(operations, deferred, run_id=run_id, ref=ref)
    operation_id = outcome.receipt.operation_id
    payload_json = ServiceLearningPayload(
        operation_id=operation_id,
        server_id=SERVER_A,
        application_id=APP_MAIN_ID,
    ).model_dump_json()

    with pytest.raises(RuntimeError):
        _run(crashy(payload_json))

    durable = _row(worker_url, operation_id)
    assert durable["status"] == "applying"
    assert durable["sub_effects"] == {}  # crashed before any checkpoint
    assert _fact_count(worker_url, str(_FACT["statement"])) == 1
    assert _fact_revision(worker_url) == 1

    healthy, _operations, _map_store, _write, _coordinator = _wire(
        worker_url, factory, tmp_path, operations=operations
    )
    receipt = _run(healthy(payload_json))
    assert receipt["status"] == "applied", (
            receipt.get("error_code"), receipt,
        )
    assert receipt["sub_effects"]["facts"] == "applied"
    assert _fact_count(worker_url, str(_FACT["statement"])) == 1
    assert _fact_revision(worker_url) == 1  # no duplicate revision
    assert _revision_history_count(worker_url) == 1
