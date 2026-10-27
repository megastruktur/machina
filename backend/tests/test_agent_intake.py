"""Focused tests for tool-free structured intake (card 12-intake).

Acceptance (BRIEF, act_CONTRACTS §5/§6, act_RUNTIME P1–P4):

1. **Tool-free variants (P1/P2/P3):** ``answer`` / ``plan`` /
   ``new_task`` / ``continue_task`` (and ``clarify`` / ``blocked``)
   produce bounded structured ``TurnInterpretation`` output against the
   canonical conversation/task snapshot with ZERO effectful tools on the
   intake agent. A direct question (P1) yields a compact answer without
   creating any task machinery and with exactly ONE model call; a
   plan-only request (P2) records the explicit user constraint anchored
   to the VERIFIED user message and stays in ``plan``; a continuation
   delta (P3) preserves completed steps and updates only affected items
   under CAS.
2. **Provenance guardrails + bounded repair (P4):** invalid schema,
   nonexistent source/evidence references, stale ``expected_revision``,
   cyclic dependency patches and attempted constraint removal are
   rejected with AT MOST one structured validation-feedback retry before
   failing closed (:class:`intake.IntakeRejection`) — zero storage
   effects on any rejected turn, never silent fabrication, never
   unbounded loops.
3. **Author↔origin defense-in-depth (R-11-task-store review):** intake
   output is always model output, so an ``origin=USER`` claim is only
   admissible as an ``add`` anchored to verified user messages
   (role ``user``) of the interpreted conversation; a model-produced
   proposal can never claim user origin to bypass
   ``CONSTRAINT_ESCALATION``. Authority fields, fabricated certainty and
   self-granted execution/verification are unrepresentable or rejected.

Boundary discipline (test policy, same as the sibling storage card):
every persistence claim runs against a real disposable PostgreSQL
fixture (``MAI_TEST_DATABASE_URL``) with the real linear Alembic chain
applied; the model is a deterministic scripted ``FunctionModel`` (proves
the intake plumbing, not provider competence). Fixtures carry only fixed
opaque UUIDs and synthetic text.
"""

from __future__ import annotations

import os
import uuid
import pathlib
from typing import Sequence

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, ToolCallPart
from sqlalchemy import text

from mai.agents.conversation_store import (
    aappend_producer_message,
    acreate_conversation,
    asubmit_turn,
)
from mai.agents.intake import (
    IntakeSnapshot,
    IntakeRejection,
    aload_intake_snapshot,
    apropose_model_task_update,
    arun_intake_turn,
    build_repair_prompt,
    build_snapshot_prompt,
    validate_intake_decision,
)
from mai.agents.task_store import (
    acreate_task,
    aget_task,
    alist_constraints,
    alist_steps,
    aupdate_item,
)
from mai.contracts.conversations import (
    MessageRole,
    MessageSourceKind,
    parse_turn_interpretation,
)
from mai.contracts.tasks import (
    AcceptanceCriterion,
    ConstraintOrigin,
    ConstraintProposal,
    EvidenceKind,
    EvidenceRef,
    Phase,
    StepStatus,
    TaskContractError,
    TaskErrorCode,
    TaskPlanPatch,
    VerificationStatus,
)
from mai.database import create_app_engine

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

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
    """Disposable fixture database migrated from zero through ``0009``."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the intake claims need a real "
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


async def _count(sessions, table: str) -> int:
    row = await _fetchone(sessions, f"SELECT COUNT(*) FROM {table}")
    return int(row[0])


# ---------------------------------------------------------------------------
# scripted intake model (deterministic FunctionModel; plumbing only)
# ---------------------------------------------------------------------------


class ScriptedModel:
    """Deterministic FunctionModel replaying prescribed output-tool calls.

    Each entry is ``{"payload": {...}}`` (a contract-shaped turn dict the
    harness wraps into the ``final_result`` output tool), or
    ``{"tool_name": "wrong", "args": {...}}`` to script a wrong channel.
    The script EXHAUSTS: a call beyond the last entry raises, so every
    attempt count asserted in this suite is honest.
    """

    def __init__(self, entries: Sequence[dict]) -> None:
        self._entries: list[dict] = list(entries)
        self.calls: int = 0
        #: ``AgentInfo`` captured per call: the harness's own declaration
        #: of function tools (must be empty) and the output tool.
        self.infos: list[object] = []

    @property
    def model(self) -> FunctionModel:
        return FunctionModel(self._respond)

    def _respond(self, messages, info):  # noqa: ANN001 - FunctionModel ABI
        if self.calls >= len(self._entries):
            raise AssertionError("scripted intake model exhausted")
        entry = self._entries[self.calls]
        self.calls += 1
        self.infos.append(info)
        tool_name = entry.get("tool_name", "final_result")
        args = entry.get("args", {"response": entry["payload"]})
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name=tool_name,
                    args=args,
                    tool_call_id=f"c{self.calls}",
                )
            ]
        )

    def function_tools_seen(self) -> list[str]:
        names: list[str] = []
        for info in self.infos:
            for tool in getattr(info, "function_tools", ()) or ():
                names.append(getattr(tool, "name", str(tool)))
        return names


# ---------------------------------------------------------------------------
# payload builders (contract-shaped dicts; uuids as strings)
# ---------------------------------------------------------------------------


def _patch_dict(
    source_message_id: uuid.UUID,
    base_revision: int,
    *operations: dict,
    idempotency_key: uuid.UUID | None = None,
    **extra: object,
) -> dict:
    return {
        "schema_version": 1,
        "source_message_id": str(source_message_id),
        "base_revision": base_revision,
        "idempotency_key": str(idempotency_key or uuid.uuid4()),
        "operations": list(operations),
        **extra,
    }


def _add_op(step_id: str, **step_extra: object) -> dict:
    step = {"step_id": step_id, "title": f"step {step_id}", **step_extra}
    return {"op": "add", "step": step}


def _update_op(step_id: str, **step_extra: object) -> dict:
    step = {"step_id": step_id, "title": f"step {step_id}", **step_extra}
    return {"op": "update", "step": step}


def _turn_base(
    turn_kind: str, source_message_id: uuid.UUID, **extra: object
) -> dict:
    return {
        "schema_version": 1,
        "source_message_id": str(source_message_id),
        "turn_kind": turn_kind,
        "understanding": "the user asks a self-contained question",
        **extra,
    }


def _answer_payload(source_message_id: uuid.UUID, text: str) -> dict:
    return _turn_base(
        "answer", source_message_id, understanding="greeting", answer_text=text
    )


def _plan_payload(
    source_message_id: uuid.UUID,
    *,
    objective: str = "provision the fixture service",
    constraints: Sequence[dict] = (),
    criteria: Sequence[dict] | None = None,
    operations: Sequence[dict] = (),
    phase: str = "plan",
    assumptions: Sequence[dict] = (),
    **extra: object,
) -> dict:
    return _turn_base(
        "plan",
        source_message_id,
        understanding="the user requests a plan-only task",
        objective=objective,
        acceptance_criteria=list(
            criteria
            if criteria is not None
            else [{"criterion_id": "done", "statement": "the objective is met"}]
        ),
        constraints_proposed=list(constraints),
        assumptions=list(assumptions),
        requested_phase=phase,
        todo_patch=_patch_dict(source_message_id, 1, *operations),
        **extra,
    )


def _continue_payload(
    source_message_id: uuid.UUID,
    base_revision: int,
    *operations: dict,
    **extra: object,
) -> dict:
    return _turn_base(
        "continue_task",
        source_message_id,
        understanding="the user continues the current task",
        todo_patch=_patch_dict(source_message_id, base_revision, *operations),
        **extra,
    )


async def _make_conversation(sessions, server_id: str = SERVER_A) -> uuid.UUID:
    conversation = await acreate_conversation(
        sessions, server_id=uuid.UUID(server_id)
    )
    return conversation.conversation_id


async def _submit_user(
    sessions, conversation_id: uuid.UUID, message: str
) -> uuid.UUID:
    result = await asubmit_turn(
        sessions,
        conversation_id=conversation_id,
        server_id=uuid.UUID(SERVER_A),
        client_request_id=uuid.uuid4(),
        message=message,
    )
    return result.message.message_id


def _user_constraint(
    constraint_id: str, source_message_id: uuid.UUID, statement: str
) -> dict:
    return {
        "constraint_id": constraint_id,
        "category": "safety",
        "statement": statement,
        "origin": "user",
        "source_message_refs": [str(source_message_id)],
        "suggested": "add",
    }


# ---------------------------------------------------------------------------
# P1 — compact answer, no task machinery, zero tools, one call
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_p1_answer_turn_creates_no_task_machinery(sessions) -> None:
    conversation_id = await _make_conversation(sessions)
    message_id = await _submit_user(sessions, conversation_id, "What is 2 + 2?")
    scripted = ScriptedModel([{"payload": _answer_payload(message_id, "4")}])

    outcome = await arun_intake_turn(
        scripted.model,
        sessions,
        server_id=uuid.UUID(SERVER_A),
        conversation_id=conversation_id,
        source_message_id=message_id,
    )

    assert outcome.turn.turn_kind == "answer"
    assert outcome.applied is False
    assert outcome.task_id is None and outcome.revision is None
    assert outcome.record.model_calls == 1  # P1: no second action-model call
    assert outcome.record.function_tools == 0
    assert scripted.function_tools_seen() == []  # harness-declared: zero tools
    # No task machinery of any kind was created.
    assert await _count(sessions, "agent_tasks") == 0
    assert await _count(sessions, "agent_task_steps") == 0
    assert await _count(sessions, "agent_task_constraints") == 0
    assert await _count(sessions, "agent_task_revisions") == 0


@pytest.mark.anyio
async def test_p1_clarify_produces_no_task_machinery(sessions) -> None:
    conversation_id = await _make_conversation(sessions)
    message_id = await _submit_user(sessions, conversation_id, "do the thing")
    scripted = ScriptedModel(
        [
            {
                "payload": _turn_base(
                    "clarify",
                    message_id,
                    understanding="which target?",
                    open_questions=[
                        {"proposal_id": "target", "question": "Which target?"}
                    ],
                )
            },
        ]
    )
    outcome = await arun_intake_turn(
        scripted.model,
        sessions,
        server_id=uuid.UUID(SERVER_A),
        conversation_id=conversation_id,
        source_message_id=message_id,
    )
    assert outcome.turn.turn_kind == "clarify"
    assert outcome.applied is False
    assert await _count(sessions, "agent_tasks") == 0


# ---------------------------------------------------------------------------
# P2 — plan-only request records the user constraint, stays in plan
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_p2_plan_only_records_user_constraint_and_stays_in_plan(
    sessions,
) -> None:
    conversation_id = await _make_conversation(sessions)
    message_id = await _submit_user(
        sessions,
        conversation_id,
        "Plan only; do not change files. Set up the report service "
        "(missing prerequisite: the report API does not exist yet).",
    )
    scripted = ScriptedModel(
        [
            {
                "payload": _plan_payload(
                    message_id,
                    constraints=[
                        _user_constraint(
                            "no_file_changes",
                            message_id,
                            "Do not modify any files (plan only).",
                        )
                    ],
                    operations=[
                        _add_op(
                            "discover_prereq",
                            prerequisite_unresolved=True,
                            expected_check="prerequisite location identified",
                        ),
                        _add_op(
                            "draft_rollout",
                            depends_on=["discover_prereq"],
                        ),
                    ],
                    assumptions=[
                        {
                            "statement": "the report API is reachable from the host",
                            "basis": "unverified user remark",
                        }
                    ],
                )
            }
        ]
    )

    outcome = await arun_intake_turn(
        scripted.model,
        sessions,
        server_id=uuid.UUID(SERVER_A),
        conversation_id=conversation_id,
        source_message_id=message_id,
    )

    assert outcome.turn.turn_kind == "plan"
    assert outcome.applied is True
    assert outcome.record.model_calls == 1
    assert outcome.record.function_tools == 0
    task = await aget_task(
        sessions, outcome.task_id, server_id=uuid.UUID(SERVER_A)
    )
    assert task.requested_phase is Phase.PLAN  # stays advisory-plan
    # The user constraint is recorded with origin=user anchored to the
    # verified user message.
    page = await alist_constraints(
        sessions, outcome.task_id, server_id=uuid.UUID(SERVER_A)
    )
    assert page.total == 1
    constraint = page.items[0]
    assert constraint.origin is ConstraintOrigin.USER
    assert constraint.source_message_refs == (message_id,)
    # The discovery step keeps its unresolved prerequisite; no effects ran.
    steps = await alist_steps(
        sessions, outcome.task_id, server_id=uuid.UUID(SERVER_A)
    )
    by_id = {step.step_id: step for step in steps.items}
    assert by_id["discover_prereq"].prerequisite_unresolved is True
    assert by_id["draft_rollout"].depends_on == ("discover_prereq",)
    assert all(step.status is StepStatus.PENDING for step in steps.items)
    assert await _count(sessions, "conversation_runs") == 0


@pytest.mark.anyio
async def test_p2_user_origin_constraint_requires_verified_user_anchor(
    sessions,
) -> None:
    conversation_id = await _make_conversation(sessions)
    message_id = await _submit_user(sessions, conversation_id, "plan the rollout")
    assistant = await aappend_producer_message(
        sessions,
        conversation_id=conversation_id,
        server_id=uuid.UUID(SERVER_A),
        role=MessageRole.ASSISTANT,
        source_kind=MessageSourceKind.SYSTEM,
        content="earlier assistant remark",
    )
    # The model anchors a "user" constraint to an ASSISTANT row: user
    # authority cannot be manufactured from producer material.
    bad = _user_constraint("no_delete", assistant.message.message_id, "never delete rows")
    scripted = ScriptedModel(
        [{"payload": _plan_payload(message_id, constraints=[bad])}, {"payload": _plan_payload(message_id, constraints=[bad])}]
    )

    with pytest.raises(IntakeRejection) as excinfo:
        await arun_intake_turn(
            scripted.model,
            sessions,
            server_id=uuid.UUID(SERVER_A),
            conversation_id=conversation_id,
            source_message_id=message_id,
        )
    assert any(
        code == "constraint_origin_anchor" for code in excinfo.value.fields.values()
    )
    assert excinfo.value.attempts == 2  # bounded: exactly one repair
    assert excinfo.value.repaired is True
    assert scripted.calls == 2
    assert await _count(sessions, "agent_tasks") == 0


@pytest.mark.anyio
async def test_p2_user_origin_supersede_claim_is_refused(sessions) -> None:
    """The R-11 bypass: origin=user claimed ON a supersede proposal."""
    conversation_id = await _make_conversation(sessions)
    message_id = await _submit_user(sessions, conversation_id, "plan the rollout")
    payload = _plan_payload(
        message_id,
        constraints=[
            {
                "constraint_id": "rewrite_history",
                "category": "safety",
                "statement": "allow history rewrites",
                "origin": "user",
                "suggested": "supersede",
                "supersedes_constraint_id": "some_existing",
            }
        ],
    )
    scripted = ScriptedModel([{"payload": payload}, {"payload": payload}])
    with pytest.raises(IntakeRejection) as excinfo:
        await arun_intake_turn(
            scripted.model,
            sessions,
            server_id=uuid.UUID(SERVER_A),
            conversation_id=conversation_id,
            source_message_id=message_id,
        )
    assert any(
        code == TaskErrorCode.CONSTRAINT_ESCALATION
        for code in excinfo.value.fields.values()
    )
    assert await _count(sessions, "agent_tasks") == 0


# ---------------------------------------------------------------------------
# P3 — continuation deltas preserve completed work and respect CAS
# ---------------------------------------------------------------------------


async def _seed_continuation_task(
    sessions, conversation_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, int]:
    """Create a task via intake, submit a follow-up user message and mark
    ``s1_build`` executed with that message as evidence."""
    first = await _submit_user(sessions, conversation_id, "plan the rollout")
    scripted = ScriptedModel(
        [
            {
                "payload": _plan_payload(
                    first,
                    operations=[_add_op("s1_build"), _add_op("s2_deploy")],
                )
            }
        ]
    )
    outcome = await arun_intake_turn(
        scripted.model,
        sessions,
        server_id=uuid.UUID(SERVER_A),
        conversation_id=conversation_id,
        source_message_id=first,
    )
    task_id = outcome.task_id
    assert task_id is not None
    follow_up = await _submit_user(sessions, conversation_id, "build step is done")
    result = await aupdate_item(
        sessions,
        task_id,
        server_id=uuid.UUID(SERVER_A),
        step_id="s1_build",
        expected_revision=outcome.revision,
        idempotency_key=uuid.uuid4(),
        status=StepStatus.EXECUTED,
        result_summary="build finished",
        evidence_refs=[
            EvidenceRef(
                kind=EvidenceKind.MESSAGE,
                evidence_id=follow_up,
                note="user confirmation",
            )
        ],
        source_message_id=follow_up,
    )
    return task_id, follow_up, result.task.revision


@pytest.mark.anyio
async def test_p3_continue_preserves_completed_step_and_updates_affected(
    sessions,
) -> None:
    conversation_id = await _make_conversation(sessions)
    task_id, evidence_id, revision = await _seed_continuation_task(
        sessions, conversation_id
    )
    third = await _submit_user(sessions, conversation_id, "continue; rename deploy")
    scripted = ScriptedModel(
        [
            {
                "payload": _continue_payload(
                    third,
                    revision,
                    _update_op("s2_deploy", title="deploy canary"),
                    _add_op(
                        "s3_verify",
                        depends_on=["s2_deploy"],
                        risk="read_only",
                    ),
                )
            }
        ]
    )

    outcome = await arun_intake_turn(
        scripted.model,
        sessions,
        server_id=uuid.UUID(SERVER_A),
        conversation_id=conversation_id,
        source_message_id=third,
    )

    assert outcome.turn.turn_kind == "continue_task"
    assert outcome.applied is True
    assert outcome.record.model_calls == 1
    steps = await alist_steps(
        sessions, task_id, server_id=uuid.UUID(SERVER_A)
    )
    by_id = {step.step_id: step for step in steps.items}
    # Completed work preserved verbatim: stable ID, executed status, evidence.
    assert by_id["s1_build"].status is StepStatus.EXECUTED
    assert by_id["s1_build"].evidence_refs[0].evidence_id == evidence_id
    assert by_id["s1_build"].result_summary == "build finished"
    # Only the affected item changed; expanded scope arrived pending.
    assert by_id["s2_deploy"].title == "deploy canary"
    assert by_id["s3_verify"].status is StepStatus.PENDING
    assert outcome.revision == revision + 1


@pytest.mark.anyio
async def test_p3_stale_revision_is_repaired_once_then_applied(sessions) -> None:
    conversation_id = await _make_conversation(sessions)
    task_id, _evidence, revision = await _seed_continuation_task(
        sessions, conversation_id
    )
    third = await _submit_user(sessions, conversation_id, "continue")
    stale = _continue_payload(
        third, revision - 1, _add_op("s3_note")
    )
    fixed = _continue_payload(third, revision, _add_op("s3_note"))
    scripted = ScriptedModel([{"payload": stale}, {"payload": fixed}])

    outcome = await arun_intake_turn(
        scripted.model,
        sessions,
        server_id=uuid.UUID(SERVER_A),
        conversation_id=conversation_id,
        source_message_id=third,
    )

    assert outcome.applied is True
    assert outcome.record.repaired is True
    assert outcome.record.model_calls == 2
    assert outcome.record.repair_feedback[0]["todo_patch.base_revision"] == (
        TaskErrorCode.REVISION_CONFLICT
    )
    assert outcome.revision == revision + 1


@pytest.mark.anyio
async def test_p3_stale_revision_insisting_fails_closed(sessions) -> None:
    conversation_id = await _make_conversation(sessions)
    task_id, _evidence, revision = await _seed_continuation_task(
        sessions, conversation_id
    )
    third = await _submit_user(sessions, conversation_id, "continue")
    stale = _continue_payload(third, revision - 1, _add_op("s3_note"))
    scripted = ScriptedModel([{"payload": stale}, {"payload": stale}])

    with pytest.raises(IntakeRejection) as excinfo:
        await arun_intake_turn(
            scripted.model,
            sessions,
            server_id=uuid.UUID(SERVER_A),
            conversation_id=conversation_id,
            source_message_id=third,
        )
    assert excinfo.value.attempts == 2
    assert any(
        code == TaskErrorCode.REVISION_CONFLICT
        for code in excinfo.value.fields.values()
    )
    task = await aget_task(sessions, task_id, server_id=uuid.UUID(SERVER_A))
    assert task.revision == revision  # zero effects
    steps = await alist_steps(sessions, task_id, server_id=uuid.UUID(SERVER_A))
    assert "s3_note" not in {step.step_id for step in steps.items}


# ---------------------------------------------------------------------------
# A2 — plan editing vs task progress: ordinary continuation, one repair
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a2_ordinary_continue_todo_patch_null_is_validated_noop(
    sessions,
) -> None:
    conversation_id = await _make_conversation(sessions)
    task_id, _evidence, revision = await _seed_continuation_task(
        sessions, conversation_id
    )
    third = await _submit_user(sessions, conversation_id, "continue")
    scripted = ScriptedModel(
        [
            {
                "payload": _turn_base(
                    "continue_task",
                    third,
                    understanding="the user continues the current task",
                    todo_patch=None,
                )
            }
        ]
    )

    outcome = await arun_intake_turn(
        scripted.model,
        sessions,
        server_id=uuid.UUID(SERVER_A),
        conversation_id=conversation_id,
        source_message_id=third,
    )

    # One call, no repair, no store write: the validated no-op binds the
    # EXISTING episode (disposition None, revision unchanged).
    assert outcome.turn.turn_kind == "continue_task"
    assert outcome.turn.todo_patch is None
    assert outcome.applied is True
    assert outcome.task_id == task_id
    assert outcome.disposition is None
    assert outcome.record.model_calls == 1
    assert outcome.record.repaired is False
    assert outcome.revision == revision
    steps = await alist_steps(sessions, task_id, server_id=uuid.UUID(SERVER_A))
    by_id = {step.step_id: step for step in steps.items}
    assert by_id["s1_build"].status is StepStatus.EXECUTED
    assert by_id["s2_deploy"].status is StepStatus.PENDING


@pytest.mark.anyio
async def test_a2_progress_report_patch_is_repaired_once_to_noop(
    sessions,
) -> None:
    """Incident-shaped replay: the model reports step execution AND
    verification through a continue_task plan patch — even backed by a
    REAL evidence reference. The offending field paths reach the ONE
    bounded structured repair, and the repaired decision is a validated
    no-op continuation."""
    conversation_id = await _make_conversation(sessions)
    task_id, evidence_id, revision = await _seed_continuation_task(
        sessions, conversation_id
    )
    third = await _submit_user(sessions, conversation_id, "deploy is done too")
    progress_patch = _continue_payload(
        third,
        revision,
        _update_op(
            "s2_deploy",
            status="executed",
            verification="passed",
            evidence_refs=[
                {
                    "kind": "message",
                    "evidence_id": str(evidence_id),
                    "note": "user confirmation",
                }
            ],
        ),
    )
    noop = _turn_base(
        "continue_task",
        third,
        understanding="the user continues the current task",
        todo_patch=None,
    )
    scripted = ScriptedModel([{"payload": progress_patch}, {"payload": noop}])

    outcome = await arun_intake_turn(
        scripted.model,
        sessions,
        server_id=uuid.UUID(SERVER_A),
        conversation_id=conversation_id,
        source_message_id=third,
    )

    assert outcome.record.repaired is True
    assert outcome.record.model_calls == 2
    assert scripted.calls == 2
    feedback = outcome.record.repair_feedback[0]
    assert (
        feedback["todo_patch.operations[0].step.status"]
        == "progress_not_in_patch"
    )
    assert (
        feedback["todo_patch.operations[0].step.verification"]
        == "progress_not_in_patch"
    )
    # The repaired continuation is a validated no-op: nothing persisted.
    assert outcome.turn.todo_patch is None
    assert outcome.applied is True
    assert outcome.task_id == task_id
    assert outcome.disposition is None
    assert outcome.revision == revision
    steps = await alist_steps(sessions, task_id, server_id=uuid.UUID(SERVER_A))
    by_id = {step.step_id: step for step in steps.items}
    assert by_id["s2_deploy"].status is StepStatus.PENDING
    assert by_id["s1_build"].status is StepStatus.EXECUTED


@pytest.mark.anyio
async def test_a2_progress_report_insisting_fails_closed(sessions) -> None:
    conversation_id = await _make_conversation(sessions)
    task_id, _evidence, revision = await _seed_continuation_task(
        sessions, conversation_id
    )
    third = await _submit_user(sessions, conversation_id, "deploy is done too")
    progress_patch = _continue_payload(
        third,
        revision,
        _update_op(
            "s2_deploy",
            status="executed",
            verification="passed",
            evidence_refs=[
                {
                    "kind": "message",
                    "evidence_id": str(_evidence),
                    "note": "user confirmation",
                }
            ],
        ),
    )
    scripted = ScriptedModel(
        [{"payload": progress_patch}, {"payload": progress_patch}]
    )

    with pytest.raises(IntakeRejection) as excinfo:
        await arun_intake_turn(
            scripted.model,
            sessions,
            server_id=uuid.UUID(SERVER_A),
            conversation_id=conversation_id,
            source_message_id=third,
        )
    assert excinfo.value.attempts == 2
    assert excinfo.value.repaired is True
    assert any(
        code == "progress_not_in_patch" for code in excinfo.value.fields.values()
    )
    task = await aget_task(sessions, task_id, server_id=uuid.UUID(SERVER_A))
    assert task.revision == revision  # zero effects
    steps = await alist_steps(sessions, task_id, server_id=uuid.UUID(SERVER_A))
    by_id = {step.step_id: step for step in steps.items}
    assert by_id["s2_deploy"].status is StepStatus.PENDING


@pytest.mark.anyio
async def test_a2_self_executed_add_via_continue_fails_closed(sessions) -> None:
    """A continue_task patch cannot invent a completed item: an add
    carrying executed/passed state is refused pre-apply even with a REAL
    evidence reference (the store would have accepted it — regression)."""
    conversation_id = await _make_conversation(sessions)
    task_id, evidence_id, revision = await _seed_continuation_task(
        sessions, conversation_id
    )
    third = await _submit_user(sessions, conversation_id, "mark it complete")
    ghost_done = _continue_payload(
        third,
        revision,
        _add_op(
            "s3_ghost",
            status="executed",
            verification="passed",
            evidence_refs=[
                {
                    "kind": "message",
                    "evidence_id": str(evidence_id),
                    "note": "user confirmation",
                }
            ],
        ),
    )
    scripted = ScriptedModel([{"payload": ghost_done}, {"payload": ghost_done}])

    with pytest.raises(IntakeRejection) as excinfo:
        await arun_intake_turn(
            scripted.model,
            sessions,
            server_id=uuid.UUID(SERVER_A),
            conversation_id=conversation_id,
            source_message_id=third,
        )
    assert excinfo.value.attempts == 2
    assert (
        excinfo.value.fields.get("todo_patch.operations[0].step.status")
        == "progress_not_in_patch"
    )
    task = await aget_task(sessions, task_id, server_id=uuid.UUID(SERVER_A))
    assert task.revision == revision  # zero effects
    steps = await alist_steps(sessions, task_id, server_id=uuid.UUID(SERVER_A))
    assert "s3_ghost" not in {step.step_id for step in steps.items}


# ---------------------------------------------------------------------------
# A5 — direct simple operations select act from the outset (one turn)
# ---------------------------------------------------------------------------


def _catalog_addition_operations() -> tuple[dict, ...]:
    """The reversible catalog-entry addition journey as plan steps:
    preparatory read, bounded reversible mutation, evidence-bound check."""
    return (
        _add_op("s1_locate", risk="read_only"),
        _add_op("s2_add_entry", risk="mutating"),
        _add_op(
            "s3_verify",
            risk="read_only",
            expected_check="the catalog lists the new entry",
        ),
    )


@pytest.mark.anyio
async def test_a5_english_catalog_addition_selects_act_from_outset(
    sessions,
) -> None:
    conversation_id = await _make_conversation(sessions)
    message_id = await _submit_user(
        sessions, conversation_id, "Add Frametop to the catalog on Frame"
    )
    scripted = ScriptedModel(
        [
            {
                "payload": _plan_payload(
                    message_id,
                    objective="Add the Frametop catalog entry on Frame",
                    operations=_catalog_addition_operations(),
                    phase="act",
                )
            }
        ]
    )

    outcome = await arun_intake_turn(
        scripted.model,
        sessions,
        server_id=uuid.UUID(SERVER_A),
        conversation_id=conversation_id,
        source_message_id=message_id,
    )

    # act FROM THE OUTSET: exactly ONE accepted turn, no waiting state,
    # no phase confirmation — inspection rides the act run itself.
    assert outcome.turn.turn_kind == "plan"
    assert outcome.turn.requested_phase is Phase.ACT
    assert outcome.applied is True
    assert outcome.disposition == "created"
    assert outcome.record.model_calls == 1
    assert outcome.record.function_tools == 0
    task = await aget_task(
        sessions, outcome.task_id, server_id=uuid.UUID(SERVER_A)
    )
    assert task.requested_phase is Phase.ACT
    steps = await alist_steps(
        sessions, outcome.task_id, server_id=uuid.UUID(SERVER_A)
    )
    by_id = {step.step_id: step for step in steps.items}
    assert by_id["s1_locate"].risk.value == "read_only"
    assert by_id["s2_add_entry"].risk.value == "mutating"
    assert by_id["s3_verify"].risk.value == "read_only"
    assert by_id["s3_verify"].expected_check == (
        "the catalog lists the new entry"
    )
    # Nothing is executed or verified by intake itself: the plan starts
    # pending and unchecked; verification stays evidence-bound.
    assert all(
        step.status is StepStatus.PENDING
        and step.verification is VerificationStatus.NOT_CHECKED
        for step in steps.items
    )


@pytest.mark.anyio
async def test_a5_russian_direct_action_selects_act(sessions) -> None:
    conversation_id = await _make_conversation(sessions)
    message_id = await _submit_user(
        sessions, conversation_id, "Добавь Frametop в каталог на Frame"
    )
    scripted = ScriptedModel(
        [
            {
                "payload": _plan_payload(
                    message_id,
                    objective="Add the Frametop catalog entry on Frame",
                    operations=_catalog_addition_operations(),
                    phase="act",
                )
            }
        ]
    )

    outcome = await arun_intake_turn(
        scripted.model,
        sessions,
        server_id=uuid.UUID(SERVER_A),
        conversation_id=conversation_id,
        source_message_id=message_id,
    )

    assert outcome.turn.requested_phase is Phase.ACT
    assert outcome.applied is True
    assert outcome.disposition == "created"
    assert outcome.record.model_calls == 1


@pytest.mark.anyio
async def test_a5_read_only_requests_stay_inspect(sessions) -> None:
    for wording in (
        "Which ports are open on the device?",
        "Покажи открытые порты на сервере",
    ):
        conversation_id = await _make_conversation(sessions)
        message_id = await _submit_user(sessions, conversation_id, wording)
        scripted = ScriptedModel(
            [
                {
                    "payload": _plan_payload(
                        message_id,
                        objective="Inspect listening ports",
                        operations=[_add_op("s1_ports", risk="read_only")],
                        phase="inspect",
                    )
                }
            ]
        )

        outcome = await arun_intake_turn(
            scripted.model,
            sessions,
            server_id=uuid.UUID(SERVER_A),
            conversation_id=conversation_id,
            source_message_id=message_id,
        )

        # Read-only intent stays inspect: no mutation eligibility is
        # selected for an explore/inspect request.
        assert outcome.turn.requested_phase is Phase.INSPECT
        assert outcome.applied is True


def test_a5_guidance_renders_autonomous_simple_operation_policy() -> None:
    """The rendered snapshot prompt carries the phase policy: act from
    the outset for explicit bounded changes, inspection as a preparatory
    step inside the one run, eligibility-only semantics, verified
    postcondition, read-only stays inspect, ambiguity stops."""
    prompt = build_snapshot_prompt(_bare_snapshot())
    assert 'requested_phase "act"' in prompt
    assert "FROM THE OUTSET" in prompt
    assert "PREPARATORY" in prompt
    assert "inspect -> mutate -> verify -> report" in prompt
    assert "ELIGIBILITY only" in prompt
    assert "requested postcondition to be VERIFIED" in prompt
    assert 'requested_phase "inspect"' in prompt
    assert "Ambiguity is a stop, never a guess" in prompt


# ---------------------------------------------------------------------------
# P4 — schema, sources, cycles, authority: bounded repair, zero effects
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_p4_invalid_schema_fails_closed_after_bounded_repair(
    sessions,
) -> None:
    conversation_id = await _make_conversation(sessions)
    message_id = await _submit_user(sessions, conversation_id, "plan the rollout")
    missing_required = _turn_base("plan", message_id)  # no objective/patch
    broken_union = {
        "schema_version": 1,
        "source_message_id": str(message_id),
        "turn_kind": "teleport",  # not a ratified variant
        "understanding": "n/a",
    }
    scripted = ScriptedModel(
        [{"payload": missing_required}, {"payload": broken_union}]
    )

    with pytest.raises(IntakeRejection) as excinfo:
        await arun_intake_turn(
            scripted.model,
            sessions,
            server_id=uuid.UUID(SERVER_A),
            conversation_id=conversation_id,
            source_message_id=message_id,
        )
    assert excinfo.value.attempts == 2 and scripted.calls == 2
    assert excinfo.value.fields  # value-free field mapping present
    assert await _count(sessions, "agent_tasks") == 0
    assert await _count(sessions, "agent_task_revisions") == 0


@pytest.mark.anyio
async def test_p4_authority_fields_are_unrepresentable(sessions) -> None:
    conversation_id = await _make_conversation(sessions)
    message_id = await _submit_user(sessions, conversation_id, "plan the rollout")
    self_approved = _plan_payload(message_id, approved=True)
    scripted = ScriptedModel([{"payload": self_approved}, {"payload": self_approved}])
    with pytest.raises(IntakeRejection) as excinfo:
        await arun_intake_turn(
            scripted.model,
            sessions,
            server_id=uuid.UUID(SERVER_A),
            conversation_id=conversation_id,
            source_message_id=message_id,
        )
    assert any("extra_forbidden" in code for code in excinfo.value.fields.values())
    assert await _count(sessions, "agent_tasks") == 0


@pytest.mark.anyio
async def test_p4_self_executed_step_without_evidence_rejected(sessions) -> None:
    conversation_id = await _make_conversation(sessions)
    message_id = await _submit_user(sessions, conversation_id, "plan the rollout")
    fake_done = _plan_payload(
        message_id,
        operations=[
            _add_op("s1_build", status="executed")  # no evidence refs
        ],
    )
    scripted = ScriptedModel([{"payload": fake_done}, {"payload": fake_done}])
    with pytest.raises(IntakeRejection) as excinfo:
        await arun_intake_turn(
            scripted.model,
            sessions,
            server_id=uuid.UUID(SERVER_A),
            conversation_id=conversation_id,
            source_message_id=message_id,
        )
    assert await _count(sessions, "agent_tasks") == 0
    assert excinfo.value.fields


@pytest.mark.anyio
async def test_p4_hallucinated_evidence_reference_rejected(sessions) -> None:
    conversation_id = await _make_conversation(sessions)
    message_id = await _submit_user(sessions, conversation_id, "what is this?")
    fabricated = _answer_payload(message_id, "based on this record")
    fabricated["evidence_refs"] = [
        {
            "kind": "message",
            "evidence_id": str(uuid.uuid4()),  # does not exist anywhere
            "note": "cited",
        }
    ]
    scripted = ScriptedModel([{"payload": fabricated}, {"payload": fabricated}])
    with pytest.raises(IntakeRejection) as excinfo:
        await arun_intake_turn(
            scripted.model,
            sessions,
            server_id=uuid.UUID(SERVER_A),
            conversation_id=conversation_id,
            source_message_id=message_id,
        )
    assert any(
        code == TaskErrorCode.UNKNOWN_EVIDENCE_REF
        for code in excinfo.value.fields.values()
    )
    assert await _count(sessions, "agent_tasks") == 0


@pytest.mark.anyio
async def test_p4_hallucinated_constraint_source_ref_rejected(sessions) -> None:
    conversation_id = await _make_conversation(sessions)
    message_id = await _submit_user(sessions, conversation_id, "plan the rollout")
    bad = _user_constraint("no_delete", uuid.uuid4(), "never delete rows")
    scripted = ScriptedModel(
        [
            {"payload": _plan_payload(message_id, constraints=[bad])},
            {"payload": _plan_payload(message_id, constraints=[bad])},
        ]
    )
    with pytest.raises(IntakeRejection) as excinfo:
        await arun_intake_turn(
            scripted.model,
            sessions,
            server_id=uuid.UUID(SERVER_A),
            conversation_id=conversation_id,
            source_message_id=message_id,
        )
    assert any(
        code == "constraint_origin_anchor" for code in excinfo.value.fields.values()
    )
    assert await _count(sessions, "agent_tasks") == 0


@pytest.mark.anyio
async def test_p4_cyclic_dependency_patch_fails_closed(sessions) -> None:
    conversation_id = await _make_conversation(sessions)
    task_id, _evidence, revision = await _seed_continuation_task(
        sessions, conversation_id
    )
    third = await _submit_user(sessions, conversation_id, "continue")
    cyclic = _continue_payload(
        third,
        revision,
        _add_op("s_a", depends_on=["s_b"]),
        _add_op("s_b", depends_on=["s_a"]),
    )
    scripted = ScriptedModel([{"payload": cyclic}, {"payload": cyclic}])
    with pytest.raises(IntakeRejection) as excinfo:
        await arun_intake_turn(
            scripted.model,
            sessions,
            server_id=uuid.UUID(SERVER_A),
            conversation_id=conversation_id,
            source_message_id=third,
        )
    assert any(
        code == TaskErrorCode.DEPENDENCY_CYCLE
        for code in excinfo.value.fields.values()
    )
    task = await aget_task(sessions, task_id, server_id=uuid.UUID(SERVER_A))
    assert task.revision == revision  # zero effects


@pytest.mark.anyio
async def test_p4_model_cannot_supersede_user_constraint(sessions) -> None:
    """Store-level authority: model-author update via the intake seam."""
    conversation_id = await _make_conversation(sessions)
    message_id = await _submit_user(
        sessions,
        conversation_id,
        "never force-push to main; plan the release",
    )
    create = await acreate_task(
        sessions,
        server_id=uuid.UUID(SERVER_A),
        conversation_id=conversation_id,
        title="release",
        constraints=(
            ConstraintProposal(
                constraint_id="no_force_push",
                category="safety",
                statement="never force-push to main",
                origin=ConstraintOrigin.USER,
                source_message_refs=[message_id],
                suggested="add",
            ),
        ),
        criteria=(AcceptanceCriterion(criterion_id="released", statement="shipped"),),
        source_message_id=message_id,
    )
    task_id = create.task.task_id
    revision = create.task.revision

    async def _attempt_supersede(origin: ConstraintOrigin) -> None:
        patch = TaskPlanPatch(
            source_message_id=message_id,
            base_revision=revision,
            idempotency_key=uuid.uuid4(),
            summary="try to relax the prohibition",
        )
        await apropose_model_task_update(
            sessions,
            task_id,
            server_id=uuid.UUID(SERVER_A),
            conversation_id=conversation_id,
            patch=patch,
            constraint_proposals=(
                ConstraintProposal(
                    constraint_id="allow_force_push",
                    category="safety",
                    statement="force-push allowed",
                    origin=origin,
                    suggested="supersede",
                    supersedes_constraint_id="no_force_push",
                ),
            ),
        )

    # (a) honest model origin: the store's own validator refuses.
    with pytest.raises(TaskContractError) as model_exc:
        await _attempt_supersede(ConstraintOrigin.MODEL)
    assert model_exc.value.fields["constraints_proposed[0]"] == (
        TaskErrorCode.CONSTRAINT_ESCALATION
    )
    # (b) the R-11 bypass attempt: claimed user origin is refused at the
    # intake seam BEFORE the store can be fooled by the declared origin.
    with pytest.raises(TaskContractError) as user_exc:
        await _attempt_supersede(ConstraintOrigin.USER)
    assert user_exc.value.fields["constraints_proposed[0]"] == (
        TaskErrorCode.CONSTRAINT_ESCALATION
    )
    # The prohibition survives both attempts; no revision was consumed.
    page = await alist_constraints(
        sessions, task_id, server_id=uuid.UUID(SERVER_A)
    )
    assert page.total == 1
    assert page.items[0].origin is ConstraintOrigin.USER
    assert page.items[0].superseded_at is None
    task = await aget_task(sessions, task_id, server_id=uuid.UUID(SERVER_A))
    assert task.revision == revision


# ---------------------------------------------------------------------------
# provenance of the interpreted source + variant/state consistency
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_non_user_source_message_is_never_interpreted(sessions) -> None:
    conversation_id = await _make_conversation(sessions)
    await _submit_user(sessions, conversation_id, "hello")
    assistant = await aappend_producer_message(
        sessions,
        conversation_id=conversation_id,
        server_id=uuid.UUID(SERVER_A),
        role=MessageRole.ASSISTANT,
        source_kind=MessageSourceKind.SYSTEM,
        content="an assistant row",
    )
    scripted = ScriptedModel([])  # must never be called
    with pytest.raises(IntakeRejection) as excinfo:
        await arun_intake_turn(
            scripted.model,
            sessions,
            server_id=uuid.UUID(SERVER_A),
            conversation_id=conversation_id,
            source_message_id=assistant.message.message_id,
        )
    assert excinfo.value.fields == {"source_message_id": "source_message_not_user"}
    assert excinfo.value.model_calls == 0
    assert scripted.calls == 0


@pytest.mark.anyio
async def test_turn_cannot_reinterpret_a_different_message(sessions) -> None:
    conversation_id = await _make_conversation(sessions)
    message_id = await _submit_user(sessions, conversation_id, "what is 2+2?")
    forged_source = _answer_payload(uuid.uuid4(), "4")  # claims another message
    scripted = ScriptedModel([{"payload": forged_source}, {"payload": forged_source}])
    with pytest.raises(IntakeRejection) as excinfo:
        await arun_intake_turn(
            scripted.model,
            sessions,
            server_id=uuid.UUID(SERVER_A),
            conversation_id=conversation_id,
            source_message_id=message_id,
        )
    assert any(
        code == "source_message_mismatch" for code in excinfo.value.fields.values()
    )


@pytest.mark.anyio
async def test_plan_turn_with_active_task_fails_closed_without_reset(
    sessions,
) -> None:
    conversation_id = await _make_conversation(sessions)
    task_id, _evidence, revision = await _seed_continuation_task(
        sessions, conversation_id
    )
    third = await _submit_user(sessions, conversation_id, "actually plan something else")
    payload = _plan_payload(third, objective="a different goal entirely")
    scripted = ScriptedModel([{"payload": payload}, {"payload": payload}])
    with pytest.raises(IntakeRejection) as excinfo:
        await arun_intake_turn(
            scripted.model,
            sessions,
            server_id=uuid.UUID(SERVER_A),
            conversation_id=conversation_id,
            source_message_id=third,
        )
    assert any(
        code == "active_task_exists" for code in excinfo.value.fields.values()
    )
    task = await aget_task(sessions, task_id, server_id=uuid.UUID(SERVER_A))
    assert task.revision == revision  # no silent reset, no second episode
    assert await _count(sessions, "agent_tasks") == 1


@pytest.mark.anyio
async def test_continue_turn_without_task_fails_closed(sessions) -> None:
    conversation_id = await _make_conversation(sessions)
    message_id = await _submit_user(sessions, conversation_id, "continue")
    payload = _continue_payload(message_id, 1, _add_op("s1"))
    scripted = ScriptedModel([{"payload": payload}, {"payload": payload}])
    with pytest.raises(IntakeRejection) as excinfo:
        await arun_intake_turn(
            scripted.model,
            sessions,
            server_id=uuid.UUID(SERVER_A),
            conversation_id=conversation_id,
            source_message_id=message_id,
        )
    assert any(code == "no_active_task" for code in excinfo.value.fields.values())


@pytest.mark.anyio
async def test_reinterpreting_same_turn_deduplicates_onto_same_task(
    sessions,
) -> None:
    conversation_id = await _make_conversation(sessions)
    message_id = await _submit_user(sessions, conversation_id, "plan the rollout")
    payload = _plan_payload(message_id, operations=[_add_op("s1_build")])
    scripted = ScriptedModel([{"payload": payload}, {"payload": payload}])

    first = await arun_intake_turn(
        scripted.model,
        sessions,
        server_id=uuid.UUID(SERVER_A),
        conversation_id=conversation_id,
        source_message_id=message_id,
    )
    second = await arun_intake_turn(
        scripted.model,
        sessions,
        server_id=uuid.UUID(SERVER_A),
        conversation_id=conversation_id,
        source_message_id=message_id,
    )
    assert first.disposition == "created"
    assert second.disposition == "deduplicated"
    assert second.task_id == first.task_id
    assert await _count(sessions, "agent_tasks") == 1
    steps = await alist_steps(
        sessions, first.task_id, server_id=uuid.UUID(SERVER_A)
    )
    assert steps.total == 1  # no duplicated rows


# ---------------------------------------------------------------------------
# pure validation helpers
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_validate_intake_decision_rejects_without_state(sessions) -> None:
    conversation_id = await _make_conversation(sessions)
    message_id = await _submit_user(sessions, conversation_id, "plan the rollout")
    snapshot = await aload_intake_snapshot(
        sessions,
        server_id=uuid.UUID(SERVER_A),
        conversation_id=conversation_id,
        source_message_id=message_id,
    )
    turn = _plan_payload(uuid.uuid4())  # wrong source message
    with pytest.raises(TaskContractError) as excinfo:
        validate_intake_decision(
            parse_turn_interpretation(turn),
            snapshot,
            constraint_anchors={},
            evidence_exists={},
        )
    assert "source_message_id" in excinfo.value.fields


# ---------------------------------------------------------------------------
# R-12 follow-ups: N1 snapshot-window regression + N2 variant coverage
# (new_task / blocked were proven only by the reviewer's throwaway
# extension; they are committed suite rows from 13-intake-runner on)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_n1_snapshot_window_covers_recent_turns_above_default_page_limit(
    sessions,
) -> None:
    """R-12 N1: with ``transcript_limit`` above the store's default page
    limit (50) and ``50 < total <= transcript_limit``, the window MUST be
    the whole (recent) transcript with ``omitted_older=False`` — not the
    FIRST 50 rows silently kept with a dishonest omission marker."""
    conversation_id = await _make_conversation(sessions)
    for index in range(55):
        await _submit_user(sessions, conversation_id, f"filler {index}")
    last = await _submit_user(sessions, conversation_id, "the interpreted turn")

    snapshot = await aload_intake_snapshot(
        sessions,
        server_id=uuid.UUID(SERVER_A),
        conversation_id=conversation_id,
        source_message_id=last,
        transcript_limit=60,  # > DEFAULT_PAGE_LIMIT (50); total=56 <= 60
    )

    assert snapshot.transcript_omitted_older is False
    assert len(snapshot.transcript) == 56
    # The window is the RECENT tail, oldest first: the interpreted turn
    # is the LAST row (the defect kept the first 50 rows instead).
    assert snapshot.transcript[-1] == (MessageRole.USER, "the interpreted turn")
    assert snapshot.transcript[0] == (MessageRole.USER, "filler 0")


@pytest.mark.anyio
async def test_n1_snapshot_window_refetches_tail_when_total_exceeds_limit(
    sessions,
) -> None:
    """The same widened window stays honest when the transcript exceeds
    ``transcript_limit``: exactly the most recent ``limit`` rows ride the
    window and the omission is stated, never hidden."""
    conversation_id = await _make_conversation(sessions)
    for index in range(119):
        await _submit_user(sessions, conversation_id, f"filler {index}")
    last = await _submit_user(sessions, conversation_id, "the interpreted turn")

    snapshot = await aload_intake_snapshot(
        sessions,
        server_id=uuid.UUID(SERVER_A),
        conversation_id=conversation_id,
        source_message_id=last,
        transcript_limit=60,  # total=120 > 60 → tail page + omission flag
    )

    assert snapshot.transcript_omitted_older is True
    assert len(snapshot.transcript) == 60
    assert snapshot.transcript[-1] == (MessageRole.USER, "the interpreted turn")
    # total=120, limit=60 → the window is exactly messages 61..120
    # (filler 60 .. filler 118 + the interpreted turn).
    assert snapshot.transcript[0] == (MessageRole.USER, "filler 60")


def _new_task_payload(
    source_message_id: uuid.UUID,
    *,
    objective: str = "fresh episode",
    operations: Sequence[dict] = (),
    phase: str = "plan",
    **extra: object,
) -> dict:
    return _turn_base(
        "new_task",
        source_message_id,
        understanding="the user explicitly starts over",
        objective=objective,
        rationale="user explicitly starts a new goal episode",
        acceptance_criteria=[
            {"criterion_id": "done", "statement": "the new episode is done"}
        ],
        requested_phase=phase,
        todo_patch=_patch_dict(source_message_id, 1, *operations),
        **extra,
    )


@pytest.mark.anyio
async def test_n2_new_task_variant_creates_episode_with_zero_tools(sessions) -> None:
    """R-12 N2: ``new_task`` end-to-end — one episode created, one model
    call, zero declared AND harness-declared tools."""
    conversation_id = await _make_conversation(sessions)
    message_id = await _submit_user(sessions, conversation_id, "start a fresh goal")
    scripted = ScriptedModel(
        [
            {
                "payload": _new_task_payload(
                    message_id, operations=[_add_op("s1_first")]
                )
            },
        ]
    )

    outcome = await arun_intake_turn(
        scripted.model,
        sessions,
        server_id=uuid.UUID(SERVER_A),
        conversation_id=conversation_id,
        source_message_id=message_id,
    )

    assert outcome.turn.turn_kind == "new_task"
    assert outcome.applied is True and outcome.disposition == "created"
    assert outcome.task_id is not None and outcome.revision is not None
    assert outcome.record.model_calls == 1
    assert outcome.record.function_tools == 0
    assert scripted.function_tools_seen() == []  # harness-declared: zero tools
    assert await _count(sessions, "agent_tasks") == 1
    steps = await alist_steps(
        sessions, outcome.task_id, server_id=uuid.UUID(SERVER_A)
    )
    assert steps.total == 1


@pytest.mark.anyio
async def test_n2_blocked_variant_persists_nothing_on_active_task(sessions) -> None:
    """R-12 N2: ``blocked`` on a conversation WITH an active task — no
    task machinery touched, no task bound, one model call, zero tools."""
    conversation_id = await _make_conversation(sessions)
    first_message = await _submit_user(sessions, conversation_id, "plan the rollout")
    scripted_plan = ScriptedModel(
        [{"payload": _plan_payload(first_message, operations=[_add_op("s1_build")])}]
    )
    created = await arun_intake_turn(
        scripted_plan.model,
        sessions,
        server_id=uuid.UUID(SERVER_A),
        conversation_id=conversation_id,
        source_message_id=first_message,
    )
    assert created.applied is True

    blocked_message = await _submit_user(sessions, conversation_id, "go on")
    scripted_blocked = ScriptedModel(
        [
            {
                "payload": _turn_base(
                    "blocked",
                    blocked_message,
                    understanding="prerequisite missing",
                    reason="the required prerequisite service is missing",
                    blocked_kind="missing_prerequisite",
                )
            },
        ]
    )

    outcome = await arun_intake_turn(
        scripted_blocked.model,
        sessions,
        server_id=uuid.UUID(SERVER_A),
        conversation_id=conversation_id,
        source_message_id=blocked_message,
    )

    assert outcome.turn.turn_kind == "blocked"
    assert outcome.applied is False and outcome.task_id is None
    assert outcome.record.model_calls == 1
    assert outcome.record.function_tools == 0
    assert scripted_blocked.function_tools_seen() == []
    # The active episode is untouched: still exactly one task.
    assert await _count(sessions, "agent_tasks") == 1
    assert await _count(sessions, "agent_task_steps") == 1
    assert await _count(sessions, "agent_task_revisions") == 1


# ---------------------------------------------------------------------------
# pinned server scope in the FIRST intake call (chat-server-scope-fix)
# ---------------------------------------------------------------------------

#: Second synthetic scope: never enrolled as a conversation scope here —
#: it exists only as canonical profile material that must NOT leak.
SERVER_B = "b20c9d6f-2e3a-4b5c-8d7e-1f2a3b4c5d6e"

_SCOPE_HOST_KEY = "SHA256:" + "A" * 43

FRAME_INSTRUCTIONS = (
    "Frame is a SteamOS Steam Frame VR headset.\n"
    "Manage services through the adopted service inventory only.\n"
)


def _scope_profile_text(server_id: uuid.UUID, name: str) -> str:
    from mai.contracts.server import SCHEMA_VERSION

    return (
        f"schema_version: {SCHEMA_VERSION}\n"
        f'server_id: "{server_id}"\n'
        f'name: "{name}"\n'
        "prompt_revision: 1\n"
        "environment: linux_ssh\n"
        "ssh:\n"
        '  host: "198.51.100.99"\n'
        "  port: 22\n"
        '  user: "frame"\n'
        f'  host_key: "{_SCOPE_HOST_KEY}"\n'
        '  credential_ref: "MAI_SSH_KEY_FRAME"\n'
        "privilege: none\n"
        "capabilities: []\n"
    )


def _scope_materials(root, server_id: uuid.UUID, name: str, instructions: str):
    from pathlib import Path

    from mai.agents.context import load_server_context
    from mai.profiles.files import ProfileFileStore

    root = Path(root)
    store = ProfileFileStore(root)
    store.write(server_id, _scope_profile_text(server_id, name), expected_revision=0)
    server_dir = root / "servers" / str(server_id)
    server_dir.mkdir(parents=True, exist_ok=True)
    (server_dir / "prompt.md").write_text(instructions, encoding="utf-8")
    return load_server_context(root, server_id)


def _bare_snapshot(**overrides: object) -> IntakeSnapshot:
    """A minimal valid snapshot for prompt-level scope rendering tests."""
    from mai.contracts.conversations import MessageRole

    base: dict[str, object] = dict(
        server_id=uuid.UUID(SERVER_A),
        conversation_id=uuid.uuid4(),
        source_message_id=uuid.uuid4(),
        source_message_role=MessageRole.USER,
        source_message_content="Which ports are open on the device?",
        transcript=(),
        transcript_omitted_older=False,
        task_id=None,
        task_revision=None,
        task_phase=None,
        task_title=None,
        steps=(),
        steps_omitted=False,
        constraints=(),
        constraints_omitted=False,
    )
    base.update(overrides)
    return IntakeSnapshot(**base)  # type: ignore[arg-type]


class TestPinnedServerScope:
    @pytest.mark.anyio
    async def test_snapshot_prompt_renders_pinned_scope_and_repair_preserves_it(
        self, sessions, tmp_path
    ) -> None:
        materials = _scope_materials(
            tmp_path, uuid.UUID(SERVER_A), "Frame", FRAME_INSTRUCTIONS
        )
        conversation_id = await _make_conversation(sessions)
        message_id = await _submit_user(
            sessions, conversation_id, "Which ports are open on the device?"
        )
        snapshot = await aload_intake_snapshot(
            sessions,
            server_id=uuid.UUID(SERVER_A),
            conversation_id=conversation_id,
            source_message_id=message_id,
            server_materials=materials,
        )
        assert snapshot.server_name == "Frame"
        assert snapshot.server_environment == "linux_ssh"
        assert "Steam Frame VR headset" in snapshot.server_instructions

        prompt = build_snapshot_prompt(snapshot)
        assert "# Pinned server scope" in prompt
        assert f"server_id: {SERVER_A}" in prompt
        assert "server_name: Frame" in prompt
        assert "environment: linux_ssh" in prompt
        assert "Steam Frame VR headset" in prompt
        # The deterministic pinned-scope instructions ride the prompt.
        assert '"the device" mean THAT server' in prompt
        # Repair carries the FULL snapshot again: the scope survives.
        repair = build_repair_prompt(snapshot, {"turn_kind": "invalid_kind"})
        assert "# Pinned server scope" in repair
        assert "server_name: Frame" in repair

    @pytest.mark.anyio
    async def test_two_servers_never_leak_each_others_scope(
        self, sessions, tmp_path
    ) -> None:
        materials_frame = _scope_materials(
            tmp_path, uuid.UUID(SERVER_A), "Frame", FRAME_INSTRUCTIONS
        )
        other_instructions = "OtherBox is an unrelated synthetic backup host.\n"
        _scope_materials(
            tmp_path, uuid.UUID(SERVER_B), "OtherBox", other_instructions
        )
        conversation_id = await _make_conversation(sessions)
        message_id = await _submit_user(sessions, conversation_id, "check the device")

        frame_snapshot = await aload_intake_snapshot(
            sessions,
            server_id=uuid.UUID(SERVER_A),
            conversation_id=conversation_id,
            source_message_id=message_id,
            server_materials=materials_frame,
        )
        frame_prompt = build_snapshot_prompt(frame_snapshot)
        assert "server_name: Frame" in frame_prompt
        assert "OtherBox" not in frame_prompt
        assert "unrelated synthetic backup host" not in frame_prompt

        # The other server's materials render their own scope only.
        other_prompt = build_snapshot_prompt(
            _bare_snapshot(
                server_id=uuid.UUID(SERVER_B),
                server_name="OtherBox",
                server_environment="linux_ssh",
                server_instructions=other_instructions,
            )
        )
        assert "server_name: OtherBox" in other_prompt
        assert "Frame" not in other_prompt
        assert "Steam Frame VR headset" not in other_prompt

    def test_global_consult_scope_is_explicit(self) -> None:
        from mai.agents.context import load_server_context

        materials = load_server_context(
            pathlib.Path("/nonexistent-data-root"), uuid.UUID(int=0)
        )
        prompt = build_snapshot_prompt(
            _bare_snapshot(
                server_id=materials.server_id,
                server_name=materials.profile.name,
                server_environment=materials.profile.environment.value,
                server_instructions=materials.prompt_text,
            )
        )
        assert "# Pinned server scope" in prompt
        assert "no specific server is pinned" in prompt
        assert "general_consult" in prompt

    def test_unavailable_profile_is_rendered_explicitly(self) -> None:
        prompt = build_snapshot_prompt(_bare_snapshot())
        assert f"server_id: {SERVER_A}" in prompt
        assert "(canonical profile unavailable" in prompt
        assert "administrator server instructions: (canonical profile unavailable)" in (
            prompt
        )

    @pytest.mark.anyio
    async def test_scope_mismatch_rejected_before_model_calls(
        self, sessions, tmp_path
    ) -> None:
        materials_other = _scope_materials(
            tmp_path, uuid.UUID(SERVER_B), "OtherBox", "irrelevant\n"
        )
        conversation_id = await _make_conversation(sessions)
        message_id = await _submit_user(sessions, conversation_id, "check the device")
        scripted = ScriptedModel([])  # any call fails the test

        with pytest.raises(IntakeRejection) as excinfo:
            await arun_intake_turn(
                scripted.model,
                sessions,
                server_id=uuid.UUID(SERVER_A),
                conversation_id=conversation_id,
                source_message_id=message_id,
                server_materials=materials_other,
            )
        assert excinfo.value.fields == {"server_id": "server_scope_mismatch"}
        assert excinfo.value.model_calls == 0
        assert scripted.calls == 0
