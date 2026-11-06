"""Focused tests for aggregate delegation budgets (card 06-delegation-budgets).

Acceptance (BRIEF):

1. Atomic root-budget reservation/consumption and depth/concurrency
   limits cannot be bypassed by siblings — the ledger's check-then-append
   pairs are one transaction under the root ``job_runs`` row lock (the
   ONE whole-stream event-writer contract), so racing delegating runs on
   distinct connections can never oversubscribe the root's active-slot,
   step or token budgets; the budget document only ever NARROWS the
   delegation module's bounds, and a delegation that could never fit the
   remaining budget is rejected whole (no partial reservation, no partial
   charge).
2. Parent cancellation/failure propagates by frozen policy and preserves
   useful child evidence; waiting states remain truthful — the ledger's
   release half of the cascade refunds only OUTSTANDING reservations
   (unknown/double releases are hard rejections; the stream never goes
   negative), charged consumption is never un-spent, and the charged
   events stay on the root stream as the preserved value-free child
   evidence.

Boundary discipline (test policy): the durable-ledger claims run against
a real disposable PostgreSQL fixture (``MAI_TEST_DATABASE_URL``) with the
real Alembic chain and REAL concurrent transactions — sibling reserves
race through ``asyncio.gather`` on distinct sessions/connections, so the
root-row-lock serialization is exercised server-side, not simulated in
process order. The pure vocabulary/fold claims (document parsing,
narrowing gates, replayable fold) run without a database. Fixtures carry
only frozen UUID identities and integer counts — never secret values.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents import budgets
from mai.agents.budgets import (
    BUDGET_CHARGED_EVENT_TYPE,
    BUDGET_RELEASED_EVENT_TYPE,
    BUDGET_RESERVED_EVENT_TYPE,
    BudgetError,
    BudgetErrorCode,
    BudgetLedger,
    BudgetState,
    RootBudget,
    check_fanout,
    check_nesting,
    ledger_state,
    parse_root_budget,
)
from mai.contracts.jobs import EVENT_PAYLOAD_VERSION, parse_job_event
from mai.database import create_app_engine

BACKEND_ROOT = Path(__file__).resolve().parents[1]

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Frozen fixture identities (canonical lowercase UUID strings, C01 rule 1).
SERVER_ID = "7c2d4f6a-8b1e-4a3c-9d5f-2e4b6a8c0d1e"
ROOT_ID = "aa000000-0000-4000-8000-000000000001"

#: The canonical fixture budget: a tree of at most depth 3, 4 children per
#: delegation, 3 concurrent active slots, 20 aggregate steps, 5k tokens.
BUDGET_DOC: dict[str, object] = {
    "v": budgets.BUDGET_PAYLOAD_VERSION,
    "max_depth": 3,
    "max_children": 4,
    "max_active": 3,
    "max_steps": 20,
    "max_total_tokens": 5_000,
}


def _run_id(n: int) -> str:
    return f"bb000000-0000-4000-8000-{n:012d}"


# ---------------------------------------------------------------------------
# persistent test loop (same discipline the production executor applies)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# pure vocabulary: the frozen budget document + narrowing gates
# ---------------------------------------------------------------------------


class TestBudgetDocument:
    def test_valid_document_parses_with_narrowed_bounds(self) -> None:
        budget = parse_root_budget(BUDGET_DOC)
        assert budget == RootBudget(
            max_depth=3,
            max_children=4,
            max_active=3,
            max_steps=20,
            max_total_tokens=5_000,
        )
        assert budget is not None
        # Narrowing only: the effective bounds never exceed the module's.
        assert budget.allowed_depth == min(3, budgets.MAX_DELEGATION_DEPTH)
        assert budget.allowed_children == min(4, budgets.MAX_CHILDREN_PER_RUN)

    def test_null_document_is_the_explicit_unbudgeted_root(self) -> None:
        assert parse_root_budget(None) is None

    @pytest.mark.parametrize(
        ("document", "why"),
        [
            ({}, "empty object"),
            (
                {
                    "v": 2,
                    "max_depth": 3,
                    "max_children": 4,
                    "max_active": 3,
                    "max_steps": 20,
                    "max_total_tokens": 5_000,
                },
                "foreign payload version",
            ),
            (
                {
                    "max_depth": 3,
                    "max_children": 4,
                    "max_active": 3,
                    "max_steps": 20,
                    "max_total_tokens": 5_000,
                },
                "missing version",
            ),
            (
                {
                    "v": 1,
                    "max_depth": 3,
                    "max_children": 4,
                    "max_active": 3,
                    "max_steps": 20,
                },
                "missing cap",
            ),
            (
                {
                    "v": 1,
                    "max_depth": 3,
                    "max_children": 4,
                    "max_active": 3,
                    "max_steps": 20,
                    "max_total_tokens": 5_000,
                    "extra": 1,
                },
                "unknown field",
            ),
            *[
                (
                    {
                        "v": 1,
                        "max_depth": 0 if field == "max_depth" else 3,
                        "max_children": 0 if field == "max_children" else 4,
                        "max_active": 0 if field == "max_active" else 3,
                        "max_steps": 0 if field == "max_steps" else 20,
                        "max_total_tokens": (
                            0 if field == "max_total_tokens" else 5_000
                        ),
                    },
                    f"zero {field}",
                )
                for field in (
                    "max_depth",
                    "max_children",
                    "max_active",
                    "max_steps",
                    "max_total_tokens",
                )
            ],
            (
                {
                    "v": 1,
                    "max_depth": budgets.MAX_DELEGATION_DEPTH + 1,
                    "max_children": 4,
                    "max_active": 3,
                    "max_steps": 20,
                    "max_total_tokens": 5_000,
                },
                "depth above the module bound",
            ),
            (
                {
                    "v": 1,
                    "max_depth": 3,
                    "max_children": budgets.MAX_CHILDREN_PER_RUN + 1,
                    "max_active": 3,
                    "max_steps": 20,
                    "max_total_tokens": 5_000,
                },
                "fan-out above the module bound",
            ),
            (
                {
                    "v": 1,
                    "max_depth": 3,
                    "max_children": 4,
                    "max_active": budgets.MAX_ACTIVE_RUNS + 1,
                    "max_steps": 20,
                    "max_total_tokens": 5_000,
                },
                "active cap above the absolute bound",
            ),
            (
                {
                    "v": 1,
                    "max_depth": 3,
                    "max_children": 4,
                    "max_active": 3,
                    "max_steps": True,
                    "max_total_tokens": 5_000,
                },
                "boolean is not an integer",
            ),
            (
                {
                    "v": 1,
                    "max_depth": 3.0,
                    "max_children": 4,
                    "max_active": 3,
                    "max_steps": 20,
                    "max_total_tokens": 5_000,
                },
                "float is not an integer",
            ),
        ],
    )
    def test_malformed_documents_reject_fail_closed(
        self, document: object, why: str
    ) -> None:
        with pytest.raises(BudgetError) as excinfo:
            parse_root_budget(document)
        assert excinfo.value.code is BudgetErrorCode.INVALID_PAYLOAD, why


class TestNarrowingGates:
    def test_unbudgeted_tree_keeps_the_module_bounds(self) -> None:
        assert check_nesting(budgets.MAX_DELEGATION_DEPTH - 1, None) == (
            budgets.MAX_DELEGATION_DEPTH
        )
        with pytest.raises(BudgetError) as excinfo:
            check_nesting(budgets.MAX_DELEGATION_DEPTH, None)
        assert excinfo.value.code is BudgetErrorCode.DEPTH_EXHAUSTED
        assert check_fanout(budgets.MAX_CHILDREN_PER_RUN, None) == (
            budgets.MAX_CHILDREN_PER_RUN
        )
        with pytest.raises(BudgetError) as excinfo:
            check_fanout(budgets.MAX_CHILDREN_PER_RUN + 1, None)
        assert excinfo.value.code is BudgetErrorCode.FANOUT_EXHAUSTED

    def test_budget_narrows_the_module_bounds_fail_closed(self) -> None:
        budget = parse_root_budget(BUDGET_DOC)
        assert budget is not None
        assert check_nesting(2, budget) == 3
        with pytest.raises(BudgetError) as excinfo:
            check_nesting(3, budget)
        assert excinfo.value.code is BudgetErrorCode.DEPTH_EXHAUSTED
        assert check_fanout(4, budget) == 4
        with pytest.raises(BudgetError) as excinfo:
            check_fanout(5, budget)
        assert excinfo.value.code is BudgetErrorCode.FANOUT_EXHAUSTED

    @pytest.mark.parametrize("depth", [-1, True, 2.5, "3"])
    def test_nesting_gate_rejects_non_integer_depth(self, depth: object) -> None:
        with pytest.raises(BudgetError) as excinfo:
            check_nesting(depth, None)  # type: ignore[arg-type]
        assert excinfo.value.code is BudgetErrorCode.INVALID_PAYLOAD

    def test_fanout_gate_rejects_zero_count(self) -> None:
        with pytest.raises(BudgetError) as excinfo:
            check_fanout(0, None)
        assert excinfo.value.code is BudgetErrorCode.FANOUT_EXHAUSTED

    @pytest.mark.parametrize("count", [True, 2.5, "2"])
    def test_fanout_gate_rejects_non_integer_count(self, count: object) -> None:
        with pytest.raises(BudgetError) as excinfo:
            check_fanout(count, None)  # type: ignore[arg-type]
        assert excinfo.value.code is BudgetErrorCode.INVALID_PAYLOAD


# ---------------------------------------------------------------------------
# pure fold: the replayable ledger state
# ---------------------------------------------------------------------------


class TestLedgerFold:
    def test_reserve_release_charge_folds_to_truth(self) -> None:
        budget = parse_root_budget(BUDGET_DOC)
        assert budget is not None
        owner, child_a, child_b = _run_id(1), _run_id(2), _run_id(3)
        state = ledger_state(
            budget,
            [
                {
                    "type": BUDGET_RESERVED_EVENT_TYPE,
                    "data": {"owner": owner, "children": [child_a, child_b]},
                },
                {
                    "type": BUDGET_CHARGED_EVENT_TYPE,
                    "data": {"run": child_a, "steps": 4, "tokens": 1_000},
                },
                {
                    "type": BUDGET_RELEASED_EVENT_TYPE,
                    "data": {"owner": owner, "children": [child_b]},
                },
            ],
        )
        assert state.outstanding == (child_a,)
        assert state.consumed_steps == 4 and state.consumed_tokens == 1_000
        assert state.remaining_active == 2
        assert state.remaining_steps == 16 and state.remaining_tokens == 4_000

    def test_release_without_outstanding_reservation_is_rejected(self) -> None:
        budget = parse_root_budget(BUDGET_DOC)
        assert budget is not None
        with pytest.raises(BudgetError) as excinfo:
            ledger_state(
                budget,
                [
                    {
                        "type": BUDGET_RELEASED_EVENT_TYPE,
                        "data": {"owner": _run_id(1), "children": [_run_id(2)]},
                    }
                ],
            )
        assert excinfo.value.code is BudgetErrorCode.LEDGER_STATE

    def test_double_reservation_in_the_stream_is_rejected(self) -> None:
        budget = parse_root_budget(BUDGET_DOC)
        assert budget is not None
        child = _run_id(5)
        with pytest.raises(BudgetError) as excinfo:
            ledger_state(
                budget,
                [
                    {
                        "type": BUDGET_RESERVED_EVENT_TYPE,
                        "data": {"owner": _run_id(1), "children": [child]},
                    },
                    {
                        "type": BUDGET_RESERVED_EVENT_TYPE,
                        "data": {"owner": _run_id(2), "children": [child]},
                    },
                ],
            )
        assert excinfo.value.code is BudgetErrorCode.LEDGER_STATE

    @pytest.mark.parametrize(
        "event",
        [
            {"type": BUDGET_RESERVED_EVENT_TYPE, "data": {"owner": _run_id(1)}},
            {
                "type": BUDGET_RESERVED_EVENT_TYPE,
                "data": {"owner": _run_id(1), "children": ["not-a-uuid"]},
            },
            {
                "type": BUDGET_CHARGED_EVENT_TYPE,
                "data": {"run": _run_id(1), "steps": 0, "tokens": 0},
            },
            {
                "type": BUDGET_CHARGED_EVENT_TYPE,
                "data": {"run": _run_id(1), "steps": -1, "tokens": 5},
            },
            {"type": "agent.unknown_budget_type", "data": {}},
            {"data": {}},
        ],
    )
    def test_corrupt_stream_payloads_reject_fail_closed(self, event: object) -> None:
        budget = parse_root_budget(BUDGET_DOC)
        assert budget is not None
        with pytest.raises(BudgetError) as excinfo:
            ledger_state(budget, [event])  # type: ignore[arg-type]
        assert excinfo.value.code is BudgetErrorCode.LEDGER_STATE


# ---------------------------------------------------------------------------
# live fixtures: real disposable Postgres + real concurrent transactions
# ---------------------------------------------------------------------------


def _alembic_config() -> Config:
    return Config(os.path.join(BACKEND_ROOT, "alembic.ini"))


def _drop_app_tables(url: str) -> None:
    """Reset the fixture database to pre-bootstrap state."""
    with psycopg.connect(url) as conn:
        # Whole-schema reset (D09): FK-safe and immune to future
        # revisions (the enumerated drop list went stale across
        # migrations).
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


@pytest.fixture(scope="session")
def budget_url() -> str:
    """Disposable fixture database: the real Alembic chain only.

    Deliberately narrower than the delegation card's fixture: the ledger
    touches ONLY the facade-owned tables (``job_runs``/``job_events``);
    the engine-owned ``dbos`` schema is not created because no engine
    workflow runs in this suite.
    """
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the durable budget-ledger claims "
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


@pytest.fixture
def factory(budget_url: str) -> async_sessionmaker[AsyncSession]:
    """Yield the session factory; clean + dispose afterward."""
    _eng, sessions = create_app_engine(budget_url)

    async def _clean() -> None:
        async with sessions() as session:
            for table in (
                "job_schedule_fires",
                "job_steps",
                "job_events",
                "job_schedules",
                "job_runs",
                "approvals",
            ):
                await session.execute(text(f"DELETE FROM {table}"))
            await session.commit()

    yield sessions
    _run(_clean())
    _run(_eng.dispose())


@pytest.fixture
def insert_root(
    factory: async_sessionmaker[AsyncSession],
) -> Callable[..., object]:
    """Insert a budgeted (or unbudgeted) root ``job_runs`` row by SQL."""

    async def _insert(
        run_id: str = ROOT_ID, budget_doc: dict[str, object] | None = BUDGET_DOC
    ) -> None:
        async with factory() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO job_runs
                        (run_id, server_id, depth, profile_revision,
                         prompt_revision, model_profile, requested_grants,
                         root_budget, submitted_at, updated_at)
                    VALUES
                        (:run_id, :server_id, 0, 3, 7, 'fixture-fast',
                         '[]', CAST(:root_budget AS jsonb), now(), now())
                    """
                ),
                {
                    "run_id": uuid.UUID(run_id),
                    "server_id": uuid.UUID(SERVER_ID),
                    "root_budget": (
                        json.dumps(budget_doc) if budget_doc is not None else None
                    ),
                },
            )
            await session.commit()

    return lambda **kwargs: _run(_insert(**kwargs))


async def _count_events(
    factory: async_sessionmaker[AsyncSession],
    run_id: str,
    event_type: str | None = None,
) -> int:
    async with factory() as session:
        if event_type is None:
            return (
                await session.execute(
                    text("SELECT count(*) FROM job_events WHERE run_id = :r"),
                    {"r": uuid.UUID(run_id)},
                )
            ).scalar_one()
        return (
            await session.execute(
                text(
                    "SELECT count(*) FROM job_events "
                    "WHERE run_id = :r AND type = :t"
                ),
                {"r": uuid.UUID(run_id), "t": event_type},
            )
        ).scalar_one()


async def _event_rows(
    factory: async_sessionmaker[AsyncSession], run_id: str
) -> list[dict[str, object]]:
    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT seq, ts, type, v, data FROM job_events "
                    "WHERE run_id = :r ORDER BY seq"
                ),
                {"r": uuid.UUID(run_id)},
            )
        ).mappings().all()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# live: the atomic ledger against the real database
# ---------------------------------------------------------------------------


class TestAtomicLedger:
    def test_reserve_release_charge_reflects_in_the_snapshot(
        self, factory: async_sessionmaker[AsyncSession], insert_root: Callable[..., object]
    ) -> None:
        insert_root()
        ledger = BudgetLedger(factory)
        owner, child_a, child_b = _run_id(1), _run_id(2), _run_id(3)
        state = _run(ledger.areserve(ROOT_ID, owner, [child_a, child_b]))
        assert isinstance(state, BudgetState)
        assert state.outstanding == tuple(sorted([child_a, child_b]))
        assert state.remaining_active == 1
        state = _run(ledger.aconsume(ROOT_ID, child_a, steps=4, tokens=1_000))
        assert state.consumed_steps == 4 and state.consumed_tokens == 1_000
        state = _run(ledger.arelease(ROOT_ID, owner, [child_b]))
        assert state.outstanding == (child_a,)
        # The snapshot folds the persisted stream independently.
        snapshot = _run(ledger.asnapshot(ROOT_ID))
        assert snapshot.outstanding == (child_a,)
        assert snapshot.consumed_steps == 4 and snapshot.consumed_tokens == 1_000
        assert snapshot.remaining_active == 2
        assert snapshot.remaining_steps == 16 and snapshot.remaining_tokens == 4_000

    def test_sibling_race_cannot_oversubscribe_the_active_budget(
        self, factory: async_sessionmaker[AsyncSession], insert_root: Callable[..., object]
    ) -> None:
        insert_root()
        ledger = BudgetLedger(factory)
        owner = _run_id(1)

        async def _race() -> tuple[int, int]:
            # Six sibling delegations race for THREE active slots on
            # distinct connections; the root row lock serializes the
            # check-then-append pairs.
            results = await asyncio.gather(
                *(
                    ledger.areserve(ROOT_ID, owner, [_run_id(100 + index)])
                    for index in range(6)
                ),
                return_exceptions=True,
            )
            accepted = [item for item in results if isinstance(item, BudgetState)]
            rejected = [item for item in results if isinstance(item, BudgetError)]
            return len(accepted), len(rejected)

        accepted, rejected = _run(_race())
        assert (accepted, rejected) == (3, 3)
        snapshot = _run(ledger.asnapshot(ROOT_ID))
        assert snapshot.active_reserved == 3
        assert snapshot.remaining_active == 0
        # Exactly the three winning reservations persisted.
        assert (
            _run(
                _count_events(factory, ROOT_ID, BUDGET_RESERVED_EVENT_TYPE)
            )
            == 3
        )

    def test_all_race_rejections_name_the_exhausted_budget(
        self, factory: async_sessionmaker[AsyncSession], insert_root: Callable[..., object]
    ) -> None:
        insert_root()
        ledger = BudgetLedger(factory)
        owner = _run_id(1)

        async def _race() -> list[BudgetErrorCode]:
            results = await asyncio.gather(
                *(
                    ledger.areserve(ROOT_ID, owner, [_run_id(110 + index)])
                    for index in range(6)
                ),
                return_exceptions=True,
            )
            return sorted(
                {
                    item.code
                    for item in results
                    if isinstance(item, BudgetError)
                }
            )

        assert _run(_race()) == [BudgetErrorCode.ACTIVE_BUDGET_EXHAUSTED]

    def test_batch_race_cannot_partially_reserve(
        self, factory: async_sessionmaker[AsyncSession], insert_root: Callable[..., object]
    ) -> None:
        insert_root()
        ledger = BudgetLedger(factory)
        owner = _run_id(1)

        async def _race() -> tuple[list[BudgetState], list[BudgetError]]:
            # Two sibling batches of two children race for a remaining
            # budget of three: one whole batch wins, the other is
            # rejected whole (no partial reservation).
            results = await asyncio.gather(
                ledger.areserve(ROOT_ID, owner, [_run_id(200), _run_id(201)]),
                ledger.areserve(ROOT_ID, owner, [_run_id(202), _run_id(203)]),
                return_exceptions=True,
            )
            ok = [item for item in results if isinstance(item, BudgetState)]
            errors = [item for item in results if isinstance(item, BudgetError)]
            return ok, errors

        ok, errors = _run(_race())
        assert len(ok) == 1 and len(errors) == 1
        assert errors[0].code is BudgetErrorCode.ACTIVE_BUDGET_EXHAUSTED
        snapshot = _run(ledger.asnapshot(ROOT_ID))
        assert snapshot.active_reserved == 2
        assert snapshot.remaining_active == 1

    def test_consume_race_fails_closed_at_the_remaining_budget(
        self, factory: async_sessionmaker[AsyncSession], insert_root: Callable[..., object]
    ) -> None:
        insert_root()
        ledger = BudgetLedger(factory)

        async def _race() -> tuple[list[BudgetState], list[BudgetError]]:
            # max_steps is 20: three racing charges of 8 must land
            # exactly two (16) — the third is rejected whole.
            results = await asyncio.gather(
                *(
                    ledger.aconsume(ROOT_ID, _run_id(300 + index), steps=8)
                    for index in range(3)
                ),
                return_exceptions=True,
            )
            ok = [item for item in results if isinstance(item, BudgetState)]
            errors = [item for item in results if isinstance(item, BudgetError)]
            return ok, errors

        ok, errors = _run(_race())
        assert len(ok) == 2 and len(errors) == 1
        assert errors[0].code is BudgetErrorCode.BUDGET_EXHAUSTED
        snapshot = _run(ledger.asnapshot(ROOT_ID))
        assert snapshot.consumed_steps == 16
        assert snapshot.remaining_steps == 4

    def test_child_cap_can_never_exceed_the_root_budget(
        self, factory: async_sessionmaker[AsyncSession], insert_root: Callable[..., object]
    ) -> None:
        insert_root()
        ledger = BudgetLedger(factory)
        # A delegation of four children can never fit an active budget
        # of three, whatever the current state is.
        with pytest.raises(BudgetError) as excinfo:
            _run(
                ledger.areserve(
                    ROOT_ID, _run_id(1), [_run_id(400 + index) for index in range(4)]
                )
            )
        assert excinfo.value.code is BudgetErrorCode.CHILD_CAP_EXCEEDS_BUDGET
        snapshot = _run(ledger.asnapshot(ROOT_ID))
        assert snapshot.active_reserved == 0
        assert snapshot.consumed_steps == 0
        assert _run(_count_events(factory, ROOT_ID)) == 0

    def test_fanout_narrowing_rejects_whole_delegation(
        self, factory: async_sessionmaker[AsyncSession], insert_root: Callable[..., object]
    ) -> None:
        insert_root(budget_doc=dict(BUDGET_DOC, max_children=2))
        ledger = BudgetLedger(factory)
        # The active budget would allow three, but the fan-out cap is 2.
        with pytest.raises(BudgetError) as excinfo:
            _run(
                ledger.areserve(
                    ROOT_ID, _run_id(1), [_run_id(500 + index) for index in range(3)]
                )
            )
        assert excinfo.value.code is BudgetErrorCode.FANOUT_EXHAUSTED

    def test_release_of_unknown_or_released_child_is_rejected(
        self, factory: async_sessionmaker[AsyncSession], insert_root: Callable[..., object]
    ) -> None:
        insert_root()
        ledger = BudgetLedger(factory)
        owner, child = _run_id(1), _run_id(2)
        _run(ledger.areserve(ROOT_ID, owner, [child]))
        _run(ledger.arelease(ROOT_ID, owner, [child]))
        with pytest.raises(BudgetError) as excinfo:
            _run(ledger.arelease(ROOT_ID, owner, [child]))
        assert excinfo.value.code is BudgetErrorCode.LEDGER_STATE
        # A never-reserved child is equally rejected.
        with pytest.raises(BudgetError) as excinfo:
            _run(ledger.arelease(ROOT_ID, owner, [_run_id(3)]))
        assert excinfo.value.code is BudgetErrorCode.LEDGER_STATE
        snapshot = _run(ledger.asnapshot(ROOT_ID))
        assert snapshot.active_reserved == 0
        # The rejected releases wrote nothing.
        assert (
            _run(_count_events(factory, ROOT_ID, BUDGET_RELEASED_EVENT_TYPE)) == 1
        )

    def test_reservation_replay_is_idempotent_but_partial_overlap_is_not(
        self, factory: async_sessionmaker[AsyncSession], insert_root: Callable[..., object]
    ) -> None:
        insert_root()
        ledger = BudgetLedger(factory)
        owner, child_a, child_b, child_c = (
            _run_id(1),
            _run_id(2),
            _run_id(3),
            _run_id(4),
        )
        first = _run(ledger.areserve(ROOT_ID, owner, [child_a, child_b]))
        replay = _run(ledger.areserve(ROOT_ID, owner, [child_a, child_b]))
        assert isinstance(first, BudgetState) and isinstance(replay, BudgetState)
        assert replay == first
        with pytest.raises(BudgetError) as excinfo:
            _run(ledger.areserve(ROOT_ID, owner, [child_b, child_c]))
        assert excinfo.value.code is BudgetErrorCode.INVALID_RESERVATION

    def test_unbudgeted_root_is_an_explicit_noop(
        self, factory: async_sessionmaker[AsyncSession], insert_root: Callable[..., object]
    ) -> None:
        insert_root(budget_doc=None)
        ledger = BudgetLedger(factory)
        owner, child = _run_id(1), _run_id(2)
        for state in (
            _run(ledger.areserve(ROOT_ID, owner, [child])),
            _run(ledger.arelease(ROOT_ID, owner, [child])),
            _run(ledger.aconsume(ROOT_ID, child, steps=5, tokens=100)),
        ):
            assert isinstance(state, BudgetState)
            assert state.is_unbudgeted
            assert state.remaining_active is None
            assert state.remaining_steps is None
            assert state.remaining_tokens is None
        snapshot = _run(ledger.asnapshot(ROOT_ID))
        assert snapshot.is_unbudgeted
        assert _run(_count_events(factory, ROOT_ID)) == 0

    def test_missing_root_row_is_a_structured_rejection(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        ledger = BudgetLedger(factory)
        with pytest.raises(BudgetError) as excinfo:
            _run(ledger.areserve(ROOT_ID, _run_id(1), [_run_id(2)]))
        assert excinfo.value.code is BudgetErrorCode.RUN_NOT_FOUND
        with pytest.raises(BudgetError) as excinfo:
            _run(ledger.arelease(ROOT_ID, _run_id(1), [_run_id(2)]))
        assert excinfo.value.code is BudgetErrorCode.RUN_NOT_FOUND
        with pytest.raises(BudgetError) as excinfo:
            _run(ledger.aconsume(ROOT_ID, _run_id(1), steps=1))
        assert excinfo.value.code is BudgetErrorCode.RUN_NOT_FOUND
        with pytest.raises(BudgetError) as excinfo:
            _run(ledger.asnapshot(ROOT_ID))
        assert excinfo.value.code is BudgetErrorCode.RUN_NOT_FOUND

    def test_ledger_rejects_bad_wiring(self) -> None:
        with pytest.raises(BudgetError) as excinfo:
            BudgetLedger("not-a-session-factory")  # type: ignore[arg-type]
        assert excinfo.value.code is BudgetErrorCode.INVALID_WIRING

    def test_ledger_rejects_malformed_identities_and_charges(
        self, factory: async_sessionmaker[AsyncSession], insert_root: Callable[..., object]
    ) -> None:
        insert_root()
        ledger = BudgetLedger(factory)
        with pytest.raises(BudgetError) as excinfo:
            _run(ledger.areserve(ROOT_ID, _run_id(1), ["not-a-uuid"]))
        assert excinfo.value.code is BudgetErrorCode.INVALID_PAYLOAD
        with pytest.raises(BudgetError) as excinfo:
            _run(ledger.areserve(ROOT_ID, _run_id(1), [_run_id(2), _run_id(2)]))
        assert excinfo.value.code is BudgetErrorCode.INVALID_PAYLOAD
        with pytest.raises(BudgetError) as excinfo:
            _run(ledger.aconsume(ROOT_ID, _run_id(1), steps=-1))
        assert excinfo.value.code is BudgetErrorCode.INVALID_PAYLOAD
        with pytest.raises(BudgetError) as excinfo:
            _run(ledger.aconsume(ROOT_ID, _run_id(1), steps=0, tokens=0))
        assert excinfo.value.code is BudgetErrorCode.INVALID_PAYLOAD
        # Nothing persisted on any rejection.
        assert _run(_count_events(factory, ROOT_ID)) == 0

    def test_cancel_cascade_refund_preserves_child_evidence(
        self, factory: async_sessionmaker[AsyncSession], insert_root: Callable[..., object]
    ) -> None:
        """The ledger half of the frozen cancellation cascade.

        A cancelled branch refunds its OUTSTANDING reservations; the
        charges it already incurred stay (the preserved, value-free child
        evidence on the root stream) and the freed budget is reservable
        again by the surviving siblings.
        """
        insert_root()
        ledger = BudgetLedger(factory)
        owner_a, owner_b = _run_id(1), _run_id(2)
        branch_a = [_run_id(600), _run_id(601)]
        branch_b = [_run_id(602)]
        _run(ledger.areserve(ROOT_ID, owner_a, branch_a))
        _run(ledger.areserve(ROOT_ID, owner_b, branch_b))
        _run(ledger.aconsume(ROOT_ID, _run_id(600), steps=4, tokens=1_000))
        # Owner A's branch is cancelled: release its whole reservation.
        _run(ledger.arelease(ROOT_ID, owner_a, branch_a))
        snapshot = _run(ledger.asnapshot(ROOT_ID))
        assert snapshot.outstanding == (branch_b[0],)
        assert snapshot.consumed_steps == 4 and snapshot.consumed_tokens == 1_000
        assert snapshot.remaining_active == 2
        assert snapshot.remaining_steps == 16
        # The freed slots are reservable again (budget not leaked).
        state = _run(ledger.areserve(ROOT_ID, owner_b, [_run_id(603), _run_id(604)]))
        assert isinstance(state, BudgetState)
        assert len(state.outstanding) == 3
        # The preserved evidence: the cancelled branch's charge events
        # remain on the root stream (identity + counts, never content).
        rows = _run(_event_rows(factory, ROOT_ID))
        charges = [
            row for row in rows if row["type"] == BUDGET_CHARGED_EVENT_TYPE
        ]
        assert len(charges) == 1
        charge_data = charges[0]["data"]
        assert charge_data["run"] == _run_id(600)
        assert charge_data["steps"] == 4 and charge_data["tokens"] == 1_000

    def test_budget_events_carry_the_frozen_envelope(
        self, factory: async_sessionmaker[AsyncSession], insert_root: Callable[..., object]
    ) -> None:
        insert_root()
        ledger = BudgetLedger(factory)
        owner, child = _run_id(1), _run_id(2)
        _run(ledger.areserve(ROOT_ID, owner, [child]))
        _run(ledger.aconsume(ROOT_ID, child, steps=1, tokens=10))
        rows = _run(_event_rows(factory, ROOT_ID))
        assert [row["type"] for row in rows] == [
            BUDGET_RESERVED_EVENT_TYPE,
            BUDGET_CHARGED_EVENT_TYPE,
        ]
        previous = 0
        for row in rows:
            assert row["v"] == EVENT_PAYLOAD_VERSION
            event = parse_job_event(
                {
                    "seq": row["seq"],
                    "run_id": ROOT_ID,
                    "ts": row["ts"].isoformat(),
                    "type": row["type"],
                    "v": row["v"],
                    "data": row["data"],
                }
            )
            assert event.seq == previous + 1
            previous = event.seq
            # Value-free: identities and integer counts only.
            for value in event.data.values():
                assert isinstance(value, (str, int, list))
                if isinstance(value, list):
                    assert all(isinstance(item, str) for item in value)
