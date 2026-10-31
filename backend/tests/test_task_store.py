"""Focused tests for the task-domain persistence (card 11-task-store).

Acceptance (BRIEF, act_CONTRACTS §1–§2/§5–§9):

1. Migration ``0009`` (``revision="0009"``, ``down_revision="0008"``)
   upgrades fresh databases and survives the ``downgrade 0008 → upgrade
   head`` round trip; the CHECK vocabularies mirror the frozen contract
   enums in ``mai.contracts.tasks``; the store's transition maps cover
   every enum member exhaustively.
2. Atomic revision CAS (C6): a stale ``expected_revision`` is the stable
   ``revision_conflict`` — never last-writer-wins — and concurrent
   mutations serialize under the task-row lock (one accepted writer,
   ordered revisions). Idempotency (C4): a retransmitted mutation keyed
   by the same ``idempotency_key`` replays onto the SAME revision; a
   same key with a different payload is the stable conflict.
3. Ordered pagination (AGENTS.md §1): every collection read returns
   ``limit``/``offset``/``total``/``has_more`` with ``COUNT(*)`` under
   the identical filter predicate and deterministic ordering.
4. Authority invariants (§6, P3/P4): model-authored updates can never
   supersede a user-origin constraint nor rewrite a completed step;
   completed items and superseded rows stay RECORDED (never deleted);
   hallucinated evidence/source IDs fail closed with zero effects; a
   verdict can never self-grant verified success.

Boundary discipline (test policy, same as the sibling storage card):
every storage claim runs against a real disposable PostgreSQL fixture
(``MAI_TEST_DATABASE_URL``) with the real linear Alembic chain applied —
the C6 claims are proven by real concurrent coroutines on real
connections and the C8 claim by real queries across two server IDs.
Fixtures carry only fixed opaque UUIDs and synthetic text.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import uuid

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.engine import make_url

from mai.agents.conversation_store import (
    aattach_run,
    acreate_conversation,
    asubmit_turn,
)
from mai.agents.task_store import (
    AUTHORS,
    REVISION_KINDS,
    STATUS_TRANSITIONS,
    STEP_STATUS_TRANSITIONS,
    VERIFICATION_TRANSITIONS,
    acreate_task,
    aget_task,
    aget_task_view,
    alist_completions,
    alist_constraints,
    alist_evidence,
    alist_revisions,
    alist_steps,
    apropose_completion,
    apropose_update,
    aupdate_item,
    aupdate_task_header,
)
from mai.contracts.conversations import MAX_PAGE_LIMIT, TurnDisposition
from mai.contracts.tasks import (
    AcceptanceCriterion,
    ActionRisk,
    ConstraintOrigin,
    ConstraintProposal,
    CriterionResult,
    EvidenceKind,
    EvidenceRef,
    Phase,
    StepStatus,
    TaskCompletionVerdict,
    TaskContractError,
    TaskOutcome,
    TaskPlanPatch,
    TaskPlanStep,
    TaskStepOperation,
    VerificationStatus,
)
from mai.database import create_app_engine

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Linear migration chain; tip reconciliation (card 11-task-store): the
# revision 0009 task-domain tables append to the 0008 set and revision
# 0011 appends the history-tag domain (W1).
REVISION_IDS = (
    "0001",
    "0002",
    "0003",
    "0004",
    "0005",
    "0006",
    "0007",
    "0008",
    "0009",
    "0010",
    "0011",
)

# The exact public application schema at the tip: the 0008 set plus the
# seven task-domain tables (09-agent-tasks) and the three history-tag
# tables (0011, W1) — nothing else.
EXPECTED_APP_TABLES = frozenset(
    {
        "admin_credential",
        "admin_sessions",
        "alembic_version",
        "approvals",
        "profile_projection",
        "profile_projection_journal",
        "job_runs",
        "job_steps",
        "job_events",
        "job_schedules",
        "job_schedule_fires",
        "knowledge_documents",
        "knowledge_doc_chunks",
        "application_projection",
        "application_projection_journal",
        "server_observations",
        "observation_findings",
        "notification_alerts",
        "conversations",
        "conversation_messages",
        "conversation_runs",
        "conversation_steering",
        "agent_tasks",
        "agent_task_steps",
        "agent_task_constraints",
        "agent_task_criteria",
        "agent_task_evidence",
        "agent_task_revisions",
        "agent_task_completions",
        "tags",
        "job_tags",
        "conversation_tags",
    }
)

TASK_TABLES = (
    "agent_task_completions",
    "agent_task_revisions",
    "agent_task_evidence",
    "agent_task_criteria",
    "agent_task_constraints",
    "agent_task_steps",
    "agent_tasks",
)

# Frozen fixed fixture identities (canonical lowercase UUID strings).
SERVER_A = "a10b8c5e-1d2f-4a3b-9c4d-0e5f6a7b8c9d"
SERVER_B = "b20c9d6f-2e3a-4b4c-8d5e-1f0a9b8c7d6e"


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
            "conversation_tags, "
            "notification_alerts, observation_findings, "
            "server_observations, application_projection_journal, "
            "application_projection, knowledge_doc_chunks, "
            "knowledge_documents, job_schedule_fires, job_schedules, "
            "job_events, job_steps, job_runs, job_tags, approvals, "
            "profile_projection_journal, profile_projection, "
            "admin_sessions, admin_credential, tags, alembic_version"
        )
        conn.execute("DROP SEQUENCE IF EXISTS server_observations_seq")
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through ``0009``."""
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


@pytest.fixture
async def sessions(migrated_url: str):
    engine, factory = create_app_engine(migrated_url)
    yield factory
    # Each data test starts from an empty task + conversation domain
    # (session-scoped fixture database): children before parents.
    async with factory() as session:
        for table in (
            *TASK_TABLES,
            "conversation_steering",
            "conversation_runs",
            "conversation_messages",
            "conversations",
        ):
            await session.execute(text(f"DELETE FROM {table}"))
        await session.commit()
    await engine.dispose()


async def _fetchone(sessions, sql: str, **params: object):
    async with sessions() as session:
        return (await session.execute(text(sql), params)).first()


async def _fetchall(sessions, sql: str, **params: object):
    async with sessions() as session:
        return (await session.execute(text(sql), params)).all()


async def _fetchval(sessions, sql: str, **params: object):
    async with sessions() as session:
        return (await session.execute(text(sql), params)).scalar()


def _migration_module():
    spec = importlib.util.spec_from_file_location(
        "migration_0009",
        os.path.join(BACKEND_ROOT, "migrations", "versions", "0009_agent_tasks.py"),
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


async def _constraint_def(sessions, name: str) -> str | None:
    row = await _fetchone(
        sessions,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name",
        name=name,
    )
    return None if row is None else str(row[0])


def _literals_of(constraint: str | None) -> list[str]:
    if constraint is None:
        return []
    return re.findall(r"'([^']*)'", constraint)


def _codes_of(exc: TaskContractError) -> dict[str, str]:
    return exc.fields


# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------


def _step(step_id: str, **overrides: object) -> TaskPlanStep:
    return TaskPlanStep(step_id=step_id, title=f"step {step_id}", **overrides)


def _add_op(step_id: str, **overrides: object) -> TaskStepOperation:
    return TaskStepOperation(
        op="add", step=_step(step_id, **overrides)  # type: ignore[arg-type]
    )


def _update_op(step_id: str, **overrides: object) -> TaskStepOperation:
    return TaskStepOperation(
        op="update", step=_step(step_id, **overrides)  # type: ignore[arg-type]
    )


def _supersede_op(
    step_id: str, supersedes: str, **overrides: object
) -> TaskStepOperation:
    return TaskStepOperation(
        op="supersede",
        step=_step(step_id, **overrides),  # type: ignore[arg-type]
        supersedes_step_id=supersedes,
    )


def _patch(
    base_revision: int,
    *operations: object,
    source_message_id: uuid.UUID | None = None,
    **overrides: object,
) -> TaskPlanPatch:
    overrides.setdefault("idempotency_key", uuid.uuid4())
    return TaskPlanPatch(
        source_message_id=source_message_id or uuid.uuid4(),
        base_revision=base_revision,
        operations=operations,  # type: ignore[arg-type]
        **overrides,  # type: ignore[arg-type]
    )


async def _apply_patch(
    sessions,
    task,  # TaskRow
    base_revision: int,
    *operations: object,
    author: str = "model",
    constraints: tuple[ConstraintProposal, ...] = (),
    **patch_kwargs: object,
):
    """Apply one patch with a REAL source message (the store fails closed
    on hallucinated source IDs, P4 — every accepted patch cites a user
    message that exists in the task's conversation)."""
    source = await _make_message(sessions, task.conversation_id)
    return await apropose_update(
        sessions,
        task.task_id,
        server_id=task.server_id,
        author=author,  # type: ignore[arg-type]
        patch=_patch(
            base_revision,
            *operations,
            source_message_id=source,
            **patch_kwargs,
        ),
        constraint_proposals=constraints,
    )


def _constraint(
    constraint_id: str,
    origin: ConstraintOrigin = ConstraintOrigin.USER,
    suggested: str = "add",
    supersedes: str | None = None,
    source_refs: list[uuid.UUID] | None = None,
) -> ConstraintProposal:
    return ConstraintProposal(
        constraint_id=constraint_id,
        category="safety",
        statement=f"synthetic constraint {constraint_id}",
        origin=origin,
        suggested=suggested,  # type: ignore[arg-type]
        supersedes_constraint_id=supersedes,
        source_message_refs=source_refs or [],
    )


async def _make_conversation(
    sessions, server: str = SERVER_A
) -> uuid.UUID:
    conv = await acreate_conversation(sessions, server_id=uuid.UUID(server))
    return conv.conversation_id


async def _make_message(
    sessions,
    conversation_id: uuid.UUID,
    server: str = SERVER_A,
    body: str = "synthetic user turn",
) -> uuid.UUID:
    result = await asubmit_turn(
        sessions,
        conversation_id=conversation_id,
        server_id=uuid.UUID(server),
        client_request_id=uuid.uuid4(),
        message=body,
    )
    return result.message.message_id


async def _make_task(
    sessions,
    server: str = SERVER_A,
    conversation_id: uuid.UUID | None = None,
    steps: list[TaskPlanStep] | None = None,
    criteria: list | None = None,
    constraints: list[ConstraintProposal] | None = None,
    client_request_id: uuid.UUID | None = None,
):
    cid = conversation_id or await _make_conversation(sessions, server)
    return await acreate_task(
        sessions,
        server_id=uuid.UUID(server),
        conversation_id=cid,
        title="synthetic fixture task",
        summary="fixture objective",
        steps=steps
        or [_step("s1"), _step("s2", depends_on=["s1"]), _step("s3")],
        criteria=criteria or [],
        constraints=constraints or [],
        client_request_id=client_request_id,
    )


def _criterion(criterion_id: str):

    return AcceptanceCriterion(
        criterion_id=criterion_id,
        statement=f"criterion {criterion_id} holds",
    )


def _verdict(
    task_id: uuid.UUID,
    base_revision: int,
    outcome: TaskOutcome,
    results: list,
    evidence: list[EvidenceRef] | None = None,
):
    return TaskCompletionVerdict(
        task_id=task_id,
        base_revision=base_revision,
        idempotency_key=uuid.uuid4(),
        task_outcome=outcome,
        execution_status="succeeded",
        domain_outcome="success",
        effect_certainty="certain",
        criterion_results=results,
        evidence_refs=evidence or [],
    )


def _result(criterion_id: str, evidence: list[EvidenceRef]):

    return CriterionResult(
        criterion_id=criterion_id,
        result=VerificationStatus.PASSED,
        evidence_refs=evidence,
    )


def _message_ref(message_id: uuid.UUID) -> EvidenceRef:
    return EvidenceRef(
        kind=EvidenceKind.MESSAGE, evidence_id=message_id, note="synthetic"
    )


# ---------------------------------------------------------------------------
# AC1 — migration chain, vocabulary mirror, round trip
# ---------------------------------------------------------------------------


class TestMigrationChain:
    def test_revision_constants_target_the_0008_parent(self) -> None:
        module = _migration_module()
        assert module.revision == "0009"
        assert module.down_revision == "0008"

    def test_head_is_0011_and_upgrade_is_idempotent(
        self, migrated_url: str
    ) -> None:
        previous = os.environ.get("MAI_DATABASE_URL")
        os.environ["MAI_DATABASE_URL"] = migrated_url
        try:
            command.upgrade(_alembic_config(), "head")  # second run
        finally:
            if previous is None:
                os.environ.pop("MAI_DATABASE_URL", None)
            else:
                os.environ["MAI_DATABASE_URL"] = previous
        with psycopg.connect(migrated_url) as conn:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert row == (REVISION_IDS[-1],)

    @pytest.mark.anyio
    async def test_exact_app_tables_at_the_head(self, sessions) -> None:
        rows = await _fetchall(
            sessions,
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
            """,
        )
        assert {row[0] for row in rows} == EXPECTED_APP_TABLES

    @pytest.mark.anyio
    async def test_check_vocabulary_mirrors_the_contract_enums(
        self, sessions
    ) -> None:
        assert _literals_of(
            await _constraint_def(sessions, "ck_agent_tasks_phase")
        ) == [phase.value for phase in Phase]
        assert _literals_of(
            await _constraint_def(sessions, "ck_agent_tasks_status")
        ) == [outcome.value for outcome in TaskOutcome]
        assert _literals_of(
            await _constraint_def(sessions, "ck_agent_task_steps_status")
        ) == [status.value for status in StepStatus]
        assert _literals_of(
            await _constraint_def(sessions, "ck_agent_task_steps_verification")
        ) == [verification.value for verification in VerificationStatus]
        assert _literals_of(
            await _constraint_def(sessions, "ck_agent_task_steps_risk")
        ) == [risk.value for risk in ActionRisk]
        assert _literals_of(
            await _constraint_def(sessions, "ck_agent_task_constraints_origin")
        ) == [origin.value for origin in ConstraintOrigin]
        assert _literals_of(
            await _constraint_def(sessions, "ck_agent_task_evidence_kind")
        ) == [kind.value for kind in EvidenceKind]
        assert sorted(
            _literals_of(
                await _constraint_def(sessions, "ck_agent_task_revisions_kind")
            )
        ) == sorted(REVISION_KINDS)
        assert sorted(
            _literals_of(
                await _constraint_def(sessions, "ck_agent_task_revisions_author")
            )
        ) == sorted(AUTHORS)

    @pytest.mark.anyio
    async def test_transition_maps_are_exhaustive(self) -> None:
        assert set(STATUS_TRANSITIONS) == set(TaskOutcome)
        for outcome, targets in STATUS_TRANSITIONS.items():
            assert targets <= set(TaskOutcome)
            if outcome in (
                TaskOutcome.VERIFIED,
                TaskOutcome.FAILED,
                TaskOutcome.CANCELLED,
            ):
                assert targets == {outcome}
        assert set(STEP_STATUS_TRANSITIONS) == set(StepStatus)
        assert STEP_STATUS_TRANSITIONS[StepStatus.EXECUTED] == {
            StepStatus.EXECUTED
        }
        assert STEP_STATUS_TRANSITIONS[StepStatus.SKIPPED] == {
            StepStatus.SKIPPED
        }
        assert set(VERIFICATION_TRANSITIONS) == set(VerificationStatus)
        assert VERIFICATION_TRANSITIONS[VerificationStatus.PASSED] == {
            VerificationStatus.PASSED
        }
        assert VERIFICATION_TRANSITIONS[VerificationStatus.FAILED] == {
            VerificationStatus.FAILED,
            VerificationStatus.PASSED,
        }

    def test_downgrade_round_trip_restores_the_head(
        self, migrated_url: str
    ) -> None:
        previous = os.environ.get("MAI_DATABASE_URL")
        os.environ["MAI_DATABASE_URL"] = migrated_url
        try:
            command.downgrade(_alembic_config(), "0008")
            with psycopg.connect(migrated_url) as conn:
                version = conn.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()
                leftovers = conn.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = ANY(%(names)s)",
                    {"names": list(TASK_TABLES)},
                ).fetchone()
            assert version == ("0008",)
            assert leftovers == (0,)
            command.upgrade(_alembic_config(), "head")
        finally:
            if previous is None:
                os.environ.pop("MAI_DATABASE_URL", None)
            else:
                os.environ["MAI_DATABASE_URL"] = previous
        with psycopg.connect(migrated_url) as conn:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert row == (REVISION_IDS[-1],)


# ---------------------------------------------------------------------------
# creation, identity, one-active-task (§1)
# ---------------------------------------------------------------------------


class TestCreation:
    @pytest.mark.anyio
    async def test_create_sets_revision_one_and_the_active_pointer(
        self, sessions
    ) -> None:
        cid = await _make_conversation(sessions)
        result = await _make_task(sessions, conversation_id=cid)
        assert result.task.revision == 1
        assert result.task.status is TaskOutcome.IN_PROGRESS
        assert result.task.requested_phase is Phase.PLAN
        assert result.disposition is TurnDisposition.CREATED
        active = await _fetchval(
            sessions,
            "SELECT active_task_id FROM conversations "
            "WHERE conversation_id = :cid",
            cid=cid,
        )
        assert active == result.task.task_id
        page = await alist_steps(sessions, result.task.task_id, server_id=result.task.server_id)
        assert [step.step_id for step in page.items] == ["s1", "s2", "s3"]
        assert [step.position for step in page.items] == [0, 1, 2]

    @pytest.mark.anyio
    async def test_creation_idempotency_replays_and_conflicts(
        self, sessions
    ) -> None:
        cid = await _make_conversation(sessions)
        key = uuid.uuid4()
        first = await _make_task(
            sessions, conversation_id=cid, client_request_id=key
        )
        replay = await _make_task(
            sessions, conversation_id=cid, client_request_id=key
        )
        assert replay.disposition is TurnDisposition.DEDUPLICATED
        assert replay.task.task_id == first.task.task_id
        with pytest.raises(TaskContractError) as exc:
            await acreate_task(
                sessions,
                server_id=uuid.UUID(SERVER_A),
                conversation_id=cid,
                title="a DIFFERENT task",
                client_request_id=key,
            )
        assert _codes_of(exc.value)["client_request_id"] == "invalid_task_patch"

    @pytest.mark.anyio
    async def test_one_active_task_per_conversation(self, sessions) -> None:
        cid = await _make_conversation(sessions)
        first = await _make_task(sessions, conversation_id=cid)
        with pytest.raises(ValueError, match="active task episode"):
            await _make_task(sessions, conversation_id=cid)
        await aupdate_task_header(
            sessions,
            first.task.task_id,
            server_id=first.task.server_id,
            expected_revision=first.task.revision,
            status=TaskOutcome.CANCELLED,
        )
        second = await _make_task(sessions, conversation_id=cid)
        assert second.task.task_id != first.task.task_id
        active = await _fetchval(
            sessions,
            "SELECT active_task_id FROM conversations "
            "WHERE conversation_id = :cid",
            cid=cid,
        )
        assert active == second.task.task_id

    @pytest.mark.anyio
    async def test_initial_plan_structure_is_validated(self, sessions) -> None:
        cid = await _make_conversation(sessions)
        with pytest.raises(TaskContractError) as dup:
            await _make_task(
                sessions,
                conversation_id=cid,
                steps=[_step("s1"), _step("s1")],
            )
        assert _codes_of(dup.value)["steps[1].step_id"] == "duplicate_step_id"
        with pytest.raises(TaskContractError) as dep:
            await _make_task(
                sessions, conversation_id=cid, steps=[_step("s1", depends_on=["nope"])]
            )
        assert _codes_of(dep.value)["steps[0].depends_on[0]"] == "unknown_dependency"
        with pytest.raises(TaskContractError) as cycle:
            await _make_task(
                sessions,
                conversation_id=cid,
                steps=[
                    _step("a", depends_on=["b"]),
                    _step("b", depends_on=["a"]),
                ],
            )
        assert _codes_of(cycle.value)["steps"] == "dependency_cycle"
        with pytest.raises(TaskContractError) as sup:
            await _make_task(
                sessions,
                conversation_id=cid,
                constraints=[
                    _constraint("c1", suggested="supersede", supersedes="c0")
                ],
            )
        assert (
            _codes_of(sup.value)["constraints[0].suggested"]
            == "invalid_task_patch"
        )
        with pytest.raises(ValueError):
            await _make_task(
                sessions,
                conversation_id=cid,
                steps=[_step(f"s{i}") for i in range(13)],
            )

    @pytest.mark.anyio
    async def test_creation_source_message_must_exist(self, sessions) -> None:
        cid = await _make_conversation(sessions)
        with pytest.raises(TaskContractError) as exc:
            await acreate_task(
                sessions,
                server_id=uuid.UUID(SERVER_A),
                conversation_id=cid,
                title="task",
                source_message_id=uuid.uuid4(),
            )
        assert _codes_of(exc.value)["source_message_id"] == "invalid_task_patch"


# ---------------------------------------------------------------------------
# AC1 — atomic revision CAS + concurrency (C6) + idempotency (C4)
# ---------------------------------------------------------------------------


class TestCASAndIdempotency:
    @pytest.mark.anyio
    async def test_stale_revision_is_a_structured_conflict(
        self, sessions
    ) -> None:
        task = await _make_task(sessions)
        # move the task to revision 2, then replay the OLD base: stale.
        applied = await _apply_patch(
            sessions, task.task, task.task.revision, _add_op("s4")
        )
        assert applied.task.revision == 2
        with pytest.raises(TaskContractError) as exc:
            await apropose_update(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                author="model",
                patch=_patch(1, _add_op("s5")),
            )
        assert _codes_of(exc.value)["base_revision"] == "revision_conflict"
        after = await aget_task(sessions, task.task.task_id, server_id=task.task.server_id)
        assert after.revision == 2  # zero effects, never last-writer-wins

    @pytest.mark.anyio
    async def test_concurrent_patches_yield_one_writer(self, sessions) -> None:
        task = await _make_task(sessions)
        base = task.task.revision

        async def _attempt(offset: int):
            return await _apply_patch(
                sessions, task.task, base, _add_op(f"extra_{offset}")
            )

        results = await asyncio.gather(
            _attempt(1), _attempt(2), return_exceptions=True
        )
        outcomes = []
        for item in results:
            if isinstance(item, TaskContractError):
                outcomes.append(_codes_of(item)["base_revision"])
            else:
                outcomes.append(item.disposition)
        assert sorted(outcomes, key=str) == ["created", "revision_conflict"]
        after = await aget_task(
            sessions, task.task.task_id, server_id=task.task.server_id
        )
        assert after.revision == base + 1
        total = await _fetchval(
            sessions,
            "SELECT COUNT(*) FROM agent_task_steps WHERE task_id = :tid "
            "AND superseded_at IS NULL",
            tid=task.task.task_id,
        )
        assert total == 4  # s1, s2, s3 + exactly one added step

    @pytest.mark.anyio
    async def test_same_key_retransmit_deduplicates(self, sessions) -> None:
        task = await _make_task(sessions)
        key = uuid.uuid4()
        source = await _make_message(sessions, task.task.conversation_id)
        patch = _patch(
            task.task.revision,
            _add_op("once"),
            source_message_id=source,
            idempotency_key=key,
        )
        first = await apropose_update(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            author="model",
            patch=patch,
        )
        assert first.disposition is TurnDisposition.CREATED
        replay = await apropose_update(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            author="model",
            patch=patch,
        )
        assert replay.disposition is TurnDisposition.DEDUPLICATED
        assert replay.revision.revision == first.revision.revision
        total = await _fetchval(
            sessions,
            "SELECT COUNT(*) FROM agent_task_steps WHERE task_id = :tid "
            "AND step_id = 'once'",
            tid=task.task.task_id,
        )
        assert total == 1
        conflict = _patch(
            task.task.revision + 1,
            _add_op("twice"),
            idempotency_key=key,
        )
        with pytest.raises(TaskContractError) as exc:
            await apropose_update(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                author="model",
                patch=conflict,
            )
        assert (
            _codes_of(exc.value)["idempotency_key"] == "invalid_task_patch"
        )

    @pytest.mark.anyio
    async def test_item_update_cas_and_replay(self, sessions) -> None:
        cid = await _make_conversation(sessions)
        message = await _make_message(sessions, cid)
        task = await _make_task(sessions, conversation_id=cid)
        key = uuid.uuid4()
        first = await aupdate_item(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            step_id="s1",
            expected_revision=task.task.revision,
            idempotency_key=key,
            status=StepStatus.EXECUTED,
            evidence_refs=[_message_ref(message)],
        )
        assert first.disposition is TurnDisposition.CREATED
        assert first.step.status is StepStatus.EXECUTED
        replay = await aupdate_item(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            step_id="s1",
            expected_revision=task.task.revision,
            idempotency_key=key,
            status=StepStatus.EXECUTED,
            evidence_refs=[_message_ref(message)],
        )
        assert replay.disposition is TurnDisposition.DEDUPLICATED
        assert replay.revision.revision == first.revision.revision
        with pytest.raises(TaskContractError) as exc:
            await aupdate_item(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                step_id="s1",
                expected_revision=task.task.revision,  # stale now
                idempotency_key=uuid.uuid4(),
                status=StepStatus.IN_PROGRESS,
            )
        assert _codes_of(exc.value)["base_revision"] == "revision_conflict"


# ---------------------------------------------------------------------------
# plan patches: deltas, stable IDs, DAG, P3 done-work protection
# ---------------------------------------------------------------------------


class TestPlanPatches:
    @pytest.mark.anyio
    async def test_delta_preserves_stable_ids_and_bumps_revision(
        self, sessions
    ) -> None:
        task = await _make_task(sessions)
        result = await _apply_patch(
            sessions,
            task.task,
            task.task.revision,
            _add_op("s4"),
            title="renamed objective",
            rationale="continuation delta",
        )
        assert result.task.revision == 2
        assert result.task.title == "renamed objective"
        page = await alist_steps(
            sessions, task.task.task_id, server_id=task.task.server_id
        )
        assert [step.step_id for step in page.items] == [
            "s1",
            "s2",
            "s3",
            "s4",
        ]
        log = await alist_revisions(
            sessions, task.task.task_id, server_id=task.task.server_id
        )
        assert log.total == 2
        assert log.items[0].kind == "plan_patch"
        assert log.items[0].author == "model"

    @pytest.mark.anyio
    async def test_update_op_cannot_change_status_or_miss_target(
        self, sessions
    ) -> None:
        task = await _make_task(sessions)
        with pytest.raises(TaskContractError) as missing:
            await apropose_update(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                author="model",
                patch=_patch(
                    task.task.revision,
                    _update_op("ghost"),
                ),
            )
        assert (
            _codes_of(missing.value)["operations[0].step.step_id"]
            == "invalid_task_patch"
        )
        with pytest.raises(TaskContractError) as echo:
            await apropose_update(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                author="model",
                patch=_patch(
                    task.task.revision,
                    _update_op("s1", status=StepStatus.BLOCKED),
                ),
            )
        assert (
            _codes_of(echo.value)["operations[0].step.status"]
            == "invalid_task_patch"
        )

    @pytest.mark.anyio
    async def test_supersede_records_history_and_compacts_positions(
        self, sessions
    ) -> None:
        task = await _make_task(sessions)

        await _apply_patch(
            sessions,
            task.task,
            task.task.revision,
            _supersede_op("s3b", "s3"),
            author="user",
        )
        active = await alist_steps(
            sessions, task.task.task_id, server_id=task.task.server_id
        )
        assert [step.step_id for step in active.items] == ["s1", "s2", "s3b"]
        assert [step.position for step in active.items] == [0, 1, 2]
        history = await alist_steps(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            include_superseded=True,
        )
        assert history.total == 4
        superseded = [
            step for step in history.items if step.step_id == "s3"
        ]
        assert superseded and superseded[0].superseded_by == "s3b"
        assert superseded[0].superseded_at is not None

    @pytest.mark.anyio
    async def test_model_cannot_rewrite_completed_work_but_user_can_supersede(
        self, sessions
    ) -> None:
        cid = await _make_conversation(sessions)
        message = await _make_message(sessions, cid)
        task = await _make_task(sessions, conversation_id=cid)
        done = await aupdate_item(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            step_id="s1",
            expected_revision=task.task.revision,
            idempotency_key=uuid.uuid4(),
            status=StepStatus.EXECUTED,
            evidence_refs=[_message_ref(message)],
        )
        revision_after_done = done.task.revision

        with pytest.raises(TaskContractError) as model:
            await apropose_update(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                author="model",
                patch=_patch(
                    revision_after_done,
                    _supersede_op("s1b", "s1"),
                ),
            )
        assert (
            _codes_of(model.value)["operations[0].step.step_id"]
            == "invalid_task_patch"
        )
        still_there = await alist_steps(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            include_superseded=True,
        )
        assert [step.step_id for step in still_there.items] == [
            "s1",
            "s2",
            "s3",
        ]
        await _apply_patch(
            sessions,
            done.task,
            done.task.revision,
            # a real user correction re-points the dependent step in the
            # SAME delta, so the resulting DAG stays dangling-free
            _update_op("s2", depends_on=["s1b"]),
            _supersede_op("s1b", "s1"),
            author="user",
        )
        history = await alist_steps(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            include_superseded=True,
        )
        recorded = {step.step_id: step for step in history.items}
        assert recorded["s1"].superseded_by == "s1b"
        assert recorded["s1"].status is StepStatus.EXECUTED  # stays recorded

    @pytest.mark.anyio
    async def test_full_graph_cycle_and_dangling_dependency_rejected(
        self, sessions
    ) -> None:
        task = await _make_task(sessions)  # s2 depends on s1

        with pytest.raises(TaskContractError) as cycle:
            await apropose_update(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                author="model",
                patch=_patch(
                    task.task.revision,
                    _update_op("s1", depends_on=["s2"]),
                ),
            )
        assert _codes_of(cycle.value)["steps"] == "dependency_cycle"
        with pytest.raises(TaskContractError) as dangling:
            await apropose_update(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                author="model",
                patch=_patch(
                    task.task.revision,
                    _supersede_op("s1b", "s1"),
                ),
            )
        assert (
            _codes_of(dangling.value)["steps[s2].depends_on[0]"]
            == "unknown_dependency"
        )

    @pytest.mark.anyio
    async def test_replacement_summary_never_drops_work(self, sessions) -> None:
        cid = await _make_conversation(sessions)
        message = await _make_message(sessions, cid)
        task = await _make_task(
            sessions,
            conversation_id=cid,
            constraints=[_constraint("c1")],
        )
        await aupdate_item(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            step_id="s1",
            expected_revision=task.task.revision,
            idempotency_key=uuid.uuid4(),
            status=StepStatus.EXECUTED,
            evidence_refs=[_message_ref(message)],
        )
        view_before = await aget_task_view(
            sessions, task.task.task_id, server_id=task.task.server_id
        )
        await _apply_patch(
            sessions,
            view_before,
            view_before.revision,
            summary="a WHOLE new summary that replaces the old one",
        )
        view_after = await aget_task_view(
            sessions, task.task.task_id, server_id=task.task.server_id
        )
        assert view_after.summary == "a WHOLE new summary that replaces the old one"
        assert view_after.counts.steps_total == view_before.counts.steps_total
        assert (
            view_after.counts.steps_executed
            == view_before.counts.steps_executed
            == 1
        )
        assert (
            view_after.counts.constraints_total
            == view_before.counts.constraints_total
            == 1
        )


# ---------------------------------------------------------------------------
# constraint authority (§6, P4)
# ---------------------------------------------------------------------------


class TestConstraintAuthority:
    @pytest.mark.anyio
    async def test_model_cannot_supersede_user_constraint(self, sessions) -> None:
        cid = await _make_conversation(sessions)
        message = await _make_message(sessions, cid)
        task = await _make_task(
            sessions,
            conversation_id=cid,
            constraints=[
                _constraint("c1", source_refs=[message]),
            ],
        )
        with pytest.raises(TaskContractError) as exc:
            await apropose_update(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                author="model",
                patch=_patch(task.task.revision),
                constraint_proposals=[
                    _constraint(
                        "c2",
                        origin=ConstraintOrigin.MODEL,
                        suggested="supersede",
                        supersedes="c1",
                    )
                ],
            )
        assert (
            _codes_of(exc.value)["constraints_proposed[0]"]
            == "constraint_escalation"
        )
        page = await alist_constraints(
            sessions, task.task.task_id, server_id=task.task.server_id
        )
        assert [c.constraint_id for c in page.items] == ["c1"]  # still active
        assert task.task.revision == (
            await aget_task(
                sessions, task.task.task_id, server_id=task.task.server_id
            )
        ).revision  # zero effects

    @pytest.mark.anyio
    async def test_user_correction_supersedes_and_records(self, sessions) -> None:
        task = await _make_task(
            sessions, constraints=[_constraint("c1")]
        )
        result = await _apply_patch(
            sessions,
            task.task,
            task.task.revision,
            author="user",
            constraints=(
                _constraint(
                    "c2",
                    origin=ConstraintOrigin.USER,
                    suggested="supersede",
                    supersedes="c1",
                ),
            ),
        )
        assert result.disposition is TurnDisposition.CREATED
        active = await alist_constraints(
            sessions, task.task.task_id, server_id=task.task.server_id
        )
        assert [c.constraint_id for c in active.items] == ["c2"]
        history = await alist_constraints(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            include_superseded=True,
        )
        recorded = {c.constraint_id: c for c in history.items}
        assert recorded["c1"].superseded_by == "c2"
        assert recorded["c1"].superseded_at is not None

    @pytest.mark.anyio
    async def test_unknown_supersede_target_rejected(self, sessions) -> None:
        task = await _make_task(sessions)
        with pytest.raises(TaskContractError) as exc:
            await apropose_update(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                author="user",
                patch=_patch(task.task.revision),
                constraint_proposals=[
                    _constraint(
                        "c9",
                        suggested="supersede",
                        supersedes="ghost",
                    )
                ],
            )
        assert (
            _codes_of(exc.value)["constraints_proposed[0]"]
            == "unknown_constraint_target"
        )

    @pytest.mark.anyio
    async def test_constraint_source_refs_must_exist(self, sessions) -> None:
        task = await _make_task(sessions)
        real_source = await _make_message(sessions, task.task.conversation_id)
        with pytest.raises(TaskContractError) as exc:
            await apropose_update(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                author="user",
                patch=_patch(
                    task.task.revision, source_message_id=real_source
                ),
                constraint_proposals=[
                    _constraint("c1", source_refs=[uuid.uuid4()])
                ],
            )
        assert (
            _codes_of(exc.value)["constraints_proposed[0].source_message_refs[0]"]
            == "invalid_task_patch"
        )


# ---------------------------------------------------------------------------
# item status rules + evidence honesty (§7, P4)
# ---------------------------------------------------------------------------


class TestItemUpdates:
    @pytest.mark.anyio
    async def test_lifecycle_with_evidence_and_active_step_clearing(
        self, sessions
    ) -> None:
        cid = await _make_conversation(sessions)
        message = await _make_message(sessions, cid)
        task = await _make_task(sessions, conversation_id=cid)
        await aupdate_task_header(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            expected_revision=task.task.revision,
            active_step_id="s1",
        )
        in_progress = await aupdate_item(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            step_id="s1",
            expected_revision=2,
            idempotency_key=uuid.uuid4(),
            status=StepStatus.IN_PROGRESS,
        )
        assert in_progress.step.status is StepStatus.IN_PROGRESS
        done = await aupdate_item(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            step_id="s1",
            expected_revision=3,
            idempotency_key=uuid.uuid4(),
            status=StepStatus.EXECUTED,
            result_summary="wiped the cache",
            evidence_refs=[_message_ref(message)],
        )
        assert done.step.status is StepStatus.EXECUTED
        assert done.step.result_summary == "wiped the cache"
        assert done.step.evidence_refs[0].evidence_id == message
        header = await aget_task(
            sessions, task.task.task_id, server_id=task.task.server_id
        )
        assert header.active_step_id is None  # cleared on execution
        evidence_page = await alist_evidence(
            sessions, task.task.task_id, server_id=task.task.server_id
        )
        assert evidence_page.total == 1
        assert evidence_page.items[0].origin_step_id == "s1"

    @pytest.mark.anyio
    async def test_frozen_and_evidence_bound_status_rules(
        self, sessions
    ) -> None:
        cid = await _make_conversation(sessions)
        message = await _make_message(sessions, cid)
        task = await _make_task(sessions, conversation_id=cid)
        with pytest.raises(TaskContractError) as no_evidence:
            await aupdate_item(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                step_id="s1",
                expected_revision=task.task.revision,
                idempotency_key=uuid.uuid4(),
                status=StepStatus.EXECUTED,
            )
        assert (
            _codes_of(no_evidence.value)["evidence_refs"]
            == "invalid_task_patch"
        )
        with pytest.raises(TaskContractError) as no_reason:
            await aupdate_item(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                step_id="s1",
                expected_revision=task.task.revision,
                idempotency_key=uuid.uuid4(),
                status=StepStatus.SKIPPED,
            )
        assert _codes_of(no_reason.value)["status"] == "invalid_task_patch"
        await aupdate_item(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            step_id="s1",
            expected_revision=task.task.revision,
            idempotency_key=uuid.uuid4(),
            status=StepStatus.EXECUTED,
            evidence_refs=[_message_ref(message)],
        )
        with pytest.raises(TaskContractError) as reopen:
            await aupdate_item(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                step_id="s1",
                expected_revision=task.task.revision + 1,
                idempotency_key=uuid.uuid4(),
                status=StepStatus.PENDING,
            )
        assert _codes_of(reopen.value)["status"] == "invalid_task_patch"

    @pytest.mark.anyio
    async def test_verification_is_monotone_and_evidence_bound(
        self, sessions
    ) -> None:
        cid = await _make_conversation(sessions)
        message = await _make_message(sessions, cid)
        other_cid = await _make_conversation(sessions)
        foreign_message = await _make_message(sessions, other_cid)
        task = await _make_task(sessions, conversation_id=cid)
        await aupdate_item(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            step_id="s1",
            expected_revision=task.task.revision,
            idempotency_key=uuid.uuid4(),
            status=StepStatus.EXECUTED,
            evidence_refs=[_message_ref(message)],
        )
        with pytest.raises(TaskContractError) as foreign:
            await aupdate_item(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                step_id="s1",
                expected_revision=task.task.revision + 1,
                idempotency_key=uuid.uuid4(),
                verification=VerificationStatus.PASSED,
                evidence_refs=[_message_ref(foreign_message)],
            )
        assert (
            _codes_of(foreign.value)["evidence_refs[0]"]
            == "unknown_evidence_ref"
        )
        passed = await aupdate_item(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            step_id="s1",
            expected_revision=task.task.revision + 1,
            idempotency_key=uuid.uuid4(),
            verification=VerificationStatus.PASSED,
            evidence_refs=[_message_ref(message)],
        )
        assert passed.step.verification is VerificationStatus.PASSED
        with pytest.raises(TaskContractError) as clear:
            await aupdate_item(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                step_id="s1",
                expected_revision=passed.task.revision,
                idempotency_key=uuid.uuid4(),
                verification=VerificationStatus.NOT_CHECKED,
            )
        assert _codes_of(clear.value)["verification"] == "invalid_task_patch"

    @pytest.mark.anyio
    async def test_hallucinated_evidence_has_zero_effects(self, sessions) -> None:
        task = await _make_task(sessions)
        with pytest.raises(TaskContractError) as exc:
            await aupdate_item(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                step_id="s1",
                expected_revision=task.task.revision,
                idempotency_key=uuid.uuid4(),
                status=StepStatus.EXECUTED,
                evidence_refs=[
                    EvidenceRef(kind=EvidenceKind.MESSAGE, evidence_id=uuid.uuid4())
                ],
            )
        assert _codes_of(exc.value)["evidence_refs[0]"] == "unknown_evidence_ref"
        after = await aget_task(
            sessions, task.task.task_id, server_id=task.task.server_id
        )
        assert after.revision == task.task.revision
        step = await alist_steps(
            sessions, task.task.task_id, server_id=task.task.server_id
        )
        assert step.items[0].status is StepStatus.PENDING

    @pytest.mark.anyio
    async def test_artifact_evidence_fails_closed(self, sessions) -> None:
        cid = await _make_conversation(sessions)
        task = await _make_task(sessions, conversation_id=cid)
        with pytest.raises(TaskContractError) as exc:
            await aupdate_item(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                step_id="s1",
                expected_revision=task.task.revision,
                idempotency_key=uuid.uuid4(),
                status=StepStatus.EXECUTED,
                evidence_refs=[
                    EvidenceRef(kind=EvidenceKind.ARTIFACT, evidence_id=uuid.uuid4())
                ],
            )
        assert _codes_of(exc.value)["evidence_refs[0]"] == "unknown_evidence_ref"

    @pytest.mark.anyio
    async def test_run_event_evidence_via_real_run_linkage(
        self, sessions
    ) -> None:
        cid = await _make_conversation(sessions)
        run_id = uuid.uuid4()
        await aattach_run(
            sessions,
            conversation_id=cid,
            server_id=uuid.UUID(SERVER_A),
            run_id=run_id,
        )
        task = await _make_task(sessions, conversation_id=cid)
        done = await aupdate_item(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            step_id="s1",
            expected_revision=task.task.revision,
            idempotency_key=uuid.uuid4(),
            status=StepStatus.EXECUTED,
            evidence_refs=[
                EvidenceRef(kind=EvidenceKind.RUN_EVENT, evidence_id=run_id)
            ],
        )
        assert done.step.status is StepStatus.EXECUTED


# ---------------------------------------------------------------------------
# completion verdicts (§9, P4/E2)
# ---------------------------------------------------------------------------


class TestCompletion:
    @pytest.mark.anyio
    async def test_verified_requires_full_evidence_bound_coverage(
        self, sessions
    ) -> None:
        cid = await _make_conversation(sessions)
        message = await _make_message(sessions, cid)
        task = await _make_task(
            sessions,
            conversation_id=cid,
            criteria=[_criterion("c1"), _criterion("c2")],
        )
        ref = _message_ref(message)
        with pytest.raises(TaskContractError) as missing:
            await apropose_completion(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                verdict=_verdict(
                    task.task.task_id,
                    task.task.revision,
                    TaskOutcome.VERIFIED,
                    [_result("c1", [ref])],
                ),
            )
        assert _codes_of(missing.value)["criterion_results"] == "missing_criterion"
        with pytest.raises(TaskContractError) as fabricated:
            await apropose_completion(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                verdict=_verdict(
                    task.task.task_id,
                    task.task.revision,
                    TaskOutcome.VERIFIED,
                    [
                        _result("c1", [ref]),
                        _result(
                            "c2",
                            [
                                EvidenceRef(
                                    kind=EvidenceKind.MESSAGE,
                                    evidence_id=uuid.uuid4(),
                                )
                            ],
                        ),
                    ],
                ),
            )
        assert (
            _codes_of(fabricated.value)["criterion_results[1].evidence_refs[0]"]
            == "unknown_evidence_ref"
        )
        page = await alist_completions(
            sessions, task.task.task_id, server_id=task.task.server_id
        )
        assert page.total == 0  # rejected verdicts leave zero receipts

    @pytest.mark.anyio
    async def test_partial_then_verified_advances_criteria(self, sessions) -> None:
        cid = await _make_conversation(sessions)
        message = await _make_message(sessions, cid)
        task = await _make_task(
            sessions,
            conversation_id=cid,
            criteria=[_criterion("c1"), _criterion("c2")],
        )
        ref = _message_ref(message)
        # the executor first records the evidence on the task (§7), so
        # the verdict can cite REGISTERED evidence (E2: no fabricated IDs)
        await aupdate_item(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            step_id="s1",
            expected_revision=task.task.revision,
            idempotency_key=uuid.uuid4(),
            status=StepStatus.EXECUTED,
            evidence_refs=[ref],
        )
        current = await aget_task(
            sessions, task.task.task_id, server_id=task.task.server_id
        )
        partial = await apropose_completion(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            verdict=_verdict(
                task.task.task_id,
                current.revision,
                TaskOutcome.PARTIALLY_VERIFIED,
                [_result("c1", [ref])],
            ),
        )
        assert partial.task.status is TaskOutcome.PARTIALLY_VERIFIED
        view = await aget_task_view(
            sessions, task.task.task_id, server_id=task.task.server_id
        )
        assert view.counts.criteria_verified == 1
        verified = await apropose_completion(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            verdict=_verdict(
                task.task.task_id,
                partial.task.revision,
                TaskOutcome.VERIFIED,
                [_result("c1", [ref]), _result("c2", [ref])],
            ),
        )
        assert verified.task.status is TaskOutcome.VERIFIED
        cid_active = await _fetchval(
            sessions,
            "SELECT active_task_id FROM conversations WHERE conversation_id = :cid",
            cid=cid,
        )
        assert cid_active is None  # episode closed, pointer released
        with pytest.raises(TaskContractError) as frozen:
            await aupdate_task_header(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                expected_revision=verified.task.revision,
                status=TaskOutcome.IN_PROGRESS,
            )
        assert _codes_of(frozen.value)["status"] == "invalid_task_patch"
        receipts = await alist_completions(
            sessions, task.task.task_id, server_id=task.task.server_id
        )
        assert receipts.total == 2
        assert receipts.items[-1].task_outcome is TaskOutcome.VERIFIED

    @pytest.mark.anyio
    async def test_verdict_replay_deduplicates(self, sessions) -> None:
        cid = await _make_conversation(sessions)
        message = await _make_message(sessions, cid)
        task = await _make_task(
            sessions, conversation_id=cid, criteria=[_criterion("c1")]
        )
        ref = _message_ref(message)
        await aupdate_item(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            step_id="s1",
            expected_revision=task.task.revision,
            idempotency_key=uuid.uuid4(),
            status=StepStatus.EXECUTED,
            evidence_refs=[ref],
        )
        current = await aget_task(
            sessions, task.task.task_id, server_id=task.task.server_id
        )
        verdict = _verdict(
            task.task.task_id,
            current.revision,
            TaskOutcome.PARTIALLY_VERIFIED,
            [_result("c1", [ref])],
        )
        first = await apropose_completion(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            verdict=verdict,
        )
        replay = await apropose_completion(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            verdict=verdict,
        )
        assert replay.disposition is TurnDisposition.DEDUPLICATED
        assert replay.completion.completion_id == first.completion.completion_id
        page = await alist_completions(
            sessions, task.task.task_id, server_id=task.task.server_id
        )
        assert page.total == 1


# ---------------------------------------------------------------------------
# header metadata (§6 advisory phase; §8 user controls)
# ---------------------------------------------------------------------------


class TestHeaderMetadata:
    @pytest.mark.anyio
    async def test_phase_is_advisory_and_status_follows_the_map(
        self, sessions
    ) -> None:
        task = await _make_task(sessions)
        moved = await aupdate_task_header(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            expected_revision=task.task.revision,
            requested_phase=Phase.ACT,
        )
        assert moved.task.requested_phase is Phase.ACT
        awaiting = await aupdate_task_header(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            expected_revision=moved.task.revision,
            status=TaskOutcome.NEEDS_INPUT,
        )
        assert awaiting.task.status is TaskOutcome.NEEDS_INPUT
        resumed = await aupdate_task_header(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            expected_revision=awaiting.task.revision,
            status=TaskOutcome.IN_PROGRESS,
        )
        assert resumed.task.status is TaskOutcome.IN_PROGRESS
        log = await alist_revisions(
            sessions, task.task.task_id, server_id=task.task.server_id
        )
        assert log.items[0].kind == "metadata"
        assert log.items[0].author == "user"

    @pytest.mark.anyio
    async def test_active_step_must_exist(self, sessions) -> None:
        task = await _make_task(sessions)
        with pytest.raises(TaskContractError) as exc:
            await aupdate_task_header(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                expected_revision=task.task.revision,
                active_step_id="ghost",
            )
        assert _codes_of(exc.value)["step_id"] == "invalid_task_patch"


# ---------------------------------------------------------------------------
# scope (C8)
# ---------------------------------------------------------------------------


class TestScope:
    @pytest.mark.anyio
    async def test_cross_server_access_fails_closed(self, sessions) -> None:
        task = await _make_task(sessions, server=SERVER_A)
        other = uuid.UUID(SERVER_B)
        with pytest.raises(TaskContractError) as read:
            await aget_task(sessions, task.task.task_id, server_id=other)
        assert "task_not_found" in _codes_of(read.value).values()
        with pytest.raises(TaskContractError):
            await alist_steps(sessions, task.task.task_id, server_id=other)
        with pytest.raises(TaskContractError):
            await apropose_update(
                sessions,
                task.task.task_id,
                server_id=other,
                author="model",
                patch=_patch(task.task.revision),
            )
        # The same closed code covers a missing task: no existence leak.
        with pytest.raises(TaskContractError) as missing:
            await aget_task(sessions, uuid.uuid4(), server_id=other)
        assert (
            set(_codes_of(missing.value).values())
            == set(_codes_of(read.value).values())
            == {"task_not_found"}
        )


# ---------------------------------------------------------------------------
# ordered pagination (AGENTS.md §1)
# ---------------------------------------------------------------------------


class TestPagination:
    @pytest.mark.anyio
    async def test_steps_page_is_ordered_and_bounded(self, sessions) -> None:
        task = await _make_task(
            sessions, steps=[_step(f"s{i:02d}") for i in range(10)]
        )
        # two patches of 8 and 7 more steps → 25 active rows
        applied = await _apply_patch(
            sessions,
            task.task,
            task.task.revision,
            *[_add_op(f"p1_{i:02d}") for i in range(8)],
        )
        await _apply_patch(
            sessions,
            applied.task,
            applied.task.revision,
            *[_add_op(f"p2_{i:02d}") for i in range(7)],
        )
        page1 = await alist_steps(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            limit=10,
        )
        assert (page1.total, page1.limit, page1.offset) == (25, 10, 0)
        assert page1.has_more is True
        assert len(page1.items) == 10
        assert page1.has_more == (page1.offset + len(page1.items) < page1.total)
        page3 = await alist_steps(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            limit=10,
            offset=20,
        )
        assert (page3.total, len(page3.items), page3.has_more) == (25, 5, False)
        positions = [step.position for step in page3.items]
        assert positions == sorted(positions)
        # deterministic ordering across page boundaries
        everything = await alist_steps(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            limit=MAX_PAGE_LIMIT,
        )
        assert [step.step_id for step in everything.items] == [
            *[f"s{i:02d}" for i in range(10)],
            *[f"p1_{i:02d}" for i in range(8)],
            *[f"p2_{i:02d}" for i in range(7)],
        ]
        assert [step.position for step in everything.items] == list(range(25))
        with pytest.raises(ValueError):
            await alist_steps(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                limit=MAX_PAGE_LIMIT + 1,
            )
        with pytest.raises(ValueError):
            await alist_steps(
                sessions,
                task.task.task_id,
                server_id=task.task.server_id,
                offset=-1,
            )

    @pytest.mark.anyio
    async def test_other_collections_page_the_same_way(self, sessions) -> None:
        cid = await _make_conversation(sessions)
        message = await _make_message(sessions, cid)
        task = await _make_task(
            sessions,
            conversation_id=cid,
            constraints=[_constraint(f"c{i}") for i in range(4)],
            criteria=[_criterion(f"cr{i}") for i in range(3)],
        )
        await aupdate_item(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            step_id="s1",
            expected_revision=task.task.revision,
            idempotency_key=uuid.uuid4(),
            status=StepStatus.EXECUTED,
            evidence_refs=[_message_ref(message)],
        )
        constraints = await alist_constraints(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            limit=2,
        )
        assert (constraints.total, len(constraints.items), constraints.has_more) == (
            4,
            2,
            True,
        )
        assert [c.constraint_id for c in constraints.items] == ["c0", "c1"]
        evidence = await alist_evidence(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            limit=2,
        )
        assert (evidence.total, len(evidence.items), evidence.has_more) == (1, 1, False)
        revisions = await alist_revisions(
            sessions,
            task.task.task_id,
            server_id=task.task.server_id,
            limit=2,
        )
        assert revisions.total == 2
        assert [r.revision for r in revisions.items] == [2, 1]  # newest first
        assert revisions.has_more is False
        view = await aget_task_view(
            sessions, task.task.task_id, server_id=task.task.server_id
        )
        assert view.counts.steps_executed == 1
        assert view.counts.evidence_total == 1
        assert view.counts.constraints_total == 4
        assert view.counts.criteria_total == 3
        assert view.counts.criteria_verified == 0
