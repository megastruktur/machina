"""Focused tests for the checkpointed intake + task binding (card 13).

Acceptance (BRIEF, act_CONTRACTS §5/§6, act_RUNTIME C4/C5/P1-P4):

1. **Idempotent intake checkpoint (C4, AC 1):** ONE checkpointed tool-free
   interpretation per accepted turn; a replayed turn observes the SAME
   accepted result without a second intake-model call or a second task;
   planner usage is accounted for (model calls, repairs, zero function
   tools) and never double-counted on replay; an unrepairable invalid
   intake fails the run closed with ZERO effects (P4).
2. **Task binding + phase gate (AC 1/2):** a validated episode binds to
   the run's link; the phase gate denies target effects outside the
   ratified ``validate_phase`` mapping (plan: none, inspect: read,
   act: granted) BEFORE the effect fence; the binding header rides the
   leading system part so task id/phase/steps/constraints survive
   steering and compaction; ``answer``/``clarify``/``blocked`` never
   launch the executor (P1).
3. **Worker scenarios (C4/C5):** the durable claims (kill -9 after the
   intake checkpoint → replay idempotency; steering then deterministic
   pause across effect batches) run against the SAME proven 06-agent-run
   harness (real worker subprocesses, real engine, disposable Postgres).

Boundary discipline: real disposable PostgreSQL fixture (real Alembic
chain), deterministic ``FunctionModel`` fakes (plumbing only), the REAL
CapabilityDispatcher for the gate tests, and the existing
``test_agent_run`` harness imported unchanged for the worker scenarios.
Fixtures carry only fixed opaque UUIDs and synthetic text.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import test_agent_intake as intake_fixtures
import test_agent_run as harness
from mai.agents import run as agent_run
from mai.agents.conversation_store import (
    aattach_run,
    acreate_conversation,
    aenqueue_steering,
    arelease_run,
    asubmit_turn,
)
from mai.agents.model_store import ModelConfigStore
from mai.agents.models import SCHEMA_VERSION as MODEL_SCHEMA_VERSION
from mai.capabilities.dispatch import (
    EFFECT_NOT_ATTEMPTED,
    CapabilityDispatcher,
    ExecutorResult,
    canonical_payload_hash,
)
from mai.capabilities.task_state import (
    TASK_PROPOSE_UPDATE_CAPABILITY,
    TASK_READ_CAPABILITY,
    TASK_UPDATE_ITEM_CAPABILITY,
)
from mai.contracts.conversations import SteerAction
from mai.credentials.env import EnvResolver
from mai.database import create_app_engine
from mai.jobs import engine as job_engine
from mai.jobs.engine import EngineClient
from mai.jobs.resource_locks import ResourceLockService
from mai.plugins.registry import CapabilityRegistry, parse_descriptor
from mai.policy.approvals import ApprovalStore
from mai.policy.evaluate import Policy

#: Re-exported fixture objects: the SAME proven 06 harness fixtures
#: (fresh scenario dir with the real worker handler module + the
#: model/data roots), resolved under the same names in THIS module.
scenario = harness.scenario
composition = harness.composition

LIVE_URL_VAR = harness.LIVE_URL_VAR
BACKEND_ROOT = harness.BACKEND_ROOT

#: Same frozen fixture identities as the sibling suites (one vocabulary).
SERVER_ID = harness.SERVER_ID
READ_CAP = harness.READ_CAP
PRIV_CAP = harness.PRIV_CAP

SERVER_UUID = uuid.UUID(SERVER_ID)

#: Every app-owned table this card touches (complete drop/reset list, so
#: this suite composes safely with the sibling suites in ONE session).
DOMAIN_TABLES = (
    "agent_task_completions",
    "agent_task_revisions",
    "agent_task_evidence",
    "agent_task_criteria",
    "agent_task_constraints",
    "agent_task_steps",
    "agent_tasks",
    "job_resource_locks",
    "job_schedule_fires",
    "job_steps",
    "job_events",
    "job_schedules",
    "job_runs",
    "approvals",
    "conversation_steering",
    "conversation_runs",
    "conversation_messages",
    "conversations",
)

INTAKE_EVENT_TYPE = agent_run.AGENT_INTAKE_EVENT_TYPE

_run = harness._run  # the proven persistent test loop


# ---------------------------------------------------------------------------
# disposable PostgreSQL fixture (complete reset; real Alembic chain)
# ---------------------------------------------------------------------------


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(os.path.join(BACKEND_ROOT, "alembic.ini"))


def _drop_all(url: str) -> None:
    """Reset the fixture database to pre-bootstrap state (engine too)."""
    table_list = ", ".join(
        name
        for name in (
            *DOMAIN_TABLES,
            "profile_projection_journal",
            "profile_projection",
            "admin_sessions",
            "admin_credential",
            "notification_alerts",
            "observation_findings",
            "server_observations",
            "knowledge_doc_chunks",
            "knowledge_documents",
            "application_projection_journal",
            "application_projection",
            "alembic_version",
        )
    )
    with psycopg.connect(url) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {table_list}")
        conn.execute("DROP SEQUENCE IF EXISTS server_observations_seq")
        conn.execute("DROP SCHEMA IF EXISTS dbos CASCADE")
        conn.commit()


@pytest.fixture(scope="session")
def task_run_url() -> str:
    """Disposable fixture database migrated from zero through the head."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the intake-runner claims need a "
            "real disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        try:
            _drop_all(url)
            command.upgrade(_alembic_config(), "head")
        finally:
            if previous is None:
                os.environ.pop("MAI_DATABASE_URL", None)
            else:
                os.environ["MAI_DATABASE_URL"] = previous
        job_engine.launch_worker(url)  # creates the engine-owned dbos schema
        job_engine.shutdown()
        yield url
    finally:
        job_engine.shutdown()
        _drop_all(url)


@pytest.fixture
async def sessions(task_run_url: str):
    """NullPool session factory; ONE truncated domain per test."""
    eng, factory = create_app_engine(task_run_url)
    yield factory
    try:
        async with factory() as session:
            present = (
                await session.execute(
                    text(
                        "SELECT c.relname FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                        "AND c.relname = ANY(:names)"
                    ),
                    {"names": list(DOMAIN_TABLES)},
                )
            ).scalars().all()
            if present:
                await session.execute(
                    text(f"TRUNCATE {', '.join(sorted(present))} CASCADE")
                )
            await session.commit()
    finally:
        await eng.dispose()


@pytest.fixture
def domain(task_run_url: str):
    """Sync-friendly variant for the subprocess scenarios: the same clean
    domain, driven through the harness's persistent loop."""
    eng, factory = create_app_engine(task_run_url)
    yield factory
    try:
        _run(_aclean_domain(factory))
    finally:
        _run(eng.dispose())


async def _aclean_domain(factory) -> None:
    async with factory() as session:
        present = (
            await session.execute(
                text(
                    "SELECT c.relname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                    "AND c.relname = ANY(:names)"
                ),
                {"names": list(DOMAIN_TABLES)},
            )
        ).scalars().all()
        if present:
            await session.execute(
                text(f"TRUNCATE {', '.join(sorted(present))} CASCADE")
            )
        await session.commit()


# ---------------------------------------------------------------------------
# composition helpers (mirrors the proven 06 harness wiring, in-process)
# ---------------------------------------------------------------------------


class JournalExecutor:
    """Journaled capability executor: effect markers make the gate's
    "nothing ran" claim observable (same vocabulary as the 06 harness)."""

    def __init__(self, journal: Path) -> None:
        self.journal = journal

    def _mark(self, marker: str) -> None:
        with open(self.journal, "a", encoding="utf-8") as handle:
            handle.write(marker + "\n")

    @pytest.mark.anyio
    async def __call__(self, call):
        capability_id = call.descriptor.identifier
        marker = call.payload.get("marker", "?")
        self._mark(f"begin:{capability_id}:{marker}")
        self._mark(f"effect:{capability_id}:{marker}")
        document = {
            "status": "succeeded",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
        }
        return ExecutorResult(ok=True, output=json.dumps(document, sort_keys=True))


def _descriptor(capability_id: str, effect_class: str):
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
            "output_schema": {
                "type": "object",
                "required": ["status", "exit_code", "stdout", "stderr"],
                "properties": {
                    "status": {"type": "string"},
                    "exit_code": {"type": ["integer", "null"]},
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                },
            },
            "execution_environments": ["linux_ssh"],
            "effect_class": effect_class,
            "resource_scope": [] if effect_class == "read" else ["target.exec"],
            "required_grants": [],
        },
        source=f"<{capability_id} descriptor>",
    )


class DispatchModel:
    """FunctionModel dispatcher: the tool-free intake call is recognized
    by its canonical-snapshot prompt; executor rounds replay the round
    script. Per-channel call counters make the usage accounting honest."""

    def __init__(
        self,
        intake_payloads: list[dict],
        round_entries: list[dict],
        journal: Path | None = None,
        soft: bool = False,
    ) -> None:
        self.intake_payloads = intake_payloads
        self.round_entries = round_entries
        self.journal = journal
        #: ``soft=True``: an exhausted round script replies with a plain
        #: text turn instead of raising (for tests that do not pin the
        #: exact compaction call count).
        self.soft = soft
        self.intake_calls = 0
        self.round_calls = 0
        #: Full prompt text of each intake call (pinned-scope proofs).
        self.intake_prompts: list[str] = []

    @property
    def model(self) -> FunctionModel:
        return FunctionModel(self._respond)

    def _mark(self, marker: str) -> None:
        if self.journal is not None:
            with open(self.journal, "a", encoding="utf-8") as handle:
                handle.write(marker + "\n")

    def _respond(self, messages, info):  # noqa: ANN001 - FunctionModel ABI
        last = messages[-1]
        prompt_text = "".join(
            str(getattr(part, "content", "") or "") for part in last.parts
        )
        if "# Canonical snapshot" in prompt_text:
            if self.intake_calls >= len(self.intake_payloads):
                raise AssertionError("scripted intake model exhausted")
            self._mark("intake_call")
            self.intake_prompts.append(prompt_text)
            self.intake_calls += 1
            payload = self.intake_payloads[self.intake_calls - 1]
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="final_result",
                        args={"response": payload},
                        tool_call_id=f"i{self.intake_calls}",
                    )
                ]
            )
        if self.round_calls >= len(self.round_entries):
            if self.soft:
                self.round_calls += 1
                return ModelResponse(parts=[TextPart(content="ok")])
            raise AssertionError("scripted round model exhausted")
        self._mark("round_call")
        self.round_calls += 1
        entry = self.round_entries[self.round_calls - 1]
        if "tool_call" in entry:
            call = entry["tool_call"]
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=call["name"],
                        args=call.get("args", {"marker": "m1"}),
                        tool_call_id=call.get("id", "c1"),
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content=entry.get("text", "done"))])


def _prepare_roots(tmp_path: Path) -> tuple[Path, Path]:
    """Write the fixture model/data roots ONCE per test directory."""
    model_root = tmp_path / "model-root"
    data_root = tmp_path / "data-root"
    sentinel = tmp_path / ".roots-ready"
    if not sentinel.exists():
        harness._write_model_root(
            model_root, base_url="http://127.0.0.1:9/v1", credential=False
        )
        harness._write_data_root(data_root)
        sentinel.write_text("ready", encoding="utf-8")
    return model_root, data_root


def _compose(
    sessions: async_sessionmaker[AsyncSession],
    task_run_url: str,
    tmp_path: Path,
    model: FunctionModel,
    *,
    data_root: Path | None = None,
) -> tuple[agent_run.AgentRunExecutor, JournalExecutor]:
    """The executor composition with the REAL dispatcher (gate tests).

    ``data_root`` overrides the fixture data root (pinned-scope tests
    enroll the SAME fixture server under the canonical name "Frame").
    """
    registry = CapabilityRegistry()
    registry.register(_descriptor(READ_CAP, "read"))
    registry.register(_descriptor(PRIV_CAP, "privileged"))
    # Task-state tools ride the SAME fixture descriptors: the phase gate
    # maps them by capability id (§6), so their registered effect class
    # only matters for the target-capability control cases.
    registry.register(_descriptor(TASK_READ_CAPABILITY, "read"))
    registry.register(_descriptor(TASK_PROPOSE_UPDATE_CAPABILITY, "mutate"))
    registry.register(_descriptor(TASK_UPDATE_ITEM_CAPABILITY, "mutate"))
    journal = tmp_path / "journal.log"
    executor = JournalExecutor(journal)
    dispatcher = CapabilityDispatcher(
        registry=registry,
        approvals=ApprovalStore(sessions),
        grants=lambda: {
            READ_CAP: frozenset({SERVER_UUID}),
            PRIV_CAP: frozenset({SERVER_UUID}),
            TASK_READ_CAPABILITY: frozenset({SERVER_UUID}),
            TASK_PROPOSE_UPDATE_CAPABILITY: frozenset({SERVER_UUID}),
            TASK_UPDATE_ITEM_CAPABILITY: frozenset({SERVER_UUID}),
        },
        policy=lambda: Policy(revision=1, denied_capabilities=frozenset()),
        adopted=lambda server_id: True,
        credentials=lambda: EnvResolver(_write_env(tmp_path)),
        executor=executor,
    )
    model_root, fixture_data_root = _prepare_roots(tmp_path)
    data_root = data_root if data_root is not None else fixture_data_root
    composed = agent_run.AgentRunExecutor(
        sessions=sessions,
        dispatcher=dispatcher,
        registry=registry,
        model_store=ModelConfigStore(model_root),
        data_root=data_root,
        locks=ResourceLockService(task_run_url),
        resolver=EnvResolver(_write_env(tmp_path)),
        model_builder=lambda **kwargs: model,
    )
    return composed, executor


def _write_env(tmp_path: Path) -> str:
    path = tmp_path / "fixture.env"
    if not path.exists():
        path.write_text("", encoding="utf-8")
        os.chmod(path, 0o600)
    return str(path)


@pytest.mark.anyio
async def _ainsert_run(
    sessions: async_sessionmaker[AsyncSession],
    run_id: str,
) -> None:
    async with sessions() as session:
        await session.execute(
            text(
                """
                INSERT INTO job_runs
                    (run_id, server_id, depth, profile_revision,
                     prompt_revision, model_profile, requested_grants,
                     submitted_at, updated_at)
                VALUES
                    (:run_id, :server_id, 0, 1, 1, 'fast',
                     :requested_grants, now(), now())
                """
            ),
            {
                "run_id": uuid.UUID(run_id),
                "server_id": SERVER_UUID,
                "requested_grants": json.dumps([READ_CAP, PRIV_CAP]),
            },
        )
        await session.commit()


@pytest.mark.anyio
async def _alinked_turn(
    sessions: async_sessionmaker[AsyncSession],
    run_id: str,
    message: str = "plan the rollout",
) -> tuple[uuid.UUID, uuid.UUID]:
    """One accepted canonical turn bound to ``run_id`` (submit + attach)."""
    conversation = await acreate_conversation(sessions, server_id=SERVER_UUID)
    result = await asubmit_turn(
        sessions,
        conversation_id=conversation.conversation_id,
        server_id=SERVER_UUID,
        client_request_id=uuid.UUID(run_id),
        message=message,
    )
    await aattach_run(
        sessions,
        conversation_id=conversation.conversation_id,
        server_id=SERVER_UUID,
        run_id=uuid.UUID(run_id),
    )
    return conversation.conversation_id, result.message.message_id


@pytest.mark.anyio
async def _aresubmit_same_turn(
    sessions: async_sessionmaker[AsyncSession],
    conversation_id: uuid.UUID,
    run_id: str,
    message: str,
) -> tuple[str, uuid.UUID]:
    """The C4 store belt: the SAME ``client_request_id`` replays onto the
    ORIGINAL row (deduplicated), never a second message."""
    result = await asubmit_turn(
        sessions,
        conversation_id=conversation_id,
        server_id=SERVER_UUID,
        client_request_id=uuid.UUID(run_id),
        message=message,
    )
    return result.disposition.value, result.message.message_id


@pytest.mark.anyio
async def _fetchone(sessions, sql: str, **params: object):
    async with sessions() as session:
        return (await session.execute(text(sql), params)).first()


@pytest.mark.anyio
async def _count(sessions, table: str) -> int:
    row = await _fetchone(sessions, f"SELECT COUNT(*) FROM {table}")
    return int(row[0])


def _events_of(task_run_url: str, run_id: str) -> list[dict]:
    with psycopg.connect(task_run_url) as conn:
        rows = conn.execute(
            "SELECT type, data FROM job_events WHERE run_id = %s ORDER BY seq",
            (uuid.UUID(run_id),),
        ).fetchall()
    return [{"type": row[0], "data": row[1]} for row in rows]


def _intent_of(run_id: str) -> agent_run.AgentRunIntent:
    return agent_run.parse_agent_run_intent(
        harness._canonical(harness._intent_document(run_id))
    )


def _read_step_rows(task_run_url: str, run_id: str) -> list[dict]:
    return harness._read_step_rows(task_run_url, run_id)


# ---------------------------------------------------------------------------
# the checkpointed intake seam (in-process, real store; C4 belt + AC 1)
# ---------------------------------------------------------------------------


class TestIntakeCheckpointSeam:
    @pytest.mark.anyio
    async def test_answer_variant_accounts_usage_without_task_machinery(
        self, sessions, task_run_url, tmp_path
    ) -> None:
        run_id = str(uuid.uuid4())
        conversation_id, message_id = await _alinked_turn(sessions, run_id)
        await _ainsert_run(sessions, run_id)
        scripted = DispatchModel(
            [intake_fixtures._answer_payload(message_id, "4")], []
        )
        executor, _journal = _compose(sessions, task_run_url, tmp_path, scripted.model)

        result = _run(
            executor.ainterpret_intake_turn(_intent_of(run_id), _pins_of(executor))
        )

        assert result["interpreted"] is True
        assert result["turn_kind"] == "answer"
        assert result["applied"] is False
        assert result["task_id"] is None
        assert result["output_text"] == "4"
        # Planner usage accounting is honest and bounded (AC 1).
        usage = result["planner_usage"]
        assert usage == {
            "model_calls": 1,
            "attempts": 1,
            "repaired": False,
            "function_tools": 0,
        }
        assert scripted.intake_calls == 1 and scripted.round_calls == 0
        # The value-free usage event landed exactly once (C05 rule 4).
        events = [
            event
            for event in _events_of(task_run_url, run_id)
            if event["type"] == INTAKE_EVENT_TYPE
        ]
        assert len(events) == 1
        data = events[0]["data"]
        assert data["interpreted"] is True and data["rejected"] is False
        assert data["planner_model_calls"] == 1
        assert data["planner_function_tools"] == 0
        # P1: no task machinery of any kind.
        assert await _count(sessions, "agent_tasks") == 0
        assert await _count(sessions, "agent_task_steps") == 0

    @pytest.mark.anyio
    async def test_plan_variant_binds_task_link_and_header(
        self, sessions, task_run_url, tmp_path
    ) -> None:
        run_id = str(uuid.uuid4())
        conversation_id, message_id = await _alinked_turn(sessions, run_id)
        await _ainsert_run(sessions, run_id)
        payload = intake_fixtures._plan_payload(
            message_id,
            operations=[intake_fixtures._add_op("s1_build")],
            phase="act",
            constraints=[
                intake_fixtures._user_constraint(
                    "c_scope", message_id, "deploy only to the staging target"
                )
            ],
        )
        scripted = DispatchModel([payload], [])
        executor, _journal = _compose(sessions, task_run_url, tmp_path, scripted.model)

        result = _run(
            executor.ainterpret_intake_turn(_intent_of(run_id), _pins_of(executor))
        )

        assert result["applied"] is True and result["disposition"] == "created"
        task_id = uuid.UUID(str(result["task_id"]))
        # The run link row names the episode this run contributes to (§1).
        link = await _fetchone(
            sessions,
            "SELECT task_id FROM conversation_runs WHERE run_id = :r",
            r=uuid.UUID(run_id),
        )
        assert link is not None and uuid.UUID(str(link[0])) == task_id
        # The conversation's active episode is the created task.
        row = await _fetchone(
            sessions,
            "SELECT active_task_id FROM conversations WHERE conversation_id = :c",
            c=conversation_id,
        )
        assert uuid.UUID(str(row[0])) == task_id
        # The bound header carries the validated state (bounded pages,
        # omission stated).
        header = result["task_header"]
        assert header is not None
        assert header["revision"] == 1
        assert header["requested_phase"] == "act"
        assert [step["step_id"] for step in header["steps"]] == ["s1_build"]
        assert header["steps_omitted"] is False
        assert header["constraints"][0]["statement"] == (
            "deploy only to the staging target"
        )
        assert header["constraints"][0]["origin"] == "user"

    @pytest.mark.anyio
    async def test_continue_variant_applies_delta_to_same_task(
        self, sessions, task_run_url, tmp_path
    ) -> None:
        run_id = str(uuid.uuid4())
        conversation_id, first_message = await _alinked_turn(sessions, run_id)
        await _ainsert_run(sessions, run_id)
        plan = DispatchModel(
            [
                intake_fixtures._plan_payload(
                    first_message,
                    operations=[intake_fixtures._add_op("s1_build")],
                    phase="act",
                )
            ],
            [],
        )
        executor, _journal = _compose(sessions, task_run_url, tmp_path, plan.model)
        created = _run(
            executor.ainterpret_intake_turn(_intent_of(run_id), _pins_of(executor))
        )
        task_id = uuid.UUID(str(created["task_id"]))

        # The steering path: an ACTIVE-run follow-up message is delivered
        # through the durable steering queue (07 seam); the accepted turn
        # for a NEW run needs a fresh run identity, so the delta here is
        # the direct next-episode form: a second accepted turn on a NEW
        # run after the first released the lease.
        follow_up = await _fetchone(
            sessions,
            "SELECT seq FROM conversation_messages WHERE conversation_id = :c "
            "ORDER BY seq DESC LIMIT 1",
            c=conversation_id,
        )
        assert follow_up is not None
        second_run = str(uuid.uuid4())
        result = await asubmit_turn(
            sessions,
            conversation_id=conversation_id,
            server_id=SERVER_UUID,
            client_request_id=uuid.UUID(second_run),
            message="also rename the build step",
        )
        # Unit seam: release run #1's writer lease exactly like the
        # workflow's terminal finish does, so the follow-up run can
        # attach (ONE active lease per conversation).
        await arelease_run(
            sessions,
            conversation_id=conversation_id,
            server_id=SERVER_UUID,
            run_id=uuid.UUID(run_id),
        )
        await aattach_run(
            sessions,
            conversation_id=conversation_id,
            server_id=SERVER_UUID,
            run_id=uuid.UUID(second_run),
        )
        await _ainsert_run(sessions, second_run)
        # The lease is held by run #1 in this unit seam (released only at
        # workflow finish); the intake under test accepts the second turn
        # directly, which is exactly the workflow's own seam order.
        delta = DispatchModel(
            [
                intake_fixtures._continue_payload(
                    result.message.message_id,
                    created["revision"],
                    intake_fixtures._update_op("s1_build", title="build renamed"),
                )
            ],
            [],
        )
        executor2, _journal2 = _compose(
            sessions, task_run_url, tmp_path, delta.model
        )
        applied = _run(
            executor2.ainterpret_intake_turn(
                agent_run.parse_agent_run_intent(
                    harness._canonical(
                        harness._intent_document(second_run)
                    )
                ),
                _pins_of(executor2),
            )
        )

        # AC 2: the delta hit the SAME task; completed steps preserved.
        assert applied["applied"] is True
        assert applied["disposition"] == "created"  # a new accepted revision
        assert uuid.UUID(str(applied["task_id"])) == task_id
        header = applied["task_header"]
        assert header["revision"] == created["revision"] + 1
        steps = {step["step_id"]: step for step in header["steps"]}
        assert steps["s1_build"]["title"] == "build renamed"
        _ = follow_up  # the transcript read stays canonical (no dup turns)

    @pytest.mark.anyio
    async def test_interpret_twice_deduplicates_onto_same_task(
        self, sessions, task_run_url, tmp_path
    ) -> None:
        """The store belt of C4: re-interpreting the SAME accepted turn
        (lost checkpoint) never creates a second task."""
        run_id = str(uuid.uuid4())
        conversation_id, message_id = await _alinked_turn(sessions, run_id)
        await _ainsert_run(sessions, run_id)
        payload = intake_fixtures._plan_payload(
            message_id, operations=[intake_fixtures._add_op("s1_build")]
        )
        first_model = DispatchModel([payload], [])
        executor, _journal = _compose(
            sessions, task_run_url, tmp_path, first_model.model
        )
        first = _run(
            executor.ainterpret_intake_turn(_intent_of(run_id), _pins_of(executor))
        )
        second_model = DispatchModel([payload], [])
        executor2, _journal2 = _compose(
            sessions, task_run_url, tmp_path, second_model.model
        )
        second = _run(
            executor2.ainterpret_intake_turn(_intent_of(run_id), _pins_of(executor2))
        )
        assert first["disposition"] == "created"
        assert second["disposition"] == "deduplicated"
        assert second["task_id"] == first["task_id"]
        assert await _count(sessions, "agent_tasks") == 1
        assert await _count(sessions, "agent_task_steps") == 1

    @pytest.mark.anyio
    async def test_invalid_intake_fails_closed_with_value_free_fields(
        self, sessions, task_run_url, tmp_path
    ) -> None:
        """P4 through the run seam: bounded repair then fail-closed; the
        rejection event carries value-free fields; zero effects."""
        run_id = str(uuid.uuid4())
        conversation_id, message_id = await _alinked_turn(sessions, run_id)
        await _ainsert_run(sessions, run_id)
        wrong_source = intake_fixtures._answer_payload(uuid.uuid4(), "4")
        scripted = DispatchModel([wrong_source, wrong_source], [])
        executor, _journal = _compose(sessions, task_run_url, tmp_path, scripted.model)

        with pytest.raises(agent_run.AgentStageError) as excinfo:
            _run(
                executor.ainterpret_intake_turn(
                    _intent_of(run_id), _pins_of(executor)
                )
            )
        assert excinfo.value.reason == agent_run.INTAKE_REJECTED_REASON
        assert scripted.intake_calls == 2  # one bounded repair, then closed
        events = [
            event
            for event in _events_of(task_run_url, run_id)
            if event["type"] == INTAKE_EVENT_TYPE
        ]
        assert len(events) == 1
        data = events[0]["data"]
        assert data["rejected"] is True
        assert data["fields"] == {"source_message_id": "source_message_mismatch"}
        assert data["model_calls"] == 2
        # No task machinery, no run link binding (nothing to bind).
        assert await _count(sessions, "agent_tasks") == 0
        link = await _fetchone(
            sessions,
            "SELECT task_id FROM conversation_runs WHERE run_id = :r",
            r=uuid.UUID(run_id),
        )
        assert link is not None and link[0] is None

    @pytest.mark.anyio
    async def test_unlinked_run_is_not_interpreted(
        self, sessions, task_run_url, tmp_path
    ) -> None:
        """Legacy runs (no canonical conversation) keep today's behavior:
        no intake, no model call, no event."""
        run_id = str(uuid.uuid4())
        await _ainsert_run(sessions, run_id)
        scripted = DispatchModel(
            [intake_fixtures._answer_payload(uuid.uuid4(), "4")], []
        )
        executor, _journal = _compose(sessions, task_run_url, tmp_path, scripted.model)
        result = _run(
            executor.ainterpret_intake_turn(_intent_of(run_id), _pins_of(executor))
        )
        assert result == {"interpreted": False, "reason": "no_conversation_link"}
        assert scripted.intake_calls == 0
        assert _events_of(task_run_url, run_id) == []


def _pins_of(executor: agent_run.AgentRunExecutor) -> dict[str, object]:
    """Bind the fixture model store exactly like ``abind_model``."""
    binding = executor._model_store.bind(None, None)  # noqa: SLF001 - unit seam
    connection_doc = executor._model_store.read_connection(binding.connection.name)
    profile_doc = executor._model_store.read_profile(binding.profile.name)
    return {
        "connection_name": binding.connection.name,
        "profile_name": binding.profile.name,
        "model": binding.profile.model,
        "connection_text": connection_doc.text,
        "profile_text": profile_doc.text,
        "connection_revision": binding.connection_revision,
        "profile_revision": binding.profile_revision,
        "connection_sha256": binding.connection_sha256,
        "profile_sha256": binding.profile_sha256,
    }


# ---------------------------------------------------------------------------
# the phase gate (in-process, real dispatcher; AC 1)
# ---------------------------------------------------------------------------


def _tool_call(
    capability_id: str, marker: str = "m1", call_id: str = "c1"
) -> dict[str, object]:
    return {
        "tool_call_id": call_id,
        "tool_name": capability_id.replace(".", "_"),
        "capability_id": capability_id,
        "payload": {"marker": marker},
        "payload_hash": canonical_payload_hash({"marker": marker}),
        "seq": 1,
    }


@pytest.mark.anyio
async def _abind_phase(
    sessions: async_sessionmaker[AsyncSession],
    run_id: str,
    phase: str,
) -> uuid.UUID:
    """Bind the run link to a task IN ``phase`` (the gate's exact read)."""
    async with sessions() as session:
        created = await session.execute(
            text(
                """
                INSERT INTO agent_tasks
                    (task_id, conversation_id, server_id, title, summary,
                     requested_phase, status, revision, client_request_id,
                     created_at, updated_at)
                SELECT gen_random_uuid(), r.conversation_id, c.server_id,
                       'gate task', 'gate objective', :phase, 'in_progress',
                       1, gen_random_uuid(), now(), now()
                FROM conversation_runs r
                JOIN conversations c ON c.conversation_id = r.conversation_id
                WHERE r.run_id = :rid
                RETURNING task_id
                """
            ),
            {"phase": phase, "rid": uuid.UUID(run_id)},
        )
        task_id = uuid.UUID(str(created.scalar_one()))
        await session.execute(
            text(
                "UPDATE conversation_runs SET task_id = :task "
                "WHERE run_id = :rid"
            ),
            {"task": task_id, "rid": uuid.UUID(run_id)},
        )
        await session.commit()
    return task_id


class TestPhaseGate:
    @pytest.mark.anyio
    async def _denied_or_dispatched(
        self,
        sessions,
        task_run_url,
        tmp_path,
        run_id: str,
        capability_id: str,
        allowed: tuple[str, ...] | None = None,
    ) -> tuple[dict[str, object], list[str]]:
        composed, journal_executor = _compose(
            sessions, task_run_url, tmp_path, DispatchModel([], []).model
        )
        intent = (
            _intent_of(run_id)
            if allowed is None
            else agent_run.parse_agent_run_intent(
                harness._canonical(harness._intent_document(run_id, allowed=allowed))
            )
        )
        outcome = _run(composed.aexecute_tool(intent, _tool_call(capability_id)))
        journal_path = journal_executor.journal
        lines = (
            journal_path.read_text().splitlines()
            if journal_path.exists()
            else []
        )
        return outcome, lines

    @pytest.mark.anyio
    async def test_plan_phase_denies_read_before_fence(
        self, sessions, task_run_url, tmp_path
    ) -> None:
        run_id = str(uuid.uuid4())
        await _alinked_turn(sessions, run_id)
        await _ainsert_run(sessions, run_id)
        await _abind_phase(sessions, run_id, "plan")

        outcome, journal = await self._denied_or_dispatched(
            sessions, task_run_url, tmp_path, run_id, READ_CAP
        )

        # Denied BEFORE the effect fence: honest failed row, nothing ran.
        assert outcome["state"] == "failed"
        assert outcome["reason"] == "phase_not_allowed"
        receipt = await _fetchone(
            sessions,
            "SELECT receipt FROM job_steps WHERE run_id = :r AND seq = 1",
            r=uuid.UUID(run_id),
        )
        parsed = receipt[0] if isinstance(receipt[0], dict) else json.loads(str(receipt[0]))
        assert parsed["effect"] == EFFECT_NOT_ATTEMPTED
        assert parsed["reason"] == "phase_not_allowed"
        assert parsed["error_type"] == "collect_evidence_in_plan"
        assert journal == []  # no begin, no effect: nothing was even planned

    @pytest.mark.anyio
    async def test_inspect_phase_allows_read_and_gates_effects(
        self, sessions, task_run_url, tmp_path
    ) -> None:
        run_id = str(uuid.uuid4())
        await _alinked_turn(sessions, run_id)
        await _ainsert_run(sessions, run_id)
        await _abind_phase(sessions, run_id, "inspect")

        outcome, journal = await self._denied_or_dispatched(
            sessions, task_run_url, tmp_path, run_id, READ_CAP
        )
        assert outcome["state"] == "succeeded"
        assert f"effect:{READ_CAP}:m1" in journal

        priv_run = str(uuid.uuid4())
        await _alinked_turn(sessions, priv_run)
        await _ainsert_run(sessions, priv_run)
        await _abind_phase(sessions, priv_run, "inspect")
        marker_lines = len(journal)  # the read case's markers
        priv_outcome, priv_journal = await self._denied_or_dispatched(
            sessions, task_run_url, tmp_path, priv_run, PRIV_CAP
        )
        assert priv_outcome["state"] == "failed"
        assert priv_outcome["reason"] == "phase_not_allowed"
        # The privileged effect never reached the executor (same journal
        # file per test: no NEW markers after the denial).
        assert len(priv_journal) == marker_lines

    @pytest.mark.anyio
    async def test_act_phase_allows_effects(
        self, sessions, task_run_url, tmp_path
    ) -> None:
        run_id = str(uuid.uuid4())
        await _alinked_turn(sessions, run_id)
        await _ainsert_run(sessions, run_id)
        await _abind_phase(sessions, run_id, "act")

        outcome, journal = await self._denied_or_dispatched(
            sessions, task_run_url, tmp_path, run_id, READ_CAP
        )
        assert outcome["state"] == "succeeded"
        assert f"effect:{READ_CAP}:m1" in journal

    @pytest.mark.anyio
    async def test_unbound_run_is_not_gated(
        self, sessions, task_run_url, tmp_path
    ) -> None:
        """No task link → the gate is inert (legacy behavior preserved)."""
        run_id = str(uuid.uuid4())
        await _alinked_turn(sessions, run_id)
        await _ainsert_run(sessions, run_id)

        outcome, journal = await self._denied_or_dispatched(
            sessions, task_run_url, tmp_path, run_id, READ_CAP
        )
        assert outcome["state"] == "succeeded"
        assert f"effect:{READ_CAP}:m1" in journal

    @pytest.mark.anyio
    async def test_task_state_tools_allowed_in_plan_and_inspect_phases(
        self, sessions, task_run_url, tmp_path
    ) -> None:
        """Task-state tools are run control, never target effects: the
        §6 gate maps them to always-allowed actions (``task.propose_update``
        → PLAN_PROPOSAL, ``task.read``/``task.update_item`` →
        METADATA_UPDATE), so they dispatch in EVERY phase — while target
        capabilities stay strictly gated in the same phases.
        """
        # The run context enables the task tools next to the target
        # capabilities (the same allowlist for the controls below, so the
        # outcomes are phase-driven, not allowlist-driven).
        allowed = (
            READ_CAP,
            PRIV_CAP,
            TASK_READ_CAPABILITY,
            TASK_PROPOSE_UPDATE_CAPABILITY,
            TASK_UPDATE_ITEM_CAPABILITY,
        )
        for capability_id in (
            TASK_READ_CAPABILITY,
            TASK_PROPOSE_UPDATE_CAPABILITY,
            TASK_UPDATE_ITEM_CAPABILITY,
        ):
            for phase in ("plan", "inspect", "act"):
                run_id = str(uuid.uuid4())
                await _alinked_turn(sessions, run_id)
                await _ainsert_run(sessions, run_id)
                await _abind_phase(sessions, run_id, phase)

                outcome, journal = await self._denied_or_dispatched(
                    sessions,
                    task_run_url,
                    tmp_path,
                    run_id,
                    capability_id,
                    allowed=allowed,
                )
                assert outcome["state"] == "succeeded", (
                    capability_id,
                    phase,
                    outcome["reason"],
                )
                assert f"effect:{capability_id}:m1" in journal

        # Controls: target capabilities remain gated in these phases.
        plan_run = str(uuid.uuid4())
        await _alinked_turn(sessions, plan_run)
        await _ainsert_run(sessions, plan_run)
        await _abind_phase(sessions, plan_run, "plan")
        plan_outcome, _plan_journal = await self._denied_or_dispatched(
            sessions,
            task_run_url,
            tmp_path,
            plan_run,
            READ_CAP,
            allowed=allowed,
        )
        assert plan_outcome["state"] == "failed"
        assert plan_outcome["reason"] == "phase_not_allowed"

        inspect_run = str(uuid.uuid4())
        await _alinked_turn(sessions, inspect_run)
        await _ainsert_run(sessions, inspect_run)
        await _abind_phase(sessions, inspect_run, "inspect")
        inspect_outcome, _inspect_journal = await self._denied_or_dispatched(
            sessions,
            task_run_url,
            tmp_path,
            inspect_run,
            PRIV_CAP,
            allowed=allowed,
        )
        assert inspect_outcome["state"] == "failed"
        assert inspect_outcome["reason"] == "phase_not_allowed"


# ---------------------------------------------------------------------------
# pure projections (binding block + no-executor renderings)
# ---------------------------------------------------------------------------


class TestPureProjections:
    def test_task_context_block_carries_binding_and_states_omissions(self) -> None:
        header = {
            "task_id": "0f0e0d0c-0b0a-4988-8776-6554433221ff",
            "revision": 3,
            "requested_phase": "inspect",
            "title": "provision fixture",
            "summary": "provision the fixture service",
            "steps": [
                {
                    "step_id": "s1",
                    "status": "executed",
                    "verification": "passed",
                    "title": "first",
                }
            ],
            "steps_omitted": True,
            "constraints": [
                {
                    "constraint_id": "c1",
                    "origin": "user",
                    "statement": "deploy only to the staging target",
                }
            ],
            "constraints_omitted": False,
        }
        block = agent_run._task_context_block(header)
        assert header["task_id"] in block
        assert "revision 3" in block and "phase inspect" in block
        assert "s1 | executed | passed | first" in block
        assert "[user] deploy only to the staging target" in block
        # Omissions are stated, never hidden (bounded pages).
        assert "plan shows the first 1 steps; more exist" in block
        assert "constraints show the first" not in block

    def test_intake_output_text_renders_no_executor_variants(self) -> None:
        from mai.contracts.conversations import parse_turn_interpretation

        mid = uuid.uuid4()
        answer = parse_turn_interpretation(
            intake_fixtures._answer_payload(mid, "4")
        )
        assert agent_run._intake_output_text(answer) == "4"
        clarify = parse_turn_interpretation(
            intake_fixtures._turn_base(
                "clarify",
                mid,
                understanding="which target?",
                open_questions=[
                    {"proposal_id": "target", "question": "Which target?"}
                ],
            )
        )
        clarify_text = agent_run._intake_output_text(clarify)
        assert "which target?" in clarify_text
        assert "target: Which target?" in clarify_text
        blocked = parse_turn_interpretation(
            intake_fixtures._turn_base(
                "blocked",
                mid,
                understanding="cannot proceed",
                reason="the prerequisite service is missing",
                blocked_kind="missing_prerequisite",
            )
        )
        blocked_text = agent_run._intake_output_text(blocked)
        assert "cannot proceed" in blocked_text
        assert "Blocked: the prerequisite service is missing" in blocked_text

    def test_summary_document_is_value_free(self) -> None:
        document = agent_run._intake_summary_document(
            {
                "interpreted": True,
                "turn_kind": "plan",
                "applied": True,
                "task_id": str(uuid.uuid4()),
                "disposition": "created",
                "planner_usage": {"model_calls": 2, "repaired": True},
                "output_text": "SHOULD NOT LEAK",
            }
        )
        assert document["planner_model_calls"] == 2
        assert document["planner_repaired"] is True
        assert "output_text" not in document
        assert "SHOULD NOT LEAK" not in json.dumps(document)


# ---------------------------------------------------------------------------
# C5 at the seam (in-process): steering + compaction preserve the binding
# ---------------------------------------------------------------------------


class TestBindingPreservedAcrossSteerAndCompaction:
    def _tiny_context_profile(self, tmp_path: Path) -> Path:
        """A model root whose profile declares a TINY context limit, so
        the pinned TieredCompaction stack actually triggers."""
        root = tmp_path / "model-root-tiny"
        store = ModelConfigStore(root)
        connection = {
            "schema_version": MODEL_SCHEMA_VERSION,
            "name": "gateway",
            "base_url": "http://127.0.0.1:9/v1",
            "timeout_seconds": 5,
            "capabilities": {"tool_calls": True},
        }
        store.write_connection(
            "gateway", json.dumps(connection) + "\n", expected_revision=0
        )
        profile = {
            "schema_version": MODEL_SCHEMA_VERSION,
            "name": "tiny",
            "connection": "gateway",
            "model": "fixture-model",
            "max_tokens": 256,
            "context_limit": 64,
        }
        store.write_profile(
            "tiny", json.dumps(profile) + "\n", expected_revision=0
        )
        store.set_selection("gateway", "tiny", expected_revision=0)
        return root

    @pytest.mark.anyio
    async def test_task_binding_survives_steering_and_compaction(
        self, sessions, task_run_url, tmp_path
    ) -> None:
        """AC 2: the validated task header rides the LEADING system part,
        so a steering round and a compaction event leave task id, phase,
        steps and constraints intact in the next checkpointed history."""
        run_id = str(uuid.uuid4())
        conversation_id, message_id = await _alinked_turn(
            sessions, run_id, message="provision the staging service"
        )
        await _ainsert_run(sessions, run_id)
        payload = intake_fixtures._plan_payload(
            message_id,
            operations=[intake_fixtures._add_op("s1_build")],
            phase="act",
            constraints=[
                intake_fixtures._user_constraint(
                    "c_scope", message_id, "deploy only to the staging target"
                )
            ],
        )
        # Soft round fallback: whether the summarizer tier consumes a call
        # depends on the compaction trigger; the binding assertions below
        # are about the HISTORY, not the call count.
        scripted = DispatchModel([payload], [{"text": "all done"}], soft=True)
        composed, _journal_executor = _compose(
            sessions, task_run_url, tmp_path, scripted.model
        )
        bound = _run(
            composed.ainterpret_intake_turn(_intent_of(run_id), _pins_of(composed))
        )
        header = bound["task_header"]
        assert header is not None
        intent = _intent_of(run_id)
        pins = _pins_of(composed)

        # The workflow's exact binding move: the block joins the system
        # prompt BEFORE the seed step builds the checkpointed seed.
        assembled = {
            "system_prompt": (
                "You administer the fixture host."
                + agent_run._task_context_block(header)
            ),
            "user_message": "provision the staging service",
        }
        seed = _run(composed.abuild_conversation_seed(intent, assembled))
        assert seed["report"]["conversation_linked"] is True
        assert "deploy only to the staging target" in str(seed["history_json"])

        # Round 0 UNDER STEERING: the steering rides the next round; the
        # task binding is still in the leading system part of the
        # checkpointed history the round returns.
        steered = _run(
            composed.amodel_round(
                intent,
                pins,
                str(seed["history_json"]),
                None,
                0,
                agent_run.canonical_json(["prefer the staging target"]),
            )
        )
        round0_history = str(steered["history_json"])
        assert "[User steering message]: prefer the staging target" in round0_history
        assert str(header["task_id"]) in round0_history
        assert "deploy only to the staging target" in round0_history

        # A compaction-forcing round (64-token declared window): the task
        # block survives the pinned compaction stack verbatim (leading
        # system parts are re-emitted ahead of any summary).
        model_root = self._tiny_context_profile(tmp_path)
        tiny_store = ModelConfigStore(model_root)
        binding = tiny_store.bind(None, None)
        connection_doc = tiny_store.read_connection(binding.connection.name)
        profile_doc = tiny_store.read_profile(binding.profile.name)
        tiny_pins = {
            "connection_name": binding.connection.name,
            "profile_name": binding.profile.name,
            "model": binding.profile.model,
            "connection_text": connection_doc.text,
            "profile_text": profile_doc.text,
            "connection_revision": binding.connection_revision,
            "profile_revision": binding.profile_revision,
            "connection_sha256": binding.connection_sha256,
            "profile_sha256": binding.profile_sha256,
        }
        compacted = _run(
            composed.amodel_round(
                intent, tiny_pins, round0_history, None, 1, None
            )
        )
        round1_history = str(compacted["history_json"])
        assert str(header["task_id"]) in round1_history
        assert "phase act" in round1_history
        assert "deploy only to the staging target" in round1_history
        assert "s1_build" in round1_history


# ---------------------------------------------------------------------------
# worker scenarios (the SAME proven 06 harness; real subprocesses)
# ---------------------------------------------------------------------------


def _count_marker(scenario: Path, marker: str) -> int:
    return harness._journal_count(scenario, marker)


def _intake_event(task_run_url: str, run_id: str) -> dict | None:
    events = [
        event
        for event in _events_of(task_run_url, run_id)
        if event["type"] == INTAKE_EVENT_TYPE
    ]
    return events[0] if events else None


@pytest.mark.anyio
async def _aenqueue_steering(
    sessions: async_sessionmaker[AsyncSession],
    conversation_id: uuid.UUID,
    run_id: str,
    action: SteerAction,
    content: str | None,
) -> None:
    await aenqueue_steering(
        sessions,
        conversation_id=conversation_id,
        server_id=SERVER_UUID,
        action=action,
        client_request_id=uuid.uuid4(),
        content=content,
        run_id=uuid.UUID(run_id),
    )


class TestWorkerScenarios:
    def test_c4_same_client_request_id_never_duplicates(
        self, task_run_url, domain, scenario, composition
    ) -> None:
        """C4 (worker level): the REAL workflow runs the checkpointed
        intake once; the SAME ``client_request_id`` replays onto the SAME
        message and task — one intake result, one task row, no duplicated
        model call."""
        composition()
        harness._write_policy_files(scenario, grants=[{READ_CAP: True}])
        run_id = str(uuid.uuid4())
        conversation_id, message_id = _run(_alinked_turn(domain, run_id))
        _run(_ainsert_run(domain, run_id))
        intake_payload = intake_fixtures._plan_payload(
            message_id,
            operations=[intake_fixtures._add_op("s1_build")],
            phase="act",
        )
        harness._script(
            scenario,
            [
                {
                    "tool_call": {
                        "name": "final_result",
                        "args": {"response": intake_payload},
                    }
                },
                harness._tool_call_entry(),
                {"text": "all done"},
            ],
        )
        document = harness._intent_document(
            run_id, connection="gateway", profile="fast", wait_poll_seconds=0.5
        )
        client = EngineClient(task_run_url)
        worker = None
        try:
            worker = harness._spawn_worker(
                task_run_url, scenario / "handlers", scenario, "w1"
            )
            _run(harness._enqueue_run(client, _noop_insert, run_id, document))
            harness._wait_until(
                lambda: (harness._terminal_engine_state(client, run_id) or "")
                == "succeeded",
                harness.RUN_TIMEOUT,
                "run success",
            )
        finally:
            if worker is not None:
                harness._stop(worker)
            client.close()

        # ONE checkpointed intake result with honest planner accounting.
        intake_events = [
            event
            for event in _events_of(task_run_url, run_id)
            if event["type"] == INTAKE_EVENT_TYPE
        ]
        assert len(intake_events) == 1
        assert intake_events[0]["data"]["turn_kind"] == "plan"
        assert intake_events[0]["data"]["applied"] is True
        assert intake_events[0]["data"]["planner_model_calls"] == 1
        # Model calls: intake (1) + THREE executor rounds. The 06 harness
        # derives the script position from the durable message count, and
        # an empty-prompt round carries no extra message, so round 0
        # re-reads entry 0 (the final_result tool call — an UNKNOWN tool
        # for the executor surface, denied honestly with no effect),
        # round 1 runs the real fixture_read effect, round 2 finishes.
        # A replayed INTAKE would still show up here (planner event) and
        # as a second task; both are asserted below.
        assert _count_marker(scenario, "model_call") == 4
        assert _count_marker(scenario, "effect:fixture.read:m1") == 1
        with psycopg.connect(task_run_url) as conn:
            tasks = conn.execute(
                "SELECT task_id FROM agent_tasks"
            ).fetchall()
            steps = conn.execute(
                "SELECT COUNT(*) FROM agent_task_steps"
            ).fetchone()[0]
            link = conn.execute(
                "SELECT task_id FROM conversation_runs WHERE run_id = %s",
                (uuid.UUID(run_id),),
            ).fetchone()[0]
        assert len(tasks) == 1 and steps == 1
        task_id = tasks[0][0]
        assert link is not None and str(link) == str(task_id)
        completed = [
            event
            for event in _events_of(task_run_url, run_id)
            if event["type"] == "agent.completed"
        ]
        assert len(completed) == 1
        with psycopg.connect(task_run_url) as conn:
            succeeded_steps = conn.execute(
                "SELECT COUNT(*) FROM job_steps WHERE state = 'succeeded'"
            ).fetchone()[0]
        assert succeeded_steps == 1  # the real effect ran exactly once
        # The store belt (C4): the SAME client_request_id + same projected
        # content deduplicates onto the ORIGINAL row — the intake key
        # therefore names ONE task forever.
        disposition, message = _run(
            _aresubmit_same_turn(
                domain, conversation_id, run_id, "plan the rollout"
            )
        )
        assert disposition == "deduplicated"
        assert message == message_id
        with psycopg.connect(task_run_url) as conn:
            assert (
                conn.execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0]
                == 1
            )


async def _asteering_rows(
    sessions: async_sessionmaker[AsyncSession],
    conversation_id: uuid.UUID,
) -> list[dict]:
    async with sessions() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT action, applied_at FROM conversation_steering "
                    "WHERE conversation_id = :c ORDER BY created_at, steering_id"
                ),
                {"c": conversation_id},
            )
        ).all()
    return [{"action": row[0], "applied_at": row[1]} for row in rows]


@pytest.mark.anyio
async def _noop_insert(run_id: str) -> None:
    """The ``job_runs`` row exists already (the test inserted it)."""
    return None


# ---------------------------------------------------------------------------
# pinned server scope in the FIRST intake call (chat-server-scope-fix)
# ---------------------------------------------------------------------------


def _frame_data_root(tmp_path: Path) -> Path:
    """The SAME fixture server enrolled under the canonical name "Frame"
    (SteamOS Steam Frame VR headset acceptance wording)."""
    from mai.profiles.files import ProfileFileStore

    root = tmp_path / "frame-data-root"
    harness._write_data_root(root)
    store = ProfileFileStore(root)
    stored = store.read(SERVER_UUID)
    store.write(
        SERVER_UUID,
        stored.text.replace('name: "agent-run-fixture"', 'name: "Frame"'),
        expected_revision=stored.revision,
    )
    return root


class TestPinnedScopeFirstIntakeCall:
    @pytest.mark.anyio
    async def test_ports_wording_gets_frame_scope_and_inspects(
        self, sessions, task_run_url, tmp_path
    ) -> None:
        run_id = str(uuid.uuid4())
        conversation_id, message_id = await _alinked_turn(
            sessions, run_id, message="Which ports are open on the device?"
        )
        await _ainsert_run(sessions, run_id)
        payload = intake_fixtures._plan_payload(
            message_id,
            objective="Inspect listening/open ports on Frame",
            operations=[intake_fixtures._add_op("s1_ports")],
            phase="inspect",
        )
        scripted = DispatchModel([payload], [])
        executor, journal_executor = _compose(
            sessions,
            task_run_url,
            tmp_path,
            scripted.model,
            data_root=_frame_data_root(tmp_path),
        )

        result = _run(
            executor.ainterpret_intake_turn(_intent_of(run_id), _pins_of(executor))
        )

        # The FIRST model call received the pinned Frame scope.
        assert scripted.intake_calls == 1
        prompt = scripted.intake_prompts[0]
        assert "# Pinned server scope" in prompt
        assert f"server_id: {SERVER_ID}" in prompt
        assert "server_name: Frame" in prompt
        assert "environment: linux_ssh" in prompt
        # Not a clarify: an inspect-phase task naming Frame was planned.
        assert result["turn_kind"] == "plan"
        assert result["applied"] is True and result["disposition"] == "created"
        assert result["requested_phase"] == "inspect"
        task_row = await _fetchone(
            sessions,
            "SELECT summary FROM agent_tasks WHERE task_id = :t",
            t=uuid.UUID(str(result["task_id"])),
        )
        assert "Frame" in task_row[0]

        # The executor (not intake) performs reads: a harmless READ tool
        # attempt against the ORIGINAL pinned ExecutionContext runs the
        # gated read; no mutation/approval expansion happens.
        outcome = _run(
            executor.aexecute_tool(_intent_of(run_id), _tool_call(READ_CAP))
        )
        assert outcome["state"] == "succeeded"
        journal = (
            journal_executor.journal.read_text().splitlines()
            if journal_executor.journal.exists()
            else []
        )
        assert f"effect:{READ_CAP}:m1" in journal
        assert not any(line.startswith(f"effect:{PRIV_CAP}") for line in journal)
        assert await _count(sessions, "approvals") == 0
        # Intake itself stayed tool-free.
        usage = result["planner_usage"]
        assert usage["function_tools"] == 0

    @pytest.mark.anyio
    async def test_service_inventory_wording_retains_frame_scope(
        self, sessions, task_run_url, tmp_path
    ) -> None:
        run_id = str(uuid.uuid4())
        conversation_id, message_id = await _alinked_turn(
            sessions, run_id, message="What do you know about this service?"
        )
        await _ainsert_run(sessions, run_id)
        payload = intake_fixtures._plan_payload(
            message_id,
            objective=(
                "Inspect Frame's existing service inventory "
                "(app.discover / app.list_adopted)"
            ),
            operations=[intake_fixtures._add_op("s1_inventory")],
            phase="inspect",
        )
        scripted = DispatchModel([payload], [])
        executor, _journal = _compose(
            sessions,
            task_run_url,
            tmp_path,
            scripted.model,
            data_root=_frame_data_root(tmp_path),
        )

        result = _run(
            executor.ainterpret_intake_turn(_intent_of(run_id), _pins_of(executor))
        )

        prompt = scripted.intake_prompts[0]
        assert "server_name: Frame" in prompt
        assert f"server_id: {SERVER_ID}" in prompt
        # Retains Frame and plans inventory inspection — not a clarify
        # about which device/server the conversation targets.
        assert result["turn_kind"] == "plan"
        assert result["applied"] is True and result["disposition"] == "created"
        task_row = await _fetchone(
            sessions,
            "SELECT summary FROM agent_tasks WHERE task_id = :t",
            t=uuid.UUID(str(result["task_id"])),
        )
        assert "Frame" in task_row[0]

    def test_pinned_frame_scope_flows_full_route_intake_to_executor_read(
        self, task_run_url, domain, scenario, composition
    ) -> None:
        """Full orchestration (real DBOS worker + engine + dispatcher):
        the ORIGINAL ports wording must keep the pinned Frame scope end
        to end — the FIRST intake call and every executor round receive
        "server_name: Frame", intake applies an inspect task, and the
        executor (not intake) performs one harmless gated READ against
        the original pinned ExecutionContext. No approval/mutation."""
        composition()
        # Re-enroll the SAME fixture server under the canonical name
        # "Frame"; the worker reads this data root (MAI_FX_DATA_ROOT).
        from mai.profiles.files import ProfileFileStore

        data_root = scenario / "data-root"
        store = ProfileFileStore(data_root)
        stored = store.read(SERVER_UUID)
        store.write(
            SERVER_UUID,
            stored.text.replace('name: "agent-run-fixture"', 'name: "Frame"'),
            expected_revision=stored.revision,
        )
        (scenario / "expect-scope.txt").write_text("Frame", encoding="utf-8")
        harness._write_policy_files(scenario, grants=[{READ_CAP: True}])
        run_id = str(uuid.uuid4())
        conversation_id, message_id = _run(
            _alinked_turn(domain, run_id, message="Which ports are open on the device?")
        )
        _run(_ainsert_run(domain, run_id))
        intake_payload = intake_fixtures._plan_payload(
            message_id,
            objective="Inspect listening/open ports on Frame",
            operations=[intake_fixtures._add_op("s1_ports")],
            phase="inspect",
        )
        harness._script(
            scenario,
            [
                {
                    "tool_call": {
                        "name": "final_result",
                        "args": {"response": intake_payload},
                    }
                },
                harness._tool_call_entry(),
                {"text": "ports inspected; done"},
            ],
        )
        document = harness._intent_document(
            run_id, connection="gateway", profile="fast", wait_poll_seconds=0.5
        )
        client = EngineClient(task_run_url)
        worker = None
        try:
            worker = harness._spawn_worker(
                task_run_url, scenario / "handlers", scenario, "frame"
            )
            _run(harness._enqueue_run(client, _noop_insert, run_id, document))
            harness._wait_until(
                lambda: (harness._terminal_engine_state(client, run_id) or "")
                == "succeeded",
                harness.RUN_TIMEOUT,
                "run success",
            )
        finally:
            if worker is not None:
                harness._stop(worker)
            client.close()

        # Intake applied an INSPECT task (not a clarify) naming Frame.
        intake_events = [
            event
            for event in _events_of(task_run_url, run_id)
            if event["type"] == INTAKE_EVENT_TYPE
        ]
        assert len(intake_events) == 1
        data = intake_events[0]["data"]
        assert data["turn_kind"] == "plan" and data["applied"] is True
        assert data["requested_phase"] == "inspect"
        with psycopg.connect(task_run_url) as conn:
            summary = conn.execute(
                "SELECT t.summary FROM agent_tasks t "
                "JOIN conversation_runs r ON r.task_id = t.task_id "
                "WHERE r.run_id = %s",
                (uuid.UUID(run_id),),
            ).fetchone()[0]
            approvals = conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
        assert "Frame" in summary
        # EVERY model call (intake + executor rounds) saw the Frame scope.
        model_calls = _count_marker(scenario, "model_call")
        assert model_calls >= 2  # intake + at least one executor round
        assert _count_marker(scenario, "scope_name_seen") == model_calls
        assert _count_marker(scenario, "scope_name_missing") == 0
        # The executor performed exactly one harmless gated read; no
        # privileged effect and no approvals were created.
        assert _count_marker(scenario, "effect:fixture.read:m1") == 1
        assert not any(
            line.startswith("effect:fixture.priv")
            for line in harness._journal_lines(scenario)
        )
        assert approvals == 0
        completed = [
            event
            for event in _events_of(task_run_url, run_id)
            if event["type"] == "agent.completed"
        ]
        assert len(completed) == 1
