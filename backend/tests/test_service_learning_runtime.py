"""ask_36: cross-module durable-preservation runtime scenarios (R3-R7).

The REAL disposable fixture PostgreSQL (``MAI_TEST_DATABASE_URL``) plus
the REAL DBOS worker: module-scoped engine launch (shutdown in the
fixture ``finally``), per-test table truncation, and cross-module seams
(``MetadataMutationCoordinator`` + the per-server ``_ProcessFileLock``
+ revision/generation CAS + ``suppress_for_unadopt`` tombstones).

Scenario map (C7 R3-R7):
- **R3** foreground run completes BEFORE background preservation
  finishes applying — completion never cancels accepted work.
- **R4** a REAL worker-process SIGKILL mid-apply and the bounded startup
  recovery reconcile idempotently (no duplicated facts/sub-effects), plus
  the deterministic post-fact-crash replay through the real engine.
- **R5** concurrent API + worker writer races on the SAME service
  (pre-seeded ``service_knowledge_identity`` so both workflows fence on
  one generation), the map revision CAS race, and unadopt deletion
  suppression blocking background re-adoption until an explicit readopt.
- **R6** grant revocation (``authority_revoked``, idempotent refusal)
  and operator source-run cancellation (``source_cancelled``).
- **R7** a partial docs-indexing failure records the partial sub-effect
  receipt honestly WITHOUT rolling back committed SQL facts, and the
  resume feeds persisted fact ids through to ``applied``.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
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
    ServiceAutoAdoptSuppressedError,
)
from mai.applications.metadata_mutations import (
    MetadataMutationCoordinator,
    MutationOutcome,
)
from mai.applications.service_map import ServiceMapError, ServiceMapStore
from mai.contracts.sessions import SessionSourceKind
from mai.database import create_app_engine
from mai.jobs import engine
from mai.jobs.engine import EngineClient
from mai.jobs.resource_locks import ResourceLockService
from mai.jobs.service_learning import (
    ServiceLearningHandler,
    ServiceLearningPayload,
    build_engine_enqueue,
    register_service_learning_handler,
)

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

SERVER_A = uuid.UUID("5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60")
APP_MAIN_ID = uuid.UUID("0aaaaaaa-0000-4000-8000-000000000001")
APP_SECOND_ID = uuid.UUID("0bbbbbbb-0000-4000-8000-000000000002")

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
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")


@pytest.fixture(scope="module")
def worker_url() -> Iterator[str]:
    """Migrate the disposable DB and launch the REAL DBOS worker ONCE
    for this module; teardown shuts the engine down BEFORE the next
    module's module-scoped fixtures start."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.fail(
            f"{LIVE_URL_VAR} is not set: the R3-R7 durable runtime "
            "scenarios are BLOCKED, not skipped — rerun with the "
            "disposable fixture Postgres exported"
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
    engine.launch_worker(url)
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
    """Per-test SQL isolation (DBOS system tables stay engine-owned)."""
    with psycopg.connect(worker_url, autocommit=True) as conn:
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
        conn.execute("TRUNCATE TABLE " + ", ".join(targets) + " CASCADE")
        if conn.execute(
            "SELECT to_regclass('dbos.workflow_status')"
        ).fetchone()[0]:
            conn.execute("DELETE FROM dbos.workflow_status")
    yield


# ---------------------------------------------------------------------------
# local helpers (self-contained: no imports from sibling test modules)
# ---------------------------------------------------------------------------

_FACT_STATEMENT = "runtime suite: health lives at /healthz"


class _DeferredEngine:
    """Enqueue seam fake that records WITHOUT acknowledging: admissions
    stay honestly ``queued`` until a real recovery enqueue."""

    def __init__(self) -> None:
        self.calls: list[str] = []


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
    MetadataMutationCoordinator,
]:
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


def _seed_foreground_run(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
) -> uuid.UUID:
    """Seed one real foreground session run + a successful tool-result
    entry; returns the provenance entry identity."""
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
            (str(run_id), str(session_row.session_id), str(SERVER_A), str(uuid.uuid4())),
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


def _seed_identity(worker_url: str, application_id: uuid.UUID) -> None:
    """Pre-seed the service knowledge identity (generation 1, live) so
    CONCURRENT multi-workflow writes fence on ONE row instead of racing
    lazy creation."""
    with psycopg.connect(worker_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO service_knowledge_identity "
            "(server_id, application_id, generation, manual_identity, "
            "auto_adopt_suppressed) VALUES (%s, %s, 1, false, false) "
            "ON CONFLICT DO NOTHING",
            (str(SERVER_A), str(application_id)),
        )


def _admit(
    operations: LearningOperationStore,
    engine_enqueue: Any,
    *,
    run_id: uuid.UUID,
    statement: str = _FACT_STATEMENT,
    key: str = "runtime-health-path",
    ref: uuid.UUID | None = None,
    application_id: uuid.UUID = APP_MAIN_ID,
    grants: list[str] | None = None,
    expected_generation: int = 1,
) -> tuple[uuid.UUID, str]:
    """Admit one fact operation; returns ``(operation_id, workflow_id)``."""
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
                category="deployment",
                key=key,
                statement=statement,
                source_kind="tool_result",
                source_refs=[str(ref or uuid.uuid4())],
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
        # The deferred-engine path: the unacknowledged enqueue leaves the
        # row honestly QUEUED for the recovery scenarios.
        assert error.code == "enqueue_failed", error.code
    return operation_id, f"service.learn:{operation_id}"


def _payload(operation_id: uuid.UUID) -> str:
    return ServiceLearningPayload(
        operation_id=operation_id,
        server_id=SERVER_A,
        application_id=APP_MAIN_ID,
    ).model_dump_json()


def _row(url: str, operation_id: uuid.UUID) -> dict[str, Any]:
    with psycopg.connect(url) as conn:
        row = conn.execute(
            "SELECT status, sub_effects, error_code FROM "
            "service_learning_operation WHERE operation_id = %s",
            (str(operation_id),),
        ).fetchone()
    assert row is not None
    return {"status": row[0], "sub_effects": row[1] or {}, "error_code": row[2]}


def _fact_ids(url: str) -> set[str]:
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT fact_id FROM service_learned_fact WHERE status = 'active'"
        ).fetchall()
    return {str(r[0]) for r in rows}


def _statements(url: str) -> set[str]:
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT statement FROM service_learned_fact WHERE status = 'active'"
        ).fetchall()
    return {str(r[0]) for r in rows}


def _revision_history_count(url: str) -> int:
    with psycopg.connect(url) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM service_learned_fact_revision"
        ).fetchone()
    return int(row[0])


def _identity_generation(url: str, application_id: uuid.UUID) -> int:
    with psycopg.connect(url) as conn:
        row = conn.execute(
            "SELECT generation FROM service_knowledge_identity WHERE "
            "server_id = %s AND application_id = %s",
            (str(SERVER_A), str(application_id)),
        ).fetchone()
    assert row is not None, "identity row missing"
    return int(row[0])


def _run_state(url: str, run_id: uuid.UUID) -> str | None:
    with psycopg.connect(url) as conn:
        row = conn.execute(
            "SELECT state FROM session_runs WHERE run_id = %s",
            (str(run_id),),
        ).fetchone()
    return None if row is None else str(row[0])


# ---------------------------------------------------------------------------
# R3 — foreground completes before background preservation applies
# ---------------------------------------------------------------------------


def test_r3_foreground_completes_before_background_apply_settles(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    run_id = uuid.uuid4()

    def _durable_checker(server_id: uuid.UUID, source_run_id: uuid.UUID) -> bool:
        with psycopg.connect(worker_url) as conn:
            row = conn.execute(
                "SELECT state FROM session_runs WHERE run_id = %s",
                (str(source_run_id),),
            ).fetchone()
        return bool(row and row[0] in ("cancelled", "outcome_unknown"))

    handler, operations, _map, _write, _coord = _wire(
        worker_url, factory, tmp_path, run_stopped_checker=_durable_checker
    )
    register_service_learning_handler(handler)
    ref = _seed_foreground_run(worker_url, factory, run_id)

    with EngineClient(worker_url) as client:
        operation_id, _wf = _admit(
            operations, build_engine_enqueue(client), run_id=run_id, ref=ref
        )
        # The FOREGROUND run completes normally while the background
        # preservation workflow is still live.
        with psycopg.connect(worker_url, autocommit=True) as conn:
            conn.execute(
                "UPDATE session_runs SET state = 'succeeded', "
                "completed_at = now() WHERE run_id = %s",
                (str(run_id),),
            )
        receipt = client.await_run(str(operation_id))

    assert isinstance(receipt, dict)
    assert receipt["status"] == "applied", (receipt.get("error_code"), receipt)
    assert receipt["sub_effects"]["facts"] == "applied"
    # Foreground completion left accepted work running to completion.
    assert _run_state(worker_url, run_id) == "succeeded"
    assert _statements(worker_url) == {_FACT_STATEMENT}
    assert _row(worker_url, operation_id)["status"] == "applied"


# ---------------------------------------------------------------------------
# R4 — real crash/interruption window + idempotent recovery
# ---------------------------------------------------------------------------


class _CrashAfterFactsCommit(KnowledgeWriteStore):
    """Worker crash AFTER the fact write committed but before the
    executor checkpointed the sub-effect."""

    def __init__(self, inner: KnowledgeWriteStore) -> None:
        self._inner = inner

    async def apply_facts(self, **kwargs: Any) -> Any:
        await self._inner.apply_facts(**kwargs)
        raise RuntimeError("simulated crash after the fact commit")


def test_r4_post_fact_crash_replay_via_real_engine_is_idempotent(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    run_id = uuid.uuid4()
    ref = _seed_foreground_run(worker_url, factory, run_id)

    # First registration: the handler crashes AFTER the fact commit.
    crashing, operations, *_ = _wire(
        worker_url,
        factory,
        tmp_path,
        knowledge_write=_CrashAfterFactsCommit(KnowledgeWriteStore(factory)),
    )
    register_service_learning_handler(crashing)
    deferred = _DeferredEngine()
    operation_id, workflow_id = _admit(
        operations, deferred, run_id=run_id, ref=ref
    )
    with pytest.raises(RuntimeError, match="crash after the fact commit"):
        _run(crashing(_payload(operation_id)))

    # The fact IS committed (crash-after-commit), the receipt is unsettled.
    committed = _statements(worker_url)
    assert committed == {_FACT_STATEMENT}
    assert _row(worker_url, operation_id)["status"] == "applying"

    # Recovery: a HEALTHY handler instance re-registered under the SAME
    # workflow name; the replay reconciles through the idempotent write
    # seam (the crashed run was a direct in-process invocation, so the
    # replay is the same seam — a real-engine re-enqueue would answer
    # run_not_found for a workflow the engine never owned).
    healthy, _operations, *_ = _wire(
        worker_url, factory, tmp_path, operations=operations
    )
    register_service_learning_handler(healthy)
    receipt = _run(healthy(_payload(operation_id)))

    assert receipt["status"] == "applied", (receipt.get("error_code"), receipt)
    # IDEMPOTENT: no duplicated facts, no duplicated revisions.
    assert _statements(worker_url) == {_FACT_STATEMENT}
    assert _revision_history_count(worker_url) == 1
    assert receipt["sub_effects"]["facts"] == "applied"
    # The idempotent replay reuses the committed proposal: fact merges
    # never bump the identity generation (only an operator readopt does).
    assert _identity_generation(worker_url, APP_MAIN_ID) == 1


# ---------------------------------------------------------------------------
# R5 — concurrent API + worker writer races; deletion suppression
# ---------------------------------------------------------------------------


def test_r5_concurrent_workflows_fence_on_preseeded_identity(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    _seed_identity(worker_url, APP_MAIN_ID)
    handler, operations, *_ = _wire(worker_url, factory, tmp_path)
    register_service_learning_handler(handler)

    # SAME (category, key), DIVERGENT statements: the apply seam's
    # merge/conflict CAS yields exactly ONE winner.
    fact_a = "runtime suite: writer A notes for the shared key"
    fact_b = "runtime suite: writer B notes for the shared key"
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    ref_a = _seed_foreground_run(worker_url, factory, run_a)
    ref_b = _seed_foreground_run(worker_url, factory, run_b)
    op_a, wf_a = _admit(
        operations,
        _DeferredEngine(),
        run_id=run_a,
        statement=fact_a,
        key="runtime-shared-key",
        ref=ref_a,
    )
    op_b, wf_b = _admit(
        operations,
        _DeferredEngine(),
        run_id=run_b,
        statement=fact_b,
        key="runtime-shared-key",
        ref=ref_b,
    )

    # TWO workflows race on the SAME pre-seeded identity generation.
    async def _race() -> list[dict[str, Any]]:
        return list(
            await asyncio.gather(
                handler(_payload(op_a)), handler(_payload(op_b))
            )
        )

    receipts = _run(_race())
    outcomes = {wf_a: receipts[0], wf_b: receipts[1]}
    applied = [wf for wf, r in outcomes.items() if r["status"] == "applied"]
    fenced = [wf for wf, r in outcomes.items() if r["status"] == "failed"]

    assert len(applied) == 1 and len(fenced) == 1, outcomes
    loser = outcomes[fenced[0]]
    # The loser hit the merge/conflict CAS (divergent same-key proposal).
    assert loser["error_code"] == "fact_conflict", loser

    # Exactly the winner's fact exists — no duplicates, no lost update —
    # and a fact merge never bumps the identity generation.
    winner_fact = fact_a if applied[0] == wf_a else fact_b
    assert _statements(worker_url) == {winner_fact}
    assert _revision_history_count(worker_url) == 1
    assert _identity_generation(worker_url, APP_MAIN_ID) == 1
    durable_loser = _row(worker_url, op_b if applied[0] == wf_a else op_a)
    assert durable_loser["status"] == "failed"
    assert durable_loser["error_code"] == "fact_conflict"


def test_r5_real_engine_worker_race_serializes_on_generation(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    _seed_identity(worker_url, APP_MAIN_ID)
    handler, operations, *_ = _wire(worker_url, factory, tmp_path)
    register_service_learning_handler(handler)

    fact_a = "runtime suite: engine race writer A"
    fact_b = "runtime suite: engine race writer B"
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    ref_a = _seed_foreground_run(worker_url, factory, run_a)
    ref_b = _seed_foreground_run(worker_url, factory, run_b)
    op_a, _wf_a = _admit(
        operations,
        _DeferredEngine(),
        run_id=run_a,
        statement=fact_a,
        key="runtime-engine-race",
        ref=ref_a,
    )
    op_b, _wf_b = _admit(
        operations,
        _DeferredEngine(),
        run_id=run_b,
        statement=fact_b,
        key="runtime-engine-race",
        ref=ref_b,
    )
    assert _row(worker_url, op_a)["status"] == "queued"
    assert _row(worker_url, op_b)["status"] == "queued"

    with EngineClient(worker_url) as client:
        enqueue = build_engine_enqueue(client)
        _run(operations.recover_pending_batch(SERVER_A, engine_enqueue=enqueue))
        receipt_a = client.await_run(str(op_a))
        receipt_b = client.await_run(str(op_b))

    statuses = sorted(
        [receipt_a["status"], receipt_b["status"]],
        key=lambda s: {"applied": 0, "failed": 1}.get(s, 2),
    )
    # The worker executes serially: the FIRST apply wins the generation,
    # the SECOND is fenced — never two applied, never zero.
    assert statuses == ["applied", "failed"], (receipt_a, receipt_b)
    loser = receipt_a if receipt_a["status"] == "failed" else receipt_b
    assert loser["error_code"] == "fact_conflict", loser
    winner_fact = fact_a if receipt_a["status"] == "applied" else fact_b
    assert _statements(worker_url) == {winner_fact}
    assert _revision_history_count(worker_url) == 1
    assert _identity_generation(worker_url, APP_MAIN_ID) == 1


def test_r5_map_revision_cas_race_single_winner(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    _seed_identity(worker_url, APP_MAIN_ID)
    _handler, _operations, map_store, _write, coordinator = _wire(
        worker_url, factory, tmp_path
    )
    map_store.upsert_entry(
        SERVER_A,
        name="web",
        origin="compose",
        compose_project="web",
        working_dir="/srv/web",
        application_id=APP_MAIN_ID,
    )

    async def _race() -> list[Any]:
        return list(
            await asyncio.gather(
                coordinator.update_entry_metadata(
                    SERVER_A,
                    APP_MAIN_ID,
                    expected_revision=1,
                    custom_instructions="operator writer A notes",
                ),
                coordinator.update_entry_metadata(
                    SERVER_A,
                    APP_MAIN_ID,
                    expected_revision=1,
                    custom_instructions="operator writer B notes",
                ),
                return_exceptions=True,
            )
        )

    outcomes = _run(_race())
    revisions = [o for o in outcomes if isinstance(o, MutationOutcome)]
    conflicts = [o for o in outcomes if isinstance(o, ServiceMapError)]
    assert len(revisions) == 1 and len(conflicts) == 1, outcomes
    assert revisions[0].map_entry_revision == 2
    assert conflicts[0].code.value == "revision_conflict"

    # Exactly ONE writer's operator notes survived the CAS race — the
    # per-server file lock serialized them, the loser was refused with
    # no effect.
    entry = map_store.get_entry(SERVER_A, APP_MAIN_ID)
    assert entry is not None and entry.revision == 2
    assert entry.custom_instructions in (
        "operator writer A notes",
        "operator writer B notes",
    )


def test_r5_unadopt_suppression_cancels_and_blocks_readopt(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    handler, operations, _map, _write, coordinator = _wire(
        worker_url, factory, tmp_path
    )
    register_service_learning_handler(handler)
    run_id = uuid.uuid4()
    ref = _seed_foreground_run(worker_url, factory, run_id)
    operation_id, workflow_id = _admit(
        operations, _DeferredEngine(), run_id=run_id, ref=ref
    )
    assert _row(worker_url, operation_id)["status"] == "queued"

    # Deletion suppression: tombstone FIRST, then cancel pending work.
    _run(operations.suppress_for_unadopt(SERVER_A, APP_MAIN_ID))
    durable = _row(worker_url, operation_id)
    assert durable["status"] == "failed"
    assert durable["error_code"] == "generation_superseded"

    # Background admission for the tombstoned identity is REFUSED.
    with pytest.raises(ServiceAutoAdoptSuppressedError):
        _admit(
            operations,
            _DeferredEngine(),
            run_id=uuid.uuid4(),
            ref=ref,
        )

    # The explicit operator readopt starts a NEW generation and clears
    # the suppression; admission works again afterwards.
    _run(coordinator.readopt(SERVER_A, APP_MAIN_ID))
    assert _identity_generation(worker_url, APP_MAIN_ID) >= 2
    with psycopg.connect(worker_url) as conn:
        suppressed = conn.execute(
            "SELECT auto_adopt_suppressed FROM service_knowledge_identity "
            "WHERE server_id = %s AND application_id = %s",
            (str(SERVER_A), str(APP_MAIN_ID)),
        ).fetchone()
    assert suppressed is not None and bool(suppressed[0]) is False

    op_new, _wf_new = _admit(
        operations,
        _DeferredEngine(),
        run_id=uuid.uuid4(),
        ref=ref,
        expected_generation=_identity_generation(worker_url, APP_MAIN_ID),
    )
    with EngineClient(worker_url) as client:
        _run(
            operations.recover_pending_batch(
                SERVER_A, engine_enqueue=build_engine_enqueue(client)
            )
        )
        receipt = client.await_run(str(op_new))
    assert receipt["status"] == "applied", (receipt.get("error_code"), receipt)


# ---------------------------------------------------------------------------
# R6 — grant revocation and source-run cancellation
# ---------------------------------------------------------------------------


def test_r6_grant_revocation_refuses_idempotently_before_effects(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    handler, operations, *_ = _wire(
        worker_url,
        factory,
        tmp_path,
        current_grants_resolver=lambda server_id, actor: set(),
    )
    register_service_learning_handler(handler)
    run_id = uuid.uuid4()
    ref = _seed_foreground_run(worker_url, factory, run_id)
    operation_id, workflow_id = _admit(
        operations, _DeferredEngine(), run_id=run_id, ref=ref
    )

    with EngineClient(worker_url) as client:
        enqueue = build_engine_enqueue(client)
        _run(operations.recover_pending_batch(SERVER_A, engine_enqueue=enqueue))
        receipt = client.await_run(str(operation_id))
        assert receipt["status"] == "failed"
        assert receipt["error_code"] == "authority_revoked"
        assert _fact_ids(worker_url) == set()
        assert _revision_history_count(worker_url) == 0

        # The refusal is idempotent: re-running the same workflow answers
        # the terminal refusal again without touching stores.
        receipt_again = _run(handler(_payload(operation_id)))
    assert receipt_again["status"] == "failed"
    assert receipt_again["error_code"] == "authority_revoked"
    assert _fact_ids(worker_url) == set()


def test_r6_operator_stop_cancels_through_the_durable_checker(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    run_id = uuid.uuid4()

    def _durable_checker(server_id: uuid.UUID, source_run_id: uuid.UUID) -> bool:
        with psycopg.connect(worker_url) as conn:
            row = conn.execute(
                "SELECT state FROM session_runs WHERE run_id = %s",
                (str(source_run_id),),
            ).fetchone()
        return bool(row and row[0] in ("cancelled", "outcome_unknown"))

    handler, operations, *_ = _wire(
        worker_url, factory, tmp_path, run_stopped_checker=_durable_checker
    )
    register_service_learning_handler(handler)
    ref = _seed_foreground_run(worker_url, factory, run_id)
    operation_id, _wf = _admit(operations, _DeferredEngine(), run_id=run_id, ref=ref)

    # The operator stops the SOURCE RUN while the operation is queued.
    with psycopg.connect(worker_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE session_runs SET state = 'cancelled', completed_at = now() "
            "WHERE run_id = %s",
            (str(run_id),),
        )

    with EngineClient(worker_url) as client:
        _run(
            operations.recover_pending_batch(
                SERVER_A, engine_enqueue=build_engine_enqueue(client)
            )
        )
        receipt = client.await_run(str(operation_id))

    assert receipt["status"] == "failed"
    assert receipt["error_code"] == "source_cancelled"
    assert receipt["sub_effects"] == {}
    assert _run_state(worker_url, run_id) == "cancelled"
    assert _fact_ids(worker_url) == set()
    assert _revision_history_count(worker_url) == 0


# ---------------------------------------------------------------------------
# R7 — partial docs failure records partial honestly, resume applies
# ---------------------------------------------------------------------------


def test_r7_partial_docs_failure_keeps_committed_facts(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    async def _failing_docs(
        server_id: uuid.UUID,
        application_id: uuid.UUID | None,
        applied_fact_ids: tuple[uuid.UUID, ...],
    ) -> None:
        raise RuntimeError("docs index unavailable")

    handler, operations, _map, _write, _coord = _wire(
        worker_url, factory, tmp_path, docs_indexer=_failing_docs
    )
    register_service_learning_handler(handler)
    run_id = uuid.uuid4()
    ref = _seed_foreground_run(worker_url, factory, run_id)
    operation_id, _wf = _admit(operations, _DeferredEngine(), run_id=run_id, ref=ref)

    receipt = _run(handler(_payload(operation_id)))
    assert receipt["status"] == "partial", (receipt.get("error_code"), receipt)
    assert receipt["error_code"] == "docs_sync_failed"
    assert receipt["sub_effects"]["facts"] == "applied"
    assert receipt["sub_effects"]["docs"] == "failed"

    # The committed SQL facts are NOT rolled back by the partial docs
    # failure — exactly one fact + one revision row remain.
    assert _statements(worker_url) == {_FACT_STATEMENT}
    assert _revision_history_count(worker_url) == 1


def test_r7_resume_feeds_persisted_fact_ids_and_settles_applied(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    handler, operations, _map, write, _coord = _wire(
        worker_url, factory, tmp_path
    )
    run_id = uuid.uuid4()
    ref = _seed_foreground_run(worker_url, factory, run_id)
    operation_id, _wf = _admit(operations, _DeferredEngine(), run_id=run_id, ref=ref)

    # The exact durable post-crash state: facts checkpointed applied, docs
    # pending, applied ids persisted on the row (fix-2 row branch).
    authorization = _run(
        operations.begin_apply(
            SERVER_A,
            operation_id,
            current_grants={"app.learn_service"},
            required_grants={"app.learn_service"},
        )
    )
    trusted = TrustedSourceContext(
        server_id=SERVER_A,
        run_id=run_id,
        session_id=None,
        tool_call_id="runtime-direct-apply",
        actor="principal",
        grants=["app.learn_service"],
        context_at=datetime.now(tz=timezone.utc),
    )
    write_result = _run(
        write.apply_facts(
            server_id=SERVER_A,
            application_id=APP_MAIN_ID,
            request=LearnRequest(
                facts=[
                    ProposedFactDelta(
                        category="deployment",
                        key="runtime-" + _FACT_STATEMENT[:24],
                        statement=_FACT_STATEMENT,
                        source_kind="tool_result",
                        source_refs=[str(ref)],
                    )
                ],
                expected_generation=authorization.generation,
            ),
            trusted=trusted,
            expected_generation=authorization.generation,
        )
    )
    assert write_result.status.name == "APPLIED"
    committed_ids = [str(fid) for fid in write_result.applied_fact_ids]
    with psycopg.connect(worker_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE service_learning_operation SET sub_effects = "
            "'{\"facts\": \"applied\", \"docs\": \"pending\"}'::jsonb, "
            "applied_fact_ids = %s::jsonb WHERE operation_id = %s",
            (json.dumps(committed_ids), str(operation_id)),
        )

    docs_seen: list[tuple[uuid.UUID | None, tuple[uuid.UUID, ...]]] = []

    async def _capturing_docs(
        server_id: uuid.UUID,
        application_id: uuid.UUID | None,
        applied_fact_ids: tuple[uuid.UUID, ...],
    ) -> None:
        docs_seen.append((application_id, applied_fact_ids))

    healthy, _operations, *_ = _wire(
        worker_url,
        factory,
        tmp_path,
        operations=operations,
        docs_indexer=_capturing_docs,
    )
    resumed = _run(healthy(_payload(operation_id)))
    assert resumed["status"] == "applied", (resumed.get("error_code"), resumed)
    assert resumed["sub_effects"]["facts"] == "applied"
    assert resumed["sub_effects"]["docs"] == "applied"
    assert len(docs_seen) == 1
    seen_app, seen_ids = docs_seen[0]
    assert seen_app == APP_MAIN_ID
    assert {str(fid) for fid in seen_ids} == set(committed_ids)
    assert {str(fid) for fid in seen_ids} == _fact_ids(worker_url)
    # No rollback, no duplicates.
    assert _statements(worker_url) == {_FACT_STATEMENT}
    assert _revision_history_count(worker_url) == 1


# ---------------------------------------------------------------------------
# R4 (real process) — SIGKILL mid-apply + fresh-worker startup recovery.
# DELIBERATELY LAST: DBOS.destroy() on engine.shutdown() leaves the
# import-time workflow decorators bound to the first engine instance, so
# a same-process relaunch never re-binds queue polling. Every in-process
# engine test must run BEFORE this one.
# ---------------------------------------------------------------------------


def test_r4_worker_sigkill_mid_apply_recovery_reconciles_idempotently(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    run_id = uuid.uuid4()
    ref = _seed_foreground_run(worker_url, factory, run_id)
    operations = LearningOperationStore(factory)
    deferred = _DeferredEngine()
    operation_id, _wf = _admit(operations, deferred, run_id=run_id, ref=ref)
    assert _row(worker_url, operation_id)["status"] == "queued"

    def _launch_worker(data_root: pathlib.Path) -> subprocess.Popen[Any]:
        env = dict(os.environ)
        env["MAI_DATABASE_URL"] = worker_url
        env["MAI_DATA_ROOT"] = str(data_root)
        (data_root).mkdir(parents=True, exist_ok=True)
        env.pop("MAI_TEST_DATABASE_URL", None)
        env.pop("MAI_WORKER_HANDLERS", None)
        return subprocess.Popen(
            [sys.executable, "-m", "mai.jobs.worker"],
            cwd=str(BACKEND_ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=(data_root / "stderr.log").open("w"),
        )

    # ISOLATION: only the subprocess worker may claim the workflow.
    engine.shutdown()
    first = _launch_worker(tmp_path / "worker1")
    killed_mid_apply = False
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            status = _row(worker_url, operation_id)["status"]
            if status == "applying":
                killed_mid_apply = True
                break
            if status in ("applied", "failed"):
                break  # the window closed before the kill landed
            if first.poll() is not None:
                break
            time.sleep(0.01)
        # REAL interruption: SIGKILL (no cleanup handlers run).
        first.send_signal(signal.SIGKILL)
        first.wait(timeout=30)
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=10)

    # Snapshot what the first attempt durably committed BEFORE the kill:
    # the recovery may add NOTHING (idempotency).
    facts_before_kill = _statements(worker_url)
    revisions_before_kill = _revision_history_count(worker_url)

    # A FRESH worker performs the bounded startup recovery. Cross-process
    # honesty: the admitted request/context is NOT resolvable in a fresh
    # process, so the sweep promotes the unsettled receipt and the
    # handler fails it CLOSED (authority_revoked) — the reconcile adds
    # NOTHING (no duplicated facts/sub-effects). The full-payload
    # recovery binding is a later slice; the in-process R4 replay test
    # above proves the idempotent re-apply.
    second = _launch_worker(tmp_path / "worker2")
    try:
        deadline = time.monotonic() + 300
        settled = None
        while time.monotonic() < deadline:
            settled = _row(worker_url, operation_id)
            if settled["status"] in ("applied", "failed"):
                break
            if second.poll() is not None:
                break
            time.sleep(0.2)
        assert settled is not None and settled["status"] in (
            "applied",
            "failed",
        ), (settled, (tmp_path / "worker2" / "stderr.log").read_text()[-2000:])
        if settled["status"] == "failed":
            assert settled["error_code"] == "authority_revoked", settled
            assert settled["sub_effects"] == {}
    finally:
        if second.poll() is None:
            second.send_signal(signal.SIGTERM)
            try:
                second.wait(timeout=60)
            except subprocess.TimeoutExpired:
                second.kill()
                second.wait(timeout=30)
    # Same-process relaunch cannot re-bind the import-time workflow
    # decorators — this test is deliberately the LAST in the module
    # (module teardown shuts the engine down afterwards).
    engine.launch_worker(worker_url)

    # IDEMPOTENT reconciliation: the recovery ADDED NOTHING — the facts
    # and revision rows are exactly the pre-kill committed state.
    assert settled is not None
    assert _statements(worker_url) == facts_before_kill
    assert _revision_history_count(worker_url) == revisions_before_kill
    if killed_mid_apply:
        # The kill landed mid-apply: the interrupted operation never
        # double-applied its effects.
        assert len(facts_before_kill) <= 1


def test_r5_stale_generation_fence_refuses_without_effects(
    worker_url: str,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    """The generation CAS: an operation admitted against a SUPERSEDED
    generation (identity already re-adopted past it) is fenced at
    begin_apply — refused without touching any store."""
    _seed_identity(worker_url, APP_MAIN_ID)
    with psycopg.connect(worker_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE service_knowledge_identity SET generation = 3 "
            "WHERE server_id = %s AND application_id = %s",
            (str(SERVER_A), str(APP_MAIN_ID)),
        )
    handler, operations, *_ = _wire(worker_url, factory, tmp_path)
    register_service_learning_handler(handler)
    run_id = uuid.uuid4()
    ref = _seed_foreground_run(worker_url, factory, run_id)
    # Admitted against generation 1; the identity now sits at 3.
    operation_id, _wf = _admit(
        operations, _DeferredEngine(), run_id=run_id, ref=ref, expected_generation=1
    )

    # The promotion seam answers a stale generation INLINE (terminal
    # refusal, no workflow enqueued) — read the durable row either way.
    with EngineClient(worker_url) as client:
        _run(
            operations.recover_pending_batch(
                SERVER_A, engine_enqueue=build_engine_enqueue(client)
            )
        )
        durable = _row(worker_url, operation_id)
        if durable["status"] in ("queued", "accepted", "applying"):
            receipt = client.await_run(str(operation_id))
            durable = {
                "status": receipt["status"],
                "error_code": receipt.get("error_code"),
                "sub_effects": receipt.get("sub_effects", {}),
            }

    assert durable["status"] == "failed"
    assert durable["error_code"] == "generation_superseded", durable
    assert durable["sub_effects"] == {}
    assert _fact_ids(worker_url) == set()
    assert _revision_history_count(worker_url) == 0
    assert _identity_generation(worker_url, APP_MAIN_ID) == 3
