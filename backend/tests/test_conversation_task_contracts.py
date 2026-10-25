"""Focused tests for the conversation/task contract vocabulary (01-contracts).

Acceptance (BRIEF ``01-contracts``, P1/P4/C8/C4/C6):

1. ``TurnInterpretation`` discriminated union validates exactly the six legal
   variants (``answer|clarify|plan|continue_task|new_task|blocked``) and
   rejects unknown authority fields (``approved``, ``grants``,
   ``execute_now``, ``bypass_approval``), oversized patches (max 12 steps,
   ≤ 240 title, ≤ 1000 summary/rationale, ≤ 16 KiB serialized), duplicate
   step keys, dependency cycles, and executed steps without evidence refs.
2. ``conversations``/``tasks`` codify the ratified view/request/response
   models plus pagination envelopes (50/200, limit/offset/total/has_more).
3. Stable error codes: ``conversation_busy``, ``client_request_conflict``,
   ``conversation_not_found``, ``task_not_found``, ``revision_conflict``,
   ``invalid_task_patch``, ``phase_not_allowed``,
   ``completion_missing_evidence``, ``history_reconstruction_incomplete``.
4. Cross-server references fail closed without leaking the other scope (C8);
   retransmission deduplicates on identical content and conflicts on changed
   content (C4/C6).

The contract modules are pure — no DB, network or ambient environment.
Error strings are checked to stay value-free (C01 rule 3).
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from mai.contracts.conversations import (
    DEFAULT_PAGE_LIMIT,
    DISPOSITION_HTTP_STATUS,
    FORBIDDEN_AUTHORITY_FIELDS,
    MAX_MESSAGE_CHARS,
    MAX_PAGE_LIMIT,
    SCHEMA_VERSION,
    AnswerTurn,
    BlockingReason,
    ClarifyTurn,
    ConversationContractError,
    ConversationErrorCode,
    ConversationSteerRequest,
    ConversationSummary,
    ConversationTranscriptPage,
    ConversationTurnSubmitRequest,
    ConversationTurnSubmitResponse,
    ContinueTaskTurn,
    HistoryCompleteness,
    MessageRole,
    MessageSourceKind,
    NewTaskTurn,
    PlanTurn,
    SteerAction,
    TranscriptMessage,
    TurnDisposition,
    TurnKind,
    TurnValidationError,
    assert_conversation_scope,
    assert_history_available,
    assert_writer_admission,
    classify_retransmission,
    compute_has_more,
    content_fingerprint,
    parse_turn_interpretation,
    validate_turn_references,
)
from mai.contracts.tasks import (
    MAX_PATCH_SERIALIZED_BYTES,
    MAX_PATCH_STEPS,
    TITLE_MAX_CHARS,
    ActionRisk,
    ConstraintProposal,
    CriterionResult,
    EvidenceKind,
    EvidenceRef,
    Phase,
    PhaseAction,
    StepStatus,
    TaskCompletionVerdict,
    TaskContractError,
    TaskErrorCode,
    TaskOutcome,
    TaskPlanPatch,
    TaskPlanStep,
    TaskStepOperation,
    TaskView,
    UnderstandingSnapshot,
    assert_task_scope,
    check_revision,
    validate_completion_verdict,
    validate_constraint_proposals,
    validate_phase,
    validate_task_plan_patch,
)

SRC = "7e6c1a34-9a2b-4c3d-8e5f-1029384756af"
OTHER = "3f2e9a10-5b6c-4d7e-8f90-1a2b3c4d5e6f"
EVIDENCE_A = "11111111-2222-4333-8444-555555555555"
SERVER_A = uuid.UUID(SRC)
SERVER_B = uuid.UUID(OTHER)


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _answer_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "source_message_id": SRC,
        "turn_kind": "answer",
        "understanding": "user greeted the agent",
        "answer_text": "Hello! How can I help?",
    }
    payload.update(overrides)
    return payload


def _step(step_id: str, **overrides: object) -> dict[str, object]:
    step: dict[str, object] = {"step_id": step_id, "title": f"step {step_id}"}
    step.update(overrides)
    return step


def _patch_dict(
    operations: list[dict[str, object]] | None = None, **overrides: object
) -> dict[str, object]:
    if operations is None:
        operations = [
            {"op": "add", "step": _step("a")},
            {"op": "add", "step": _step("b", depends_on=["a"])},
        ]
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "source_message_id": SRC,
        "base_revision": 1,
        "idempotency_key": OTHER,
        "operations": operations,
    }
    payload.update(overrides)
    return payload


def _criteria() -> list[dict[str, object]]:
    return [
        {"criterion_id": "c1", "statement": "service responds on :8080"},
        {"criterion_id": "c2", "statement": "health check passes"},
    ]


def _plan_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "source_message_id": SRC,
        "turn_kind": "plan",
        "understanding": "deploy the service",
        "objective": "deploy the fixture service",
        "requested_phase": "plan",
        "acceptance_criteria": _criteria(),
        "todo_patch": _patch_dict(),
    }
    payload.update(overrides)
    return payload


def _rejected_turn(mutated: dict[str, object]) -> TurnValidationError:
    with pytest.raises(TurnValidationError) as excinfo:
        parse_turn_interpretation(mutated)
    return excinfo.value


def _task_error(call) -> TaskContractError:  # type: ignore[no-untyped-def]
    with pytest.raises(TaskContractError) as excinfo:
        call()
    return excinfo.value


def _executed_step(step_id: str = "a") -> TaskPlanStep:
    return TaskPlanStep(
        step_id=step_id,
        title=f"step {step_id}",
        status=StepStatus.EXECUTED,
        evidence_refs=[EvidenceRef(kind=EvidenceKind.OBSERVATION, evidence_id=EVIDENCE_A)],
    )


def _verdict(**overrides: object) -> TaskCompletionVerdict:
    payload: dict[str, object] = {
        "task_id": SRC,
        "base_revision": 1,
        "idempotency_key": OTHER,
        "task_outcome": "verified",
        "execution_status": "succeeded",
        "domain_outcome": "success",
        "effect_certainty": "certain",
        "criterion_results": [
            {
                "criterion_id": "c1",
                "result": "passed",
                "evidence_refs": [
                    {"kind": "observation", "evidence_id": EVIDENCE_A}
                ],
            },
            {
                "criterion_id": "c2",
                "result": "passed",
                "evidence_refs": [
                    {"kind": "observation", "evidence_id": EVIDENCE_A}
                ],
            },
        ],
    }
    payload.update(overrides)
    return TaskCompletionVerdict.model_validate(payload)


# ---------------------------------------------------------------------------
# pagination and disposition envelope (brief AC 2)
# ---------------------------------------------------------------------------


def test_pagination_bounds_are_the_ratified_50_200() -> None:
    assert DEFAULT_PAGE_LIMIT == 50
    assert MAX_PAGE_LIMIT == 200


def test_dispositions_map_onto_201_200_202() -> None:
    assert DISPOSITION_HTTP_STATUS[TurnDisposition.CREATED] == 201
    assert DISPOSITION_HTTP_STATUS[TurnDisposition.DEDUPLICATED] == 200
    assert DISPOSITION_HTTP_STATUS[TurnDisposition.QUEUED_FOLLOWUP] == 202


def test_submit_response_carries_disposition_http_status() -> None:
    response = ConversationTurnSubmitResponse(
        conversation_id=SRC,
        message_id=OTHER,
        task_id=None,
        run_id=None,
        revision=1,
        disposition="queued_followup",
    )
    assert response.http_status == 202


def test_transcript_page_envelope_is_consistent() -> None:
    def _message(sequence: int) -> TranscriptMessage:
        return TranscriptMessage(
            conversation_id=SRC,
            message_id=OTHER,
            sequence=sequence,
            role=MessageRole.USER,
            source_kind=MessageSourceKind.BROWSER,
            created_at="2026-10-24T10:00:00Z",
            content="hello",
        )

    page = ConversationTranscriptPage(
        conversation_id=SRC,
        limit=2,
        offset=0,
        total=3,
        has_more=True,
        items=[_message(1), _message(2)],
    )
    assert page.items[0].role == MessageRole.USER
    with pytest.raises(ValidationError, match="has_more"):
        ConversationTranscriptPage(
            conversation_id=SRC,
            limit=2,
            offset=0,
            total=3,
            has_more=False,
            items=[_message(1), _message(2)],
        )
    with pytest.raises(ValidationError, match="limit"):
        ConversationTranscriptPage(
            conversation_id=SRC,
            limit=2,
            offset=0,
            total=3,
            has_more=True,
            items=[_message(1), _message(2), _message(3)],
        )
    with pytest.raises(ValidationError, match="sequence-ordered"):
        ConversationTranscriptPage(
            conversation_id=SRC,
            limit=2,
            offset=0,
            total=3,
            has_more=True,
            items=[_message(2), _message(1)],
        )


def test_page_bounds_reject_out_of_range_limit_and_offset() -> None:
    with pytest.raises(ValidationError):
        ConversationTranscriptPage(
            conversation_id=SRC, limit=0, offset=0, total=0, has_more=False
        )
    with pytest.raises(ValidationError):
        ConversationTranscriptPage(
            conversation_id=SRC, limit=MAX_PAGE_LIMIT + 1, offset=0, total=0, has_more=False
        )
    with pytest.raises(ValidationError):
        ConversationTranscriptPage(
            conversation_id=SRC, limit=10, offset=-1, total=0, has_more=False
        )


def test_compute_has_more_truth_table() -> None:
    assert compute_has_more(0, 10, 25) is True
    assert compute_has_more(20, 5, 25) is False
    assert compute_has_more(30, 5, 25) is False


# ---------------------------------------------------------------------------
# TurnInterpretation union (brief AC 1; P1)
# ---------------------------------------------------------------------------


def test_p1_answer_turn_validates_without_todo() -> None:
    turn = parse_turn_interpretation(_answer_payload())
    assert isinstance(turn, AnswerTurn)
    assert turn.turn_kind == TurnKind.ANSWER
    assert not hasattr(turn, "todo_patch")
    assert turn.evidence_refs == []


def test_every_legal_variant_parses() -> None:
    clarify = parse_turn_interpretation(
        {
            "source_message_id": SRC,
            "turn_kind": "clarify",
            "understanding": "ambiguous target",
            "open_questions": [
                {"proposal_id": "q1", "question": "which server?"}
            ],
        }
    )
    assert isinstance(clarify, ClarifyTurn)

    continue_turn = parse_turn_interpretation(
        {
            "source_message_id": SRC,
            "turn_kind": "continue_task",
            "understanding": "continue after evidence",
            "todo_patch": _patch_dict(
                operations=[{"op": "add", "step": _step("a")}]
            ),
            "expected_task_revision": 3,
        }
    )
    assert isinstance(continue_turn, ContinueTaskTurn)

    new_task = parse_turn_interpretation(
        {
            "source_message_id": SRC,
            "turn_kind": "new_task",
            "understanding": "different goal",
            "objective": "rotate credentials",
            "rationale": "prior episode completed",
            "prior_task_id": OTHER,
            "requested_phase": "inspect",
            "acceptance_criteria": _criteria(),
            "todo_patch": _patch_dict(),
        }
    )
    assert isinstance(new_task, NewTaskTurn)

    blocked = parse_turn_interpretation(
        {
            "source_message_id": SRC,
            "turn_kind": "blocked",
            "understanding": "cannot proceed",
            "reason": "required prerequisite is missing",
            "blocked_kind": "missing_prerequisite",
        }
    )
    assert blocked.blocked_kind == BlockingReason.MISSING_PREREQUISITE


def test_unknown_turn_kind_is_rejected() -> None:
    exc = _rejected_turn({**_answer_payload(), "turn_kind": "execute_now"})
    # An invalid discriminator never matches any variant of the union.
    assert exc.fields == {"<root>": "union_tag_invalid"}


def test_missing_turn_kind_is_rejected() -> None:
    payload = _answer_payload()
    del payload["turn_kind"]
    exc = _rejected_turn(payload)
    assert exc.fields == {"<root>": "union_tag_not_found"}


def test_clarify_requires_open_questions() -> None:
    exc = _rejected_turn(
        {
            "source_message_id": SRC,
            "turn_kind": "clarify",
            "understanding": "ambiguous",
        }
    )
    # Cross-field rule: surfaces as a value error on the variant root.
    assert "value_error" in exc.fields.values()


def test_task_variants_require_todo_patch() -> None:
    plan = _plan_payload()
    del plan["todo_patch"]
    exc = _rejected_turn(plan)
    assert exc.fields.get("todo_patch") == "missing"

    continue_payload: dict[str, object] = {
        "source_message_id": SRC,
        "turn_kind": "continue_task",
        "understanding": "continue",
    }
    assert _rejected_turn(continue_payload).fields.get("todo_patch") == "missing"


def test_answer_and_blocked_variants_reject_patch_fields() -> None:
    for kind in ("answer", "blocked"):
        payload = _answer_payload()
        payload["turn_kind"] = kind
        payload["todo_patch"] = _patch_dict()
        if kind == "blocked":
            payload["reason"] = "blocked"
            payload["blocked_kind"] = "ambiguous_scope"
            del payload["answer_text"]
        exc = _rejected_turn(payload)
        assert exc.fields.get("todo_patch") == "extra_forbidden"


def test_schema_version_drift_is_rejected() -> None:
    exc = _rejected_turn(_answer_payload(schema_version=SCHEMA_VERSION + 1))
    assert exc.fields.get("schema_version") == "value_error"


def test_answer_bounds_are_enforced() -> None:
    exc = _rejected_turn(_answer_payload(answer_text="x" * 4001))
    assert exc.fields.get("answer_text") == "string_too_long"
    parse_turn_interpretation(_answer_payload(answer_text="x" * 4000))
    exc = _rejected_turn(_answer_payload(answer_text="   "))
    assert exc.fields.get("answer_text") == "value_error"


def test_open_questions_are_bounded_with_stable_ids() -> None:
    questions = [
        {"proposal_id": f"q{index}", "question": "which one?"} for index in range(9)
    ]
    exc = _rejected_turn(
        {
            "source_message_id": SRC,
            "turn_kind": "clarify",
            "understanding": "ambiguous",
            "open_questions": questions,
        }
    )
    assert exc.fields.get("open_questions") == "too_long"
    with pytest.raises(TurnValidationError) as excinfo:
        parse_turn_interpretation(
            {
                "source_message_id": SRC,
                "turn_kind": "clarify",
                "understanding": "ambiguous",
                "open_questions": [
                    {"proposal_id": "Bad-ID", "question": "which one?"}
                ],
            }
        )
    assert excinfo.value.fields.get("open_questions.0.proposal_id") == (
        "string_pattern_mismatch"
    )


# ---------------------------------------------------------------------------
# authority-field rejection (brief AC 1; §5 no writable authority)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", sorted(FORBIDDEN_AUTHORITY_FIELDS))
def test_unknown_authority_fields_are_rejected_top_level(field: str) -> None:
    exc = _rejected_turn(_answer_payload(**{field: True}))
    assert exc.fields.get(field) == "extra_forbidden"


def test_unknown_authority_fields_are_rejected_nested() -> None:
    payload = _plan_payload(
        todo_patch=_patch_dict(
            operations=[
                {
                    "op": "add",
                    "step": _step("a", approved=True),
                }
            ]
        )
    )
    exc = _rejected_turn(payload)
    assert exc.fields.get("todo_patch.operations.0.step.approved") == "extra_forbidden"


def test_grants_field_cannot_hide_inside_next_action() -> None:
    payload = _plan_payload(
        next_action={
            "kind": "execute_pending",
            "detail": "run the deploy",
            "grants": ["shell.exec"],
        }
    )
    exc = _rejected_turn(payload)
    assert exc.fields.get("next_action.grants") == "extra_forbidden"


# ---------------------------------------------------------------------------
# patch bounds and structure (brief AC 1; P4)
# ---------------------------------------------------------------------------


def test_patch_rejects_more_than_twelve_steps() -> None:
    operations = [
        {"op": "add", "step": _step(f"s{index:02d}")} for index in range(MAX_PATCH_STEPS + 1)
    ]
    with pytest.raises(ValidationError, match="operations"):
        TaskPlanPatch.model_validate(_patch_dict(operations=operations))
    legal = TaskPlanPatch.model_validate(
        _patch_dict(operations=operations[:MAX_PATCH_STEPS])
    )
    assert len(legal.operations) == MAX_PATCH_STEPS


def test_patch_title_and_summary_bounds() -> None:
    with pytest.raises(ValidationError, match="title"):
        TaskPlanPatch.model_validate(_patch_dict(title="x" * (TITLE_MAX_CHARS + 1)))
    TaskPlanPatch.model_validate(_patch_dict(title="x" * TITLE_MAX_CHARS))
    with pytest.raises(ValidationError, match="summary"):
        TaskPlanPatch.model_validate(_patch_dict(summary="x" * 1001))
    with pytest.raises(ValidationError, match="rationale"):
        TaskPlanPatch.model_validate(_patch_dict(rationale="x" * 1001))
    TaskPlanPatch.model_validate(
        _patch_dict(summary="x" * 1000, rationale="y" * 1000)
    )


def test_patch_serialized_budget_is_16kib() -> None:
    oversized = [
        {
            "op": "add",
            "step": _step(
                f"s{index:02d}",
                title="t" * TITLE_MAX_CHARS,
                expected_check="c" * 500,
                skip_reason="s" * 500,
            ),
        }
        for index in range(MAX_PATCH_STEPS)
    ]
    payload = _patch_dict(operations=oversized, rationale="r" * 1000)
    with pytest.raises(ValidationError, match="exceeds"):
        TaskPlanPatch.model_validate(payload)
    serialized = len(TaskPlanPatch.model_validate(_patch_dict()).model_dump_json())
    assert serialized <= MAX_PATCH_SERIALIZED_BYTES


def test_duplicate_step_keys_are_rejected() -> None:
    operations = [
        {"op": "add", "step": _step("a")},
        {"op": "add", "step": _step("a")},
    ]
    patch = TaskPlanPatch.model_validate(_patch_dict(operations=operations))
    exc = _task_error(lambda: validate_task_plan_patch(patch))
    assert exc.fields.get("operations[1].step.step_id") == "duplicate_step_id"


def test_add_collision_with_existing_step_is_rejected() -> None:
    patch = TaskPlanPatch.model_validate(
        _patch_dict(operations=[{"op": "add", "step": _step("existing")}])
    )
    exc = _task_error(lambda: validate_task_plan_patch(patch, ["existing"]))
    assert exc.fields.get("operations[0].step.step_id") == "duplicate_step_id"


def test_dependency_cycles_are_rejected() -> None:
    two_cycle = TaskPlanPatch.model_validate(
        _patch_dict(
            operations=[
                {"op": "add", "step": _step("a", depends_on=["b"])},
                {"op": "add", "step": _step("b", depends_on=["a"])},
            ]
        )
    )
    exc = _task_error(lambda: validate_task_plan_patch(two_cycle))
    assert "dependency_cycle" in exc.fields.values()

    self_cycle = TaskPlanPatch.model_validate(
        _patch_dict(operations=[{"op": "add", "step": _step("a", depends_on=["a"])}])
    )
    assert "dependency_cycle" in _task_error(
        lambda: validate_task_plan_patch(self_cycle)
    ).fields.values()


def test_unknown_dependencies_fail_closed_only_against_live_state() -> None:
    patch = TaskPlanPatch.model_validate(
        _patch_dict(
            operations=[
                {"op": "add", "step": _step("a", depends_on=["ghost"])}
            ]
        )
    )
    exc = _task_error(lambda: validate_task_plan_patch(patch, ["known_step"]))
    assert exc.fields.get("operations[0].step.depends_on[0]") == "unknown_dependency"
    # Without live state the shape is acceptable (existence is the store's duty).
    validate_task_plan_patch(patch)
    # Dependency satisfied by live state or by the patch itself is accepted.
    ok = TaskPlanPatch.model_validate(
        _patch_dict(
            operations=[
                {"op": "add", "step": _step("a", depends_on=["known_step", "b"])},
                {"op": "add", "step": _step("b")},
            ]
        )
    )
    validate_task_plan_patch(ok, ["known_step"])


def test_supersede_requires_existing_target() -> None:
    patch = TaskPlanPatch.model_validate(
        _patch_dict(
            operations=[
                {
                    "op": "supersede",
                    "step": _step("new"),
                    "supersedes_step_id": "old",
                }
            ]
        )
    )
    exc = _task_error(lambda: validate_task_plan_patch(patch, ["other"]))
    assert (
        exc.fields.get("operations[0].supersedes_step_id")
        == "unknown_supersede_target"
    )
    validate_task_plan_patch(patch, ["old"])


def test_executed_step_requires_evidence_refs() -> None:
    with pytest.raises(ValidationError, match="executed step requires"):
        TaskPlanStep(step_id="a", title="A", status=StepStatus.EXECUTED)
    step = _executed_step()
    assert step.status == StepStatus.EXECUTED

    with pytest.raises(ValidationError, match="skip_reason"):
        TaskPlanStep(step_id="a", title="A", status=StepStatus.SKIPPED)
    skipped = TaskPlanStep(
        step_id="a", title="A", status=StepStatus.SKIPPED, skip_reason="superseded"
    )
    assert skipped.skip_reason == "superseded"

    with pytest.raises(ValidationError, match="evidence"):
        TaskPlanStep(
            step_id="a",
            title="A",
            status=StepStatus.IN_PROGRESS,
            verification="passed",
        )


def test_step_operation_supersede_shape_is_enforced() -> None:
    with pytest.raises(ValidationError, match="supersedes_step_id"):
        TaskStepOperation(op="supersede", step=_executed_step("new"))
    with pytest.raises(ValidationError, match="only supersede"):
        TaskStepOperation(
            op="add", step=TaskPlanStep(step_id="a", title="A"), supersedes_step_id="b"
        )


def test_patch_risk_vocabulary_is_frozen() -> None:
    step = TaskPlanStep(step_id="a", title="A", risk=ActionRisk.IRREVERSIBLE)
    assert step.risk == ActionRisk.IRREVERSIBLE
    with pytest.raises(ValidationError):
        TaskPlanStep(step_id="a", title="A", risk="yolo")


# ---------------------------------------------------------------------------
# revision CAS, phase gating, scope guard (§1/§6; brief AC 3 codes)
# ---------------------------------------------------------------------------


def test_revision_conflict_is_structured() -> None:
    check_revision(4, 4)
    exc = _task_error(lambda: check_revision(4, 3))
    assert exc.fields == {"base_revision": "revision_conflict"}


def test_phase_gating_frozen_map() -> None:
    validate_phase(Phase.ACT, PhaseAction.EXECUTE_STEPS)
    validate_phase(Phase.INSPECT, PhaseAction.COLLECT_EVIDENCE)
    validate_phase(Phase.PLAN, PhaseAction.METADATA_UPDATE)
    validate_phase(Phase.ACT, PhaseAction.PLAN_PROPOSAL)

    exc = _task_error(lambda: validate_phase(Phase.PLAN, PhaseAction.EXECUTE_STEPS))
    assert exc.fields == {"requested_phase": "phase_not_allowed"}
    exc = _task_error(lambda: validate_phase(Phase.INSPECT, PhaseAction.EXECUTE_STEPS))
    assert exc.fields == {"requested_phase": "phase_not_allowed"}
    exc = _task_error(lambda: validate_phase(Phase.PLAN, PhaseAction.COLLECT_EVIDENCE))
    assert exc.fields == {"requested_phase": "phase_not_allowed"}


def test_c8_cross_server_task_reference_fails_closed_without_leak() -> None:
    exc = _task_error(lambda: assert_task_scope(SERVER_A, SERVER_B))
    assert exc.fields == {"server_id": "task_not_found"}
    assert str(SERVER_B) not in str(exc)
    assert str(SERVER_A) not in str(exc)
    # Same scope passes.
    assert_task_scope(SERVER_A, SERVER_A)


# ---------------------------------------------------------------------------
# constraint authority (§6; P4 "erase a user prohibition")
# ---------------------------------------------------------------------------


def _constraint(suggested: str, target: str | None = None, origin: str = "model") -> dict[str, object]:
    proposal: dict[str, object] = {
        "constraint_id": "c_new",
        "category": "safety",
        "statement": "keep the fixture untouched",
        "origin": origin,
        "suggested": suggested,
    }
    if target is not None:
        proposal["supersedes_constraint_id"] = target
    return proposal


def test_model_cannot_supersede_user_constraint() -> None:
    proposals = [
        ConstraintProposal.model_validate(_constraint("supersede", "keep_live"))
    ]
    exc = _task_error(
        lambda: validate_constraint_proposals(
            proposals, {"keep_live": "user", "model_c": "model"}
        )
    )
    assert exc.fields.get("constraints_proposed[0]") == "constraint_escalation"
    # Model-origin supersede of a model-origin constraint is fine.
    validate_constraint_proposals(
        proposals, {"keep_live": "model"}
    )
    # Unknown supersede target fails closed.
    exc = _task_error(
        lambda: validate_constraint_proposals(proposals, {"unrelated": "model"})
    )
    assert exc.fields.get("constraints_proposed[0]") == "unknown_constraint_target"
    # A USER-origin supersede proposal is the explicit user-correction path.
    user_proposal = ConstraintProposal.model_validate(
        _constraint("supersede", "keep_live", origin="user")
    )
    validate_constraint_proposals([user_proposal], {"keep_live": "user"})


def test_constraint_proposal_shape_is_enforced() -> None:
    with pytest.raises(ValidationError, match="supersedes_constraint_id"):
        ConstraintProposal.model_validate(_constraint("supersede"))
    with pytest.raises(ValidationError, match="must not carry"):
        ConstraintProposal.model_validate(_constraint("add", "keep_live"))


# ---------------------------------------------------------------------------
# completion verdict (§9; P4/E2: no self-granted success)
# ---------------------------------------------------------------------------


def test_verified_requires_full_evidence_bound_coverage() -> None:
    verdict = _verdict()
    validate_completion_verdict(verdict, ["c1", "c2"], [EVIDENCE_A])

    uncovered = _verdict(
        criterion_results=[
            {
                "criterion_id": "c1",
                "result": "passed",
                "evidence_refs": [{"kind": "observation", "evidence_id": EVIDENCE_A}],
            }
        ]
    )
    exc = _task_error(lambda: validate_completion_verdict(uncovered, ["c1", "c2"]))
    assert exc.fields.get("criterion_results") == "missing_criterion"

    failed = _verdict(
        criterion_results=[
            {
                "criterion_id": "c1",
                "result": "passed",
                "evidence_refs": [{"kind": "observation", "evidence_id": EVIDENCE_A}],
            },
            {
                "criterion_id": "c2",
                "result": "failed",
                "evidence_refs": [{"kind": "observation", "evidence_id": EVIDENCE_A}],
            },
        ]
    )
    exc = _task_error(lambda: validate_completion_verdict(failed, ["c1", "c2"]))
    assert exc.fields.get("criterion_results") == "criterion_not_passed"


def test_passed_result_requires_evidence() -> None:
    verdict = _verdict(
        criterion_results=[
            {"criterion_id": "c1", "result": "passed", "evidence_refs": []},
            {
                "criterion_id": "c2",
                "result": "passed",
                "evidence_refs": [{"kind": "observation", "evidence_id": EVIDENCE_A}],
            },
        ]
    )
    exc = _task_error(lambda: validate_completion_verdict(verdict, ["c1", "c2"]))
    assert (
        exc.fields.get("criterion_results[0].evidence_refs") == "missing_evidence_refs"
    )


def test_fabricated_criterion_or_evidence_is_rejected() -> None:
    verdict = _verdict(
        criterion_results=[
            {
                "criterion_id": "ghost",
                "result": "passed",
                "evidence_refs": [{"kind": "observation", "evidence_id": EVIDENCE_A}],
            },
            {
                "criterion_id": "c2",
                "result": "passed",
                "evidence_refs": [{"kind": "observation", "evidence_id": EVIDENCE_A}],
            },
        ]
    )
    exc = _task_error(lambda: validate_completion_verdict(verdict, ["c1", "c2"]))
    assert exc.fields.get("criterion_results[0].criterion_id") == "unknown_criterion"

    fabricated = _verdict(
        criterion_results=[
            {
                "criterion_id": "c1",
                "result": "passed",
                "evidence_refs": [{"kind": "observation", "evidence_id": OTHER}],
            },
            {
                "criterion_id": "c2",
                "result": "passed",
                "evidence_refs": [{"kind": "observation", "evidence_id": EVIDENCE_A}],
            },
        ]
    )
    exc = _task_error(
        lambda: validate_completion_verdict(fabricated, ["c1", "c2"], [EVIDENCE_A])
    )
    assert (
        exc.fields.get("criterion_results[0].evidence_refs[0]")
        == "unknown_evidence_ref"
    )


def test_partially_verified_requires_one_passed() -> None:
    verdict = _verdict(
        task_outcome=TaskOutcome.PARTIALLY_VERIFIED,
        criterion_results=[
            {
                "criterion_id": "c1",
                "result": "failed",
                "evidence_refs": [{"kind": "observation", "evidence_id": EVIDENCE_A}],
            }
        ],
    )
    exc = _task_error(lambda: validate_completion_verdict(verdict, ["c1"]))
    assert exc.fields.get("criterion_results") == "partial_requires_passed"


def test_verdict_results_are_decided_and_result_data_is_safe() -> None:
    with pytest.raises(ValidationError, match="not_checked"):
        CriterionResult(criterion_id="c1", result="not_checked")
    with pytest.raises(ValidationError, match="credential-shaped"):
        _verdict(result_data={"api_key": "value"})
    with pytest.raises(ValidationError, match="at most 8"):
        _verdict(result_data={f"k{index}": "v" for index in range(9)})
    with pytest.raises(ValidationError, match="at most 200"):
        _verdict(result_data={"k": "v" * 201})
    ok = _verdict(result_data={"deployed_revision": "r1293"})
    assert ok.result_data == {"deployed_revision": "r1293"}


def test_duplicate_criterion_results_are_rejected() -> None:
    result = {
        "criterion_id": "c1",
        "result": "passed",
        "evidence_refs": [{"kind": "observation", "evidence_id": EVIDENCE_A}],
    }
    verdict = _verdict(criterion_results=[result, dict(result)])
    exc = _task_error(lambda: validate_completion_verdict(verdict, ["c1"]))
    assert exc.fields.get("criterion_results[1].criterion_id") == "invalid_task_patch"


# ---------------------------------------------------------------------------
# turn reference validation (§6; P4 hallucinated IDs)
# ---------------------------------------------------------------------------


def test_hallucinated_evidence_in_answer_is_rejected() -> None:
    turn = parse_turn_interpretation(
        _answer_payload(
            evidence_refs=[{"kind": "message", "evidence_id": EVIDENCE_A}]
        )
    )
    exc = _task_error(
        lambda: validate_turn_references(turn, known_evidence_ids=[OTHER])
    )
    assert exc.fields.get("evidence_refs[0]") == "unknown_evidence_ref"
    validate_turn_references(turn, known_evidence_ids=[EVIDENCE_A])


def test_hallucinated_patch_references_are_path_prefixed() -> None:
    patch = _patch_dict(
        operations=[
            {"op": "add", "step": _step("a")},
            {"op": "add", "step": _step("a")},
        ]
    )
    turn = parse_turn_interpretation(_plan_payload(todo_patch=patch))
    exc = _task_error(lambda: validate_turn_references(turn, known_step_ids=[]))
    assert (
        exc.fields.get("todo_patch.operations[1].step.step_id")
        == "duplicate_step_id"
    )


def test_user_prohibition_escalation_surfaces_through_turn_validation() -> None:
    turn = parse_turn_interpretation(
        _plan_payload(
            constraints_proposed=[_constraint("supersede", "keep_live")],
        )
    )
    exc = _task_error(
        lambda: validate_turn_references(
            turn,
            known_step_ids=[],
            known_constraint_origins={"keep_live": "user"},
        )
    )
    assert exc.fields.get("constraints_proposed[0]") == "constraint_escalation"


# ---------------------------------------------------------------------------
# submission, control and history gates (§1/§3/§4; C4/C6/C7)
# ---------------------------------------------------------------------------


def test_retransmission_deduplicates_and_conflicts() -> None:
    fingerprint = content_fingerprint("restart the worker")
    assert (
        classify_retransmission(fingerprint, content_fingerprint("restart the worker"))
        == TurnDisposition.DEDUPLICATED
    )
    with pytest.raises(ConversationContractError) as excinfo:
        classify_retransmission(fingerprint, content_fingerprint("CANCEL EVERYTHING"))
    assert excinfo.value.fields == {
        "client_request_id": "client_request_conflict"
    }


def test_submit_request_bounds() -> None:
    request = ConversationTurnSubmitRequest(
        server_id=SERVER_A,
        client_request_id=OTHER,
        message="please deploy",
        task_action="continue",
        expected_revision=2,
    )
    assert request.task_action == "continue"
    with pytest.raises(ValidationError):
        ConversationTurnSubmitRequest(
            server_id=SERVER_A, client_request_id=OTHER, message="   "
        )
    with pytest.raises(ValidationError):
        ConversationTurnSubmitRequest(
            server_id=SERVER_A,
            client_request_id=OTHER,
            message="x" * (MAX_MESSAGE_CHARS + 1),
        )
    with pytest.raises(ValidationError):
        ConversationTurnSubmitRequest(
            server_id=SERVER_A,
            client_request_id=OTHER,
            message="go",
            task_action="rewind",
        )


def test_pause_cancel_are_deterministic_and_carry_no_planner_input() -> None:
    steer = ConversationSteerRequest(
        conversation_id=SRC,
        server_id=SERVER_A,
        action=SteerAction.STEER,
        message="skip step b",
    )
    assert steer.action == SteerAction.STEER
    with pytest.raises(ValidationError, match="steer action requires"):
        ConversationSteerRequest(
            conversation_id=SRC, server_id=SERVER_A, action=SteerAction.STEER
        )
    with pytest.raises(ValidationError, match="carry no message"):
        ConversationSteerRequest(
            conversation_id=SRC,
            server_id=SERVER_A,
            action=SteerAction.PAUSE,
            message="think harder",
        )
    pause = ConversationSteerRequest(
        conversation_id=SRC, server_id=SERVER_A, action=SteerAction.PAUSE
    )
    assert pause.message is None


def test_writer_lease_admission() -> None:
    with pytest.raises(ConversationContractError) as excinfo:
        assert_writer_admission(uuid.UUID(OTHER), follow_up=False)
    assert excinfo.value.fields == {"conversation_id": "conversation_busy"}
    # Follow-ups queue against the active writer; a free lease admits anything.
    assert_writer_admission(uuid.UUID(OTHER), follow_up=True)
    assert_writer_admission(None, follow_up=False)


def test_history_gate_requires_confirmation() -> None:
    assert_history_available(HistoryCompleteness.COMPLETE, user_confirmed=False)
    with pytest.raises(ConversationContractError) as excinfo:
        assert_history_available(HistoryCompleteness.PARTIAL, user_confirmed=False)
    assert excinfo.value.fields == {
        "history_completeness": "history_reconstruction_incomplete"
    }
    assert_history_available(HistoryCompleteness.PARTIAL, user_confirmed=True)
    with pytest.raises(ConversationContractError):
        assert_history_available(HistoryCompleteness.UNAVAILABLE, user_confirmed=False)
    assert_history_available(HistoryCompleteness.UNAVAILABLE, user_confirmed=True)


def test_c8_cross_server_conversation_reference_fails_closed_without_leak() -> None:
    exc = _conversation_error(lambda: assert_conversation_scope(SERVER_A, SERVER_B))
    assert exc.fields == {"server_id": "conversation_not_found"}
    assert str(SERVER_B) not in str(exc)


def _conversation_error(call) -> ConversationContractError:  # type: ignore[no-untyped-def]
    with pytest.raises(ConversationContractError) as excinfo:
        call()
    return excinfo.value


# ---------------------------------------------------------------------------
# bounded views (brief AC 2; §2/§8 no unbounded arrays)
# ---------------------------------------------------------------------------


def test_understanding_snapshot_and_task_view_are_bounded() -> None:
    snapshot = UnderstandingSnapshot.model_validate(
        {
            "task_id": SRC,
            "conversation_id": OTHER,
            "server_id": SRC,
            "revision": 2,
            "objective": "deploy the fixture service",
            "requested_phase": "act",
            "status": "in_progress",
            "active_step_id": "a",
            "critical_constraints": ["never touch production data"],
            "constraint_refs": ["keep_live"],
            "open_questions": [{"proposal_id": "q1", "question": "which port?"}],
            "next_actions": [
                {
                    "step_id": "a",
                    "title": "deploy",
                    "continuation": "resume at compose up",
                }
            ],
            "counts": {
                "steps_total": 2,
                "steps_executed": 1,
                "constraints_total": 1,
                "evidence_total": 3,
                "criteria_total": 2,
                "criteria_verified": 1,
            },
        }
    )
    assert snapshot.next_actions[0].step_id == "a"

    view = TaskView.model_validate(
        {
            "task_id": SRC,
            "conversation_id": OTHER,
            "server_id": SRC,
            "revision": 2,
            "title": "deploy the fixture service",
            "requested_phase": "act",
            "status": "in_progress",
            "counts": {
                "steps_total": 2,
                "steps_executed": 1,
                "constraints_total": 1,
                "evidence_total": 3,
                "criteria_total": 2,
                "criteria_verified": 1,
            },
            "created_at": "2026-10-24T10:00:00Z",
            "updated_at": "2026-10-24T10:05:00Z",
        }
    )
    assert view.status == TaskOutcome.IN_PROGRESS


def test_conversation_summary_has_no_transcript_content() -> None:
    summary = ConversationSummary.model_validate(
        {
            "conversation_id": SRC,
            "server_id": SERVER_A,
            "created_at": "2026-10-24T10:00:00Z",
            "updated_at": "2026-10-24T10:05:00Z",
            "message_count": 4,
            "active_task_id": OTHER,
        }
    )
    assert not hasattr(summary, "messages")
    assert summary.message_count == 4


# ---------------------------------------------------------------------------
# value-free error discipline (C01 rule 3)
# ---------------------------------------------------------------------------


def test_error_payloads_never_carry_submitted_values() -> None:
    canary = "TOP-SECRET-CANARY-VALUE"
    turn_exc = _rejected_turn(
        _answer_payload(answer_text=f"{canary} {'x' * 4200}")  # noqa: S106 - test canary
    )
    assert canary not in str(turn_exc)

    patch = TaskPlanPatch.model_validate(
        _patch_dict(
            operations=[
                {"op": "add", "step": _step("a", depends_on=["b"])},
                {"op": "add", "step": _step("b", depends_on=["a"])},
            ]
        )
    )
    task_exc = _task_error(lambda: validate_task_plan_patch(patch))
    assert "TOP-SECRET" not in str(task_exc)

    with pytest.raises(ConversationContractError) as excinfo:
        classify_retransmission("fp-a", "fp-b")
    assert "fp-a" not in str(excinfo.value)


def test_stable_error_codes_exist() -> None:
    # The nine ratified codes from the brief.
    assert ConversationErrorCode.CONVERSATION_BUSY == "conversation_busy"
    assert ConversationErrorCode.CLIENT_REQUEST_CONFLICT == "client_request_conflict"
    assert ConversationErrorCode.CONVERSATION_NOT_FOUND == "conversation_not_found"
    assert (
        ConversationErrorCode.HISTORY_RECONSTRUCTION_INCOMPLETE
        == "history_reconstruction_incomplete"
    )
    assert TaskErrorCode.TASK_NOT_FOUND == "task_not_found"
    assert TaskErrorCode.REVISION_CONFLICT == "revision_conflict"
    assert TaskErrorCode.INVALID_TASK_PATCH == "invalid_task_patch"
    assert TaskErrorCode.PHASE_NOT_ALLOWED == "phase_not_allowed"
    assert TaskErrorCode.COMPLETION_MISSING_EVIDENCE == "completion_missing_evidence"
