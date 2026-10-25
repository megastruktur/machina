"""Focused tests for the ``task.*`` agent tools (card 14-task-tools).

Acceptance (BRIEF, act_CONTRACTS §7):

1. **Same task service, ratified rules, agent-ready descriptors, bounded
   output (AC 1).** Every tool call resolves the ACTIVE task from the
   bound run identity and runs through ``mai.agents.task_store`` (no
   parallel write path): CAS revisions, idempotency replay and the
   deterministic page envelopes are the store's own. Every payload field
   (nested included) declares a meaningful ``Field(description=...)`` and
   the descriptors pass ``assert_agent_ready_descriptor`` at build time;
   collection outputs stay strictly under the dispatcher's 64 KiB cap
   (over-budget pages are honestly re-bounded, never sliced silently).
2. **No authority self-grant (AC 2, P3/P4/C8).** The model lane cannot
   supersede a user-origin constraint, cannot claim ``origin=USER``
   without verified user-message anchors, cannot rewrite completed work,
   cannot mark a verdict without evidence, and cannot see or mutate
   another server's task (fail-closed C8) — payloads carry NO task/server
   selector at all.

Boundary discipline (test policy, sibling storage card): every storage
claim runs against a real disposable PostgreSQL fixture
(``MAI_TEST_DATABASE_URL``) with the real linear Alembic chain applied.
Fixtures carry only fixed opaque UUIDs and synthetic text. Run this suite
SEPARATELY from ``tests/test_task_store.py`` — the cross-suite
session-fixture teardown conflict is a documented baseline condition.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass

import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text

from mai.agents.conversation_store import (
    aattach_run,
    acreate_conversation,
    asubmit_turn,
)
from mai.agents.task_store import acreate_task
from mai.capabilities.dispatch import CapabilityCall, ExecutorResult
from mai.capabilities.internal_api import (
    InternalApiComposition,
    assert_agent_ready_descriptor,
)
from mai.capabilities.task_state import (
    ENVELOPE_MAX_BYTES,
    TASK_PROPOSE_UPDATE_CAPABILITY,
    TASK_READ_CAPABILITY,
    TASK_STATE_CAPABILITY_IDS,
    TASK_UPDATE_ITEM_CAPABILITY,
    _bound_page_envelope,
    build_task_state_capabilities,
)
from mai.contracts.tasks import (
    ConstraintOrigin,
    ConstraintProposal,
    EvidenceKind,
    StepStatus,
    TaskPlanStep,
)
from mai.database import create_app_engine
from mai.policy.evaluate import ExecutionContext, PolicyMode

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

# Fixed opaque fixture identities (canonical lowercase UUID strings).
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
# fixture builders
# ---------------------------------------------------------------------------


def _step(step_id: str, **overrides: object) -> TaskPlanStep:
    return TaskPlanStep(step_id=step_id, title=f"step {step_id}", **overrides)


def _constraint(
    constraint_id: str,
    *,
    origin: ConstraintOrigin = ConstraintOrigin.USER,
    suggested: str = "add",
    supersedes: str | None = None,
    source_refs: list[uuid.UUID] | None = None,
    statement: str | None = None,
) -> ConstraintProposal:
    return ConstraintProposal(
        constraint_id=constraint_id,
        category="safety",
        statement=statement or f"synthetic constraint {constraint_id}",
        origin=origin,
        suggested=suggested,  # type: ignore[arg-type]
        supersedes_constraint_id=supersedes,
        source_message_refs=source_refs or [],
    )


async def _make_conversation(sessions, server: str = SERVER_A) -> uuid.UUID:
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


@dataclass(slots=True)
class Fixture:
    conversation_id: uuid.UUID
    source_message: uuid.UUID
    task_id: uuid.UUID
    revision: int
    run_id: uuid.UUID
    server: str


async def _make_task_fixture(
    sessions,
    *,
    server: str = SERVER_A,
    with_constraint: bool = True,
    attach_task: bool = True,
    steps: list[TaskPlanStep] | None = None,
) -> Fixture:
    """One conversation + one user message + one task + one attached run.

    ``attach_task=False`` leaves the run unbound so the tool must resolve
    the conversation's ONE active episode (§1) instead.
    """
    conversation_id = await _make_conversation(sessions, server)
    source_message = await _make_message(sessions, conversation_id, server)
    run_id = uuid.uuid4()
    constraints = (
        [_constraint("keep_safe", source_refs=[source_message])]
        if with_constraint
        else []
    )
    created = await acreate_task(
        sessions,
        server_id=uuid.UUID(server),
        conversation_id=conversation_id,
        title="synthetic fixture task",
        summary="fixture objective",
        steps=steps or [_step("s1"), _step("s2", depends_on=["s1"]), _step("s3")],
        constraints=constraints,
    )
    await aattach_run(
        sessions,
        conversation_id=conversation_id,
        server_id=uuid.UUID(server),
        run_id=run_id,
        task_id=created.task.task_id if attach_task else None,
    )
    return Fixture(
        conversation_id=conversation_id,
        source_message=source_message,
        task_id=created.task.task_id,
        revision=created.task.revision,
        run_id=run_id,
        server=server,
    )


def _context(server: str, run_id: uuid.UUID) -> ExecutionContext:
    return ExecutionContext(
        principal="principal",
        server_id=uuid.UUID(server),
        run_id=run_id,
        parent_scope=None,
        profile_revision="1",
        prompt_revision="1",
        credential_references=(),
        allowed_capabilities=TASK_STATE_CAPABILITY_IDS,
        mode=PolicyMode.DELEGATED,
    )


def _composition(sessions) -> InternalApiComposition:
    return build_task_state_capabilities(session_factory=sessions)


def _call(
    composition: InternalApiComposition,
    capability_id: str,
    payload: dict[str, object],
    context: ExecutionContext,
) -> CapabilityCall:
    descriptor = {d.identifier: d for d in composition.descriptors}[capability_id]
    return CapabilityCall(
        descriptor=descriptor,
        payload=payload,
        context=context,
        credentials={},
        op_id=uuid.uuid4(),
    )


async def _execute(
    composition: InternalApiComposition,
    capability_id: str,
    payload: dict[str, object],
    context: ExecutionContext,
) -> tuple[ExecutorResult, dict[str, object]]:
    result = await composition.executors[capability_id](
        _call(composition, capability_id, payload, context)
    )
    return result, json.loads(result.output)


def _add_step_payload(
    source_message: uuid.UUID,
    base_revision: int,
    step_id: str = "s4",
    *,
    idempotency_key: uuid.UUID | None = None,
    constraints_proposed: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "patch": {
            "source_message_id": str(source_message),
            "base_revision": base_revision,
            "idempotency_key": str(idempotency_key or uuid.uuid4()),
            "rationale": "synthetic bounded delta",
            "operations": [
                {"op": "add", "step": {"step_id": step_id, "title": f"step {step_id}"}}
            ],
        },
        "constraints_proposed": constraints_proposed or [],
    }
    return payload


# ---------------------------------------------------------------------------
# AC 1 — agent-ready descriptors, bounded payloads, no scope selector
# ---------------------------------------------------------------------------


class TestDescriptors:
    def test_task_tools_are_agent_ready_and_fail_closed_at_build(self) -> None:
        composition = build_task_state_capabilities(session_factory=None)
        by_id = {d.identifier: d for d in composition.descriptors}
        assert set(by_id) == set(TASK_STATE_CAPABILITY_IDS)
        assert by_id[TASK_READ_CAPABILITY].effect_class == "read"
        assert by_id[TASK_PROPOSE_UPDATE_CAPABILITY].effect_class == "mutate"
        assert by_id[TASK_UPDATE_ITEM_CAPABILITY].effect_class == "mutate"
        for descriptor in composition.descriptors:
            assert_agent_ready_descriptor(descriptor)
            assert len(str(descriptor.input_schema["description"])) >= 30
            assert tuple(descriptor.execution_environments) == ("controller",)
            assert tuple(descriptor.required_grants) == (descriptor.identifier,)

    def test_payloads_carry_no_task_or_server_selector(self) -> None:
        composition = build_task_state_capabilities(session_factory=None)
        for descriptor in composition.descriptors:
            props = descriptor.input_schema["properties"]
            assert "task_id" not in props
            assert "server_id" not in props

    def test_read_page_bounds_are_declared_in_the_schema(self) -> None:
        composition = build_task_state_capabilities(session_factory=None)
        read_props = {
            d.identifier: d for d in composition.descriptors
        }[TASK_READ_CAPABILITY].input_schema["properties"]
        assert read_props["section"]["enum"] == [
            "header",
            "steps",
            "constraints",
            "evidence",
        ]
        assert read_props["limit"]["minimum"] == 1
        assert read_props["limit"]["maximum"] == 100
        assert read_props["offset"]["minimum"] == 0

    def test_propose_update_projects_nested_contract_models(self) -> None:
        composition = build_task_state_capabilities(session_factory=None)
        props = {
            d.identifier: d for d in composition.descriptors
        }[TASK_PROPOSE_UPDATE_CAPABILITY].input_schema["properties"]
        patch = props["patch"]
        assert patch["required"] == [
            "source_message_id",
            "base_revision",
            "idempotency_key",
        ]
        operations = patch["properties"]["operations"]
        step = operations["items"]["properties"]["step"]
        assert step["properties"]["status"]["enum"] == [s.value for s in StepStatus]
        evidence = step["properties"]["evidence_refs"]["items"]["properties"]
        assert evidence["kind"]["enum"] == [k.value for k in EvidenceKind]
        constraints = props["constraints_proposed"]["items"]["properties"]
        assert constraints["origin"]["enum"] == [
            o.value for o in ConstraintOrigin
        ]

    @pytest.mark.anyio
    async def test_unavailable_composition_refuses_fail_closed(self) -> None:
        composition = build_task_state_capabilities(session_factory=None)
        context = _context(SERVER_A, uuid.uuid4())
        minimal_payloads = {
            TASK_READ_CAPABILITY: {"section": "header"},
            TASK_PROPOSE_UPDATE_CAPABILITY: {
                "patch": {
                    "source_message_id": str(uuid.uuid4()),
                    "base_revision": 1,
                    "idempotency_key": str(uuid.uuid4()),
                    "operations": [],
                },
            },
            TASK_UPDATE_ITEM_CAPABILITY: {
                "step_id": "s1",
                "expected_revision": 1,
                "idempotency_key": str(uuid.uuid4()),
            },
        }
        for capability_id, payload in minimal_payloads.items():
            result = await composition.executors[capability_id](
                _call(composition, capability_id, payload, context)
            )
            document = json.loads(result.output)
            assert result.ok is False
            assert document["status"] == "refused"
            assert document["code"] == "unavailable"


class TestEnvelopeBound:
    def test_oversized_page_is_rebounded_honestly(self) -> None:
        items = [
            {"statement": "x" * 500, "position": index} for index in range(8)
        ]
        envelope: dict[str, object] = {
            "status": "ok",
            "items": items,
            "has_more": False,
            "total": 8,
        }
        bounded = _bound_page_envelope(envelope, max_bytes=2000)
        raw = json.dumps(bounded, sort_keys=True).encode("utf-8")
        assert len(raw) <= 2000
        assert bounded["items_rebounded"] is True
        assert bounded["has_more"] is True
        assert len(bounded["items"]) < 8  # type: ignore[arg-type]
        assert bounded["total"] == 8

    def test_undersized_page_is_untouched(self) -> None:
        envelope: dict[str, object] = {
            "status": "ok",
            "items": [{"position": 1}],
            "has_more": False,
        }
        bounded = _bound_page_envelope(envelope, max_bytes=ENVELOPE_MAX_BYTES)
        assert bounded == envelope
        assert "items_rebounded" not in bounded


# ---------------------------------------------------------------------------
# AC 1 — same task service: bounded reads, CAS, idempotency (real PG)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestTaskRead:
    async def test_header_and_section_pages_come_from_the_store(
        self, sessions
    ) -> None:
        fixture = await _make_task_fixture(sessions)
        composition = _composition(sessions)
        context = _context(fixture.server, fixture.run_id)

        result, header = await _execute(
            composition, TASK_READ_CAPABILITY, {"section": "header"}, context
        )
        assert result.ok is True, header
        assert header["status"] == "ok"
        assert header["task_id"] == str(fixture.task_id)
        assert header["revision"] == fixture.revision
        assert header["title"] == "synthetic fixture task"
        assert header["task_status"] == "in_progress"
        assert header["requested_phase"] == "plan"
        assert header["counts"]["steps_total"] == 3
        assert header["counts"]["constraints_total"] == 1
        assert header["counts"]["evidence_total"] == 0

        result, steps = await _execute(
            composition,
            TASK_READ_CAPABILITY,
            {"section": "steps", "limit": 2},
            context,
        )
        assert result.ok is True, steps
        assert [item["step_id"] for item in steps["items"]] == ["s1", "s2"]
        assert steps["total"] == 3
        assert steps["has_more"] is True
        assert steps["limit"] == 2
        assert steps["offset"] == 0

        result, tail = await _execute(
            composition,
            TASK_READ_CAPABILITY,
            {"section": "steps", "limit": 2, "offset": 2},
            context,
        )
        assert result.ok is True, tail
        assert [item["step_id"] for item in tail["items"]] == ["s3"]
        assert tail["has_more"] is False

        result, constraints = await _execute(
            composition, TASK_READ_CAPABILITY, {"section": "constraints"}, context
        )
        assert result.ok is True, constraints
        assert constraints["total"] == 1
        row = constraints["items"][0]
        assert row["constraint_id"] == "keep_safe"
        assert row["origin"] == "user"
        assert row["source_message_refs"] == [str(fixture.source_message)]

        result, evidence = await _execute(
            composition, TASK_READ_CAPABILITY, {"section": "evidence"}, context
        )
        assert result.ok is True, evidence
        assert evidence["items"] == []
        assert evidence["total"] == 0

    async def test_unbound_run_resolves_the_active_episode(
        self, sessions
    ) -> None:
        fixture = await _make_task_fixture(sessions, attach_task=False)
        composition = _composition(sessions)
        context = _context(fixture.server, fixture.run_id)
        result, header = await _execute(
            composition, TASK_READ_CAPABILITY, {"section": "header"}, context
        )
        assert result.ok is True, header
        assert header["task_id"] == str(fixture.task_id)

    async def test_cross_server_and_unknown_runs_fail_closed_c8(
        self, sessions
    ) -> None:
        fixture = await _make_task_fixture(sessions)
        composition = _composition(sessions)

        # Another server's context: the task is invisible, never leaked.
        intruder = _context(SERVER_B, fixture.run_id)
        result, document = await _execute(
            composition, TASK_READ_CAPABILITY, {"section": "header"}, intruder
        )
        assert result.ok is False
        assert document["status"] == "refused"
        assert document["code"] == "task_not_found"
        assert "synthetic" not in result.output
        assert str(fixture.task_id) not in result.output

        # A run that is not bound to any conversation: closed, not guessed.
        stranger = _context(fixture.server, uuid.uuid4())
        result, document = await _execute(
            composition, TASK_READ_CAPABILITY, {"section": "header"}, stranger
        )
        assert result.ok is False
        assert document["status"] == "refused"
        assert document["code"] == "not_found"

    async def test_read_pages_stay_under_the_dispatcher_cap(
        self, sessions
    ) -> None:
        """A full bound page of the largest contract-bounded rows still
        serializes strictly under the 64 KiB dispatcher budget."""
        conversation_id = await _make_conversation(sessions)
        source_message = await _make_message(sessions, conversation_id)
        run_id = uuid.uuid4()
        big_constraints = [
            _constraint(
                f"c{index:02d}",
                origin=ConstraintOrigin.MODEL,
                statement="y" * 500,
            )
            for index in range(12)
        ]
        created = await acreate_task(
            sessions,
            server_id=uuid.UUID(SERVER_A),
            conversation_id=conversation_id,
            title="oversized rows fixture",
            summary="fixture objective",
            steps=[_step("s1")],
            constraints=big_constraints,
        )
        await aattach_run(
            sessions,
            conversation_id=conversation_id,
            server_id=uuid.UUID(SERVER_A),
            run_id=run_id,
            task_id=created.task.task_id,
        )
        composition = _composition(sessions)
        outcome, page = await _execute(
            composition,
            TASK_READ_CAPABILITY,
            {"section": "constraints", "limit": 100},
            _context(SERVER_A, run_id),
        )
        assert outcome.ok is True, page
        assert page["total"] == 12
        assert len(page["items"]) == 12
        assert len(outcome.output.encode("utf-8")) < 64 * 1024
        assert "items_rebounded" not in page


@pytest.mark.anyio
class TestTaskProposeUpdate:
    async def test_delta_applies_under_cas_and_replays_idempotently(
        self, sessions
    ) -> None:
        fixture = await _make_task_fixture(sessions)
        composition = _composition(sessions)
        context = _context(fixture.server, fixture.run_id)
        key = uuid.uuid4()
        payload = _add_step_payload(
            fixture.source_message, fixture.revision, idempotency_key=key
        )

        result, applied = await _execute(
            composition, TASK_PROPOSE_UPDATE_CAPABILITY, payload, context
        )
        assert result.ok is True, applied
        assert applied["status"] == "ok"
        assert applied["task_id"] == str(fixture.task_id)
        assert applied["revision"] == fixture.revision + 1
        assert applied["disposition"] == "created"
        assert applied["revision_kind"] == "plan_patch"
        assert applied["author"] == "model"

        # C4: the SAME payload replays onto the SAME revision.
        result, replay = await _execute(
            composition, TASK_PROPOSE_UPDATE_CAPABILITY, payload, context
        )
        assert result.ok is True, replay
        assert replay["revision"] == fixture.revision + 1
        assert replay["disposition"] == "deduplicated"

        _, steps = await _execute(
            composition, TASK_READ_CAPABILITY, {"section": "steps"}, context
        )
        assert [item["step_id"] for item in steps["items"]] == [
            "s1",
            "s2",
            "s3",
            "s4",
        ]

        # C6: a stale base revision is the stable conflict — twice the
        # same key with a DIFFERENT payload is the stable replay conflict.
        stale = _add_step_payload(
            fixture.source_message, fixture.revision, step_id="s5"
        )
        result, document = await _execute(
            composition, TASK_PROPOSE_UPDATE_CAPABILITY, stale, context
        )
        assert result.ok is False
        assert document["status"] == "refused"
        assert document["code"] == "revision_conflict"

        clashing_key = _add_step_payload(
            fixture.source_message,
            fixture.revision + 1,
            step_id="s6",
            idempotency_key=key,
        )
        result, document = await _execute(
            composition, TASK_PROPOSE_UPDATE_CAPABILITY, clashing_key, context
        )
        assert result.ok is False
        assert document["status"] == "refused"
        assert document["code"] == "invalid_task_patch"

    async def test_model_cannot_supersede_user_constraint_nor_claim_user_origin(
        self, sessions
    ) -> None:
        fixture = await _make_task_fixture(sessions)
        composition = _composition(sessions)
        context = _context(fixture.server, fixture.run_id)

        # (a) A model-origin supersede of the USER constraint: escalation.
        payload = _add_step_payload(
            fixture.source_message,
            fixture.revision,
            constraints_proposed=[
                {
                    "constraint_id": "watered_down",
                    "category": "safety",
                    "statement": "synthetic weaker constraint",
                    "origin": "model",
                    "suggested": "supersede",
                    "supersedes_constraint_id": "keep_safe",
                }
            ],
        )
        result, document = await _execute(
            composition, TASK_PROPOSE_UPDATE_CAPABILITY, payload, context
        )
        assert result.ok is False
        assert document["status"] == "refused"
        assert "cannot supersede a user-origin constraint" in document["message"]

        # (b) A USER-origin claim without verified user-message anchors:
        # the R-11 author↔origin defense refuses it before any write.
        claimed = _add_step_payload(
            fixture.source_message,
            fixture.revision,
            constraints_proposed=[
                {
                    "constraint_id": "claimed_user",
                    "category": "safety",
                    "statement": "synthetic claimed user constraint",
                    "origin": "user",
                    "suggested": "add",
                    "source_message_refs": [],
                }
            ],
        )
        result, document = await _execute(
            composition, TASK_PROPOSE_UPDATE_CAPABILITY, claimed, context
        )
        assert result.ok is False
        assert document["status"] == "refused"
        assert "must anchor to at least one verified user message" in document[
            "message"
        ]

        # (c) A USER-origin SUPERSEDE claim is refused outright: user
        # constraint removal stays on the user revision API.
        claimed_supersede = _add_step_payload(
            fixture.source_message,
            fixture.revision,
            constraints_proposed=[
                {
                    "constraint_id": "claimed_removal",
                    "category": "safety",
                    "statement": "synthetic claimed removal",
                    "origin": "user",
                    "suggested": "supersede",
                    "supersedes_constraint_id": "keep_safe",
                    "source_message_refs": [str(fixture.source_message)],
                }
            ],
        )
        result, document = await _execute(
            composition, TASK_PROPOSE_UPDATE_CAPABILITY, claimed_supersede, context
        )
        assert result.ok is False
        assert document["status"] == "refused"
        assert "cannot claim user origin" in document["message"]

        # Zero effects from the three refusals: the revision and the
        # constraint set are untouched.
        _, header = await _execute(
            composition, TASK_READ_CAPABILITY, {"section": "header"}, context
        )
        assert header["revision"] == fixture.revision
        assert header["counts"]["constraints_total"] == 1
        _, constraints = await _execute(
            composition, TASK_READ_CAPABILITY, {"section": "constraints"}, context
        )
        assert [row["constraint_id"] for row in constraints["items"]] == [
            "keep_safe"
        ]

        # (d) An honest model-origin ADD is admissible and recorded as
        # model inference — the defense targets authority claims, not
        # model constraints as such.
        honest = _add_step_payload(
            fixture.source_message,
            fixture.revision,
            constraints_proposed=[
                {
                    "constraint_id": "model_note",
                    "category": "scope",
                    "statement": "synthetic model-inferred constraint",
                    "origin": "model",
                    "suggested": "add",
                }
            ],
        )
        result, applied = await _execute(
            composition, TASK_PROPOSE_UPDATE_CAPABILITY, honest, context
        )
        assert result.ok is True, applied
        _, constraints = await _execute(
            composition, TASK_READ_CAPABILITY, {"section": "constraints"}, context
        )
        assert [row["constraint_id"] for row in constraints["items"]] == [
            "keep_safe",
            "model_note",
        ]
        assert constraints["items"][1]["origin"] == "model"


@pytest.mark.anyio
class TestTaskUpdateItem:
    async def test_item_advances_with_evidence_and_freezes(self, sessions) -> None:
        fixture = await _make_task_fixture(sessions)
        composition = _composition(sessions)
        context = _context(fixture.server, fixture.run_id)
        evidence = [
            {
                "kind": "message",
                "evidence_id": str(fixture.source_message),
                "note": "synthetic",
            }
        ]

        payload = {
            "step_id": "s1",
            "expected_revision": fixture.revision,
            "idempotency_key": str(uuid.uuid4()),
            "status": "in_progress",
        }
        result, applied = await _execute(
            composition, TASK_UPDATE_ITEM_CAPABILITY, payload, context
        )
        assert result.ok is True, applied
        assert applied["revision"] == fixture.revision + 1
        assert applied["step_status"] == "in_progress"
        assert applied["disposition"] == "created"

        executed = {
            "step_id": "s1",
            "expected_revision": fixture.revision + 1,
            "idempotency_key": str(uuid.uuid4()),
            "status": "executed",
            "verification": "passed",
            "result_summary": "synthetic bounded result",
            "evidence_refs": evidence,
        }
        result, applied = await _execute(
            composition, TASK_UPDATE_ITEM_CAPABILITY, executed, context
        )
        assert result.ok is True, applied
        assert applied["step_status"] == "executed"
        assert applied["step_verification"] == "passed"

        # C4: the SAME executed payload replays onto the SAME revision.
        result, replay = await _execute(
            composition, TASK_UPDATE_ITEM_CAPABILITY, executed, context
        )
        assert result.ok is True, replay
        assert replay["disposition"] == "deduplicated"
        assert replay["revision"] == fixture.revision + 2

        # P3: executed work is frozen recorded work — no reopening.
        reopen = {
            "step_id": "s1",
            "expected_revision": fixture.revision + 2,
            "idempotency_key": str(uuid.uuid4()),
            "status": "pending",
        }
        result, document = await _execute(
            composition, TASK_UPDATE_ITEM_CAPABILITY, reopen, context
        )
        assert result.ok is False
        assert document["status"] == "refused"
        assert document["code"] == "invalid_task_patch"

        # The evidence registry recorded the referenced message once.
        _, evidence_page = await _execute(
            composition, TASK_READ_CAPABILITY, {"section": "evidence"}, context
        )
        assert evidence_page["total"] == 1
        assert evidence_page["items"][0]["kind"] == "message"

    async def test_verdicts_are_evidence_bound_and_cite_real_records(
        self, sessions
    ) -> None:
        fixture = await _make_task_fixture(sessions)
        composition = _composition(sessions)
        context = _context(fixture.server, fixture.run_id)

        # A passed verdict without evidence is refused (P4: no false
        # verification through the tool lane).
        unevidenced = {
            "step_id": "s2",
            "expected_revision": fixture.revision,
            "idempotency_key": str(uuid.uuid4()),
            "verification": "passed",
        }
        result, document = await _execute(
            composition, TASK_UPDATE_ITEM_CAPABILITY, unevidenced, context
        )
        assert result.ok is False
        assert document["status"] == "refused"
        assert "evidence reference" in document["message"]

        # A hallucinated evidence record fails closed (P4).
        hallucinated = {
            "step_id": "s2",
            "expected_revision": fixture.revision,
            "idempotency_key": str(uuid.uuid4()),
            "verification": "passed",
            "evidence_refs": [
                {
                    "kind": "message",
                    "evidence_id": str(uuid.uuid4()),
                    "note": "synthetic",
                }
            ],
        }
        result, document = await _execute(
            composition, TASK_UPDATE_ITEM_CAPABILITY, hallucinated, context
        )
        assert result.ok is False
        assert document["status"] == "refused"
        assert document["code"] == "invalid_task_patch"

        # Zero effects from both refusals.
        _, header = await _execute(
            composition, TASK_READ_CAPABILITY, {"section": "header"}, context
        )
        assert header["revision"] == fixture.revision
        _, steps = await _execute(
            composition, TASK_READ_CAPABILITY, {"section": "steps"}, context
        )
        assert [item["step_id"] for item in steps["items"]] == ["s1", "s2", "s3"]

        # With a REAL message reference the verdict is admissible.
        evidenced = {
            "step_id": "s2",
            "expected_revision": fixture.revision,
            "idempotency_key": str(uuid.uuid4()),
            "verification": "passed",
            "evidence_refs": [
                {
                    "kind": "message",
                    "evidence_id": str(fixture.source_message),
                    "note": "synthetic",
                }
            ],
        }
        result, applied = await _execute(
            composition, TASK_UPDATE_ITEM_CAPABILITY, evidenced, context
        )
        assert result.ok is True, applied
        assert applied["step_verification"] == "passed"

    async def test_stale_revision_and_unknown_steps_fail_closed(
        self, sessions
    ) -> None:
        fixture = await _make_task_fixture(sessions)
        composition = _composition(sessions)
        context = _context(fixture.server, fixture.run_id)

        stale = {
            "step_id": "s1",
            "expected_revision": fixture.revision + 7,
            "idempotency_key": str(uuid.uuid4()),
            "status": "in_progress",
        }
        result, document = await _execute(
            composition, TASK_UPDATE_ITEM_CAPABILITY, stale, context
        )
        assert result.ok is False
        assert document["status"] == "refused"
        assert document["code"] == "revision_conflict"

        unknown = {
            "step_id": "no_such_step",
            "expected_revision": fixture.revision,
            "idempotency_key": str(uuid.uuid4()),
            "status": "in_progress",
        }
        result, document = await _execute(
            composition, TASK_UPDATE_ITEM_CAPABILITY, unknown, context
        )
        assert result.ok is False
        assert document["status"] == "refused"
        assert document["code"] == "invalid_task_patch"


@pytest.mark.anyio
class TestScopeBinding:
    async def test_mutations_cannot_reach_another_server(self, sessions) -> None:
        fixture = await _make_task_fixture(sessions)
        composition = _composition(sessions)
        intruder = _context(SERVER_B, fixture.run_id)
        for capability_id, payload in (
            (TASK_READ_CAPABILITY, {"section": "header"}),
            (
                TASK_PROPOSE_UPDATE_CAPABILITY,
                _add_step_payload(fixture.source_message, fixture.revision),
            ),
            (
                TASK_UPDATE_ITEM_CAPABILITY,
                {
                    "step_id": "s1",
                    "expected_revision": fixture.revision,
                    "idempotency_key": str(uuid.uuid4()),
                    "status": "in_progress",
                },
            ),
        ):
            result, document = await _execute(
                composition, capability_id, payload, intruder
            )
            assert result.ok is False, document
            assert document["status"] == "refused"
            assert document["code"] == "task_not_found"
            assert str(fixture.task_id) not in result.output
        # The refused mutations wrote nothing.
        _, header = await _execute(
            composition,
            TASK_READ_CAPABILITY,
            {"section": "header"},
            _context(fixture.server, fixture.run_id),
        )
        assert header["revision"] == fixture.revision
