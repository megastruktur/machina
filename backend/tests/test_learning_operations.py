"""Focused tests for the durable preservation operation admission store
(ask_13): trusted identity, deterministic derivations, atomic 16-op
per-run budget, duplicate coalescing BEFORE the budget, replay-vs-
conflict by source call id, NO accepted result before the engine
enqueue acknowledgment, honest progression/cancellation and bounded
paged reads.

Real PostgreSQL (the campaign disposable fixture). The engine enqueue
seam is a small async fake recording acknowledged workflow ids — it
simulates the EXTERNAL engine, never the code under test. No DBOS
worker is launched here: end-to-end enqueue proof is later integration
evidence (recorded as the honest scope limitation).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker

from mai.applications.knowledge_contracts import (
    LearnRequest,
    MAX_PAGE_LIMIT,
    OperationStatus,
    ProposedFactDelta,
    ScopeMismatchError,
    SubEffectStatus,
    TrustedSourceContext,
)
from mai.applications.learning_operations import (
    AdmitOperationResult,
    LearningOperationStore,
    MAX_PRESERVATION_OPERATIONS_PER_RUN,
    OperationAdmissionError,
    ServiceAutoAdoptSuppressedError,
    UnknownOutcomeRetryForbiddenError,
)
from mai.database import create_app_engine
from mai.jobs.engine import RunIdInUseError

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")
LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

SERVER_A = uuid.UUID("5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60")
SERVER_B = uuid.UUID("9a8b7c6d-1e2f-4a3b-8c5d-6e7f8a9b0c1d")
APP_MAIN_ID = uuid.UUID("0aaaaaaa-0000-4000-8000-000000000001")

_SEED_REF = "00000000-0000-4000-8000-000000000001"

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
            f"{LIVE_URL_VAR} is not set; the admission claims need a real "
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
def factory(migrated_url: str) -> async_sessionmaker:
    engine, sessions = create_app_engine(migrated_url)
    # Per-test SQL isolation: every test starts with clean knowledge
    # tables (seeded rows use fresh UUIDs per test).
    with psycopg.connect(migrated_url, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE TABLE service_learned_fact_revision, "
            "service_learned_fact, service_learning_operation, "
            "service_knowledge_identity CASCADE"
        )
    yield sessions
    _run(engine.dispose())


# ---------------------------------------------------------------------------
# fakes + builders (the fake engine simulates the EXTERNAL engine only)
# ---------------------------------------------------------------------------


class _RecordingEngine:
    """Minimal async engine-enqueue fake: records acknowledged workflow
    ids; ``fail`` mode raises instead of acknowledging (an unacknowledged
    enqueue NEVER lands in ``calls``)."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    async def __call__(self, workflow_id: str) -> None:
        if self.fail:
            raise RuntimeError("simulated engine outage: no acknowledgment")
        self.calls.append(workflow_id)


def _trusted(
    server_id: uuid.UUID,
    *,
    tool_call_id: str,
    run_id: uuid.UUID | None = None,
    actor: str = "agent:mai",
    grants: list[str] | None = None,
) -> TrustedSourceContext:
    return TrustedSourceContext(
        server_id=server_id,
        run_id=run_id if run_id is not None else uuid.uuid4(),
        session_id=None,
        tool_call_id=tool_call_id,
        actor=actor,
        grants=grants if grants is not None else ["knowledge.learn"],
        context_at=datetime.now(tz=timezone.utc),
    )


def _delta(
    key: str,
    statement: str,
    *,
    category: str = "runtime",
) -> ProposedFactDelta:
    return ProposedFactDelta(
        category=category,
        key=key,
        statement=statement,
        source_kind="tool_result",
        source_refs=[_SEED_REF],
    )


def _request(
    facts: list[ProposedFactDelta],
    *,
    expected_generation: int = 1,
) -> LearnRequest:
    return LearnRequest(facts=facts, expected_generation=expected_generation)


def _admit(
    factory: async_sessionmaker,
    *,
    trusted: TrustedSourceContext,
    request: LearnRequest,
    application_id: uuid.UUID | None = APP_MAIN_ID,
    engine: _RecordingEngine,
) -> AdmitOperationResult:
    store = LearningOperationStore(factory)
    return _run(
        store.admit(
            request=request,
            trusted=trusted,
            application_id=application_id,
            engine_enqueue=engine,
        )
    )


async def _gather(coros: list) -> list:
    """Gather on the persistent test loop thread (gather needs a running
    loop; MainThread has none)."""
    return await asyncio.gather(*coros, return_exceptions=True)


def _row(migrated_url: str, operation_id: uuid.UUID) -> dict[str, object]:
    with psycopg.connect(migrated_url) as conn:
        record = conn.execute(
            "SELECT operation_id, server_id, application_id, generation, "
            "payload_digest, workflow_id, status, sub_effects, coalesce_key, "
            "source_run_id, source_tool_call_id, actor, applied_fact_ids, "
            "conflict_keys, error_code, error_detail, accepted_at, updated_at "
            "FROM service_learning_operation WHERE operation_id = %s",
            (str(operation_id),),
        ).fetchone()
    assert record is not None, "expected a durable operation row"
    names = (
        "operation_id",
        "server_id",
        "application_id",
        "generation",
        "payload_digest",
        "workflow_id",
        "status",
        "sub_effects",
        "coalesce_key",
        "source_run_id",
        "source_tool_call_id",
        "actor",
        "applied_fact_ids",
        "conflict_keys",
        "error_code",
        "error_detail",
        "accepted_at",
        "updated_at",
    )
    return dict(zip(names, record, strict=True))


def _op_count(migrated_url: str, where: str = "TRUE") -> int:
    with psycopg.connect(migrated_url) as conn:
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM service_learning_operation WHERE {where}"
            ).fetchone()[0]
        )


def _op_count_where(migrated_url: str, where: str, params: tuple) -> int:
    with psycopg.connect(migrated_url) as conn:
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM service_learning_operation WHERE {where}",
                params,
            ).fetchone()[0]
        )


def _ordered_ids(migrated_url: str, where: str, order: str) -> list[uuid.UUID]:
    with psycopg.connect(migrated_url) as conn:
        rows = conn.execute(
            "SELECT operation_id FROM service_learning_operation "
            f"WHERE {where} ORDER BY {order}"
        ).fetchall()
    return [uuid.UUID(str(row[0])) for row in rows]


def _bulk_insert(migrated_url: str, count: int, run_id: uuid.UUID) -> None:
    """Directly persist ``count`` plausible accepted rows for envelope
    read tests (exercises the read seam, not admit)."""
    with psycopg.connect(migrated_url, autocommit=True) as conn:
        for index in range(count):
            operation_id = uuid.uuid5(
                uuid.NAMESPACE_URL, f"bulk-operation:{run_id}:{index}"
            )
            conn.execute(
                "INSERT INTO service_learning_operation (operation_id, "
                "server_id, application_id, generation, payload_digest, "
                "workflow_id, status, sub_effects, coalesce_key, "
                "source_run_id, source_tool_call_id, actor, accepted_at, "
                "updated_at) VALUES (%s, %s, %s, 1, %s, %s, 'accepted', "
                "'{}'::jsonb, %s, %s, %s, 'agent:bulk', "
                "now() - (%s * interval '1 second'), now())",
                (
                    str(operation_id),
                    str(SERVER_A),
                    str(APP_MAIN_ID),
                    hashlib.sha256(str(index).encode()).hexdigest(),
                    f"service.learn:{operation_id}",
                    f"bulk:{index}",
                    str(run_id),
                    f"bulk-{index}",
                    index,
                ),
            )


# ---------------------------------------------------------------------------
# 1. fresh admit happy path: deterministic derivations + generation map
# ---------------------------------------------------------------------------


def test_fresh_admit_happy_path_deterministic(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    run_id = uuid.uuid4()
    trusted = _trusted(SERVER_A, tool_call_id="call-1", run_id=run_id)
    request = _request([_delta("k1", "statement one")], expected_generation=0)
    engine = _RecordingEngine()

    result = _admit(factory, trusted=trusted, request=request, engine=engine)

    expected_operation_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"mai:learning-operation:{run_id}:call-1",
    )
    expected_workflow = f"service.learn:{expected_operation_id}"
    expected_digest = hashlib.sha256(
        request.model_dump_json().encode("utf-8")
    ).hexdigest()

    assert result.replayed is False
    assert result.coalesced is False
    assert result.receipt.status is OperationStatus.ACCEPTED
    assert result.receipt.operation_id == expected_operation_id
    assert result.receipt.workflow_id == expected_workflow
    assert result.receipt.payload_digest == expected_digest
    assert result.budget_used == 1
    assert result.budget_limit == MAX_PRESERVATION_OPERATIONS_PER_RUN
    assert engine.calls == [expected_workflow]

    row = _row(migrated_url, expected_operation_id)
    assert row["actor"] == "agent:mai"  # trusted actor, not caller payload
    assert row["status"] == "accepted"
    assert row["generation"] is None  # expected_generation 0 maps to NULL

    second = _admit(
        factory,
        trusted=_trusted(SERVER_A, tool_call_id="call-2", run_id=run_id),
        request=_request([_delta("k2", "statement two")], expected_generation=5),
        engine=_RecordingEngine(),
    )
    assert _row(migrated_url, second.receipt.operation_id)["generation"] == 5


# ---------------------------------------------------------------------------
# 2. same call id + same payload -> replay, one row, no re-enqueue
# ---------------------------------------------------------------------------


def test_same_call_same_payload_replays_without_reenqueue(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    run_id = uuid.uuid4()
    trusted = _trusted(SERVER_A, tool_call_id="call-1", run_id=run_id)
    request = _request([_delta("k1", "statement one")])
    first = _admit(factory, trusted=trusted, request=request, engine=_RecordingEngine())

    replay_engine = _RecordingEngine()
    replay = _admit(
        factory,
        trusted=trusted,
        request=_request([_delta("k1", "statement one")]),
        engine=replay_engine,
    )

    assert replay.replayed is True
    assert replay.coalesced is False
    assert replay.receipt.operation_id == first.receipt.operation_id
    assert replay.receipt.status is OperationStatus.ACCEPTED
    assert replay.budget_used == 1  # budget NOT consumed again
    assert replay_engine.calls == []  # stored row already accepted
    assert _op_count(migrated_url) == 1


# ---------------------------------------------------------------------------
# 3. same call id + different payload -> typed conflict, row untouched
# ---------------------------------------------------------------------------


def test_same_call_different_payload_conflict(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    run_id = uuid.uuid4()
    trusted = _trusted(SERVER_A, tool_call_id="call-1", run_id=run_id)
    request = _request([_delta("k1", "statement one")])
    first = _admit(factory, trusted=trusted, request=request, engine=_RecordingEngine())
    before = _row(migrated_url, first.receipt.operation_id)

    with pytest.raises(OperationAdmissionError) as err:
        _admit(
            factory,
            trusted=trusted,
            request=_request([_delta("k1", "a DIFFERENT statement")]),
            engine=_RecordingEngine(),
        )
    assert err.value.code == "payload_conflict"

    after = _row(migrated_url, first.receipt.operation_id)
    assert before == after  # byte-identical: digest/status/updated_at unchanged
    assert _op_count(migrated_url) == 1


# ---------------------------------------------------------------------------
# 4. duplicate observation coalesces BEFORE budget; divergence is a new op
# ---------------------------------------------------------------------------


def test_coalesce_before_budget_divergent_is_new_op(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    run_id = uuid.uuid4()
    first_trusted = _trusted(SERVER_A, tool_call_id="call-1", run_id=run_id)
    request = _request([_delta("deploy.port", "8443 with tls")])
    first = _admit(
        factory, trusted=first_trusted, request=request, engine=_RecordingEngine()
    )

    # Different tool_call_id, identical payload observation -> coalesce.
    duplicate = _admit(
        factory,
        trusted=_trusted(SERVER_A, tool_call_id="call-2", run_id=run_id),
        request=_request([_delta("deploy.port", "8443 with tls")]),
        engine=_RecordingEngine(),
    )
    assert duplicate.coalesced is True
    assert duplicate.replayed is False
    assert duplicate.receipt.operation_id == first.receipt.operation_id
    assert duplicate.budget_used == 1
    assert _op_count(migrated_url) == 1

    # Same coalesce key (same fact key set) but a different statement ->
    # different digest -> a DISTINCT operation consuming budget.
    divergent = _admit(
        factory,
        trusted=_trusted(SERVER_A, tool_call_id="call-3", run_id=run_id),
        request=_request([_delta("deploy.port", "8080 without tls")]),
        engine=_RecordingEngine(),
    )
    assert divergent.coalesced is False
    assert divergent.replayed is False
    assert divergent.receipt.operation_id != first.receipt.operation_id
    assert divergent.budget_used == 2
    assert _op_count(migrated_url) == 2

    # A later observation identical to the DIVERGENT second payload
    # coalesces onto THAT row (the digest filter in the coalescing
    # lookup must match the newest identical payload, not the oldest
    # row under the coalesce key).
    onto_divergent = _admit(
        factory,
        trusted=_trusted(SERVER_A, tool_call_id="call-4", run_id=run_id),
        request=_request([_delta("deploy.port", "8080 without tls")]),
        engine=_RecordingEngine(),
    )
    assert onto_divergent.coalesced is True
    assert onto_divergent.replayed is False
    assert onto_divergent.receipt.operation_id == divergent.receipt.operation_id
    assert onto_divergent.budget_used == 2  # no duplicate slot burned
    assert _op_count(migrated_url) == 2


# ---------------------------------------------------------------------------
# 5. atomic 16-op per-run budget under concurrent admissions
# ---------------------------------------------------------------------------


def test_concurrent_admissions_respect_budget_cap(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    run_id = uuid.uuid4()
    store = LearningOperationStore(factory)
    engine = _RecordingEngine()

    def _coro(index: int, prefix: str):
        trusted = _trusted(SERVER_A, tool_call_id=f"{prefix}-{index}", run_id=run_id)
        request = _request([_delta(f"{prefix}.k{index}", f"statement {prefix} {index}")])
        return store.admit(
            request=request,
            trusted=trusted,
            application_id=APP_MAIN_ID,
            engine_enqueue=engine,
        )

    for index in range(12):  # pre-fill 12 distinct operations
        _run(_coro(index, "pre"))

    results = _run(_gather([_coro(index, "conc") for index in range(10)]))

    admitted = [r for r in results if isinstance(r, AdmitOperationResult)]
    refused = [r for r in results if isinstance(r, OperationAdmissionError)]
    assert len(admitted) == 4
    assert len(refused) == 6
    assert all(r.code == "operation_budget_exceeded" for r in refused)
    assert all(r.budget_used == 16 for r in refused)
    assert all(r.budget_limit == MAX_PRESERVATION_OPERATIONS_PER_RUN for r in refused)
    assert all(r.budget_used <= 16 for r in admitted)
    assert sorted(r.budget_used for r in admitted) == [13, 14, 15, 16]
    assert _op_count_where(migrated_url, "source_run_id = %s", (str(run_id),)) == 16
    assert _op_count(migrated_url) == 16

    # An idempotent replay of an admitted call still succeeds at 16/16.
    replay = _run(_coro(0, "pre"))
    assert replay.replayed is True
    assert _op_count(migrated_url) == 16


# ---------------------------------------------------------------------------
# 6. NO accepted result before the enqueue acknowledgment; queued replay
# ---------------------------------------------------------------------------


def test_no_accepted_before_enqueue_ack_and_recovery(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    run_id = uuid.uuid4()
    trusted = _trusted(SERVER_A, tool_call_id="call-1", run_id=run_id)
    request = _request([_delta("k1", "statement one")])
    failing = _RecordingEngine(fail=True)

    with pytest.raises(OperationAdmissionError) as err:
        _admit(factory, trusted=trusted, request=request, engine=failing)
    assert err.value.code == "enqueue_failed"
    assert failing.calls == []  # no acknowledgment ever recorded

    operation_id = uuid.uuid5(
        uuid.NAMESPACE_URL, f"mai:learning-operation:{run_id}:call-1"
    )
    row = _row(migrated_url, operation_id)
    assert row["status"] == "queued"  # durable, recoverable — NOT accepted

    store = LearningOperationStore(factory)
    pending = _run(store.list_pending_recovery())
    assert pending.total == 1
    assert pending.items[0].operation_id == operation_id
    assert pending.items[0].status is OperationStatus.QUEUED

    healthy = _RecordingEngine()
    recovered = _admit(factory, trusted=trusted, request=request, engine=healthy)
    assert recovered.replayed is True
    assert recovered.receipt.status is OperationStatus.ACCEPTED
    assert healthy.calls == [recovered.receipt.workflow_id]
    assert _op_count(migrated_url) == 1  # reconciled, never a second op

    after = _run(store.list_pending_recovery())
    assert after.total == 0

    # A duplicate observation under a DIFFERENT tool call id of the
    # unacknowledged row reconciles the SAME queued workflow: it retries
    # the enqueue, promotes the first row, and reports honestly as
    # coalesced (replayed=False) — never as a fresh operation.
    flaky_run = uuid.uuid4()
    flaky_request = _request([_delta("flaky.k1", "statement flaky")])
    flaky_queued_operation = uuid.uuid5(
        uuid.NAMESPACE_URL, f"mai:learning-operation:{flaky_run}:call-flaky-a"
    )
    with pytest.raises(OperationAdmissionError) as flaky_err:
        _admit(
            factory,
            trusted=_trusted(SERVER_A, tool_call_id="call-flaky-a", run_id=flaky_run),
            request=flaky_request,
            engine=_RecordingEngine(fail=True),
        )
    assert flaky_err.value.code == "enqueue_failed"

    duplicate_engine = _RecordingEngine()
    recovered_coalesce = _admit(
        factory,
        trusted=_trusted(SERVER_A, tool_call_id="call-flaky-b", run_id=flaky_run),
        request=_request([_delta("flaky.k1", "statement flaky")]),
        engine=duplicate_engine,
    )
    assert recovered_coalesce.coalesced is True
    assert recovered_coalesce.replayed is False
    assert recovered_coalesce.receipt.operation_id == flaky_queued_operation
    assert recovered_coalesce.receipt.status is OperationStatus.ACCEPTED
    assert duplicate_engine.calls == [
        f"service.learn:{flaky_queued_operation}"
    ]  # the QUEUED row's deterministic workflow, not a fresh one
    assert _op_count(migrated_url, "status = 'accepted'") == 2  # + first recovery
    assert _op_count(migrated_url) == 2  # one row per run, never a second op


# ---------------------------------------------------------------------------
# 7. trusted identity is never caller-controlled; accepted_at is DB clock
# ---------------------------------------------------------------------------


def test_payload_cannot_carry_identity_and_accepted_at_is_db_clock(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    with pytest.raises(ValidationError):
        LearnRequest(
            facts=[_delta("k1", "statement one")],
            expected_generation=1,
            actor="agent:attacker",
        )
    with pytest.raises(ValidationError):
        LearnRequest(
            facts=[_delta("k1", "statement one")],
            expected_generation=1,
            grants=["knowledge.learn"],
        )
    with pytest.raises(ValidationError):
        LearnRequest(
            facts=[_delta("k1", "statement one")],
            expected_generation=1,
            handler_name="service.learn",
        )

    before = datetime.now(tz=timezone.utc) - timedelta(seconds=2)
    trusted = _trusted(SERVER_A, tool_call_id="call-1", actor="agent:mai")
    result = _admit(
        factory,
        trusted=trusted,
        request=_request([_delta("k1", "statement one")]),
        engine=_RecordingEngine(),
    )
    after = datetime.now(tz=timezone.utc) + timedelta(seconds=2)

    row = _row(migrated_url, result.receipt.operation_id)
    assert row["actor"] == "agent:mai"
    assert result.receipt.accepted_at == row["accepted_at"]
    accepted_at = row["accepted_at"]
    assert isinstance(accepted_at, datetime)
    assert accepted_at.tzinfo is not None
    assert before <= accepted_at <= after  # the database clock, not a client


# ---------------------------------------------------------------------------
# 8. honest progression, terminal statuses, explicit source stop
# ---------------------------------------------------------------------------


def test_progression_terminal_and_cancel_pending(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    run_id = uuid.uuid4()
    store = LearningOperationStore(factory)

    applied_result = _admit(
        factory,
        trusted=_trusted(SERVER_A, tool_call_id="call-applied", run_id=run_id),
        request=_request([_delta("k.applied", "statement applied")]),
        engine=_RecordingEngine(),
    )
    accepted_result = _admit(
        factory,
        trusted=_trusted(SERVER_A, tool_call_id="call-accepted", run_id=run_id),
        request=_request([_delta("k.accepted", "statement accepted")]),
        engine=_RecordingEngine(),
    )
    fact_id = uuid.uuid4()
    progressed = _run(
        store.record_progress(
            applied_result.receipt.operation_id,
            sub_effects={"facts": SubEffectStatus.APPLIED},
            applied_fact_ids=[fact_id],
        )
    )
    assert progressed.status is OperationStatus.APPLIED
    row = _row(migrated_url, applied_result.receipt.operation_id)
    assert row["sub_effects"] == {"facts": "applied"}
    assert row["applied_fact_ids"] == [str(fact_id)]

    with pytest.raises(OperationAdmissionError) as err:
        _run(
            store.record_progress(
                applied_result.receipt.operation_id,
                sub_effects={"facts": SubEffectStatus.PENDING},
            )
        )
    assert err.value.code == "status_regression"
    assert _row(migrated_url, applied_result.receipt.operation_id)["status"] == "applied"

    # An unacknowledged enqueue leaves a queued row on the same run.
    with pytest.raises(OperationAdmissionError):
        _admit(
            factory,
            trusted=_trusted(SERVER_A, tool_call_id="call-queued", run_id=run_id),
            request=_request([_delta("k.queued", "statement queued")]),
            engine=_RecordingEngine(fail=True),
        )

    cancelled = _run(store.cancel_pending_for_run(run_id, reason="operator stop"))
    assert cancelled == 2  # accepted + queued rows; the applied row is untouched
    applied_row = _row(migrated_url, applied_result.receipt.operation_id)
    assert applied_row["status"] == "applied"
    assert applied_row["error_code"] is None
    for operation_id in (
        accepted_result.receipt.operation_id,
        uuid.uuid5(uuid.NAMESPACE_URL, f"mai:learning-operation:{run_id}:call-queued"),
    ):
        stopped = _row(migrated_url, operation_id)
        assert stopped["status"] == "failed"
        assert stopped["error_code"] == "source_cancelled"
        assert stopped["error_detail"] == "operator stop"

    assert _run(store.cancel_pending_for_run(run_id)) == 0  # nothing left pending


# ---------------------------------------------------------------------------
# 9. pagination: deterministic DESC pages + ASC pending recovery
# ---------------------------------------------------------------------------


def test_pagination_deterministic_and_bounded(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    run_id = uuid.uuid4()
    store = LearningOperationStore(factory)
    for index in range(7):
        _admit(
            factory,
            trusted=_trusted(SERVER_A, tool_call_id=f"call-{index}", run_id=run_id),
            request=_request([_delta(f"page.k{index}", f"statement {index}")]),
            engine=_RecordingEngine(),
        )

    expected = _ordered_ids(
        migrated_url, f"server_id = '{SERVER_A}'", "accepted_at DESC, operation_id DESC"
    )
    assert len(expected) == 7

    page_one = _run(store.list_operations(SERVER_A, limit=3, offset=0))
    page_two = _run(store.list_operations(SERVER_A, limit=3, offset=3))
    page_three = _run(store.list_operations(SERVER_A, limit=3, offset=6))
    assert [r.operation_id for r in page_one.items] == expected[:3]
    assert [r.operation_id for r in page_two.items] == expected[3:6]
    assert [r.operation_id for r in page_three.items] == expected[6:]
    assert (page_one.total, page_two.total, page_three.total) == (7, 7, 7)
    assert (page_one.has_more, page_two.has_more, page_three.has_more) == (
        True,
        True,
        False,
    )

    with pytest.raises(ValueError):
        _run(store.list_operations(SERVER_A, limit=0))
    with pytest.raises(ValueError):
        _run(store.list_operations(SERVER_A, limit=MAX_PAGE_LIMIT + 1))
    with pytest.raises(ValueError):
        _run(store.list_operations(SERVER_A, offset=-1))

    # Pending recovery: two queued rows, oldest-first ASC, bounded page.
    queued_run = uuid.uuid4()
    for index in range(2):
        with pytest.raises(OperationAdmissionError):
            _admit(
                factory,
                trusted=_trusted(
                    SERVER_A, tool_call_id=f"q-{index}", run_id=queued_run
                ),
                request=_request([_delta(f"queue.k{index}", f"queued {index}")]),
                engine=_RecordingEngine(fail=True),
            )
    pending_expected = _ordered_ids(
        migrated_url, "status = 'queued'", "accepted_at ASC, operation_id ASC"
    )
    pending = _run(store.list_pending_recovery(limit=1))
    assert pending.total == 2
    assert [r.operation_id for r in pending.items] == pending_expected[:1]
    assert pending.has_more is True


# ---------------------------------------------------------------------------
# 10. scope guard: a foreign trusted server scope is rejected
# ---------------------------------------------------------------------------


def test_scope_guard_rejects_foreign_server(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    run_id = uuid.uuid4()
    request = _request([_delta("k1", "statement one")])
    admitted = _admit(
        factory,
        trusted=_trusted(SERVER_A, tool_call_id="call-1", run_id=run_id),
        request=request,
        engine=_RecordingEngine(),
    )

    foreign_engine = _RecordingEngine()
    with pytest.raises(ScopeMismatchError):
        _admit(
            factory,
            trusted=_trusted(SERVER_B, tool_call_id="call-1", run_id=run_id),
            request=request,
            engine=foreign_engine,
        )
    assert foreign_engine.calls == []
    assert _op_count(migrated_url) == 1
    row = _row(migrated_url, admitted.receipt.operation_id)
    assert row["server_id"] == SERVER_A


# ---------------------------------------------------------------------------
# 11. envelope bound on representative and maximal pages
# ---------------------------------------------------------------------------


def test_envelope_bound_on_representative_and_maximal_pages(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    run_id = uuid.uuid4()
    result = _admit(
        factory,
        trusted=_trusted(SERVER_A, tool_call_id="call-1", run_id=run_id),
        request=_request([_delta("k1", "statement one")]),
        engine=_RecordingEngine(),
    )
    assert len(result.model_dump_json()) < 64 * 1024

    bulk_run = uuid.uuid4()
    _bulk_insert(migrated_url, MAX_PRESERVATION_OPERATIONS_PER_RUN * 3 + 2, bulk_run)

    store = LearningOperationStore(factory)
    maximal = _run(store.list_operations(SERVER_A, limit=50, offset=0))
    assert maximal.total == 51  # the admitted row plus 50 bulk rows
    assert len(maximal.items) == 50
    assert len(maximal.model_dump_json()) < 64 * 1024


# ---------------------------------------------------------------------------
# slice 14 helpers
# ---------------------------------------------------------------------------


def _admit_on(
    store: LearningOperationStore,
    *,
    trusted: TrustedSourceContext,
    request: LearnRequest,
    engine: _RecordingEngine,
    application_id: uuid.UUID | None = APP_MAIN_ID,
) -> AdmitOperationResult:
    """Admit through a SHARED store instance (the admission -> execution
    context registry is in-process by design)."""
    return _run(
        store.admit(
            request=request,
            trusted=trusted,
            application_id=application_id,
            engine_enqueue=engine,
        )
    )


def _queued_op(
    factory: async_sessionmaker,
    store: LearningOperationStore,
    run_id: uuid.UUID,
    index: int,
) -> None:
    """Leave one durable queued (unacknowledged) row via a failing engine."""
    with pytest.raises(OperationAdmissionError) as err:
        _admit_on(
            store,
            trusted=_trusted(SERVER_A, tool_call_id=f"call-q{index}", run_id=run_id),
            request=_request([_delta(f"rec.k{index}", f"statement rec {index}")]),
            engine=_RecordingEngine(fail=True),
        )
    assert err.value.code == "enqueue_failed"


# ---------------------------------------------------------------------------
# 12. tombstone before removal + readopt generation bump
# ---------------------------------------------------------------------------


def test_suppress_cancels_pending_blocks_admit_and_readopt_fences_old_generation(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    run_id = uuid.uuid4()
    store = LearningOperationStore(factory)
    grants = ["app.learn_service_facts"]

    accepted = _admit_on(
        store,
        trusted=_trusted(SERVER_A, tool_call_id="call-live", run_id=run_id, grants=grants),
        request=_request([_delta("gen.live", "statement live")]),
        engine=_RecordingEngine(),
    )
    _queued_op(factory, store, run_id, 9)

    state = _run(store.suppress_for_unadopt(SERVER_A, APP_MAIN_ID))
    assert state.auto_adopt_suppressed is True
    assert state.generation == 1  # tombstone row created (identity was absent)
    assert state.suppressed_generation == 1
    for operation_id in (accepted.receipt.operation_id,):
        row = _row(migrated_url, operation_id)
        assert row["status"] == "failed"
        assert row["error_code"] == "generation_superseded"
    queued_id = uuid.uuid5(
        uuid.NAMESPACE_URL, f"mai:learning-operation:{run_id}:call-q9"
    )
    assert _row(migrated_url, queued_id)["error_code"] == "generation_superseded"

    with pytest.raises(ServiceAutoAdoptSuppressedError):
        _admit_on(
            store,
            trusted=_trusted(SERVER_A, tool_call_id="call-blocked", run_id=run_id, grants=grants),
            request=_request([_delta("gen.blocked", "statement blocked")]),
            engine=_RecordingEngine(),
        )

    readopted = _run(
        store.readopt_service_identity(SERVER_A, APP_MAIN_ID, manual_identity=True)
    )
    assert readopted.generation == 2  # explicit readopt starts a NEW generation
    assert readopted.auto_adopt_suppressed is False
    assert readopted.manual_identity is True
    assert readopted.knowledge_revision == 0
    assert readopted.service_revision == 1

    # New admit at generation 2 applies; the old-generation operation is
    # fenced at begin_apply.
    fresh = _admit_on(
        store,
        trusted=_trusted(SERVER_A, tool_call_id="call-fresh", run_id=run_id, grants=grants),
        request=_request([_delta("gen.fresh", "statement fresh")], expected_generation=2),
        engine=_RecordingEngine(),
    )
    assert fresh.receipt.status is OperationStatus.ACCEPTED

    authorized = _run(
        store.begin_apply(
            SERVER_A,
            fresh.receipt.operation_id,
            current_grants={"app.learn_service_facts"},
        )
    )
    assert authorized.generation == 2
    assert authorized.effective_grants == ["app.learn_service_facts"]
    assert authorized.request is not None
    assert authorized.trusted is not None
    assert authorized.trusted.tool_call_id == "call-fresh"
    assert _row(migrated_url, fresh.receipt.operation_id)["status"] == "applying"

    stale = _admit_on(
        store,
        trusted=_trusted(SERVER_A, tool_call_id="call-stale", run_id=run_id, grants=grants),
        request=_request([_delta("gen.stale", "statement stale")], expected_generation=1),
        engine=_RecordingEngine(),
    )
    with pytest.raises(OperationAdmissionError) as err:
        _run(store.begin_apply(SERVER_A, stale.receipt.operation_id, current_grants=set(grants)))
    assert err.value.code == "generation_superseded"
    stale_row = _row(migrated_url, stale.receipt.operation_id)
    assert stale_row["status"] == "failed"
    assert stale_row["error_code"] == "generation_superseded"


# ---------------------------------------------------------------------------
# 13. current-authority intersection at execution time
# ---------------------------------------------------------------------------


def test_begin_apply_authority_intersection(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    store = LearningOperationStore(factory)
    run_id = uuid.uuid4()
    grants = ["app.learn_service_facts", "app.adopt"]
    admitted = _admit_on(
        store,
        trusted=_trusted(SERVER_A, tool_call_id="call-auth", run_id=run_id, grants=grants),
        request=_request([_delta("auth.k1", "statement auth")]),
        engine=_RecordingEngine(),
    )

    with pytest.raises(OperationAdmissionError) as err:
        _run(
            store.begin_apply(
                SERVER_A, admitted.receipt.operation_id, current_grants={"app.adopt"}
            )
        )
    assert err.value.code == "authority_revoked"
    row = _row(migrated_url, admitted.receipt.operation_id)
    assert row["status"] == "failed"
    assert row["error_code"] == "authority_revoked"

    # The failed row is terminal and cannot begin again.
    with pytest.raises(OperationAdmissionError) as err:
        _run(
            store.begin_apply(
                SERVER_A,
                admitted.receipt.operation_id,
                current_grants={"app.learn_service_facts", "app.adopt"},
            )
        )
    assert err.value.code == "operation_not_runnable"

    # Scope guard on the execution fence: a foreign server is refused.
    other = _admit_on(
        store,
        trusted=_trusted(SERVER_A, tool_call_id="call-scope", run_id=run_id, grants=grants),
        request=_request([_delta("auth.k2", "statement scope")]),
        engine=_RecordingEngine(),
    )
    with pytest.raises(ScopeMismatchError):
        _run(
            store.begin_apply(
                SERVER_B, other.receipt.operation_id, current_grants=set(grants)
            )
        )


# ---------------------------------------------------------------------------
# 14. retry of an unknown outcome is forbidden everywhere
# ---------------------------------------------------------------------------


def test_unknown_outcome_never_retried(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    store = LearningOperationStore(factory)
    run_id = uuid.uuid4()
    admitted = _admit_on(
        store,
        trusted=_trusted(SERVER_A, tool_call_id="call-unknown", run_id=run_id),
        request=_request([_delta("unk.k1", "statement unk")]),
        engine=_RecordingEngine(),
    )
    progressed = _run(
        store.record_progress(
            admitted.receipt.operation_id,
            sub_effects={"facts": SubEffectStatus.APPLIED, "metadata": SubEffectStatus.UNKNOWN},
        )
    )
    assert progressed.status is OperationStatus.UNKNOWN

    engine = _RecordingEngine()
    with pytest.raises(UnknownOutcomeRetryForbiddenError):
        _run(store.retry_operation(admitted.receipt.operation_id, engine_enqueue=engine))
    with pytest.raises(UnknownOutcomeRetryForbiddenError):
        _run(
            store.begin_apply(
                SERVER_A, admitted.receipt.operation_id, current_grants={"anything"}
            )
        )
    assert engine.calls == []  # never re-enqueued

    # The sweep never visits unknown rows (terminal -> outside the pending set).
    sweep = _run(store.recover_pending_batch(engine_enqueue=engine))
    assert sweep.total == 0
    assert sweep.skipped_unknown == 0
    assert engine.calls == []
    assert _row(migrated_url, admitted.receipt.operation_id)["status"] == "unknown"


# ---------------------------------------------------------------------------
# 15. stable keyset cursor under concurrent status mutations
# ---------------------------------------------------------------------------


def test_keyset_cursor_stable_under_mutations(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    store = LearningOperationStore(factory)
    run_id = uuid.uuid4()
    for index in range(7):
        _queued_op(factory, store, run_id, index)

    expected = _ordered_ids(
        migrated_url, "status = 'queued'", "accepted_at ASC, operation_id ASC"
    )
    assert len(expected) == 7

    page_one = _run(store.list_pending_recovery(limit=3))
    assert [r.operation_id for r in page_one.items] == expected[:3]

    # The three page-1 rows complete while the reader holds the cursor.
    for receipt in page_one.items:
        progressed = _run(
            store.record_progress(
                receipt.operation_id,
                sub_effects={"facts": SubEffectStatus.APPLIED, "metadata": SubEffectStatus.FAILED},
            )
        )
        assert progressed.status is OperationStatus.PARTIAL

    last = page_one.items[-1]
    page_two = _run(
        store.list_pending_recovery(
            limit=3,
            after_accepted_at=last.accepted_at,
            after_operation_id=last.operation_id,
        )
    )
    assert [r.operation_id for r in page_two.items] == expected[3:6]
    assert page_two.total == 4  # count under the SAME keyset predicate

    page_three = _run(
        store.list_pending_recovery(
            limit=3,
            after_accepted_at=page_two.items[-1].accepted_at,
            after_operation_id=page_two.items[-1].operation_id,
        )
    )
    assert [r.operation_id for r in page_three.items] == expected[6:]
    assert page_three.has_more is False

    visited = [r.operation_id for r in (*page_two.items, *page_three.items)]
    assert visited == expected[3:]  # zero skipped, zero duplicated

    with pytest.raises(ValueError):
        _run(store.list_pending_recovery(after_accepted_at=last.accepted_at))
    with pytest.raises(ValueError):
        _run(
            store.list_pending_recovery(
                after_accepted_at=last.accepted_at,
                after_operation_id=last.operation_id,
                offset=3,
            )
        )


# ---------------------------------------------------------------------------
# 16. partial outcomes durable
# ---------------------------------------------------------------------------


def test_partial_outcomes_durable(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    store = LearningOperationStore(factory)
    run_id = uuid.uuid4()
    admitted = _admit_on(
        store,
        trusted=_trusted(SERVER_A, tool_call_id="call-partial", run_id=run_id),
        request=_request([_delta("part.k1", "statement part")]),
        engine=_RecordingEngine(),
    )
    fact_id = uuid.uuid4()
    progressed = _run(
        store.record_progress(
            admitted.receipt.operation_id,
            sub_effects={"facts": SubEffectStatus.APPLIED, "metadata": SubEffectStatus.FAILED},
            applied_fact_ids=[fact_id],
            conflict_keys=["deploy.port"],
            error_code="metadata_write_refused",
            error_detail="the metadata writer refused the effect",
        )
    )
    assert progressed.status is OperationStatus.PARTIAL
    row = _row(migrated_url, admitted.receipt.operation_id)
    assert row["sub_effects"] == {"facts": "applied", "metadata": "failed"}
    assert row["applied_fact_ids"] == [str(fact_id)]
    assert row["conflict_keys"] == ["deploy.port"]
    assert row["error_code"] == "metadata_write_refused"
    fetched = _run(store.get_operation(admitted.receipt.operation_id))
    assert fetched is not None and fetched.status is OperationStatus.PARTIAL


# ---------------------------------------------------------------------------
# 17. post-cancel reconciliation preserves the actually-applied portion
# ---------------------------------------------------------------------------


def test_post_cancel_reconciliation_preserves_applied_portion(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    store = LearningOperationStore(factory)
    run_id = uuid.uuid4()
    raced = _admit_on(
        store,
        trusted=_trusted(
            SERVER_A, tool_call_id="call-race", run_id=run_id, grants=["app.learn_service_facts"]
        ),
        request=_request([_delta("race.k1", "statement race")]),
        engine=_RecordingEngine(),
    )
    _run(
        store.begin_apply(
            SERVER_A,
            raced.receipt.operation_id,
            current_grants={"app.learn_service_facts"},
        )
    )
    assert _run(store.cancel_pending_for_run(run_id)) == 1

    # The in-flight executor records its real outcome after the cancel race.
    fact_id = uuid.uuid4()
    reconciled = _run(
        store.record_progress(
            raced.receipt.operation_id,
            sub_effects={"facts": SubEffectStatus.APPLIED, "docs": SubEffectStatus.UNKNOWN},
            applied_fact_ids=[fact_id],
        )
    )
    assert reconciled.status is OperationStatus.UNKNOWN
    row = _row(migrated_url, raced.receipt.operation_id)
    assert row["error_code"] == "source_cancelled"  # preserved, never overwritten
    assert row["applied_fact_ids"] == [str(fact_id)]
    assert row["sub_effects"] == {"facts": "applied", "docs": "unknown"}

    # Without an applied/unknown portion the regression refusal stands.
    other = _admit_on(
        store,
        trusted=_trusted(
            SERVER_A, tool_call_id="call-race2", run_id=run_id, grants=["app.learn_service_facts"]
        ),
        request=_request([_delta("race.k2", "statement race two")]),
        engine=_RecordingEngine(),
    )
    _run(
        store.begin_apply(
            SERVER_A,
            other.receipt.operation_id,
            current_grants={"app.learn_service_facts"},
        )
    )
    assert _run(store.cancel_pending_for_run(run_id)) == 1
    with pytest.raises(OperationAdmissionError) as err:
        _run(
            store.record_progress(
                other.receipt.operation_id,
                sub_effects={"facts": SubEffectStatus.PENDING},
            )
        )
    assert err.value.code == "status_regression"


# ---------------------------------------------------------------------------
# 18. startup recovery sweep: promotion, RunIdInUse ack, stale-generation
# ---------------------------------------------------------------------------


def test_recover_pending_batch_promotes_and_reattaches(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    store = LearningOperationStore(factory)
    run_id = uuid.uuid4()
    _queued_op(factory, store, run_id, 1)
    _queued_op(factory, store, run_id, 2)

    healthy = _RecordingEngine()
    sweep = _run(store.recover_pending_batch(limit=10, engine_enqueue=healthy))
    assert sweep.promoted == 2
    assert sweep.superseded == 0
    assert sweep.enqueue_failed == 0
    assert all(r.status is OperationStatus.ACCEPTED for r in sweep.receipts)
    assert len(healthy.calls) == 2
    assert len(sweep.model_dump_json()) < 64 * 1024

    # A duplicate-workflow-id rejection means the enqueue ALREADY landed
    # before the crash: treat it as the acknowledgment and promote.
    _queued_op(factory, store, run_id, 3)

    async def _raise_run_in_use(workflow_id: str) -> None:
        raise RunIdInUseError(workflow_id)

    promoted = _run(
        store.recover_pending_batch(limit=10, engine_enqueue=_raise_run_in_use)
    )
    assert promoted.promoted == 1
    assert promoted.enqueue_failed == 0
    assert promoted.receipts[-1].status is OperationStatus.ACCEPTED

    # Accepted rows are in-flight: untouched, engine never called for them.
    live = _admit_on(
        store,
        trusted=_trusted(SERVER_A, tool_call_id="call-live-rec", run_id=run_id),
        request=_request([_delta("rec.live", "statement live rec")]),
        engine=_RecordingEngine(),
    )
    quiet = _RecordingEngine()
    sweep_two = _run(store.recover_pending_batch(limit=10, engine_enqueue=quiet))
    # The three promoted rows plus the fresh admission are all accepted:
    # in-flight work their executors own — a sweep leaves them untouched.
    assert sweep_two.in_flight == 4
    assert sweep_two.promoted == 0
    assert quiet.calls == []
    assert _row(migrated_url, live.receipt.operation_id)["status"] == "accepted"


def test_recover_pending_batch_supersedes_stale_generation(
    migrated_url: str, factory: async_sessionmaker
) -> None:
    store = LearningOperationStore(factory)
    run_id = uuid.uuid4()
    stale_live = _admit_on(
        store,
        trusted=_trusted(SERVER_A, tool_call_id="call-old", run_id=run_id),
        request=_request([_delta("gen.old", "statement old")]),
        engine=_RecordingEngine(),
    )
    _queued_op(factory, store, run_id, 5)
    first = _run(store.readopt_service_identity(SERVER_A, APP_MAIN_ID))
    assert first.generation == 1  # explicit readopt on an absent identity starts at 1
    readopted = _run(store.readopt_service_identity(SERVER_A, APP_MAIN_ID))
    assert readopted.generation == 2  # the old operations now carry a stale generation

    engine = _RecordingEngine()
    sweep = _run(store.recover_pending_batch(limit=10, engine_enqueue=engine))
    assert sweep.superseded == 2
    assert sweep.promoted == 0
    assert engine.calls == []  # the queued stale row is NEVER enqueued
    for operation_id in (
        stale_live.receipt.operation_id,
        uuid.uuid5(uuid.NAMESPACE_URL, f"mai:learning-operation:{run_id}:call-q5"),
    ):
        row = _row(migrated_url, operation_id)
        assert row["status"] == "failed"
        assert row["error_code"] == "generation_superseded"

    # Server-scoped sweep: a different server owns no pending rows here.
    scoped = _run(
        store.recover_pending_batch(SERVER_B, limit=10, engine_enqueue=engine)
    )
    assert scoped.total == 0
    assert scoped.receipts == []
