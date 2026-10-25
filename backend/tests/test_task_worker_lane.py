"""Focused tests for the worker-lane wiring (card 14b-worker-lane-wiring).

Acceptance (BRIEF §5):

1. **The ``task.*`` tools are reachable through the REAL worker lane.**
   The production composition seam (``mai.jobs.worker.
   _configure_default_agent_runtime``) registers the ``task.*``
   descriptors AND their executors, so a worker-bound agent context can
   dispatch ``task.read`` / ``task.update_item`` through the real
   ``CapabilityDispatcher`` (registry → grants/policy/adoption gates →
   executor) against real PostgreSQL. The chat default-capability
   resolution exposes the same set (and strips the mutation lane for
   read-only conversations). Guardrails are preserved: descriptors keep
   passing ``assert_agent_ready_descriptor`` at the registered surface.
2. **In-run task-header refresh after a task mutation.** After a
   successful ``task.update_item`` through the tool lane, the CURRENT
   header of the run's bound task reflects the mutation (asserted
   through the real executor surface
   ``AgentRunExecutor.arefresh_task_header`` — the exact read the
   checkpointed workflow step performs) and the deterministic re-bind
   into the leading ``SystemPromptPart`` shows the new state to the next
   model round. The refresh is idempotent and inert without a bound
   block. The DBOS workflow trampoline itself is owned by the 00-runtime
   fixture harness; this suite proves the executor + transform surface
   the workflow composes.

Storage claims run against a real disposable PostgreSQL fixture
(``MAI_TEST_DATABASE_URL``) with the real linear Alembic chain applied.
Run this suite SEPARATELY from ``tests/test_task_capabilities.py`` — the
cross-suite session-fixture teardown conflict is a documented baseline
condition (and this suite additionally composes the process-global
worker runtime).
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from types import SimpleNamespace

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from pydantic_ai import ModelMessagesTypeAdapter
from sqlalchemy import text

from mai.agents.conversation_store import (
    aattach_run,
    acreate_conversation,
    asubmit_turn,
)
from mai.agents.run import (
    TASK_BLOCK_CLOSER,
    TASK_BLOCK_HEADER_LINE,
    AgentRunIntent,
    _history_with_refreshed_task_block,
    _task_context_block,
    initial_history_json,
)
from mai.agents.task_store import acreate_task
from mai.capabilities.internal_api import assert_agent_ready_descriptor
from mai.capabilities.task_state import (
    TASK_PROPOSE_UPDATE_CAPABILITY,
    TASK_READ_CAPABILITY,
    TASK_STATE_CAPABILITY_IDS,
    TASK_UPDATE_ITEM_CAPABILITY,
)
from mai.contracts.tasks import TaskPlanStep
from mai.database import create_app_engine
from mai.policy.evaluate import ExecutionContext, PolicyMode

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# The General Consult scope (canonical zero UUID): the controller-side
# context the worker lane always adopts and grants out of the box.
GENERAL_CONSULT = "00000000-0000-0000-0000-000000000000"

pytestmark = pytest.mark.anyio


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


def _drop_app_tables(url: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            "DROP TABLE IF EXISTS agent_task_completions, "
            "agent_task_revisions, agent_task_evidence, "
            "agent_task_criteria, agent_task_constraints, "
            "agent_task_steps, agent_tasks, conversation_steering, "
            "conversation_runs, conversation_messages, conversations, "
            "notification_alerts, observation_findings, "
            "server_observations, application_projection_journal, "
            "application_projection, knowledge_doc_chunks, "
            "knowledge_documents, job_schedule_fires, job_schedules, "
            "job_events, job_steps, job_runs, approvals, "
            "profile_projection_journal, profile_projection, "
            "admin_sessions, admin_credential, alembic_version"
        )
        conn.execute("DROP SEQUENCE IF EXISTS server_observations_seq")
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through head."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the storage claims need a real "
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
    return url


@pytest.fixture(scope="session")
def worker_runtime(migrated_url: str, tmp_path_factory: pytest.TempPathFactory):
    """The REAL worker-lane composition (one per process).

    ``mai.jobs.worker._configure_default_agent_runtime`` is the exact
    seam the production worker process runs: registry, capability
    dispatcher (grants/policy/adoption/credential providers), the steps
    machinery and the ``AgentRunExecutor`` — composed over the fixture
    database and a throwaway data root. No mock anywhere in the lane.
    """
    from mai.agents import run as agent_run
    from mai.jobs import worker as worker_module

    if getattr(agent_run, "_EXECUTOR", None) is not None:
        pytest.fail(
            "the agent-run executor is already composed in this process; "
            "this suite must run in its own pytest invocation"
        )
    data_root = tmp_path_factory.mktemp("worker_lane_data_root")
    previous = os.environ.get("MAI_DATA_ROOT")
    os.environ["MAI_DATA_ROOT"] = str(data_root)
    try:
        worker_module._configure_default_agent_runtime(migrated_url)
    finally:
        if previous is None:
            os.environ.pop("MAI_DATA_ROOT", None)
        else:
            os.environ["MAI_DATA_ROOT"] = previous
    executor = getattr(agent_run, "_EXECUTOR", None)
    assert executor is not None, "worker runtime composition failed"
    return executor


@pytest.fixture
async def sessions(migrated_url: str):
    engine, factory = create_app_engine(migrated_url)
    yield factory
    # Each data test starts from an empty task + conversation domain
    # (session-scoped fixture database): children before parents.
    async with factory() as session:
        for table in (
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
        ):
            await session.execute(text(f"DELETE FROM {table}"))
        await session.commit()
    await engine.dispose()


# ---------------------------------------------------------------------------
# fixture builders (the task_state suite's shape, General Consult scope)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Fixture:
    conversation_id: uuid.UUID
    source_message: uuid.UUID
    task_id: uuid.UUID
    revision: int
    run_id: uuid.UUID


async def _make_task_fixture(sessions) -> Fixture:
    """One conversation + one user message + one task + one attached run."""
    server = uuid.UUID(GENERAL_CONSULT)
    conv = await acreate_conversation(sessions, server_id=server)
    turn = await asubmit_turn(
        sessions,
        conversation_id=conv.conversation_id,
        server_id=server,
        client_request_id=uuid.uuid4(),
        message="synthetic user turn",
    )
    run_id = uuid.uuid4()
    created = await acreate_task(
        sessions,
        server_id=server,
        conversation_id=conv.conversation_id,
        title="synthetic worker-lane task",
        summary="worker-lane fixture objective",
        steps=[
            TaskPlanStep(step_id="s1", title="step s1"),
            TaskPlanStep(step_id="s2", title="step s2", depends_on=["s1"]),
            TaskPlanStep(step_id="s3", title="step s3"),
        ],
    )
    await aattach_run(
        sessions,
        conversation_id=conv.conversation_id,
        server_id=server,
        run_id=run_id,
        task_id=created.task.task_id,
    )
    return Fixture(
        conversation_id=conv.conversation_id,
        source_message=turn.message.message_id,
        task_id=created.task.task_id,
        revision=created.task.revision,
        run_id=run_id,
    )


def _context(run_id: uuid.UUID) -> ExecutionContext:
    """The worker-bound agent context (the chat auto-resolution shape)."""
    from mai.api.chat import BUILTIN_CHAT_CAPABILITIES

    return ExecutionContext(
        principal="worker-lane-test",
        server_id=uuid.UUID(GENERAL_CONSULT),
        run_id=run_id,
        parent_scope=None,
        profile_revision="1",
        prompt_revision="1",
        credential_references=(),
        allowed_capabilities=tuple(BUILTIN_CHAT_CAPABILITIES),
        mode=PolicyMode.DELEGATED,
    )


def _intent(context: ExecutionContext) -> AgentRunIntent:
    return AgentRunIntent(
        run_id=str(context.run_id),
        server_id=str(context.server_id),
        context=context,
        role="administrator",
        task="synthetic worker-lane conversation",
        connection=None,
        profile=None,
        max_rounds=2,
        reconcile_wait_seconds=1.0,
        wait_poll_seconds=0.1,
    )


def _system_part(history_json: str) -> str:
    messages = ModelMessagesTypeAdapter.validate_json(
        history_json.encode("utf-8")
    )
    for message in messages:
        for part in message.parts:
            if getattr(part, "part_kind", None) == "system-prompt":
                return str(part.content)
    raise AssertionError("no leading system part in the seed history")


# ---------------------------------------------------------------------------
# AC 1 — the real worker lane registers task.* and executes through it
# ---------------------------------------------------------------------------


def test_worker_lane_registers_task_descriptors_and_keeps_guards(
    worker_runtime,
) -> None:
    """The REAL registry carries the task.* set, still fail-closed."""
    registered = {
        descriptor.identifier: descriptor
        for descriptor in worker_runtime._registry.list_capabilities()
    }
    for capability_id in TASK_STATE_CAPABILITY_IDS:
        assert capability_id in registered, (
            f"{capability_id} missing from the worker-lane registry"
        )
        # The registered descriptor (not the builder output) still passes
        # the fail-closed agent-ready validation at the composition seam.
        assert_agent_ready_descriptor(registered[capability_id])
    # The pre-existing lane is intact (no replacement, an extension).
    from mai.capabilities.internal_api import INTERNAL_API_CAPABILITY_IDS

    for capability_id in (*INTERNAL_API_CAPABILITY_IDS, "web.search", "web.read"):
        assert capability_id in registered


async def test_worker_bound_agent_reads_task_through_real_dispatcher(
    worker_runtime, sessions
) -> None:
    """task.read resolves through registry → gates → executor on real PG."""
    fixture = await _make_task_fixture(sessions)
    dispatcher = worker_runtime._steps._dispatcher

    async def _revision_rows() -> int:
        async with sessions() as session:
            row = (
                await session.execute(
                    text("SELECT COUNT(*) FROM agent_task_revisions")
                )
            ).scalar()
        return int(row or 0)

    before_rows = await _revision_rows()
    outcome = await dispatcher.dispatch(
        context=_context(fixture.run_id),
        capability_id=TASK_READ_CAPABILITY,
        payload={"section": "header"},
    )
    assert outcome.allowed is True, outcome.reason
    assert outcome.executor_ok is True, outcome.reason
    assert outcome.output is not None and outcome.output.truncated is False
    document = json.loads(outcome.output.text)
    assert document["status"] == "ok"
    assert document["task_status"] == "in_progress"
    assert uuid.UUID(str(document["task_id"])) == fixture.task_id
    assert document["revision"] == fixture.revision
    assert document["title"] == "synthetic worker-lane task"
    # A real read: ZERO writes (the revision ledger is untouched).
    assert await _revision_rows() == before_rows

    # A bounded page read through the same lane (AGENTS.md §1 envelope).
    page = await dispatcher.dispatch(
        context=_context(fixture.run_id),
        capability_id=TASK_READ_CAPABILITY,
        payload={"section": "steps", "limit": 2, "offset": 0},
    )
    assert page.allowed is True and page.executor_ok is True
    page_document = json.loads(page.output.text)
    assert page_document["status"] == "ok"
    assert page_document["limit"] == 2
    assert page_document["offset"] == 0
    assert page_document["total"] == 3
    assert page_document["has_more"] is True
    assert [item["step_id"] for item in page_document["items"]] == [
        "s1",
        "s2",
    ]


def test_chat_default_resolution_exposes_task_tools_and_read_only_strips() -> None:
    """The chat auto-resolution exposes task.*; read-only keeps only the
    read tool (the mutation lane is statically stripped, registry or
    not)."""
    from mai.api.chat import (
        READ_ONLY_EXCLUDED_CAPABILITIES,
        _read_only_capabilities,
        _resolve_default_capabilities,
    )

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    resolved = _resolve_default_capabilities(
        request, uuid.UUID(GENERAL_CONSULT), ()
    )
    assert set(TASK_STATE_CAPABILITY_IDS) <= set(resolved)
    filtered = _read_only_capabilities(request, resolved)
    assert TASK_READ_CAPABILITY in filtered
    assert TASK_PROPOSE_UPDATE_CAPABILITY not in filtered
    assert TASK_UPDATE_ITEM_CAPABILITY not in filtered
    # Belt and braces: the static exclusion list itself carries the
    # mutation lane even for a registry-less API process.
    assert {
        TASK_PROPOSE_UPDATE_CAPABILITY,
        TASK_UPDATE_ITEM_CAPABILITY,
    } <= set(READ_ONLY_EXCLUDED_CAPABILITIES)


# ---------------------------------------------------------------------------
# AC 2 — in-run task-header refresh after a task mutation
# ---------------------------------------------------------------------------


async def test_task_mutation_refreshes_bound_task_header(
    worker_runtime, sessions
) -> None:
    """After task.update_item through the real dispatcher, the bound
    header re-read shows the mutation and the re-bind carries it into
    the leading system part for the next model round."""
    fixture = await _make_task_fixture(sessions)
    context = _context(fixture.run_id)
    dispatcher = worker_runtime._steps._dispatcher

    # The OLD header (what the seed's system part was bound to).
    before = await dispatcher.dispatch(
        context=context,
        capability_id=TASK_READ_CAPABILITY,
        payload={"section": "header"},
    )
    old_header = json.loads(before.output.text)
    assert old_header["revision"] == fixture.revision

    seed = initial_history_json(
        "BASE SYSTEM PROMPT" + _task_context_block(old_header),
        "make progress on the plan",
    )

    outcome = await dispatcher.dispatch(
        context=context,
        capability_id=TASK_UPDATE_ITEM_CAPABILITY,
        payload={
            "step_id": "s1",
            "expected_revision": fixture.revision,
            "idempotency_key": str(uuid.uuid4()),
            "status": "in_progress",
            "result_summary": "worker-lane synthetic progress",
        },
    )
    assert outcome.allowed is True, outcome.reason
    assert outcome.executor_ok is True, outcome.reason
    assert json.loads(outcome.output.text)["status"] == "ok"

    # The refresh seam's executor read (the checkpointed workflow step
    # calls exactly this surface): the CURRENT bound header.
    refreshed = await worker_runtime.arefresh_task_header(_intent(context))
    assert refreshed is not None
    assert refreshed["task_id"] == str(fixture.task_id)
    assert refreshed["revision"] == fixture.revision + 1
    step = next(s for s in refreshed["steps"] if s["step_id"] == "s1")
    assert step["status"] == "in_progress"

    # The deterministic re-bind: the next round's leading system part
    # sees the new state and not the stale one.
    rebound = _history_with_refreshed_task_block(seed, refreshed)
    assert rebound != seed
    system = _system_part(rebound)
    assert TASK_BLOCK_HEADER_LINE in system
    assert f"revision {fixture.revision + 1}" in system
    assert f"(revision {fixture.revision}," not in system
    assert "in_progress" in system
    assert "BASE SYSTEM PROMPT" in system  # the base prompt is preserved
    assert system.index("BASE SYSTEM PROMPT") < system.index(
        TASK_BLOCK_HEADER_LINE
    )
    # The user turn and message shape are untouched by the re-bind.
    messages = ModelMessagesTypeAdapter.validate_json(rebound.encode("utf-8"))
    assert len(messages) == 2

    # Idempotent: refreshing with the same state is a fixed point (the
    # SAME deterministic block text — JSON re-dumps regenerate volatile
    # part metadata like timestamps, so compare the bound content).
    again = _history_with_refreshed_task_block(rebound, refreshed)
    assert _system_part(again) == _system_part(rebound)

    # Inert edges: no header / no bound block / plain history change
    # NOTHING (fail-closed, never a second binding).
    assert _history_with_refreshed_task_block(seed, None) == seed
    plain = initial_history_json("BASE SYSTEM PROMPT", "plain turn")
    assert _history_with_refreshed_task_block(plain, refreshed) == plain
    truncated = "BASE" + "\n" + TASK_BLOCK_HEADER_LINE + "\n orphan"
    orphan_seed = initial_history_json(truncated, "turn")
    assert _history_with_refreshed_task_block(orphan_seed, refreshed) == (
        orphan_seed
    )


async def test_refresh_is_inert_without_a_bound_task(
    worker_runtime, sessions
) -> None:
    """No run link → the refresh read answers None (legacy unbound
    shape), so the re-bind is a no-op — the seam never invents a task."""
    server = uuid.UUID(GENERAL_CONSULT)
    conv = await acreate_conversation(sessions, server_id=server)
    await asubmit_turn(
        sessions,
        conversation_id=conv.conversation_id,
        server_id=server,
        client_request_id=uuid.uuid4(),
        message="synthetic user turn",
    )
    run_id = uuid.uuid4()
    await aattach_run(
        sessions,
        conversation_id=conv.conversation_id,
        server_id=server,
        run_id=run_id,
        task_id=None,
    )
    context = _context(run_id)
    assert await worker_runtime.arefresh_task_header(_intent(context)) is None
