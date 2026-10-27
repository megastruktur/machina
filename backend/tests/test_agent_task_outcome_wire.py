"""Focused tests for the producer-side task-outcome wire projection (21b).

Acceptance (BRIEF, act_CONTRACTS §9, R-21-outcome-ui wire_gap_assessment):

1. **The real path delivers the document (AC 1):** a run bound to a task
   emits ONE ``agent.completed`` event whose ``data`` carries the additive
   ``task_outcome`` document — identifiers and frozen-vocabulary states
   only, exactly what the real final-seam producer chain
   (``_agent_task_outcome_finalized`` -> ``afinalize_task_outcome`` ->
   ``task_completion.aobserve_task_outcome``) derives — so the UI's
   ``readTaskOutcome`` narrow can consume it from the wire.
2. **Taskless runs stay byte-identical (AC 1):** a run without a task
   document in its summary emits the exact legacy event shape — NO
   ``task_outcome`` key at all (omitted, never ``None``).
3. **Additive and fail-closed (AC 2):** the legacy summary keys keep
   their positions and values; a malformed / non-mapping / empty
   ``summary["task_outcome"]`` is omitted from the event (never emitted
   as-is); the projection never touches the technical ``RunState``
   machine (the run still finishes ``succeeded``); ``stopped_by`` keeps
   its honest disposition alongside the document.

Boundary discipline: the REAL composed ``AgentRunExecutor`` over a real
disposable PostgreSQL fixture (real linear Alembic chain), in-process —
the same proven in-process seam harness as the steering-continuity suite
(no DBOS worker subprocess; the model layer stays uncomposed). Fixtures
carry only fixed opaque UUIDs and synthetic text — never secret values.
"""

from __future__ import annotations

import json
import os
import pathlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents import run as agent_run
from mai.agents import task_completion
from mai.agents.conversation_store import aattach_run, acreate_conversation
from mai.agents.model_store import ModelConfigStore
from mai.agents.task_store import acreate_task
from mai.capabilities.dispatch import CapabilityDispatcher
from mai.contracts.tasks import AcceptanceCriterion, TaskPlanStep
from mai.credentials.env import EnvResolver
from mai.database import create_app_engine
from mai.jobs import engine as jobs_engine
from mai.jobs.resource_locks import ResourceLockService
from mai.plugins.registry import CapabilityRegistry, parse_descriptor
from mai.policy.approvals import ApprovalStore
from mai.policy.evaluate import ExecutionContext, Policy, PolicyMode

#: The suite runs async tests through the anyio pytest plugin (the repo's
#: async test convention); every test below is async, so the marker is
#: applied module-wide.
pytestmark = pytest.mark.anyio

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
ALEMBIC_INI = str(BACKEND_ROOT / "alembic.ini")

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

SERVER_ID = "7b52d3f5-2c4e-4a6b-9d8f-3e4c5b6a7f81"
SERVER_UUID = uuid.UUID(SERVER_ID)

READ_CAP = "fixture.read"

#: The legacy (pre-21b) ``agent.completed`` data keys: the exact shape a
#: run without a task-outcome document must keep, byte for byte.
LEGACY_KEYS = ("rounds", "tool_calls", "output_chars")


# ---------------------------------------------------------------------------
# disposable database fixtures (real linear Alembic chain, in-process)
# ---------------------------------------------------------------------------


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


def _drop_all(url: str) -> None:
    """Reset the fixture database to pre-bootstrap state.

    ``alembic_version`` drops with the app tables so the session fixture's
    ``upgrade head`` re-runs the FULL chain (the sibling storage suites'
    proven pattern — a version row without its tables would make the
    upgrade a silent no-op).
    """
    with psycopg.connect(url) as conn:
        conn.execute(
            "DROP TABLE IF EXISTS "
            "agent_task_completions, agent_task_revisions, "
            "agent_task_evidence, agent_task_criteria, "
            "agent_task_constraints, agent_task_steps, agent_tasks, "
            "conversation_steering, conversation_runs, "
            "conversation_messages, conversations, job_resource_locks, "
            "job_schedule_fires, job_schedules, job_events, job_steps, "
            "job_runs, approvals, profile_projection_journal, "
            "profile_projection, admin_sessions, admin_credential, "
            "notification_alerts, observation_findings, "
            "server_observations, knowledge_doc_chunks, "
            "knowledge_documents, application_projection_journal, "
            "application_projection, alembic_version"
        )
        conn.execute("DROP SEQUENCE IF EXISTS server_observations_seq")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


_RESET_TABLES = (
    # children before parents
    "agent_task_completions",
    "agent_task_revisions",
    "agent_task_evidence",
    "agent_task_criteria",
    "agent_task_constraints",
    "agent_task_steps",
    "agent_tasks",
    "conversation_steering",
    "conversation_runs",
    "conversation_messages",
    "conversations",
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
)


async def _truncate(url: str) -> None:
    """Reset the facade tables between tests (the migrated schema stays)."""
    engine, _factory = create_app_engine(url)
    try:
        async with engine.begin() as conn:
            for table in _RESET_TABLES:
                present = await conn.execute(
                    text("SELECT to_regclass(:t) IS NOT NULL"),
                    {"t": f"public.{table}"},
                )
                if not present.scalar():
                    continue  # engine-owned tables may not exist pre-launch
                await conn.execute(text(f"DELETE FROM {table}"))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through the head."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the task-outcome wire claims need "
            "a real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        _drop_all(url)
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
    jobs_engine.launch_worker(migrated_url)
    jobs_engine.shutdown()
    return migrated_url


@pytest.fixture
async def sessions(migrated_url: str):
    """NullPool session factory; empty conversation+task+jobs domain."""
    engine, factory = create_app_engine(migrated_url)
    yield factory
    await _truncate(migrated_url)
    await engine.dispose()


# ---------------------------------------------------------------------------
# the REAL composed executor (in-process; no model layer needed here)
# ---------------------------------------------------------------------------


def _fixture_descriptor(capability_id: str, effect_class: str) -> object:
    return parse_descriptor(
        {
            "identifier": capability_id,
            "version": "1.0.0",
            "config_schema": None,
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["marker"],
                "properties": {"marker": {"type": "string", "minLength": 1}},
            },
            "output_schema": {"type": "object"},
            "execution_environments": ["linux_ssh"],
            "effect_class": effect_class,
            "resource_scope": [] if effect_class == "read" else ["target.exec"],
            "required_grants": [],
        },
        source=f"<{capability_id} descriptor>",
    )


def _real_registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register(_fixture_descriptor(READ_CAP, "read"))
    return registry


def _refusing_executor():
    async def _execute(call) -> object:
        raise AssertionError(f"executor invoked for a denied call: {call}")

    return _execute


@pytest.fixture
async def executor(
    sessions, tmp_path: pathlib.Path
) -> AsyncIterator[agent_run.AgentRunExecutor]:
    """The REAL composed executor over the REAL session factory.

    The finish/emit seam runs before any model call, so the model layer
    stays uncomposed; the projection exercises the real lifecycle, the
    real event stream and the real lease release machinery.
    """
    yield agent_run.AgentRunExecutor(
        sessions=sessions,
        dispatcher=CapabilityDispatcher(
            registry=_real_registry(),
            approvals=ApprovalStore(sessions),
            grants=lambda: {READ_CAP: frozenset({SERVER_UUID})},
            policy=lambda: Policy(revision=1, denied_capabilities=frozenset()),
            adopted=lambda server_id: True,
            credentials=lambda: EnvResolver(
                str(BACKEND_ROOT / ".." / "absent-fixture.env")
            ),
            executor=_refusing_executor(),
        ),
        registry=_real_registry(),
        model_store=ModelConfigStore(tmp_path / "model-root"),
        data_root=str(tmp_path / "data-root"),
        locks=ResourceLockService("postgresql://unused:unused@127.0.0.1:1/unused"),
    )


# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _intent(run_id: str) -> agent_run.AgentRunIntent:
    context = ExecutionContext(
        principal="hash:fixture-principal",
        server_id=SERVER_UUID,
        run_id=uuid.UUID(run_id),
        parent_scope=None,
        profile_revision="profile-1",
        prompt_revision="prompt-1",
        credential_references=(),
        allowed_capabilities=(READ_CAP,),
        mode=PolicyMode("delegated"),
    )
    document = {
        "v": agent_run.RUN_PAYLOAD_VERSION,
        "run_id": run_id,
        "server_id": str(SERVER_UUID),
        "context": context.to_json(),
        "role": "operator assistant",
        "task": "fixture objective",
        "connection": "gateway",
        "profile": "fast",
    }
    return agent_run.parse_agent_run_intent(_canonical(document))


async def _insert_run_row(
    sessions: async_sessionmaker[AsyncSession], run_id: str
) -> None:
    """One ``job_runs`` row (the event stream's FK parent)."""
    async with sessions() as session:
        await session.execute(
            text(
                """
                INSERT INTO job_runs
                    (run_id, server_id, depth, profile_revision,
                     prompt_revision, model_profile, requested_grants,
                     submitted_at, updated_at)
                VALUES
                    (:run_id, :server_id, 0, 3, 7, 'fixture-fast',
                     :grants, now(), now())
                """
            ),
            {
                "run_id": uuid.UUID(run_id),
                "server_id": SERVER_UUID,
                "grants": json.dumps([READ_CAP]),
            },
        )
        await session.commit()


async def _active_run(
    sessions: async_sessionmaker[AsyncSession], *, with_task: bool
) -> tuple[uuid.UUID, uuid.UUID, Any]:
    """One conversation whose writer lease is held by an attached run.

    With ``with_task`` the conversation also owns one fresh two-criterion
    task bound to the run's link — the exact producer-side binding the
    final-seam observation reads.
    """
    conversation = await acreate_conversation(sessions, server_id=SERVER_UUID)
    run_id = uuid.uuid4()
    await _insert_run_row(sessions, str(run_id))
    task = None
    if with_task:
        result = await acreate_task(
            sessions,
            server_id=SERVER_UUID,
            conversation_id=conversation.conversation_id,
            title="synthetic fixture task",
            summary="fixture objective",
            criteria=[
                AcceptanceCriterion(criterion_id="c1", statement="criterion c1 holds"),
                AcceptanceCriterion(criterion_id="c2", statement="criterion c2 holds"),
            ],
        )
        task = result.task
    await aattach_run(
        sessions,
        conversation_id=conversation.conversation_id,
        server_id=SERVER_UUID,
        run_id=run_id,
        task_id=None if task is None else task.task_id,
    )
    return conversation.conversation_id, run_id, task


async def _completed_data(
    sessions: async_sessionmaker[AsyncSession], run_id: uuid.UUID
) -> list[dict[str, Any]]:
    """The persisted ``agent.completed`` payloads of one run, in seq order."""
    async with sessions() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT data FROM job_events "
                        "WHERE run_id = :rid AND type = :etype ORDER BY seq"
                    ),
                    {
                        "rid": run_id,
                        "etype": agent_run.AGENT_COMPLETED_EVENT_TYPE,
                    },
                )
            )
            .mappings()
            .all()
        )
    return [dict(row["data"]) for row in rows]


# ---------------------------------------------------------------------------
# the wire projection (card 21b)
# ---------------------------------------------------------------------------


class TestTaskOutcomeWire:
    async def test_task_bound_run_delivers_the_document(
        self, sessions, executor
    ) -> None:
        """AC1: the real producer document rides the emitted event.

        The summary document comes from the REAL final-seam producer
        (``afinalize_task_outcome`` over the run's own task link) — the
        same value ``_agent_task_outcome_finalized`` checkpoints into the
        workflow summary — and the emitted ``agent.completed`` data must
        carry it for the UI's ``readTaskOutcome`` narrow.
        """
        _conversation_id, run_id, task = await _active_run(sessions, with_task=True)
        intent = _intent(str(run_id))
        executor.observe_run_started(intent)
        document = executor.run(executor.afinalize_task_outcome(intent))
        assert document is not None
        assert document["task_id"] == str(task.task_id)
        summary = {
            "rounds": 2,
            "tool_calls": [],
            "output_chars": 42,
            task_completion.TASK_OUTCOME_SUMMARY_KEY: document,
        }
        state = executor.run(executor.afinish_run(intent, summary))
        assert state == "succeeded"
        events = await _completed_data(sessions, run_id)
        assert len(events) == 1  # _emit_once: exactly one emission
        data = events[0]
        # The legacy keys keep their values, additively.
        assert data["rounds"] == 2
        assert data["tool_calls"] == []
        assert data["output_chars"] == 42
        # The document is the producer's own, value-free wire document.
        wire = data[task_completion.TASK_OUTCOME_SUMMARY_KEY]
        assert wire == document
        assert wire["task_id"] == str(task.task_id)
        assert wire["outcome"] == "in_progress"  # fresh episode, never closed
        assert wire["verified"] is False
        assert wire["recorded"] is False
        assert wire["proposal"] == "none"
        assert wire["criteria"] == {
            "total": 2,
            "supported": 0,
            "failed": 0,
            "unavailable": 2,
        }
        assert [
            (entry["criterion_id"], entry["state"])
            for entry in wire["criterion_states"]
        ] == [("c1", "unavailable"), ("c2", "unavailable")]
        assert set(wire) == {
            "task_id",
            "outcome",
            "verified",
            "recorded",
            "criteria",
            "criterion_states",
            "proposal",
        }
        assert "holds" not in str(wire)  # no criterion statements on the wire

    async def test_taskless_run_keeps_the_legacy_shape_byte_identical(
        self, sessions, executor
    ) -> None:
        """AC1: a run without a task document emits the exact legacy shape."""
        _conversation_id, run_id, task = await _active_run(sessions, with_task=False)
        assert task is None
        intent = _intent(str(run_id))
        executor.observe_run_started(intent)
        summary = {"rounds": 1, "tool_calls": [], "output_chars": 12}
        state = executor.run(executor.afinish_run(intent, summary))
        assert state == "succeeded"
        events = await _completed_data(sessions, run_id)
        assert len(events) == 1
        data = events[0]
        # Omitted — never None: byte-identical to the pre-21b shape.
        assert task_completion.TASK_OUTCOME_SUMMARY_KEY not in data
        assert data == {"rounds": 1, "tool_calls": [], "output_chars": 12}
        assert _canonical(data) == _canonical(
            {"rounds": 1, "tool_calls": [], "output_chars": 12}
        )
        assert set(data) == set(LEGACY_KEYS)

    @pytest.mark.parametrize(
        "malformed",
        ["not-a-mapping", {}, None, 7],
        ids=["string", "empty-mapping", "none", "int"],
    )
    async def test_malformed_or_empty_document_is_omitted(
        self, sessions, executor, malformed
    ) -> None:
        """AC2: a non-mapping / empty value is never emitted as-is."""
        _conversation_id, run_id, _task = await _active_run(sessions, with_task=False)
        intent = _intent(str(run_id))
        executor.observe_run_started(intent)
        summary = {
            "rounds": 1,
            "tool_calls": [],
            "output_chars": 12,
            task_completion.TASK_OUTCOME_SUMMARY_KEY: malformed,
        }
        state = executor.run(executor.afinish_run(intent, summary))
        # The projection never touches the technical RunState machine.
        assert state == "succeeded"
        events = await _completed_data(sessions, run_id)
        assert len(events) == 1
        data = events[0]
        assert task_completion.TASK_OUTCOME_SUMMARY_KEY not in data
        assert data == {"rounds": 1, "tool_calls": [], "output_chars": 12}

    async def test_stopped_run_carries_disposition_and_document_additively(
        self, sessions, executor
    ) -> None:
        """AC2: ``stopped_by`` keeps its value alongside the document."""
        _conversation_id, run_id, _task = await _active_run(sessions, with_task=True)
        intent = _intent(str(run_id))
        executor.observe_run_started(intent)
        document = executor.run(executor.afinalize_task_outcome(intent))
        assert document is not None
        summary = {
            "rounds": 3,
            "tool_calls": [{"name": "fixture.read", "status": "ok"}],
            "output_chars": 7,
            agent_run.STOPPED_BY_EVENT_KEY: "pause",
            task_completion.TASK_OUTCOME_SUMMARY_KEY: document,
        }
        state = executor.run(executor.afinish_run(intent, summary))
        assert state == "succeeded"
        events = await _completed_data(sessions, run_id)
        assert len(events) == 1
        data = events[0]
        # Legacy keys keep their values; both additive keys present. (The
        # persisted jsonb payload normalizes key order, so only the key
        # SET — never a sequence — is the wire contract here.)
        assert data["rounds"] == 3
        assert data["tool_calls"] == [{"name": "fixture.read", "status": "ok"}]
        assert data["output_chars"] == 7
        assert data[agent_run.STOPPED_BY_EVENT_KEY] == "pause"
        assert data[task_completion.TASK_OUTCOME_SUMMARY_KEY] == document
        # W2: the fixture document's fresh episode is open, so the event
        # also carries the honest open-episode qualifier (additive, next
        # to the disposition and the document).
        assert data[agent_run.TASK_OPEN_EVENT_KEY] == (
            agent_run.TASK_OPEN_UNVERIFIED_REASON
        )
        assert set(data) == {
            *LEGACY_KEYS,
            agent_run.STOPPED_BY_EVENT_KEY,
            task_completion.TASK_OUTCOME_SUMMARY_KEY,
            agent_run.TASK_OPEN_EVENT_KEY,
        }


# ---------------------------------------------------------------------------
# W2 — the open-episode qualifier on an honest terminal success
# ---------------------------------------------------------------------------


def _outcome_document(outcome: str) -> dict[str, object]:
    """One value-free final-seam document of the pinned wire shape."""
    return {
        "task_id": str(uuid.uuid4()),
        "outcome": outcome,
        "verified": outcome == "verified",
        "recorded": False,
        "criteria": {"total": 2, "supported": 0, "failed": 0, "unavailable": 2},
        "criterion_states": [
            {"criterion_id": "c1", "state": "unavailable"},
            {"criterion_id": "c2", "state": "unavailable"},
        ],
        "proposal": "none",
    }


class TestOpenEpisodeQualifier:
    @pytest.mark.parametrize("outcome", ["verified", "failed", "cancelled"])
    def test_closed_outcomes_keep_the_shape_unqualified(self, outcome: str) -> None:
        """A closed domain verdict (verified/failed/cancelled) never gains
        the open-episode qualifier: the existing shapes stay untouched."""
        summary = {
            task_completion.TASK_OUTCOME_SUMMARY_KEY: _outcome_document(outcome)
        }
        assert agent_run._task_open_reason(summary) is None

    @pytest.mark.parametrize(
        "outcome", ["in_progress", "needs_input", "partially_verified"]
    )
    def test_open_outcomes_qualify_the_terminal_success(self, outcome: str) -> None:
        """An open episode yields exactly the one stable qualifier."""
        summary = {
            task_completion.TASK_OUTCOME_SUMMARY_KEY: _outcome_document(outcome)
        }
        assert agent_run._task_open_reason(summary) == (
            agent_run.TASK_OPEN_UNVERIFIED_REASON
        )

    @pytest.mark.parametrize(
        "summary",
        [
            {},
            {task_completion.TASK_OUTCOME_SUMMARY_KEY: None},
            {task_completion.TASK_OUTCOME_SUMMARY_KEY: "junk"},
            {task_completion.TASK_OUTCOME_SUMMARY_KEY: {}},
        ],
        ids=["absent", "none", "string", "empty"],
    )
    def test_absent_or_malformed_documents_stay_unqualified(
        self, summary: dict[str, object]
    ) -> None:
        """Taskless (document-absent) and malformed summaries never gain
        the qualifier — the legacy surfaces stay byte-identical."""
        assert agent_run._task_open_reason(summary) is None


class TestOpenTaskQualifiedFinish:
    async def test_open_episode_finish_is_explicitly_qualified(
        self, sessions, executor
    ) -> None:
        """W2: an open episode never presents an unqualified run success.

        The frozen terminal ``RunState`` stays ``succeeded`` (the attempt
        itself did not fail: its read answered and its refusals were
        proven no-effect), but the ``agent.completed`` event carries the
        stable open-episode reason NEXT TO the honest ``task_outcome``
        document. The persisted task stays exactly as resumable as it
        was — outcome, revision, pending steps and unverified criteria
        untouched — and the finish path records NO effects of its own.
        """
        conversation = await acreate_conversation(sessions, server_id=SERVER_UUID)
        run_id = uuid.uuid4()
        await _insert_run_row(sessions, str(run_id))
        created = await acreate_task(
            sessions,
            server_id=SERVER_UUID,
            conversation_id=conversation.conversation_id,
            title="synthetic fixture task",
            summary="fixture objective",
            steps=[
                TaskPlanStep(step_id="s1", title="step s1"),
                TaskPlanStep(step_id="s2", title="step s2"),
            ],
            criteria=[
                AcceptanceCriterion(criterion_id="c1", statement="criterion c1 holds"),
                AcceptanceCriterion(criterion_id="c2", statement="criterion c2 holds"),
            ],
        )
        await aattach_run(
            sessions,
            conversation_id=conversation.conversation_id,
            server_id=SERVER_UUID,
            run_id=run_id,
            task_id=created.task.task_id,
        )
        intent = _intent(str(run_id))
        executor.observe_run_started(intent)
        document = executor.run(executor.afinalize_task_outcome(intent))
        assert document is not None
        assert document["outcome"] == "in_progress"  # open: nothing verified
        summary = {
            "rounds": 2,
            "tool_calls": [{"name": "fixture.read", "status": "ok"}],
            "output_chars": 42,
            task_completion.TASK_OUTCOME_SUMMARY_KEY: document,
        }
        state = executor.run(executor.afinish_run(intent, summary))
        assert state == "succeeded"
        events = await _completed_data(sessions, run_id)
        assert len(events) == 1  # _emit_once: exactly one emission
        data = events[0]
        # The success is QUALIFIED, never unqualified: the stable reason
        # rides the event next to the honest document.
        assert data[agent_run.TASK_OPEN_EVENT_KEY] == (
            agent_run.TASK_OPEN_UNVERIFIED_REASON
        )
        assert data[task_completion.TASK_OUTCOME_SUMMARY_KEY] == document
        assert data[task_completion.TASK_OUTCOME_SUMMARY_KEY]["verified"] is False
        # Legacy keys keep their values.
        assert data["rounds"] == 2
        assert data["tool_calls"] == [{"name": "fixture.read", "status": "ok"}]
        assert data["output_chars"] == 42
        # Persisted truth: the task row stays open at revision 1, both
        # plan steps stay pending, and the finish recorded NO step rows
        # (no second execution, no fabricated effects).
        async with sessions() as session:
            task_row = (
                await session.execute(
                    text(
                        "SELECT status, revision FROM agent_tasks "
                        "WHERE task_id = :t"
                    ),
                    {"t": created.task.task_id},
                )
            ).first()
            step_rows = (
                await session.execute(
                    text(
                        "SELECT status, COUNT(*) FROM agent_task_steps "
                        "WHERE task_id = :t GROUP BY status ORDER BY status"
                    ),
                    {"t": created.task.task_id},
                )
            ).all()
            step_events = (
                await session.execute(
                    text("SELECT COUNT(*) FROM job_steps WHERE run_id = :r"),
                    {"r": run_id},
                )
            ).scalar()
        assert task_row[0] == "in_progress"
        assert task_row[1] == 1
        assert step_rows == [("pending", 2)]
        assert step_events == 0
