"""Focused tests for the run lifecycle (card 05-run-lifecycle).

Acceptance (BRIEF):

1. Legal state transitions and monotonic persisted event cursor; terminal
   state cannot be overwritten by a stale result. The transition table is
   exactly the frozen C05 rule 2 diagram; every accepted transition is one
   ``run.transition`` event with the next per-run stream ``seq``; two
   concurrent terminal transitions of one run serialize on the ``job_runs``
   row lock and the loser is rejected — the FIRST terminal value sticks.
2. Resume/replay after process restart yields the same ordered state
   (state is replayed from the persisted log, never cached in the process);
   rejections keep the safe value-free error envelope (C01 rule 3).

Boundary discipline (test policy): cursor, race and restart claims run
against a real disposable PostgreSQL fixture (``MAI_TEST_DATABASE_URL``)
with the real Alembic chain and REAL worker subprocesses for the live
engine-interplay scenarios (kill/restart + cancel). No mock re-implements
a guard under test: the concurrency guard proven here is the production
transaction path itself. Fixtures carry only tmp-namespace paths and
marker words — never secret values.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.contracts.jobs import (
    EVENT_PAYLOAD_VERSION,
    TERMINAL_RUN_STATES,
    JobEvent,
    RunState,
    event_to_wire,
    parse_job_event,
)
from mai.database import create_app_engine
from mai.jobs import engine
from mai.jobs.engine import EngineClient
from mai.tag_store import (
    RESERVED_UNCLASSIFIED_TAG_ID,
    SYSTEM_ASSIGNMENT_SOURCE,
    TagErrorCode,
    TagStoreError,
    acreate_tag,
)
from mai.jobs.lifecycle import (
    LEGAL_RUN_TRANSITIONS,
    TRANSITION_EVENT_TYPE,
    RunLifecycle,
    RunLifecycleError,
    RunLifecycleErrorCode,
    current_state,
    is_legal_transition,
    make_transition_event,
    states_in_order,
    validate_transition,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Scratch namespace per runtime-environment ADR D2/D4 (recorded + cleaned
# in the card evidence). Each live test gets a fresh subdirectory.
FIXTURE_ROOT = Path("/tmp/mai-fxt-05-run-lifecycle")

READY_TIMEOUT = 90.0
STEP_TIMEOUT = 60.0

# Frozen fixture identities (canonical lowercase UUID strings, C01 rule 1).
SERVER_ID = "5d5349b2-6a1d-4f0e-9c3a-1b2c3d4e5f60"
RUN_ID = "7e6c1a34-9a2b-4c3d-8e5f-1029384756af"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _event(
    seq: int,
    *,
    run_id: str = RUN_ID,
    type: str = TRANSITION_EVENT_TYPE,  # noqa: A002 - envelope field name
    data: dict[str, object],
    ts: str = "2026-10-15T01:02:03.123456Z",
) -> JobEvent:
    """Build one valid envelope for replay units."""
    return parse_job_event(
        {
            "seq": seq,
            "run_id": run_id,
            "ts": ts,
            "type": type,
            "v": EVENT_PAYLOAD_VERSION,
            "data": data,
        }
    )


def _chain(*states: RunState, start_seq: int = 1) -> list[JobEvent]:
    """Anchor at the submit edge, then one event per state."""
    events = [_event(start_seq, data={"from": None, "to": RunState.QUEUED.value})]
    previous = RunState.QUEUED
    for offset, state in enumerate(states, start=1):
        events.append(
            _event(start_seq + offset, data={"from": previous.value, "to": state.value})
        )
        previous = state
    return events


def _wait_until(predicate: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.5)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


# ---------------------------------------------------------------------------
# live-fixture helpers: real disposable Postgres + REAL worker processes
# ---------------------------------------------------------------------------

HANDLER_MODULE_NAME = "lifecycle_scenario_handlers"

HANDLER_MODULE_SOURCE = '''
"""Scenario handlers for 05-run-lifecycle: harmless, journaled effects."""

import json
import os
import time

from dbos import DBOS

from mai.jobs import engine


@DBOS.step()
def _mark(path: str, marker: str) -> None:
    with open(path, "a") as f:
        f.write(marker + "\\n")


@DBOS.step()
def _gate(path: str, timeout: float) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return "released"
        time.sleep(0.1)
    raise RuntimeError("gate timeout")


def echo(payload_json: str) -> str:
    payload = json.loads(payload_json)
    _mark(payload["journal"], payload["marker"])
    return payload["marker"]


def gated(payload_json: str) -> str:
    payload = json.loads(payload_json)
    _mark(payload["journal"], payload["started"])
    _gate(payload["gate"], payload["timeout"])
    _mark(payload["journal"], payload["done"])
    return "done"


engine.register_job_handler("echo", echo)
engine.register_job_handler("gated", gated)
'''


def _journal(scenario: Path) -> Path:
    return scenario / "journal.log"


def _journal_count(scenario: Path, marker: str) -> int:
    try:
        lines = _journal(scenario).read_text().splitlines()
    except FileNotFoundError:
        return 0
    return sum(1 for line in lines if line == marker)


def _spawn_worker(
    url: str, handler_dir: Path, scenario: Path, tag: str
) -> subprocess.Popen:
    """Launch a REAL worker process against the fixture database."""
    env = dict(os.environ)
    env.pop(LIVE_URL_VAR, None)  # workers speak the production env contract
    env["MAI_DATABASE_URL"] = url
    env["MAI_WORKER_HANDLERS"] = HANDLER_MODULE_NAME
    env["PYTHONPATH"] = os.pathsep.join(
        [str(handler_dir), *(p for p in [env.get("PYTHONPATH")] if p)]
    )
    log = open(scenario / f"{tag}.log", "w")  # noqa: SIM115 - closed with proc
    return subprocess.Popen(
        [sys.executable, "-m", "mai.jobs.worker", "--concurrency", "2"],
        cwd=str(BACKEND_ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def _kill9(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=30)


def _stop(proc: subprocess.Popen) -> int:
    proc.send_signal(signal.SIGTERM)
    return proc.wait(timeout=30)


def _alembic_config() -> Config:
    return Config(os.path.join(BACKEND_ROOT, "alembic.ini"))


def _drop_app_tables(url: str) -> None:
    """Reset the fixture database to pre-bootstrap state.

    Full app set at the chain tip — the conversation domain (0008) AND
    the agent-task domain (0009) are listed beside the jobs/approval
    domains (chain-tip reconciliation, the ``test_agent_task_run``
    DOMAIN_TABLES convention), so the zero-to-head fixture stays
    order-independent after later suites ran on the same shared
    database: a partial list would leave dependent rows/tables behind
    and break the next suite's ``upgrade(head)`` or DELETE teardown.
    """
    with psycopg.connect(url) as conn:
        conn.execute(
            "DROP TABLE IF EXISTS tags, job_tags, job_resource_locks, "
            "job_schedule_fires, job_schedules, job_events, job_steps, "
            "job_runs, approvals, "
            "profile_projection_journal, profile_projection, admin_sessions, "
            "admin_credential, notification_alerts, observation_findings, "
            "server_observations, knowledge_doc_chunks, knowledge_documents, "
            "application_projection_journal, application_projection, "
            "conversation_steering, conversation_runs, conversation_tags, "
            "conversation_messages, conversations, "
            "agent_task_completions, agent_task_revisions, "
            "agent_task_evidence, agent_task_criteria, "
            "agent_task_constraints, agent_task_steps, agent_tasks, "
            "alembic_version"
        )
        conn.execute("DROP SEQUENCE IF EXISTS server_observations_seq")
        conn.commit()


@pytest.fixture(scope="session")
def lifecycle_url() -> str:
    """Disposable fixture database: Alembic chain + engine-owned schema.

    The DBOS ``dbos`` schema is created by a full engine launch (never by
    the client and never by Alembic), so the session bootstraps exactly the
    way production does: migrate, one launch + clean shutdown, then
    control-plane code runs client-only.
    """
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the lifecycle claims need a real "
            "disposable Postgres fixture"
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
    engine.launch_worker(url)  # creates the engine-owned dbos schema
    engine.shutdown()
    return url


@pytest.fixture(scope="session")
def client(lifecycle_url: str) -> EngineClient:
    """The control-plane seam (what the API process uses)."""
    with EngineClient(lifecycle_url) as facade:
        yield facade


@pytest.fixture
async def factory(lifecycle_url: str) -> async_sessionmaker[AsyncSession]:
    eng, sessions = create_app_engine(lifecycle_url)
    yield sessions
    # Each data test starts from an empty jobs domain (session-scoped
    # fixture database): delete children before parents.
    async with sessions() as session:
        for statement in (
            "DELETE FROM job_schedule_fires",
            "DELETE FROM job_steps",
            "DELETE FROM job_events",
            "DELETE FROM job_schedules",
            "DELETE FROM job_runs",
        ):
            await session.execute(text(statement))
        await session.commit()
    await eng.dispose()


@pytest.fixture
async def insert_run(factory: async_sessionmaker[AsyncSession]):
    """Insert a ``job_runs`` row (the submit owner's row, by SQL helper)."""

    async def _insert(run_id: str = RUN_ID) -> None:
        async with factory() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO job_runs
                        (run_id, server_id, depth, profile_revision,
                         prompt_revision, model_profile, requested_grants,
                         submitted_at, updated_at)
                    VALUES
                        (:run_id, :server_id, 0, 3, 7, 'build-agent',
                         '["shell.exec"]', now(), now())
                    """
                ),
                {"run_id": uuid.UUID(run_id), "server_id": uuid.UUID(SERVER_ID)},
            )
            # The 0011 mandatory-tag invariant requires the classification
            # in the SAME transaction as the run row (history-tags W5B1):
            # seed the reserved unclassified tag like the real submit writer.
            await session.execute(
                text(
                    """
                    INSERT INTO job_tags
                        (job_id, tag_id, assigned_by, assignment_source)
                    VALUES (:run_id, :reserved, :assigned_by, :source)
                    """
                ),
                {
                    "run_id": uuid.UUID(run_id),
                    "reserved": uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID),
                    "assigned_by": "",
                    "source": "system",
                },
            )
            await session.commit()

    return _insert


@pytest.fixture
def scenario() -> Path:
    """Fresh scratch namespace per live test; always cleaned up."""
    root = FIXTURE_ROOT / uuid.uuid4().hex
    (root / "handlers").mkdir(parents=True)
    (root / "handlers" / f"{HANDLER_MODULE_NAME}.py").write_text(HANDLER_MODULE_SOURCE)
    _journal(root).touch()
    yield root
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# unit half: the frozen transition table (no engine, no DB)
# ---------------------------------------------------------------------------


class TestFrozenTransitionTable:
    """Exactly the C05 rule 2 diagram edges — the stale-result guard."""

    LEGAL_EDGES = [
        (None, RunState.QUEUED),  # submit (idempotency key = workflow id)
        (RunState.QUEUED, RunState.RUNNING),  # engine PENDING
        (RunState.QUEUED, RunState.FAILED),
        (RunState.QUEUED, RunState.CANCELLED),
        (RunState.RUNNING, RunState.WAITING_APPROVAL),  # capability ask-decision
        (RunState.RUNNING, RunState.WAITING_CHILDREN),  # delegation children
        (RunState.RUNNING, RunState.RECONCILING),  # outcome_unknown present
        (RunState.RUNNING, RunState.SUCCEEDED),
        (RunState.RUNNING, RunState.FAILED),
        (RunState.RUNNING, RunState.CANCELLED),  # cancel lands
        (RunState.WAITING_APPROVAL, RunState.RUNNING),  # approval granted
        (RunState.WAITING_CHILDREN, RunState.RUNNING),  # children resolved
        (RunState.RECONCILING, RunState.RUNNING),  # reconciliation resumes
        (RunState.RECONCILING, RunState.FAILED),
    ]

    @pytest.mark.parametrize(("current", "target"), LEGAL_EDGES)
    def test_diagram_edges_are_legal(
        self, current: RunState | None, target: RunState
    ) -> None:
        assert is_legal_transition(current, target) is True
        validate_transition(current, target)  # must not raise

    def test_every_other_pair_is_rejected(self) -> None:
        legal = {(current, target) for current, target in self.LEGAL_EDGES}
        for current in [None, *RunState]:
            for target in RunState:
                if (current, target) in legal:
                    continue
                assert is_legal_transition(current, target) is False
                with pytest.raises(RunLifecycleError) as excinfo:
                    validate_transition(current, target)
                expected = (
                    RunLifecycleErrorCode.TERMINAL_IMMUTABLE
                    if current in TERMINAL_RUN_STATES
                    else RunLifecycleErrorCode.INVALID_TRANSITION
                )
                assert excinfo.value.code is expected

    def test_terminal_states_have_no_out_edges(self) -> None:
        for terminal in TERMINAL_RUN_STATES:
            assert LEGAL_RUN_TRANSITIONS[terminal] == frozenset()
            for target in RunState:
                with pytest.raises(RunLifecycleError) as excinfo:
                    validate_transition(terminal, target)
                assert excinfo.value.code is RunLifecycleErrorCode.TERMINAL_IMMUTABLE

    def test_table_is_total_over_the_frozen_vocabulary(self) -> None:
        """Every anchor has an entry; terminals are present but out-edge-free."""
        assert set(LEGAL_RUN_TRANSITIONS) == {None, *RunState}

    def test_cancel_requested_is_never_a_transition(self) -> None:
        """Guard rule 3: requested ≠ verified — there is no such state."""
        assert "cancel_requested" not in {state.value for state in RunState}
        # The cancel REQUEST is recorded by the facade (job_runs column);
        # the state only moves to cancelled when the engine lands it.

    def test_rejections_carry_the_safe_envelope(self) -> None:
        with pytest.raises(RunLifecycleError) as excinfo:
            validate_transition(RunState.SUCCEEDED, RunState.RUNNING)
        assert str(excinfo.value).startswith("terminal_immutable: ")
        assert excinfo.value.detail  # fixed, value-free reason present
        with pytest.raises(RunLifecycleError) as excinfo:
            validate_transition(RunState.QUEUED, RunState.WAITING_APPROVAL)
        assert str(excinfo.value).startswith("invalid_transition: ")

    def test_non_state_target_is_rejected(self) -> None:
        for bad in (None, "running", 42, True):
            with pytest.raises(RunLifecycleError) as excinfo:
                validate_transition(RunState.QUEUED, bad)  # type: ignore[arg-type]
            assert excinfo.value.code is RunLifecycleErrorCode.INVALID_TRANSITION


# ---------------------------------------------------------------------------
# unit half: replay (acceptance 2, pure)
# ---------------------------------------------------------------------------


class TestReplay:
    def test_ordered_history_replays(self) -> None:
        states = states_in_order(
            _chain(
                RunState.RUNNING,
                RunState.WAITING_APPROVAL,
                RunState.RUNNING,
                RunState.SUCCEEDED,
            )
        )
        assert states == (
            RunState.QUEUED,
            RunState.RUNNING,
            RunState.WAITING_APPROVAL,
            RunState.RUNNING,
            RunState.SUCCEEDED,
        )
        assert states_in_order(_chain()) == (RunState.QUEUED,)

    def test_current_state_is_the_last_replayed_state(self) -> None:
        assert current_state([]) is None
        assert current_state(_chain()) is RunState.QUEUED
        assert (
            current_state(
                _chain(RunState.RUNNING, RunState.WAITING_CHILDREN, RunState.RUNNING)
            )
            is RunState.RUNNING
        )

    def test_input_order_is_irrelevant(self) -> None:
        events = _chain(RunState.RUNNING, RunState.FAILED)
        shuffled = [events[2], events[0], events[1]]
        assert states_in_order(shuffled) == states_in_order(events)

    def test_foreign_events_are_ignored(self) -> None:
        """Step events (and other producers) share the stream; replay skips."""
        step_id = str(uuid.uuid4())
        mixed = [
            _event(1, data={"from": None, "to": "queued"}),
            _event(2, type="step.started", data={"step_id": step_id}),
            _event(3, data={"from": "queued", "to": "running"}),
            _event(4, type="step.completed", data={"step_id": step_id}),
            _event(5, data={"from": "running", "to": "succeeded"}),
        ]
        assert states_in_order(mixed) == (
            RunState.QUEUED,
            RunState.RUNNING,
            RunState.SUCCEEDED,
        )

    def test_missing_submit_anchor_fails_closed(self) -> None:
        events = [_event(1, data={"from": "queued", "to": "running"})]
        with pytest.raises(RunLifecycleError) as excinfo:
            states_in_order(events)
        assert excinfo.value.code is RunLifecycleErrorCode.INVALID_EVENT_DATA

    def test_non_contiguous_chain_fails_closed(self) -> None:
        events = [
            _event(1, data={"from": None, "to": "queued"}),
            _event(2, data={"from": "queued", "to": "running"}),
            _event(3, data={"from": "queued", "to": "failed"}),  # gap/tamper
        ]
        with pytest.raises(RunLifecycleError) as excinfo:
            states_in_order(events)
        assert excinfo.value.code is RunLifecycleErrorCode.INVALID_EVENT_DATA

    @pytest.mark.parametrize(
        "data",
        [
            {"to": "running"},  # missing 'from'
            {"from": None},  # missing 'to'
            {"from": None, "to": "running", "extra": 1},  # unknown key
            {"from": 42, "to": "running"},  # non-string 'from'
            {"from": None, "to": 42},  # non-string 'to'
            {"from": "RUNNING", "to": "succeeded"},  # non-vocabulary value
            {"from": None, "to": "QUEUED"},  # case-sensitive vocabulary
            {"from": None, "to": "cancel_requested"},  # never a run state
        ],
    )
    def test_malformed_payload_fails_closed(self, data: dict[str, object]) -> None:
        with pytest.raises(RunLifecycleError) as excinfo:
            states_in_order([_event(1, data=data)])
        assert excinfo.value.code is RunLifecycleErrorCode.INVALID_EVENT_DATA

    def test_transition_event_round_trips_the_frozen_wire(self) -> None:
        event = make_transition_event(
            RUN_ID,
            4,
            datetime(2026, 10, 15, 1, 2, 3, tzinfo=timezone.utc),
            None,
            RunState.QUEUED,
        )
        assert event.type == TRANSITION_EVENT_TYPE
        assert event.v == EVENT_PAYLOAD_VERSION
        wire = event_to_wire(event)
        assert set(wire) == {"seq", "run_id", "ts", "type", "v", "data"}
        assert wire["ts"].endswith("Z")
        parsed = parse_job_event(json.loads(json.dumps(wire)))
        assert parsed.seq == 4
        assert parsed.type == TRANSITION_EVENT_TYPE
        assert parsed.v == EVENT_PAYLOAD_VERSION
        assert parsed.data == {"from": None, "to": "queued"}


# ---------------------------------------------------------------------------
# DB half: persisted cursor + races (real disposable Postgres)
# ---------------------------------------------------------------------------


class TestPersistedCursor:
    @pytest.mark.anyio
    async def test_sequential_transitions_get_monotonic_cursor(
        self,
        factory: async_sessionmaker[AsyncSession],
        insert_run: Callable[..., object],
    ) -> None:
        await insert_run()
        lifecycle = RunLifecycle(factory)
        chain = [
            RunState.QUEUED,
            RunState.RUNNING,
            RunState.WAITING_APPROVAL,
            RunState.RUNNING,
            RunState.SUCCEEDED,
        ]
        seen: list[JobEvent] = []
        previous_ts: datetime | None = None
        for target in chain:
            event = await lifecycle.record_transition(RUN_ID, target)
            seen.append(event)
            assert event.seq == len(seen)  # cursor: 1, 2, 3, ...
            assert event.type == TRANSITION_EVENT_TYPE
            assert event.v == EVENT_PAYLOAD_VERSION
            if previous_ts is not None:
                assert event.ts >= previous_ts
            previous_ts = event.ts
        assert await lifecycle.replay_states(RUN_ID) == tuple(chain)
        assert await lifecycle.current_state(RUN_ID) is RunState.SUCCEEDED
        stream = await lifecycle.event_stream(RUN_ID)
        assert [event.seq for event in stream] == [1, 2, 3, 4, 5]
        assert [event.data for event in stream] == [
            {"from": None, "to": "queued"},
            {"from": "queued", "to": "running"},
            {"from": "running", "to": "waiting_approval"},
            {"from": "waiting_approval", "to": "running"},
            {"from": "running", "to": "succeeded"},
        ]

    @pytest.mark.anyio
    async def test_rejection_writes_nothing(
        self,
        factory: async_sessionmaker[AsyncSession],
        insert_run: Callable[..., object],
    ) -> None:
        await insert_run()
        lifecycle = RunLifecycle(factory)
        await lifecycle.record_transition(RUN_ID, RunState.QUEUED)
        before = await lifecycle.event_stream(RUN_ID)
        with pytest.raises(RunLifecycleError) as excinfo:
            # Not an edge of the diagram: queued never waits directly.
            await lifecycle.record_transition(RUN_ID, RunState.WAITING_APPROVAL)
        assert excinfo.value.code is RunLifecycleErrorCode.INVALID_TRANSITION
        assert await lifecycle.event_stream(RUN_ID) == before

    @pytest.mark.anyio
    async def test_unknown_run_is_rejected(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        lifecycle = RunLifecycle(factory)
        with pytest.raises(RunLifecycleError) as excinfo:
            await lifecycle.record_transition(RUN_ID, RunState.QUEUED)
        assert excinfo.value.code is RunLifecycleErrorCode.RUN_NOT_FOUND
        assert await lifecycle.event_stream(RUN_ID) == ()

    @pytest.mark.anyio
    async def test_invalid_run_id_is_value_free(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        lifecycle = RunLifecycle(factory)
        # Non-empty inputs must not leak into the value-free message.
        for bad in ("not-a-uuid", RUN_ID.upper(), 42):
            with pytest.raises(RunLifecycleError) as excinfo:
                await lifecycle.record_transition(bad, RunState.QUEUED)  # type: ignore[arg-type]
            assert excinfo.value.code is RunLifecycleErrorCode.INVALID_RUN_ID
            assert str(bad) not in str(excinfo.value)
        # The empty string only asserts the code ('' is contained in anything).
        with pytest.raises(RunLifecycleError) as excinfo:
            await lifecycle.record_transition("", RunState.QUEUED)
        assert excinfo.value.code is RunLifecycleErrorCode.INVALID_RUN_ID

    @pytest.mark.anyio
    async def test_cursor_is_shared_with_other_producers(
        self,
        factory: async_sessionmaker[AsyncSession],
        insert_run: Callable[..., object],
    ) -> None:
        await insert_run()
        step_id = str(uuid.uuid4())
        async with factory() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO job_events
                        (event_id, run_id, seq, ts, type, v, data)
                    VALUES
                        (:event_id, :run_id, 1, now(), 'step.completed', :v, :data)
                    """
                ),
                {
                    "event_id": uuid.uuid4(),
                    "run_id": uuid.UUID(RUN_ID),
                    "v": EVENT_PAYLOAD_VERSION,
                    "data": json.dumps({"step_id": step_id}),
                },
            )
            await session.commit()
        lifecycle = RunLifecycle(factory)
        event = await lifecycle.record_transition(RUN_ID, RunState.QUEUED)
        assert event.seq == 2  # the whole stream is the cursor
        assert await lifecycle.replay_states(RUN_ID) == (RunState.QUEUED,)

    @pytest.mark.anyio
    async def test_explicit_timestamp_normalizes_and_validates(
        self,
        factory: async_sessionmaker[AsyncSession],
        insert_run: Callable[..., object],
    ) -> None:
        await insert_run()
        lifecycle = RunLifecycle(factory)
        event = await lifecycle.record_transition(
            RUN_ID,
            RunState.QUEUED,
            occurred_at=datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert event.ts == datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(RunLifecycleError) as excinfo:
            await lifecycle.record_transition(
                RUN_ID, RunState.RUNNING, occurred_at=datetime(2026, 10, 15, 12, 0, 0)
            )
        assert excinfo.value.code is RunLifecycleErrorCode.INVALID_TIMESTAMP


class TestStaleResultRace:
    """The real-boundary race: concurrent terminal transitions, one run.

    Both go through the production transaction path (row lock → replay →
    validate → append). Exactly one commits; the loser replays the winner's
    committed terminal state and is rejected — the FIRST terminal value
    sticks, no matter which one wins the race (acceptance 1).
    """

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("first", "second"),
        [
            (RunState.CANCELLED, RunState.SUCCEEDED),  # stale completion
            (RunState.SUCCEEDED, RunState.CANCELLED),  # stale cancellation
        ],
    )
    async def test_first_terminal_value_sticks(
        self,
        factory: async_sessionmaker[AsyncSession],
        insert_run: Callable[..., object],
        first: RunState,
        second: RunState,
    ) -> None:
        await insert_run()
        lifecycle = RunLifecycle(factory)
        await lifecycle.record_transition(RUN_ID, RunState.QUEUED)
        await lifecycle.record_transition(RUN_ID, RunState.RUNNING)

        racer_a = RunLifecycle(factory)
        racer_b = RunLifecycle(factory)
        results = await asyncio.gather(
            racer_a.record_transition(RUN_ID, first),
            racer_b.record_transition(RUN_ID, second),
            return_exceptions=True,
        )
        failures = [r for r in results if isinstance(r, BaseException)]
        winners = [r for r in results if not isinstance(r, BaseException)]
        assert len(winners) == 1 and len(failures) == 1
        assert isinstance(failures[0], RunLifecycleError)
        assert failures[0].code is RunLifecycleErrorCode.TERMINAL_IMMUTABLE

        final = await lifecycle.current_state(RUN_ID)
        assert final in TERMINAL_RUN_STATES
        assert str(winners[0].data["to"]) == (final.value if final else None)
        # The cursor stayed monotonic: 3 events, no duplicates, no gaps.
        stream = await lifecycle.event_stream(RUN_ID)
        assert [event.seq for event in stream] == [1, 2, 3]

    @pytest.mark.anyio
    async def test_duplicate_stale_completions_land_once(
        self,
        factory: async_sessionmaker[AsyncSession],
        insert_run: Callable[..., object],
    ) -> None:
        await insert_run()
        lifecycle = RunLifecycle(factory)
        await lifecycle.record_transition(RUN_ID, RunState.QUEUED)
        await lifecycle.record_transition(RUN_ID, RunState.RUNNING)

        results = await asyncio.gather(
            RunLifecycle(factory).record_transition(RUN_ID, RunState.SUCCEEDED),
            RunLifecycle(factory).record_transition(RUN_ID, RunState.SUCCEEDED),
            RunLifecycle(factory).record_transition(RUN_ID, RunState.FAILED),
            return_exceptions=True,
        )
        winners = [r for r in results if not isinstance(r, BaseException)]
        failures = [r for r in results if isinstance(r, BaseException)]
        assert len(winners) == 1 and len(failures) == 2
        assert all(
            isinstance(f, RunLifecycleError)
            and f.code is RunLifecycleErrorCode.TERMINAL_IMMUTABLE
            for f in failures
        )
        assert await lifecycle.current_state(RUN_ID) is RunState.SUCCEEDED
        assert len(await lifecycle.event_stream(RUN_ID)) == 3


class TestRestartReplay:
    @pytest.mark.anyio
    async def test_new_process_replays_identical_ordered_state(
        self,
        lifecycle_url: str,
        factory: async_sessionmaker[AsyncSession],
        insert_run: Callable[..., object],
    ) -> None:
        await insert_run()
        before_restart = RunLifecycle(factory)
        chain = [
            RunState.QUEUED,
            RunState.RUNNING,
            RunState.WAITING_CHILDREN,
            RunState.RUNNING,
            RunState.FAILED,
        ]
        for target in chain:
            await before_restart.record_transition(RUN_ID, target)
        expected_stream = await before_restart.event_stream(RUN_ID)
        expected_states = await before_restart.replay_states(RUN_ID)

        # A restarted process: fresh engine, fresh factory, fresh lifecycle —
        # nothing in memory; the database log is the only source.
        fresh_engine, fresh_factory = create_app_engine(lifecycle_url)
        try:
            after_restart = RunLifecycle(fresh_factory)
            assert await after_restart.event_stream(RUN_ID) == expected_stream
            assert await after_restart.replay_states(RUN_ID) == expected_states
            assert await after_restart.current_state(RUN_ID) is RunState.FAILED
            with pytest.raises(RunLifecycleError) as excinfo:
                await after_restart.record_transition(RUN_ID, RunState.SUCCEEDED)
            assert excinfo.value.code is RunLifecycleErrorCode.TERMINAL_IMMUTABLE
        finally:
            await fresh_engine.dispose()


# ---------------------------------------------------------------------------
# live half: real engine + REAL worker processes (the brief's scenario)
# ---------------------------------------------------------------------------


class TestLiveRunLifecycle:
    @pytest.mark.anyio
    async def test_run_traverses_states_and_replays_across_worker_restart(
        self,
        lifecycle_url: str,
        client: EngineClient,
        factory: async_sessionmaker[AsyncSession],
        insert_run: Callable[..., object],
        scenario: Path,
    ) -> None:
        """queued → running → (kill -9) → restart → succeeded, replayed."""
        # A fresh run ID per invocation: the run_id IS the engine workflow
        # ID (reject policy), which outlives the cleaned domain tables.
        run_id = str(uuid.uuid4())
        await insert_run(run_id)
        lifecycle = RunLifecycle(factory)
        assert await lifecycle.current_state(run_id) is None

        await lifecycle.record_transition(run_id, RunState.QUEUED)
        gate = scenario / "gate"
        client.enqueue_run(
            run_id,
            "gated",
            json.dumps(
                {
                    "journal": str(_journal(scenario)),
                    "started": "STARTED",
                    "done": "DONE",
                    "gate": str(gate),
                    "timeout": 300.0,
                }
            ),
        )
        assert client.run_state(run_id) is RunState.QUEUED

        worker1 = _spawn_worker(
            lifecycle_url, scenario / "handlers", scenario, "w-live-1"
        )
        try:
            _wait_until(
                lambda: _journal_count(scenario, "STARTED") == 1,
                STEP_TIMEOUT,
                "worker claim + first step",
            )
            _wait_until(
                lambda: client.run_state(run_id) is RunState.RUNNING,
                READY_TIMEOUT,
                "engine PENDING",
            )
            await lifecycle.record_transition(run_id, RunState.RUNNING)
            assert await lifecycle.current_state(run_id) is RunState.RUNNING
            # A duplicate observation of the same state is not a transition:
            with pytest.raises(RunLifecycleError) as excinfo:
                await lifecycle.record_transition(run_id, RunState.RUNNING)
            assert excinfo.value.code is RunLifecycleErrorCode.INVALID_TRANSITION
            assert len(await lifecycle.event_stream(run_id)) == 2
        finally:
            _kill9(worker1)

        # Engine truth while nobody runs it: still PENDING → running; the
        # facade log is persisted, not process state.
        assert client.run_state(run_id) is RunState.RUNNING
        before = await lifecycle.event_stream(run_id)
        assert await lifecycle.replay_states(run_id) == (
            RunState.QUEUED,
            RunState.RUNNING,
        )

        gate.touch()
        worker2 = _spawn_worker(
            lifecycle_url, scenario / "handlers", scenario, "w-live-2"
        )
        try:
            _wait_until(
                lambda: client.run_state(run_id) is RunState.SUCCEEDED,
                READY_TIMEOUT,
                "recovered run success",
            )
            assert _journal_count(scenario, "STARTED") == 1  # checkpoint replay
            assert _journal_count(scenario, "DONE") == 1  # in-flight step once
            await lifecycle.record_transition(run_id, RunState.SUCCEEDED)
            assert await lifecycle.replay_states(run_id) == (
                RunState.QUEUED,
                RunState.RUNNING,
                RunState.SUCCEEDED,
            )

            # Restart replay: a fresh process observes the identical stream
            # (the one pre-restart event plus the terminal event appended
            # after recovery — ordered, contiguous, nothing duplicated).
            fresh_engine, fresh_factory = create_app_engine(lifecycle_url)
            try:
                replayed = RunLifecycle(fresh_factory)
                stream = await replayed.event_stream(run_id)
                assert [event.seq for event in stream] == [1, 2, 3]
                assert stream[: len(before)] == before
                assert await replayed.replay_states(run_id) == (
                    RunState.QUEUED,
                    RunState.RUNNING,
                    RunState.SUCCEEDED,
                )
            finally:
                await fresh_engine.dispose()

            # Stale results can never overwrite the terminal state.
            for stale in (RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED):
                with pytest.raises(RunLifecycleError) as excinfo:
                    await lifecycle.record_transition(run_id, stale)
                assert excinfo.value.code is RunLifecycleErrorCode.TERMINAL_IMMUTABLE
            assert await lifecycle.current_state(run_id) is RunState.SUCCEEDED
        finally:
            _stop(worker2)

    @pytest.mark.anyio
    async def test_cancelled_run_rejects_stale_completion(
        self,
        lifecycle_url: str,
        client: EngineClient,
        factory: async_sessionmaker[AsyncSession],
        insert_run: Callable[..., object],
        scenario: Path,
    ) -> None:
        """Race stale completion against cancellation; final state is legal."""
        run_id = str(uuid.uuid4())
        await insert_run(run_id)
        lifecycle = RunLifecycle(factory)
        await lifecycle.record_transition(run_id, RunState.QUEUED)

        gate = scenario / "cancel-gate"
        client.enqueue_run(
            run_id,
            "gated",
            json.dumps(
                {
                    "journal": str(_journal(scenario)),
                    "started": "CANCEL-STARTED",
                    "done": "CANCEL-DONE",
                    "gate": str(gate),
                    "timeout": 300.0,
                }
            ),
        )
        worker = _spawn_worker(
            lifecycle_url, scenario / "handlers", scenario, "w-cancel"
        )
        try:
            _wait_until(
                lambda: _journal_count(scenario, "CANCEL-STARTED") == 1,
                STEP_TIMEOUT,
                "worker claim",
            )
            _wait_until(
                lambda: client.run_state(run_id) is RunState.RUNNING,
                READY_TIMEOUT,
                "engine PENDING",
            )
            await lifecycle.record_transition(run_id, RunState.RUNNING)

            # Requested ≠ verified: request first, then let the engine land
            # CANCELLED at the step boundary (release the gate).
            client.request_cancel(run_id)
            gate.touch()
            _wait_until(
                lambda: client.run_state(run_id) is RunState.CANCELLED,
                STEP_TIMEOUT,
                "engine CANCELLED",
            )
            await lifecycle.record_transition(run_id, RunState.CANCELLED)

            # A stale success result arriving after cancellation is rejected;
            # the final state is the legal terminal one, engine and facade.
            with pytest.raises(RunLifecycleError) as excinfo:
                await lifecycle.record_transition(run_id, RunState.SUCCEEDED)
            assert excinfo.value.code is RunLifecycleErrorCode.TERMINAL_IMMUTABLE
            assert await lifecycle.replay_states(run_id) == (
                RunState.QUEUED,
                RunState.RUNNING,
                RunState.CANCELLED,
            )
            assert client.run_state(run_id) is RunState.CANCELLED
        finally:
            _kill9(worker)


# ---------------------------------------------------------------------------
# HT-W5B1: terminal transitions swap the job tag atomically
# ---------------------------------------------------------------------------


async def _job_tag_rows(
    factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID
) -> list[tuple[uuid.UUID, str]]:
    """The run's job_tags rows as ``(tag_id, assignment_source)``."""
    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT tag_id, assignment_source FROM job_tags "
                    "WHERE job_id = :j ORDER BY tag_id"
                ),
                {"j": job_id},
            )
        ).all()
    return [(row[0], row[1]) for row in rows]


TERMINAL_TAG_NAMES = ["succeeded", "failed", "cancelled"]


@pytest.fixture(autouse=True)
def _terminal_catalog_tags(request: pytest.FixtureRequest) -> None:
    """Seed the per-test terminal catalog tags for the terminal tag swap.

    Only anyio-driven tests reach terminal transitions, so sync tests skip
    the seeding; the missing-terminal regression requires the terminal
    names to be absent, and the session-shared catalog may hold tags left
    by earlier tests, so it purges them instead. Seeding goes through a
    throwaway engine because a sync autouse fixture cannot depend on the
    async ``factory`` fixture.
    """
    if request.node.get_closest_marker("anyio") is None:
        return
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        return  # factory skips the test on the missing fixture URL anyway
    if request.node.originalname == "test_missing_terminal_tag_rolls_back_atomically":
        # Session-shared catalog: earlier tests leave terminal tags behind,
        # and this regression requires the terminal names to be absent.
        with psycopg.connect(url) as conn:
            conn.execute(
                "DELETE FROM job_tags WHERE tag_id IN "
                "(SELECT tag_id FROM tags WHERE lower(name) = ANY(%(names)s))",
                {"names": TERMINAL_TAG_NAMES},
            )
            conn.execute(
                "DELETE FROM conversation_tags WHERE tag_id IN "
                "(SELECT tag_id FROM tags WHERE lower(name) = ANY(%(names)s))",
                {"names": TERMINAL_TAG_NAMES},
            )
            conn.execute(
                "DELETE FROM tags WHERE lower(name) = ANY(%(names)s)",
                {"names": TERMINAL_TAG_NAMES},
            )
            conn.commit()
        return

    async def _seed() -> None:
        eng, sessions = create_app_engine(url)
        try:
            for name in TERMINAL_TAG_NAMES:
                await acreate_tag(sessions, name=name)
        finally:
            await eng.dispose()

    asyncio.run(_seed())


class TestTerminalTagSwap:
    """HT-W5B1: terminal transitions drive the job-tag swap in the same tx."""

    @pytest.mark.anyio
    async def test_each_terminal_state_replaces_unclassified(
        self,
        factory: async_sessionmaker[AsyncSession],
        insert_run: Callable[..., object],
    ) -> None:
        """succeeded/failed/cancelled each swap unclassified for their tag."""
        tag_ids = {
            target: (await acreate_tag(factory, name=target.value)).tag_id
            for target in (RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED)
        }
        for target in (RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED):
            run_id = uuid.uuid4()
            await insert_run(str(run_id))
            lifecycle = RunLifecycle(factory)
            await lifecycle.record_transition(str(run_id), RunState.QUEUED)
            await lifecycle.record_transition(str(run_id), RunState.RUNNING)
            await lifecycle.record_transition(str(run_id), target)
            assert await lifecycle.replay_states(str(run_id)) == (
                RunState.QUEUED,
                RunState.RUNNING,
                target,
            )
            assert await _job_tag_rows(factory, run_id) == [
                (tag_ids[target], SYSTEM_ASSIGNMENT_SOURCE)
            ]

    @pytest.mark.anyio
    async def test_meaningful_tags_present_is_a_noop(
        self,
        factory: async_sessionmaker[AsyncSession],
        insert_run: Callable[..., object],
    ) -> None:
        """A run with meaningful tags keeps them; the terminal swap no-ops."""
        tag = await acreate_tag(factory, name="prod")
        run_id = uuid.uuid4()
        await insert_run(str(run_id))
        async with factory() as session:
            await session.execute(
                text(
                    "INSERT INTO job_tags (job_id, tag_id, assigned_by, "
                    "assignment_source) VALUES (:j, :t, :by, :src)"
                ),
                {"j": run_id, "t": tag.tag_id, "by": "tester", "src": "user"},
            )
            await session.commit()
        lifecycle = RunLifecycle(factory)
        await lifecycle.record_transition(str(run_id), RunState.QUEUED)
        await lifecycle.record_transition(str(run_id), RunState.RUNNING)
        await lifecycle.record_transition(str(run_id), RunState.FAILED)
        assert await lifecycle.replay_states(str(run_id)) == (
            RunState.QUEUED,
            RunState.RUNNING,
            RunState.FAILED,
        )
        assert await _job_tag_rows(factory, run_id) == sorted(
            [
                (uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID), SYSTEM_ASSIGNMENT_SOURCE),
                (tag.tag_id, "user"),
            ],
            key=lambda row: row[0],
        )

    @pytest.mark.anyio
    async def test_missing_terminal_tag_rolls_back_atomically(
        self,
        factory: async_sessionmaker[AsyncSession],
        insert_run: Callable[..., object],
    ) -> None:
        """No catalog terminal tag: the whole transition aborts atomically."""
        run_id = uuid.uuid4()
        await insert_run(str(run_id))
        lifecycle = RunLifecycle(factory)
        await lifecycle.record_transition(str(run_id), RunState.QUEUED)
        await lifecycle.record_transition(str(run_id), RunState.RUNNING)
        before = await lifecycle.event_stream(str(run_id))
        with pytest.raises(TagStoreError) as excinfo:
            await lifecycle.record_transition(str(run_id), RunState.SUCCEEDED)
        assert excinfo.value.code is TagErrorCode.TAG_NOT_FOUND
        assert await lifecycle.event_stream(str(run_id)) == before
        assert await lifecycle.current_state(str(run_id)) is RunState.RUNNING
        assert await _job_tag_rows(factory, run_id) == [
            (uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID), SYSTEM_ASSIGNMENT_SOURCE)
        ]
