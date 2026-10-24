"""Focused tests for evidence-backed task completion (card 20).

Acceptance (BRIEF, act_CONTRACTS §9, act_RUNTIME E1/E2/P3):

1. **Per-criterion explicitness + evidence authority (AC 1):** every
   acceptance criterion ends up explicitly ``passed`` (supported),
   ``failed`` or ``unavailable`` — never silently dropped, never assumed
   supported. Wrong (certainty-incompatible), stale (CAS
   ``revision_conflict``), wrong-target (another task's registry) and
   fabricated (unregistered) evidence refs cannot pass validation; every
   rejection has ZERO effects (no receipt, no revision bump, no
   criterion advance, no terminal transition).
2. **No run/verification conflation (AC 2):** the final-seam observation
   records NOTHING — final assistant text alone can never mark a task
   ``verified``; the technical ``RunState`` machine is untouched (the
   observation is additive and read-only); a task with
   ``failed``/``unavailable`` criteria stays OPEN and resumable, and a
   later proposal can still advance it to ``verified`` with new
   evidence (P3: same stable criterion IDs, revision delta).

Boundary discipline: real disposable PostgreSQL fixture (real Alembic
chain through the head) for the storage claims, pure in-memory tests for
the derivation matrices, and the REAL act-19 ``project_tool_result``
projection as the certainty-record producer in the cross-card test.
Fixtures carry only fixed opaque UUIDs and synthetic text.
"""

from __future__ import annotations

import os
import uuid

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
from mai.agents.task_completion import (
    COMPLETION_PROPOSAL_SCHEMA_VERSION,
    CompletionProposal,
    EvidenceCertainty,
    afinalize_completion,
    aobserve_task_outcome,
    derive_task_outcome,
    resolve_proposed_criteria,
    resolve_recorded_criteria,
)
from mai.agents.task_store import acreate_task
from mai.contracts.tasks import (
    MAX_CRITERIA,
    AcceptanceCriterion,
    CriterionResult,
    EvidenceKind,
    EvidenceRef,
    TaskOutcome,
    TaskPlanStep,
    VerificationStatus,
)
from mai.database import create_app_engine

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(BACKEND_ROOT, "alembic.ini")

LIVE_URL_VAR = "MAI_TEST_DATABASE_URL"

SERVER_A = "a10b8c5e-1d2f-4a3b-9c4d-0e5f6a7b8c9d"
SERVER_B = "b20c9d6f-2e3a-4b4c-8d5e-1f0a9b8c7d6e"
SERVER_A_UUID = uuid.UUID(SERVER_A)
SERVER_B_UUID = uuid.UUID(SERVER_B)

TASK_TABLES = (
    "agent_task_completions",
    "agent_task_revisions",
    "agent_task_evidence",
    "agent_task_criteria",
    "agent_task_constraints",
    "agent_task_steps",
    "agent_tasks",
)

DOMAIN_TABLES = (
    *TASK_TABLES,
    "conversation_steering",
    "conversation_runs",
    "conversation_messages",
    "conversations",
)


def _alembic_config() -> AlembicConfig:
    return AlembicConfig(ALEMBIC_INI)


def _drop_domain(url: str) -> None:
    """Reset the fixture database to pre-bootstrap state (engine too).

    ``alembic_version`` drops with the app tables so the session fixture's
    ``upgrade head`` re-runs the FULL chain (the sibling storage suite's
    proven pattern — a version row without its tables would make the
    upgrade a silent no-op).
    """
    table_list = ", ".join(
        name
        for name in (
            *DOMAIN_TABLES,
            "job_resource_locks",
            "job_schedule_fires",
            "job_schedules",
            "job_events",
            "job_steps",
            "job_runs",
            "approvals",
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
        conn.commit()


@pytest.fixture(scope="session")
def migrated_url() -> str:
    """Disposable fixture database migrated from zero through the head."""
    url = os.environ.get(LIVE_URL_VAR)
    if not url:
        pytest.skip(
            f"{LIVE_URL_VAR} is not set; the completion claims need a real "
            "disposable Postgres fixture"
        )
    previous = os.environ.get("MAI_DATABASE_URL")
    os.environ["MAI_DATABASE_URL"] = url  # migrations/env.py contract
    try:
        _drop_domain(url)
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
        for table in DOMAIN_TABLES:
            await session.execute(text(f"DELETE FROM {table}"))
        await session.commit()
    await engine.dispose()


# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------


def _criterion(criterion_id: str):
    return AcceptanceCriterion(
        criterion_id=criterion_id,
        statement=f"criterion {criterion_id} holds",
    )


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
    *,
    server: str = SERVER_A,
    criterion_ids: tuple[str, ...] = ("c1", "c2"),
    with_registered_evidence: bool = True,
):
    """One task with criteria and (optionally) TWO registered evidence refs.

    Each evidence ref is a REAL conversation message of the task's own
    conversation, registered through the creation path (step
    ``evidence_refs``), exactly like a legitimate producer would. Two
    distinct refs let one proposal carry independently-certified claims
    (a proven failure AND a proven success).
    """
    conversation = await acreate_conversation(sessions, server_id=uuid.UUID(server))
    steps: list[TaskPlanStep] = []
    if with_registered_evidence:
        for position in ("1", "2"):
            message_id = await _make_message(
                sessions, conversation.conversation_id, server=server
            )
            steps.append(
                TaskPlanStep(
                    step_id=f"s{position}",
                    title=f"step s{position}",
                    evidence_refs=[
                        EvidenceRef(
                            kind=EvidenceKind.MESSAGE,
                            evidence_id=message_id,
                            note="synthetic",
                        )
                    ],
                )
            )
    result = await acreate_task(
        sessions,
        server_id=uuid.UUID(server),
        conversation_id=conversation.conversation_id,
        title="synthetic fixture task",
        summary="fixture objective",
        criteria=[_criterion(cid) for cid in criterion_ids],
        steps=steps,
    )
    return result.task, conversation.conversation_id


def _message_ref(evidence_id: uuid.UUID) -> EvidenceRef:
    return EvidenceRef(
        kind=EvidenceKind.MESSAGE, evidence_id=evidence_id, note="synthetic"
    )


def _certainty(
    evidence_id: uuid.UUID,
    *,
    domain: str = "success",
    certainty: str = "certain",
) -> EvidenceCertainty:
    return EvidenceCertainty(
        evidence_id=str(evidence_id),
        domain_outcome=domain,  # type: ignore[arg-type]
        effect_certainty=certainty,  # type: ignore[arg-type]
    )


def _proposal(
    task,
    results: list[CriterionResult],
    *,
    base_revision: int | None = None,
    certainty: list[EvidenceCertainty] | None = None,
    evidence_refs: list[EvidenceRef] | None = None,
) -> CompletionProposal:
    """One typed proposal; the shape has NO outcome field to self-grant."""
    return CompletionProposal(
        task_id=task.task_id,
        base_revision=base_revision if base_revision is not None else task.revision,
        idempotency_key=uuid.uuid4(),
        execution_status="succeeded",
        domain_outcome="success",
        effect_certainty="certain",
        criterion_results=results,
        evidence_refs=evidence_refs or [],
        evidence_certainty=certainty or [],
    )


def _passed(criterion_id: str, refs: list[EvidenceRef]) -> CriterionResult:
    return CriterionResult(
        criterion_id=criterion_id,
        result=VerificationStatus.PASSED,
        evidence_refs=refs,
    )


def _failed(criterion_id: str, refs: list[EvidenceRef]) -> CriterionResult:
    return CriterionResult(
        criterion_id=criterion_id,
        result=VerificationStatus.FAILED,
        evidence_refs=refs,
    )


def _unavailable(criterion_id: str) -> CriterionResult:
    return CriterionResult(
        criterion_id=criterion_id,
        result=VerificationStatus.UNAVAILABLE,
        evidence_refs=[],
    )


async def _task_row(sessions, task_id: uuid.UUID):
    async with sessions() as session:
        return (
            await session.execute(
                text(
                    "SELECT status, revision, active_step_id FROM agent_tasks "
                    "WHERE task_id = :tid"
                ),
                {"tid": task_id},
            )
        ).first()


async def _criterion_states(sessions, task_id: uuid.UUID) -> dict[str, str]:
    async with sessions() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT criterion_id, verification FROM agent_task_criteria "
                    "WHERE task_id = :tid ORDER BY position, criterion_id"
                ),
                {"tid": task_id},
            )
        ).all()
    return {str(row[0]): str(row[1]) for row in rows}


async def _counts(sessions, task_id: uuid.UUID) -> dict[str, int]:
    async with sessions() as session:
        completions = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM agent_task_completions WHERE task_id = :tid"
                ),
                {"tid": task_id},
            )
        ).scalar()
        revisions = (
            await session.execute(
                text("SELECT COUNT(*) FROM agent_task_revisions WHERE task_id = :tid"),
                {"tid": task_id},
            )
        ).scalar()
    return {"completions": int(completions), "revisions": int(revisions)}


# ---------------------------------------------------------------------------
# AC 1 — pure derivation matrices (no database)
# ---------------------------------------------------------------------------


class TestDerivationMatrices:
    def test_outcome_matrix_is_explicit_and_never_self_closes(self) -> None:
        passed = VerificationStatus.PASSED
        failed = VerificationStatus.FAILED
        unavailable = VerificationStatus.UNAVAILABLE
        assert derive_task_outcome({}) is TaskOutcome.IN_PROGRESS
        assert derive_task_outcome({"a": passed, "b": passed}) is TaskOutcome.VERIFIED
        assert (
            derive_task_outcome({"a": passed, "b": unavailable})
            is TaskOutcome.PARTIALLY_VERIFIED
        )
        assert (
            derive_task_outcome({"a": passed, "b": failed})
            is TaskOutcome.PARTIALLY_VERIFIED
        )
        # Proven failures never close the episode (P3 resumability).
        assert (
            derive_task_outcome({"a": failed, "b": failed}) is TaskOutcome.IN_PROGRESS
        )
        assert derive_task_outcome({"a": unavailable}) is TaskOutcome.IN_PROGRESS

    def test_uncovered_criteria_are_explicitly_unavailable(self) -> None:
        resolutions, rejections = resolve_proposed_criteria(
            ["c1", "c2"],
            [_unavailable("c1")],
            {},
        )
        assert rejections == {}
        assert resolutions["c1"].state is VerificationStatus.UNAVAILABLE
        assert resolutions["c2"].state is VerificationStatus.UNAVAILABLE
        assert resolutions["c2"].reason_code == "not_proposed"

    def test_recorded_resolution_renders_every_stored_state(self) -> None:
        resolutions = resolve_recorded_criteria(
            {
                "c1": VerificationStatus.PASSED,
                "c2": VerificationStatus.FAILED,
                "c3": VerificationStatus.NOT_CHECKED,
            }
        )
        assert resolutions["c1"].state is VerificationStatus.PASSED
        assert resolutions["c1"].source == "recorded"
        assert resolutions["c2"].state is VerificationStatus.FAILED
        assert resolutions["c3"].state is VerificationStatus.UNAVAILABLE

    def test_passed_claim_needs_recorded_certain_success(self) -> None:
        evidence = uuid.uuid4()
        cases = [
            (("success", "certain"), None),
            (("success", "uncertain"), "certainty_incompatible"),
            (("failure", "certain"), "certainty_incompatible"),
            (("unknown", "unknown"), "certainty_incompatible"),
        ]
        for (domain, certainty), expected in cases:
            resolutions, rejections = resolve_proposed_criteria(
                ["c1"],
                [_passed("c1", [_message_ref(evidence)])],
                {
                    str(evidence): EvidenceCertainty(
                        evidence_id=str(evidence),
                        domain_outcome=domain,  # type: ignore[arg-type]
                        effect_certainty=certainty,  # type: ignore[arg-type]
                    )
                },
            )
            if expected is None:
                assert rejections == {}, (domain, certainty)
                assert resolutions["c1"].state is VerificationStatus.PASSED
            else:
                assert rejections == {"criterion_results[0]": expected}, (
                    domain,
                    certainty,
                )
                assert resolutions["c1"].state is VerificationStatus.UNAVAILABLE

    def test_missing_certainty_record_is_unknown_and_rejected(self) -> None:
        evidence = uuid.uuid4()
        resolutions, rejections = resolve_proposed_criteria(
            ["c1"],
            [_passed("c1", [_message_ref(evidence)])],
            {},  # no recorded certainty for the cited ref
        )
        assert rejections == {"criterion_results[0]": "certainty_incompatible"}
        assert resolutions["c1"].state is VerificationStatus.UNAVAILABLE

    def test_failed_claim_rules_mirror_the_ratified_validator(self) -> None:
        evidence = uuid.uuid4()
        # Zero refs: failure claims are not self-grants — admissible.
        _, rejections = resolve_proposed_criteria(["c1"], [_failed("c1", [])], {})
        assert rejections == {}
        # A recorded domain failure (even uncertain) supports the claim.
        _, rejections = resolve_proposed_criteria(
            ["c1"],
            [_failed("c1", [_message_ref(evidence)])],
            {
                str(evidence): _certainty(
                    evidence, domain="failure", certainty="uncertain"
                )
            },
        )
        assert rejections == {}
        # Success-only or unproven citations cannot report a failure.
        for domain, certainty in (("success", "certain"), ("unknown", "unknown")):
            _, rejections = resolve_proposed_criteria(
                ["c1"],
                [_failed("c1", [_message_ref(evidence)])],
                {
                    str(evidence): _certainty(
                        evidence, domain=domain, certainty=certainty
                    )
                },
            )
            assert rejections == {"criterion_results[0]": "certainty_incompatible"}, (
                domain,
                certainty,
            )

    def test_passed_claim_without_refs_is_rejected(self) -> None:
        resolutions, rejections = resolve_proposed_criteria(
            ["c1"], [_passed("c1", [])], {}
        )
        assert rejections == {"criterion_results[0]": "missing_evidence_refs"}
        assert resolutions["c1"].state is VerificationStatus.UNAVAILABLE

    def test_unknown_claimed_criterion_is_flagged(self) -> None:
        _, rejections = resolve_proposed_criteria(
            ["c1"], [_passed("ghost", [_message_ref(uuid.uuid4())])], {}
        )
        assert rejections == {"criterion_results[0].criterion_id": "unknown_criterion"}

    def test_proposal_schema_version_fails_closed(self) -> None:
        with pytest.raises(ValueError, match="schema version"):
            CompletionProposal(
                schema_version=COMPLETION_PROPOSAL_SCHEMA_VERSION + 1,
                task_id=uuid.uuid4(),
                base_revision=1,
                idempotency_key=uuid.uuid4(),
                execution_status="succeeded",
                domain_outcome="success",
                effect_certainty="certain",
            )

    def test_criteria_bound_is_the_contract_bound(self) -> None:
        assert MAX_CRITERIA >= 2  # the fixtures rely on multi-criterion tasks


# ---------------------------------------------------------------------------
# AC 1 — proposal finalization against the real store (E1/E2)
# ---------------------------------------------------------------------------


class TestFinalizeCompletion:
    @pytest.mark.anyio
    async def test_verified_path_closes_the_episode(self, sessions) -> None:
        task, _ = await _make_task(sessions)
        evidence = await _registered_evidence_id(sessions, task.task_id)
        proposal = _proposal(
            task,
            [
                _passed("c1", [_message_ref(evidence)]),
                _passed("c2", [_message_ref(evidence)]),
            ],
            certainty=[
                _certainty(evidence),
            ],
        )
        observation = await afinalize_completion(
            sessions, server_id=task.server_id, proposal=proposal
        )
        assert observation.recorded is True
        assert observation.outcome is TaskOutcome.VERIFIED
        assert observation.verified is True
        row = await _task_row(sessions, task.task_id)
        assert row[0] == "verified"  # terminal header
        assert row[2] is None  # active pointer released
        assert await _criterion_states(sessions, task.task_id) == {
            "c1": "passed",
            "c2": "passed",
        }
        counts = await _counts(sessions, task.task_id)
        assert counts == {"completions": 1, "revisions": 2}  # create + completion

    @pytest.mark.anyio
    async def test_uncovered_criterion_is_explicit_and_episode_stays_open(
        self, sessions
    ) -> None:
        task, _ = await _make_task(sessions, criterion_ids=("c1", "c2"))
        evidence = await _registered_evidence_id(sessions, task.task_id)
        proposal = _proposal(
            task,
            [_passed("c1", [_message_ref(evidence)])],
            certainty=[_certainty(evidence)],
        )
        observation = await afinalize_completion(
            sessions, server_id=task.server_id, proposal=proposal
        )
        assert observation.recorded is True
        assert observation.outcome is TaskOutcome.PARTIALLY_VERIFIED
        assert observation.verified is False
        states = {r.criterion_id: r.state for r in observation.criteria}
        assert states == {
            "c1": VerificationStatus.PASSED,
            "c2": VerificationStatus.UNAVAILABLE,  # explicit, never assumed
        }
        row = await _task_row(sessions, task.task_id)
        assert row[0] == "partially_verified"  # OPEN — resumable (P3)
        # The episode stays the conversation's ACTIVE task (resumable):
        async with sessions() as session:
            active = (
                await session.execute(
                    text(
                        "SELECT active_task_id FROM conversations "
                        "WHERE conversation_id = :cid"
                    ),
                    {"cid": task.conversation_id},
                )
            ).scalar()
        assert active == task.task_id
        assert await _criterion_states(sessions, task.task_id) == {
            "c1": "passed",
            "c2": "not_checked",  # nothing fabricated into the store
        }

    @pytest.mark.anyio
    async def test_continuation_advances_open_episode_to_verified(
        self, sessions
    ) -> None:
        task, _ = await _make_task(sessions, criterion_ids=("c1", "c2"))
        evidence = await _registered_evidence_id(sessions, task.task_id)
        first = await afinalize_completion(
            sessions,
            server_id=task.server_id,
            proposal=_proposal(
                task,
                [_passed("c1", [_message_ref(evidence)])],
                certainty=[_certainty(evidence)],
            ),
        )
        assert first.outcome is TaskOutcome.PARTIALLY_VERIFIED
        # The user says "continue"; the next attempt covers the missing
        # criterion with fresh evidence — SAME stable criterion IDs.
        second = await afinalize_completion(
            sessions,
            server_id=task.server_id,
            proposal=_proposal(
                task,
                [
                    _passed("c1", [_message_ref(evidence)]),
                    _passed("c2", [_message_ref(evidence)]),
                ],
                base_revision=first.revision,
                certainty=[_certainty(evidence)],
            ),
        )
        assert second.recorded is True
        assert second.outcome is TaskOutcome.VERIFIED
        assert second.revision == first.revision + 1  # revision delta, P3
        row = await _task_row(sessions, task.task_id)
        assert row[0] == "verified"

    @pytest.mark.anyio
    async def test_failed_criterion_keeps_the_episode_open(self, sessions) -> None:
        task, _ = await _make_task(sessions)
        failure_ref, success_ref = await _registered_evidence_ids(
            sessions, task.task_id
        )
        proposal = _proposal(
            task,
            [
                _failed("c1", [_message_ref(failure_ref)]),
                _passed("c2", [_message_ref(success_ref)]),
            ],
            certainty=[
                _certainty(failure_ref, domain="failure", certainty="uncertain"),
                _certainty(success_ref),
            ],
        )
        observation = await afinalize_completion(
            sessions, server_id=task.server_id, proposal=proposal
        )
        assert observation.recorded is True
        assert observation.outcome is TaskOutcome.PARTIALLY_VERIFIED
        assert observation.verified is False
        row = await _task_row(sessions, task.task_id)
        assert row[0] == "partially_verified"  # open, repairable
        assert (await _criterion_states(sessions, task.task_id))["c1"] == "failed"

    @pytest.mark.anyio
    async def test_all_failed_stays_open_and_in_progress(self, sessions) -> None:
        task, _ = await _make_task(sessions)
        evidence = await _registered_evidence_id(sessions, task.task_id)
        proposal = _proposal(
            task,
            [
                _failed("c1", [_message_ref(evidence)]),
                _failed("c2", [_message_ref(evidence)]),
            ],
            certainty=[_certainty(evidence, domain="failure", certainty="certain")],
        )
        observation = await afinalize_completion(
            sessions, server_id=task.server_id, proposal=proposal
        )
        assert observation.recorded is True
        assert observation.outcome is TaskOutcome.IN_PROGRESS
        row = await _task_row(sessions, task.task_id)
        assert row[0] == "in_progress"  # open, resumable

    @pytest.mark.anyio
    async def test_fabricated_evidence_ref_is_refused_with_zero_effects(
        self, sessions
    ) -> None:
        task, _ = await _make_task(sessions)
        before = await _task_row(sessions, task.task_id)
        ghost = uuid.uuid4()
        proposal = _proposal(
            task,
            [
                _passed("c1", [_message_ref(ghost)]),
                _passed("c2", [_message_ref(ghost)]),
            ],
            certainty=[_certainty(ghost)],  # "certain" lie about a ghost ref
        )
        observation = await afinalize_completion(
            sessions, server_id=task.server_id, proposal=proposal
        )
        assert observation.recorded is False
        assert observation.verified is False
        # The ratified validator flags EVERY offending ref, not just the
        # first — both results cited the same ghost ref.
        assert observation.fields == {
            "criterion_results[0].evidence_refs[0]": "unknown_evidence_ref",
            "criterion_results[1].evidence_refs[0]": "unknown_evidence_ref",
        }
        after = await _task_row(sessions, task.task_id)
        assert after == before  # zero effects: header untouched
        assert await _counts(sessions, task.task_id) == {
            "completions": 0,
            "revisions": 1,  # creation only
        }

    @pytest.mark.anyio
    async def test_wrong_target_evidence_ref_is_refused(self, sessions) -> None:
        task_a, _ = await _make_task(sessions, server=SERVER_A)
        task_b, _ = await _make_task(sessions, server=SERVER_A)
        evidence_b = await _registered_evidence_id(sessions, task_b.task_id)
        proposal = _proposal(
            task_a,
            [
                _passed("c1", [_message_ref(evidence_b)]),
                _passed("c2", [_message_ref(evidence_b)]),
            ],
            certainty=[_certainty(evidence_b)],
        )
        observation = await afinalize_completion(
            sessions, server_id=task_a.server_id, proposal=proposal
        )
        assert observation.recorded is False
        assert observation.fields == {
            "criterion_results[0].evidence_refs[0]": "unknown_evidence_ref",
            "criterion_results[1].evidence_refs[0]": "unknown_evidence_ref",
        }

    @pytest.mark.anyio
    async def test_stale_base_revision_is_a_structured_conflict(self, sessions) -> None:
        task, _ = await _make_task(sessions)
        evidence = await _registered_evidence_id(sessions, task.task_id)
        proposal = _proposal(
            task,
            [
                _passed("c1", [_message_ref(evidence)]),
                _passed("c2", [_message_ref(evidence)]),
            ],
            base_revision=task.revision + 7,  # stale CAS token
            certainty=[_certainty(evidence)],
        )
        observation = await afinalize_completion(
            sessions, server_id=task.server_id, proposal=proposal
        )
        assert observation.recorded is False
        assert observation.fields == {"base_revision": "revision_conflict"}
        assert (await _counts(sessions, task.task_id))["completions"] == 0

    @pytest.mark.anyio
    async def test_certainty_incompatible_passed_claim_has_zero_effects(
        self, sessions
    ) -> None:
        task, _ = await _make_task(sessions)
        evidence = await _registered_evidence_id(sessions, task.task_id)
        before = await _task_row(sessions, task.task_id)
        proposal = _proposal(
            task,
            [
                _passed("c1", [_message_ref(evidence)]),
                _passed("c2", [_message_ref(evidence)]),
            ],
            certainty=[
                # executed-but-unproven: never certifies success (E3)
                _certainty(evidence, domain="unknown", certainty="unknown")
            ],
        )
        observation = await afinalize_completion(
            sessions, server_id=task.server_id, proposal=proposal
        )
        assert observation.recorded is False
        # Both results cite the same unproven ref: both claims flagged.
        assert observation.fields == {
            "criterion_results[0]": "certainty_incompatible",
            "criterion_results[1]": "certainty_incompatible",
        }
        assert await _task_row(sessions, task.task_id) == before
        assert (await _counts(sessions, task.task_id))["completions"] == 0

    @pytest.mark.anyio
    async def test_retransmitted_proposal_deduplicates(self, sessions) -> None:
        task, _ = await _make_task(sessions)
        evidence = await _registered_evidence_id(sessions, task.task_id)
        proposal = _proposal(
            task,
            [
                _passed("c1", [_message_ref(evidence)]),
                _passed("c2", [_message_ref(evidence)]),
            ],
            certainty=[_certainty(evidence)],
        )
        first = await afinalize_completion(
            sessions, server_id=task.server_id, proposal=proposal
        )
        second = await afinalize_completion(
            sessions, server_id=task.server_id, proposal=proposal
        )
        assert first.recorded and second.recorded
        assert first.disposition == "created"
        assert second.disposition == "deduplicated"
        assert (await _counts(sessions, task.task_id))["completions"] == 1

    @pytest.mark.anyio
    async def test_out_of_scope_task_is_hidden(self, sessions) -> None:
        task, _ = await _make_task(sessions, server=SERVER_A)
        evidence = await _registered_evidence_id(sessions, task.task_id)
        proposal = _proposal(
            task,
            [
                _passed("c1", [_message_ref(evidence)]),
                _passed("c2", [_message_ref(evidence)]),
            ],
            certainty=[_certainty(evidence)],
        )
        observation = await afinalize_completion(
            sessions,
            server_id=SERVER_B_UUID,  # wrong server scope (C8)
            proposal=proposal,
        )
        assert observation.recorded is False
        assert observation.fields == {"server_id": "task_not_found"}

    @pytest.mark.anyio
    async def test_proposal_projection_from_the_real_act19_projection(
        self, sessions
    ) -> None:
        """The certainty records a producer copies come from the REAL
        act-19 ``project_tool_result`` blocks (cross-card contract)."""
        from mai.agents.tools import ToolResult, ToolStatus, project_tool_result

        task, _ = await _make_task(sessions)
        evidence = await _registered_evidence_id(sessions, task.task_id)
        result = ToolResult(
            schema_version=2,
            capability_id="app.read",
            server_id=task.server_id,
            run_id=uuid.uuid4(),
            status=ToolStatus.EXECUTED,
            allowed=True,
            reason="ok",
            effect="executed",
            payload_hash="0" * 64,
            policy_revision=1,
            effect_class="read",
            approval_id=None,
            approval_spent=False,
            approval_code=None,
            executor_ok=True,
            output_text='{"status": "ok", "evidence_refs": ['
            + f'{{"kind": "message", "evidence_id": "{evidence}"}}'
            + "]}",
            output_truncated=False,
            output_omitted_bytes=0,
            error_code=None,
            error_detail=None,
        )
        projection = project_tool_result(result)
        assert projection["domain_outcome"] == "success"
        assert projection["effect_certainty"] == "certain"
        certainty = [
            EvidenceCertainty(
                evidence_id=ref["evidence_id"],  # type: ignore[index]
                domain_outcome=str(projection["domain_outcome"]),
                effect_certainty=str(projection["effect_certainty"]),
            )
            for ref in projection["evidence_refs"]  # type: ignore[index]
        ]
        proposal = _proposal(
            task,
            [
                _passed("c1", [_message_ref(evidence)]),
                _passed("c2", [_message_ref(evidence)]),
            ],
            certainty=certainty,
        )
        observation = await afinalize_completion(
            sessions, server_id=task.server_id, proposal=proposal
        )
        assert observation.recorded is True
        assert observation.outcome is TaskOutcome.VERIFIED


async def _registered_evidence_ids(
    sessions, task_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """The two evidence refs registered for the fixture task (stable order)."""
    async with sessions() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT evidence_id FROM agent_task_evidence "
                    "WHERE task_id = :tid ORDER BY created_at, evidence_id "
                    "LIMIT 2"
                ),
                {"tid": task_id},
            )
        ).all()
    assert len(rows) == 2, "fixture task must carry two registered refs"
    return uuid.UUID(str(rows[0][0])), uuid.UUID(str(rows[1][0]))


async def _registered_evidence_id(sessions, task_id: uuid.UUID) -> uuid.UUID:
    """The first evidence ref registered for the fixture task."""
    return (await _registered_evidence_ids(sessions, task_id))[0]


# ---------------------------------------------------------------------------
# AC 2 — the final seam observes, never records (E1/P3)
# ---------------------------------------------------------------------------


class TestSeamObservation:
    @pytest.mark.anyio
    async def test_unbound_run_yields_no_document(self, sessions) -> None:
        document = await aobserve_task_outcome(
            sessions,
            server_id=SERVER_A_UUID,
            run_id=str(uuid.uuid4()),
        )
        assert document is None  # legacy shape untouched

    @pytest.mark.anyio
    async def test_final_text_alone_never_verifies_or_records(self, sessions) -> None:
        task, conversation_id = await _make_task(sessions)
        run_id = uuid.uuid4()
        await aattach_run(
            sessions,
            conversation_id=conversation_id,
            server_id=task.server_id,
            run_id=run_id,
            task_id=task.task_id,
        )
        document = await aobserve_task_outcome(
            sessions, server_id=task.server_id, run_id=str(run_id)
        )
        assert document is not None
        assert document["outcome"] == "in_progress"
        assert document["verified"] is False
        assert document["recorded"] is False
        assert document["proposal"] == "none"
        states = {
            entry["criterion_id"]: entry["state"]
            for entry in document["criterion_states"]  # type: ignore[index]
        }
        assert states == {"c1": "unavailable", "c2": "unavailable"}
        # Zero effects: the observation wrote nothing.
        row = await _task_row(sessions, task.task_id)
        assert row[0] == "in_progress"
        assert await _criterion_states(sessions, task.task_id) == {
            "c1": "not_checked",
            "c2": "not_checked",
        }
        assert await _counts(sessions, task.task_id) == {
            "completions": 0,
            "revisions": 1,
        }

    @pytest.mark.anyio
    async def test_observation_mirrors_recorded_truth_honestly(self, sessions) -> None:
        task, conversation_id = await _make_task(sessions)
        evidence = await _registered_evidence_id(sessions, task.task_id)
        await afinalize_completion(
            sessions,
            server_id=task.server_id,
            proposal=_proposal(
                task,
                [_passed("c1", [_message_ref(evidence)])],
                certainty=[_certainty(evidence)],
            ),
        )
        run_id = uuid.uuid4()
        await aattach_run(
            sessions,
            conversation_id=conversation_id,
            server_id=task.server_id,
            run_id=run_id,
            task_id=task.task_id,
        )
        document = await aobserve_task_outcome(
            sessions, server_id=task.server_id, run_id=str(run_id)
        )
        assert document is not None
        assert document["outcome"] == "partially_verified"
        assert document["verified"] is False
        states = {
            entry["criterion_id"]: entry["state"]
            for entry in document["criterion_states"]  # type: ignore[index]
        }
        assert states == {"c1": "passed", "c2": "unavailable"}

    @pytest.mark.anyio
    async def test_document_is_value_free_and_additive(self, sessions) -> None:
        task, conversation_id = await _make_task(sessions)
        run_id = uuid.uuid4()
        await aattach_run(
            sessions,
            conversation_id=conversation_id,
            server_id=task.server_id,
            run_id=run_id,
            task_id=task.task_id,
        )
        document = await aobserve_task_outcome(
            sessions, server_id=task.server_id, run_id=str(run_id)
        )
        assert set(document) == {  # type: ignore[arg-type]
            "task_id",
            "outcome",
            "verified",
            "recorded",
            "criteria",
            "criterion_states",
            "proposal",
        }
        assert "holds" not in str(document)  # no criterion statements

    @pytest.mark.anyio
    async def test_cross_scope_link_is_hidden(self, sessions) -> None:
        task, conversation_id = await _make_task(sessions, server=SERVER_A)
        run_id = uuid.uuid4()
        await aattach_run(
            sessions,
            conversation_id=conversation_id,
            server_id=task.server_id,
            run_id=run_id,
            task_id=task.task_id,
        )
        document = await aobserve_task_outcome(
            sessions, server_id=SERVER_B_UUID, run_id=str(run_id)
        )
        assert document is None  # C8: existence hidden, fail closed

    @pytest.mark.anyio
    async def test_proposal_rejection_preserves_resumability(self, sessions) -> None:
        """E2 then P3: a rejected proposal leaves the episode exactly as
        resumable as before, and a later valid proposal still closes it."""
        task, _ = await _make_task(sessions)
        evidence = await _registered_evidence_id(sessions, task.task_id)
        rejected = await afinalize_completion(
            sessions,
            server_id=task.server_id,
            proposal=_proposal(
                task,
                [
                    _passed("c1", [_message_ref(uuid.uuid4())]),
                    _passed("c2", [_message_ref(evidence)]),
                ],
                certainty=[_certainty(evidence)],
            ),
        )
        assert rejected.recorded is False
        accepted = await afinalize_completion(
            sessions,
            server_id=task.server_id,
            proposal=_proposal(
                task,
                [
                    _passed("c1", [_message_ref(evidence)]),
                    _passed("c2", [_message_ref(evidence)]),
                ],
                base_revision=task.revision,  # unchanged by the rejection
                certainty=[_certainty(evidence)],
            ),
        )
        assert accepted.recorded is True
        assert accepted.outcome is TaskOutcome.VERIFIED
