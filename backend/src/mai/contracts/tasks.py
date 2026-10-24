"""Frozen task and intake-patch vocabulary: plans, revisions, completion.

Campaign contract producer (``01-contracts``; act_CONTRACTS §5–§9, ratified):
this module is the single owner of the task-domain contract vocabulary that
persistence (04/11), runtime workers (02/03/06/07/13/20), tools (§7) and the
task API/UI (§8) must consume — no sibling redesigns (act_CONTRACTS §11).

Governing rules:

- **Identity (act_CONTRACTS §1)** — ``task_id`` is an opaque stable UUID for a
  goal episode, stable across runs; ``revision`` is the optimistic
  concurrency token for task and plan changes; V1 has one active task per
  conversation. Arbitrary goal switching is proposed, never silently applied.
- **Structured intake (§5)** — task-shaped variants of ``TurnInterpretation``
  (defined in :mod:`mai.contracts.conversations`, which imports from here)
  carry a bounded ``todo_patch`` delta, never a whole-list replacement, and
  the output schema contains NO writable ``approved``/grants/credential
  fields — those are injected read-only facts from trusted services.
- **Authority (§6)** — application ``phase`` (``plan|inspect|act``) is
  advisory intake metadata, never permission: plan grants no target tools,
  inspect exposes only read tools, act stays subject to existing grants and
  the C03 approval checks. Metadata updates are legal in every phase.
- **Task/todo tools (§7)** — stable item IDs, statuses
  ``pending|in_progress|blocked|executed|skipped`` and a SEPARATE verification
  field ``not_checked|passed|failed|unavailable`` bound to evidence. UI may
  render "done" only from agreed checks, never from a model-ticked boolean.
- **Completion (§9)** — a verdict can never self-grant verified success:
  ``verified`` requires every acceptance criterion mapped to a passed result
  with evidence references; otherwise the stable code is
  ``completion_missing_evidence``.

Stable error codes (brief ``01-contracts``): ``task_not_found``,
``revision_conflict``, ``invalid_task_patch``, ``phase_not_allowed``,
``completion_missing_evidence`` — machine-readable, value-free, safe to
return in the C01 error envelope.

The module is pure: no process environment, filesystem, network or clock
access — every result derives from its inputs, so validation is
SSR/worker/replay safe and never leaks ambient secret material.

Frozen bounds (ratified limits; additions require campaign reconciliation):
max 12 steps per patch, ≤ 240 chars title, ≤ 1000 chars summary/rationale,
≤ 16 KiB total serialized patch.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final
from typing import Literal as PyLiteral

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Payload schema version produced by this freeze. Consumers fail closed on
#: any other value (server-contract pattern): a newer producer must bump and
#: reconcile, not silently drift.
SCHEMA_VERSION: Final[int] = 1

# --- frozen bounds -----------------------------------------------------------

#: Maximum steps in one bounded patch (act_CONTRACTS §5 "bounded todo_patch";
#: brief: "max 12 steps"). Deltas only — never whole-list replacement.
MAX_PATCH_STEPS: Final[int] = 12

#: Title bound for task headers and plan steps (brief: "≤ 240 chars title").
TITLE_MAX_CHARS: Final[int] = 240

#: Summary/rationale/objective bound (brief: "≤ 1000 chars summary/rationale").
SUMMARY_MAX_CHARS: Final[int] = 1000

#: Total serialized patch budget (brief: "≤ 16 KiB total serialized patch").
MAX_PATCH_SERIALIZED_BYTES: Final[int] = 16 * 1024

#: Maximum acceptance criteria per task (aligned with MAX_PATCH_STEPS).
MAX_CRITERIA: Final[int] = 12

#: Maximum constraint proposals in one intake turn.
MAX_CONSTRAINT_PROPOSALS: Final[int] = 12

#: Maximum evidence references carried by one step / verdict / turn section.
MAX_EVIDENCE_REFS: Final[int] = 16

#: Maximum dependency references per step.
MAX_DEPENDENCIES: Final[int] = 8

#: Stable item/constraint/criterion/question IDs: lowercase slugs, stable
#: across revisions so a delta can reference earlier items (act_CONTRACTS §7
#: "Stable item IDs, dependencies, no cycles").
STABLE_ID_PATTERN: Final[str] = r"^[a-z][a-z0-9_]{0,63}$"

#: Maximum entries in a verdict's bounded ``result_data`` map, and the value
#: bound per entry (act_CONTRACTS §9 "safe bounded result data").
RESULT_DATA_MAX_ENTRIES: Final[int] = 8
RESULT_DATA_VALUE_MAX_CHARS: Final[int] = 200

#: Credential-shaped key names that may never appear as ``result_data`` keys.
#: Exact key-name matching (case-insensitive), the same discipline as the
#: jobs envelope guard — defense-in-depth only, never proof.
SECRET_DATA_KEYS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "private_key",
        "passphrase",
        "api_key",
        "access_key",
        "credential",
    }
)


# --- frozen vocabularies -----------------------------------------------------


class Phase(StrEnum):
    """Application task phases (act_CONTRACTS §5/§6): advisory, never permission."""

    PLAN = "plan"
    INSPECT = "inspect"
    ACT = "act"


class StepStatus(StrEnum):
    """Frozen todo statuses (act_CONTRACTS §7; additions need reconciliation)."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    EXECUTED = "executed"
    SKIPPED = "skipped"


class VerificationStatus(StrEnum):
    """Verification is separate from execution and bound to evidence (§7)."""

    NOT_CHECKED = "not_checked"
    PASSED = "passed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class TaskOutcome(StrEnum):
    """Application task outcome (act_CONTRACTS §9); technical RunState frozen
    at C05 rule 2 is untouched — this is the domain layer above it."""

    IN_PROGRESS = "in_progress"
    NEEDS_INPUT = "needs_input"
    PARTIALLY_VERIFIED = "partially_verified"
    VERIFIED = "verified"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ActionRisk(StrEnum):
    """Declared risk/action class of a step (§5 "risk/action class")."""

    READ_ONLY = "read_only"
    MUTATING = "mutating"
    IRREVERSIBLE = "irreversible"


class ConstraintOrigin(StrEnum):
    """Source anchor distinguishing user requirements from model inferences
    (act_CONTRACTS §5 ``constraints_proposed``)."""

    USER = "user"
    MODEL = "model"


class PatchOp(StrEnum):
    """Bounded patch operation kinds (§5 "add/update/supersede operations")."""

    ADD = "add"
    UPDATE = "update"
    SUPERSEDE = "supersede"


class EvidenceKind(StrEnum):
    """What kind of sourced record an evidence reference points at (§5 facts;
    §9 evidence validator checks provenance, task/target and observation type)."""

    MESSAGE = "message"
    OBSERVATION = "observation"
    RUN_EVENT = "run_event"
    ARTIFACT = "artifact"
    DOCUMENT = "document"


class ExecutionStatus(StrEnum):
    """Compact technical execution status of the closing attempt (§9 receipt;
    values subset the frozen C04 ``CommandOutcome`` vocabulary)."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    CANCELLED = "cancelled"


class DomainOutcome(StrEnum):
    """Domain-level outcome; ``unknown`` is honest for arbitrary shell work
    (§9: no regex oracle claims semantic success)."""

    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"


class EffectCertainty(StrEnum):
    """How certain the effect evidence is (§9 ``effect_certainty``)."""

    CERTAIN = "certain"
    UNCERTAIN = "uncertain"
    UNKNOWN = "unknown"


class PhaseAction(StrEnum):
    """The action a caller wants to take, checked against the task phase (§6)."""

    METADATA_UPDATE = "metadata_update"
    PLAN_PROPOSAL = "plan_proposal"
    COLLECT_EVIDENCE = "collect_evidence"
    EXECUTE_STEPS = "execute_steps"


class TaskErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary).

    The five ratified task codes plus the finer field-level detail codes used
    inside the ``fields`` mapping of :class:`TaskContractError`.
    """

    TASK_NOT_FOUND = "task_not_found"
    REVISION_CONFLICT = "revision_conflict"
    INVALID_TASK_PATCH = "invalid_task_patch"
    PHASE_NOT_ALLOWED = "phase_not_allowed"
    COMPLETION_MISSING_EVIDENCE = "completion_missing_evidence"

    # Field-level detail codes (carried in `TaskFieldError.code`).
    DUPLICATE_STEP_ID = "duplicate_step_id"
    UNKNOWN_DEPENDENCY = "unknown_dependency"
    UNKNOWN_SUPERSEDE_TARGET = "unknown_supersede_target"
    DEPENDENCY_CYCLE = "dependency_cycle"
    CONSTRAINT_ESCALATION = "constraint_escalation"
    UNKNOWN_CONSTRAINT_TARGET = "unknown_constraint_target"
    UNKNOWN_CRITERION = "unknown_criterion"
    UNKNOWN_EVIDENCE_REF = "unknown_evidence_ref"
    MISSING_EVIDENCE_REFS = "missing_evidence_refs"
    MISSING_CRITERION = "missing_criterion"
    CRITERION_NOT_PASSED = "criterion_not_passed"
    PARTIAL_REQUIRES_PASSED = "partial_requires_passed"
    SECRET_LIKE_RESULT_KEY = "secret_like_result_key"


#: Top-level envelope codes (the ratified five); every other TaskErrorCode
#: member is a field-level detail code carried inside the ``fields`` mapping.
TOP_LEVEL_CODES: Final[frozenset[TaskErrorCode]] = frozenset(
    {
        TaskErrorCode.TASK_NOT_FOUND,
        TaskErrorCode.REVISION_CONFLICT,
        TaskErrorCode.INVALID_TASK_PATCH,
        TaskErrorCode.PHASE_NOT_ALLOWED,
        TaskErrorCode.COMPLETION_MISSING_EVIDENCE,
    }
)


#: Phase × action admission map (§6: metadata updates in all phases; plan
#: grants no target tools; inspect only reads; act executes under grants).
PHASE_ALLOWED_ACTIONS: Final[Mapping[PhaseAction, frozenset[Phase]]] = MappingProxyType(
    {
        PhaseAction.METADATA_UPDATE: frozenset(Phase),
        PhaseAction.PLAN_PROPOSAL: frozenset(Phase),
        PhaseAction.COLLECT_EVIDENCE: frozenset({Phase.INSPECT, Phase.ACT}),
        PhaseAction.EXECUTE_STEPS: frozenset({Phase.ACT}),
    }
)


# --- rejection plumbing (server-contract pattern: value-free errors) ---------


@dataclass(frozen=True, slots=True)
class TaskFieldError:
    """One rejection: field path + stable code + value-free reason."""

    code: str
    field: str
    detail: str


class TaskContractError(ValueError):
    """All rejections for one patch / verdict / scope / phase check.

    ``message``/``str`` contain only field paths, codes and fixed reasons —
    never field values — so the exception is safe to log, store and return in
    a browser-facing error body (C01 rule 3).
    """

    def __init__(self, errors: Collection[TaskFieldError]) -> None:
        self.errors: tuple[TaskFieldError, ...] = tuple(errors)
        summary = ", ".join(f"{e.field}: {e.code}" for e in self.errors)
        super().__init__(f"task contract rejected ({summary})")

    @property
    def fields(self) -> dict[str, str]:
        """Field path → stable code, ready for the C01 envelope ``fields``."""
        return {e.field: e.code for e in self.errors}


# --- shared reference models --------------------------------------------------


class EvidenceRef(BaseModel):
    """One sourced evidence reference (§5 facts; §9 evidence binding).

    ``evidence_id`` points at an existing sourced record in the task/conversation
    scope; existence is checked by the runtime store, never assumed here.
    """

    model_config = ConfigDict(extra="forbid")

    kind: EvidenceKind = Field(description="Kind of sourced record referenced")
    evidence_id: uuid.UUID = Field(description="Opaque UUID of the referenced record")
    note: str | None = Field(
        default=None,
        max_length=200,
        description="Short safe annotation for UI display",
    )


class OpenQuestion(BaseModel):
    """One bounded user-facing question with a stable proposal ID (§5)."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(
        pattern=STABLE_ID_PATTERN,
        description="Stable slug so answers can address this proposal",
    )
    question: str = Field(
        min_length=1,
        max_length=500,
        description="Short question text safe for browser display",
    )

    @field_validator("question")
    @classmethod
    def _question_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must not be blank")
        return value


class AcceptanceCriterion(BaseModel):
    """One measurable acceptance criterion with a stable ID (§5 task variants)."""

    model_config = ConfigDict(extra="forbid")

    criterion_id: str = Field(
        pattern=STABLE_ID_PATTERN,
        description="Stable slug referenced by completion verdicts",
    )
    statement: str = Field(
        min_length=1,
        max_length=500,
        description="Measurable statement of what counts as done",
    )
    verification: VerificationStatus = Field(
        default=VerificationStatus.NOT_CHECKED,
        description="Current verification state bound to evidence",
    )

    @field_validator("statement")
    @classmethod
    def _statement_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("statement must not be blank")
        return value


class ConstraintProposal(BaseModel):
    """One proposed constraint with source anchors (§5 ``constraints_proposed``).

    ``origin`` distinguishes user requirements from model inferences: a
    model-generated update can never remove/supersede a USER-origin constraint
    (§6) — that needs an explicit user correction through the revision API.
    """

    model_config = ConfigDict(extra="forbid")

    constraint_id: str = Field(
        pattern=STABLE_ID_PATTERN,
        description="Stable slug of the proposed constraint",
    )
    category: str = Field(
        min_length=1,
        max_length=64,
        description="Constraint category slug, e.g. safety or scope",
    )
    statement: str = Field(
        min_length=1,
        max_length=500,
        description="Safe constraint statement; no secret material",
    )
    origin: ConstraintOrigin = Field(
        description="Whether the user stated this or the model inferred it"
    )
    source_message_refs: list[uuid.UUID] = Field(
        default_factory=list,
        max_length=4,
        description="User messages this constraint is anchored to",
    )
    suggested: PyLiteral["add", "supersede"] = Field(
        description="Whether to add a new constraint or supersede an existing one"
    )
    supersedes_constraint_id: str | None = Field(
        default=None,
        pattern=STABLE_ID_PATTERN,
        description="Existing constraint ID replaced by a supersede proposal",
    )

    @field_validator("statement")
    @classmethod
    def _statement_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("statement must not be blank")
        return value

    @model_validator(mode="after")
    def _supersede_target_shape(self) -> "ConstraintProposal":
        if self.suggested == "supersede" and self.supersedes_constraint_id is None:
            raise ValueError("supersede proposal requires supersedes_constraint_id")
        if self.suggested == "add" and self.supersedes_constraint_id is not None:
            raise ValueError("add proposal must not carry supersedes_constraint_id")
        return self


# --- plan steps and patches ---------------------------------------------------


class TaskPlanStep(BaseModel):
    """One stable plan item (§7): bounded, dependency-linked, evidence-bound.

    ``executed`` without evidence references is invalid (P4): "done" is only
    rendered from agreed checks, never from a model-ticked boolean.
    """

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(
        pattern=STABLE_ID_PATTERN,
        description="Stable item ID that survives replanning deltas",
    )
    title: str = Field(
        min_length=1,
        max_length=TITLE_MAX_CHARS,
        description="Short step title safe for browser display",
    )
    status: StepStatus = Field(
        default=StepStatus.PENDING,
        description="Frozen todo status of this step",
    )
    verification: VerificationStatus = Field(
        default=VerificationStatus.NOT_CHECKED,
        description="Separate verification state bound to evidence",
    )
    evidence_refs: list[EvidenceRef] = Field(
        default_factory=list,
        max_length=MAX_EVIDENCE_REFS,
        description="Sourced evidence supporting executed/verified state",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        max_length=MAX_DEPENDENCIES,
        description="Stable step IDs this step depends on; no cycles",
    )
    risk: ActionRisk = Field(
        default=ActionRisk.READ_ONLY,
        description="Declared risk/action class of the step",
    )
    prerequisite_unresolved: bool = Field(
        default=False,
        description="True when a required prerequisite is still missing",
    )
    expected_check: str | None = Field(
        default=None,
        max_length=500,
        description="The check that will decide this step is done",
    )
    skip_reason: str | None = Field(
        default=None,
        max_length=500,
        description="Required reason when status is skipped",
    )

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value

    @model_validator(mode="after")
    def _status_evidence_bound(self) -> "TaskPlanStep":
        if self.status == StepStatus.EXECUTED and not self.evidence_refs:
            raise ValueError(
                "executed step requires at least one evidence reference "
                "(verification is bound to evidence, not a ticked boolean)"
            )
        if self.status == StepStatus.SKIPPED and not (
            self.skip_reason and self.skip_reason.strip()
        ):
            raise ValueError("skipped step requires a skip_reason")
        if self.verification in (VerificationStatus.PASSED, VerificationStatus.FAILED) and not self.evidence_refs:
            raise ValueError(
                "passed/failed verification requires at least one evidence reference"
            )
        return self


class TaskStepOperation(BaseModel):
    """One bounded patch operation against a single step (§5 delta semantics)."""

    model_config = ConfigDict(extra="forbid")

    op: PatchOp = Field(description="Patch operation kind: add, update or supersede")
    step: TaskPlanStep = Field(description="The step payload the operation applies")
    position: int | None = Field(
        default=None,
        ge=0,
        description="Requested list position for ordering-sensitive updates",
    )
    supersedes_step_id: str | None = Field(
        default=None,
        pattern=STABLE_ID_PATTERN,
        description="Existing step ID replaced by a supersede operation",
    )

    @model_validator(mode="after")
    def _supersede_shape(self) -> "TaskStepOperation":
        if self.op == PatchOp.SUPERSEDE and self.supersedes_step_id is None:
            raise ValueError("supersede operation requires supersedes_step_id")
        if self.op != PatchOp.SUPERSEDE and self.supersedes_step_id is not None:
            raise ValueError("only supersede operations carry supersedes_step_id")
        return self


class TaskPlanPatch(BaseModel):
    """Bounded plan delta (§5 ``todo_patch``, §7 ``task.propose_update``).

    A patch is a DELTA against the current task at ``base_revision`` — never
    whole-list replacement — and stays strictly under the 16 KiB serialized
    budget so it can never blow the capability dispatcher envelope.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(
        default=SCHEMA_VERSION,
        description="Patch schema version; consumers fail closed on drift",
    )
    source_message_id: uuid.UUID = Field(
        description="User message this interpretation/patch was produced from",
    )
    base_revision: int = Field(
        ge=1,
        description="Expected task revision for compare-and-swap (§1)",
    )
    idempotency_key: uuid.UUID = Field(
        description="Caller key so retransmitted patches deduplicate (§1)",
    )
    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=TITLE_MAX_CHARS,
        description="Proposed task header title (≤ 240 chars)",
    )
    summary: str | None = Field(
        default=None,
        min_length=1,
        max_length=SUMMARY_MAX_CHARS,
        description="Proposed task summary (≤ 1000 chars)",
    )
    rationale: str | None = Field(
        default=None,
        min_length=1,
        max_length=SUMMARY_MAX_CHARS,
        description="Why this delta is proposed (≤ 1000 chars)",
    )
    operations: list[TaskStepOperation] = Field(
        default_factory=list,
        max_length=MAX_PATCH_STEPS,
        description="Bounded add/update/supersede operations (max 12)",
    )

    @field_validator("schema_version")
    @classmethod
    def _version_supported(cls, value: int) -> int:
        if value != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported patch schema version (supported: {SCHEMA_VERSION})"
            )
        return value

    @field_validator("title", "summary", "rationale")
    @classmethod
    def _not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def _bounded_serialization(self) -> "TaskPlanPatch":
        size = len(self.model_dump_json().encode("utf-8"))
        if size > MAX_PATCH_SERIALIZED_BYTES:
            raise ValueError(
                f"serialized task patch exceeds {MAX_PATCH_SERIALIZED_BYTES} bytes"
            )
        return self


# --- bounded snapshots and views (no unbounded arrays; §2/§8) -----------------


class UnderstandingCounts(BaseModel):
    """List counts for the bounded understanding snapshot (§2 "counts")."""

    model_config = ConfigDict(extra="forbid")

    steps_total: int = Field(ge=0, description="Total plan steps on the task")
    steps_executed: int = Field(ge=0, description="Steps in executed status")
    constraints_total: int = Field(ge=0, description="Active constraints on the task")
    evidence_total: int = Field(ge=0, description="Evidence records bound to the task")
    criteria_total: int = Field(ge=0, description="Acceptance criteria on the task")
    criteria_verified: int = Field(ge=0, description="Criteria currently passed")


class NextAction(BaseModel):
    """One bounded next action with its continuation (§2/§5; never code)."""

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(
        pattern=STABLE_ID_PATTERN,
        description="Step the next action belongs to",
    )
    title: str = Field(
        min_length=1,
        max_length=TITLE_MAX_CHARS,
        description="Short action title safe for browser display",
    )
    continuation: str | None = Field(
        default=None,
        max_length=500,
        description="How the next attempt resumes this action",
    )


class UnderstandingSnapshot(BaseModel):
    """Bounded task model snapshot (§2): goal, phase, constraints, next actions.

    Critical constraints are carried VERBATIM — they can never be silently
    truncated; if a bounded representation cannot carry them, the assembler
    retrieves controlled pages or stops context assembly explicitly.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID = Field(description="Opaque stable task identity (§1)")
    conversation_id: uuid.UUID = Field(description="Owning conversation identity")
    server_id: uuid.UUID = Field(description="Server scope pinning this task (§1)")
    revision: int = Field(ge=1, description="Current optimistic revision token")
    objective: str = Field(
        min_length=1,
        max_length=SUMMARY_MAX_CHARS,
        description="Current goal statement (≤ 1000 chars)",
    )
    requested_phase: Phase = Field(description="Advisory phase; never permission")
    status: TaskOutcome = Field(description="Domain outcome layer above RunState")
    active_step_id: str | None = Field(
        default=None,
        pattern=STABLE_ID_PATTERN,
        description="Step currently being worked on, if any",
    )
    critical_constraints: list[str] = Field(
        default_factory=list,
        max_length=4,
        description="Active critical constraint statements, carried verbatim",
    )
    constraint_refs: list[str] = Field(
        default_factory=list,
        max_length=12,
        description="Active constraint IDs referenced by this snapshot",
    )
    open_questions: list[OpenQuestion] = Field(
        default_factory=list,
        max_length=8,
        description="Unresolved questions with stable proposal IDs",
    )
    next_actions: list[NextAction] = Field(
        default_factory=list,
        max_length=4,
        description="Bounded next actions with continuations",
    )
    counts: UnderstandingCounts = Field(
        description="List counts so UI/tools never need unbounded arrays",
    )


class TaskCounts(BaseModel):
    """List counts for the compact task view (§8: no unbounded arrays)."""

    model_config = ConfigDict(extra="forbid")

    steps_total: int = Field(ge=0, description="Total plan steps on the task")
    steps_executed: int = Field(ge=0, description="Steps in executed status")
    constraints_total: int = Field(ge=0, description="Active constraints on the task")
    evidence_total: int = Field(ge=0, description="Evidence records on the task")
    criteria_total: int = Field(ge=0, description="Acceptance criteria on the task")
    criteria_verified: int = Field(ge=0, description="Criteria currently passed")


class TaskView(BaseModel):
    """Compact task header view (§8 ``GET task`` shape): revision, phase,
    status and counts only — item/constraint/evidence collections are fetched
    as explicitly paginated pages, never embedded here."""

    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID = Field(description="Opaque stable task identity (§1)")
    conversation_id: uuid.UUID = Field(description="Owning conversation identity")
    server_id: uuid.UUID = Field(description="Server scope pinning this task (§1)")
    revision: int = Field(ge=1, description="Current optimistic revision token")
    title: str = Field(
        min_length=1,
        max_length=TITLE_MAX_CHARS,
        description="Task header title (≤ 240 chars)",
    )
    summary: str | None = Field(
        default=None,
        max_length=SUMMARY_MAX_CHARS,
        description="Task summary (≤ 1000 chars)",
    )
    requested_phase: Phase = Field(description="Advisory phase; never permission")
    status: TaskOutcome = Field(description="Domain outcome layer above RunState")
    active_step_id: str | None = Field(
        default=None,
        pattern=STABLE_ID_PATTERN,
        description="Step currently being worked on, if any",
    )
    counts: TaskCounts = Field(description="List counts for bounded rendering")
    created_at: str = Field(description="RFC3339 UTC creation timestamp")
    updated_at: str = Field(description="RFC3339 UTC last-revision timestamp")


# --- completion verdict (§9: no self-granted success) -------------------------


class CriterionResult(BaseModel):
    """One requirement-result map entry (§9 ``task.propose_completion``)."""

    model_config = ConfigDict(extra="forbid")

    criterion_id: str = Field(
        pattern=STABLE_ID_PATTERN,
        description="Acceptance criterion this result reports on",
    )
    result: VerificationStatus = Field(
        description="passed, failed or unavailable; never not_checked in a verdict",
    )
    evidence_refs: list[EvidenceRef] = Field(
        default_factory=list,
        max_length=MAX_EVIDENCE_REFS,
        description="Evidence supporting this result",
    )

    @model_validator(mode="after")
    def _verdict_results_are_decided(self) -> "CriterionResult":
        if self.result == VerificationStatus.NOT_CHECKED:
            raise ValueError("a completion verdict never reports not_checked results")
        return self


class BoundedPage(BaseModel):
    """Pagination metadata for paginated verdict result data (§9; AGENTS.md
    mandatory-pagination invariant)."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(ge=1, description="Page size used for this slice")
    offset: int = Field(ge=0, description="Slice start within the collection")
    total: int = Field(ge=0, description="Pre-slice matching count")


class TaskCompletionVerdict(BaseModel):
    """Compact completion projection (§9 receipt): honest, evidence-bound.

    ``verified`` can never be self-granted: :func:`validate_completion_verdict`
    requires every acceptance criterion to be covered by a passed result with
    evidence, else ``completion_missing_evidence``.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID = Field(description="Opaque stable task identity (§1)")
    base_revision: int = Field(
        ge=1,
        description="Task revision this verdict was produced against",
    )
    idempotency_key: uuid.UUID = Field(
        description="Caller key so retransmitted verdicts deduplicate",
    )
    task_outcome: TaskOutcome = Field(
        description="Domain outcome; verified requires full criterion coverage",
    )
    execution_status: ExecutionStatus = Field(
        description="Technical status of the closing attempt (§9)",
    )
    domain_outcome: DomainOutcome = Field(
        description="Domain outcome; unknown stays unknown for arbitrary shell work",
    )
    effect_certainty: EffectCertainty = Field(
        description="Certainty of the observed effects",
    )
    error_code: str | None = Field(
        default=None,
        max_length=64,
        description="Stable machine-readable error code, when failed",
    )
    retry_hint: str | None = Field(
        default=None,
        max_length=500,
        description="Advice only; policy decides what may execute (§9)",
    )
    evidence_refs: list[EvidenceRef] = Field(
        default_factory=list,
        max_length=MAX_EVIDENCE_REFS,
        description="Aggregate evidence supporting the verdict",
    )
    criterion_results: list[CriterionResult] = Field(
        default_factory=list,
        max_length=MAX_CRITERIA,
        description="Requirement-result map bound to evidence",
    )
    result_data: dict[str, str] = Field(
        default_factory=dict,
        description="Safe bounded result data (≤ 8 entries, values ≤ 200 chars)",
    )
    result_page: BoundedPage | None = Field(
        default=None,
        description="Pagination metadata when result data is a page slice",
    )

    @field_validator("result_data")
    @classmethod
    def _bounded_safe_result_data(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > RESULT_DATA_MAX_ENTRIES:
            raise ValueError(
                f"result_data carries at most {RESULT_DATA_MAX_ENTRIES} entries"
            )
        for key, entry in value.items():
            lowered = key.lower()
            if lowered in SECRET_DATA_KEYS:
                raise ValueError(
                    "result_data keys must never be credential-shaped (S1 boundary)"
                )
            if not key.strip():
                raise ValueError("result_data keys must not be blank")
            if len(entry) > RESULT_DATA_VALUE_MAX_CHARS:
                raise ValueError(
                    f"result_data values are at most {RESULT_DATA_VALUE_MAX_CHARS} chars"
                )
        return value


# --- deterministic contract checks -------------------------------------------


def check_revision(current_revision: int, expected_revision: int) -> None:
    """Compare-and-swap check (§1): a stale write is a structured conflict,
    never last-writer-wins.

    Raises :class:`TaskContractError` with code ``revision_conflict`` when
    ``expected_revision`` does not match the current revision.
    """
    if expected_revision != current_revision:
        raise TaskContractError(
            [
                TaskFieldError(
                    TaskErrorCode.REVISION_CONFLICT,
                    "base_revision",
                    "expected revision does not match the current task revision",
                )
            ]
        )


def validate_phase(phase: Phase, action: PhaseAction) -> None:
    """Phase admission check (§6): plan grants no target tools; inspect reads;
    act executes under existing grants. Metadata updates are always allowed.

    Raises :class:`TaskContractError` with code ``phase_not_allowed``.
    """
    if phase not in PHASE_ALLOWED_ACTIONS[action]:
        raise TaskContractError(
            [
                TaskFieldError(
                    TaskErrorCode.PHASE_NOT_ALLOWED,
                    "requested_phase",
                    f"action '{action.value}' is not allowed in phase '{phase.value}'",
                )
            ]
        )


def assert_task_scope(
    request_server_id: uuid.UUID, record_server_id: uuid.UUID
) -> None:
    """Cross-server scope guard (scenario C8): a request whose ``server_id``
    does not match the record's scope fails CLOSED with ``task_not_found`` —
    the not-found code intentionally hides existence so no content or shape
    of the other server's task leaks through the error channel.
    """
    if request_server_id != record_server_id:
        raise TaskContractError(
            [
                TaskFieldError(
                    TaskErrorCode.TASK_NOT_FOUND,
                    "server_id",
                    "no task in the requesting server scope",
                )
            ]
        )


def constraint_proposal_errors(
    proposals: Collection[ConstraintProposal],
    known_constraint_origins: Mapping[str, ConstraintOrigin | str] | None = None,
) -> list[TaskFieldError]:
    """Authority check on constraint proposals (§6, P4 "erase a user
    prohibition").

    A model-origin proposal may never supersede a USER-origin constraint:
    removing an active user constraint needs an explicit user correction
    through the revision API, not an intake suggestion. Returns every
    rejection found; empty list means the proposals are admissible.
    """
    errors: list[TaskFieldError] = []
    for index, proposal in enumerate(proposals):
        field = f"constraints_proposed[{index}]"
        if proposal.suggested != "supersede":
            continue
        target = proposal.supersedes_constraint_id
        assert target is not None  # shape-enforced by the model
        if (
            known_constraint_origins is not None
            and target in known_constraint_origins
            and ConstraintOrigin(known_constraint_origins[target])
            == ConstraintOrigin.USER
            and proposal.origin == ConstraintOrigin.MODEL
        ):
            errors.append(
                TaskFieldError(
                    TaskErrorCode.CONSTRAINT_ESCALATION,
                    field,
                    "model-generated updates cannot supersede a user-origin "
                    "constraint; an explicit user correction is required",
                )
            )
        elif (
            known_constraint_origins is not None
            and target not in known_constraint_origins
        ):
            errors.append(
                TaskFieldError(
                    TaskErrorCode.UNKNOWN_CONSTRAINT_TARGET,
                    field,
                    "supersede target constraint does not exist in the task scope",
                )
            )
    return errors


def validate_constraint_proposals(
    proposals: Collection[ConstraintProposal],
    known_constraint_origins: Mapping[str, ConstraintOrigin | str] | None = None,
) -> None:
    """Raise :class:`TaskContractError` for inadmissible constraint proposals
    (see :func:`constraint_proposal_errors` for the rules)."""
    errors = constraint_proposal_errors(proposals, known_constraint_origins)
    if errors:
        raise TaskContractError(errors)


def validate_task_plan_patch(
    patch: TaskPlanPatch,
    known_step_ids: Collection[str] | None = None,
) -> None:
    """Deterministic structural validation of one patch (§6; P4).

    Always checked: duplicate step keys inside the patch, dependency DAG
    cycles. Checked when ``known_step_ids`` is provided (fail-closed — pass
    the live task's step IDs in production): add-collisions with existing
    steps, unknown dependency/supersede references.

    Field-level bounds, executed-needs-evidence and serialized-size rules are
    enforced at model construction (see :class:`TaskPlanStep` /
    :class:`TaskPlanPatch`).

    Raises :class:`TaskContractError` with top-level code
    ``invalid_task_patch`` and per-field detail codes.
    """
    errors: list[TaskFieldError] = []
    seen: dict[str, int] = {}
    patch_ids: set[str] = set()
    for index, operation in enumerate(patch.operations):
        step = operation.step
        field = f"operations[{index}].step.step_id"
        if step.step_id in seen:
            errors.append(
                TaskFieldError(
                    TaskErrorCode.DUPLICATE_STEP_ID,
                    field,
                    f"duplicate step key '{step.step_id}' inside one patch",
                )
            )
        seen[step.step_id] = index
        patch_ids.add(step.step_id)
        if (
            known_step_ids is not None
            and operation.op == PatchOp.ADD
            and step.step_id in known_step_ids
        ):
            errors.append(
                TaskFieldError(
                    TaskErrorCode.DUPLICATE_STEP_ID,
                    field,
                    f"add operation collides with existing step '{step.step_id}'",
                )
            )
        if (
            known_step_ids is not None
            and operation.op == PatchOp.SUPERSEDE
            and operation.supersedes_step_id is not None
            and operation.supersedes_step_id not in known_step_ids
            and operation.supersedes_step_id not in patch_ids
        ):
            errors.append(
                TaskFieldError(
                    TaskErrorCode.UNKNOWN_SUPERSEDE_TARGET,
                    f"operations[{index}].supersedes_step_id",
                    "supersede target step does not exist in the task scope",
                )
            )

    # Dependencies: unknown references (fail-closed only against live state)
    # and cycles inside the patch graph. Dependencies on pre-existing steps
    # cannot close a cycle back into the patch: existing steps never depend
    # on future patch steps.
    for index, operation in enumerate(patch.operations):
        for position, dep in enumerate(operation.step.depends_on):
            if dep in patch_ids:
                continue
            if known_step_ids is not None and dep not in known_step_ids:
                errors.append(
                    TaskFieldError(
                        TaskErrorCode.UNKNOWN_DEPENDENCY,
                        f"operations[{index}].step.depends_on[{position}]",
                        f"dependency '{dep}' is neither in the patch nor on the task",
                    )
                )

    errors.extend(_patch_cycle_errors(patch, patch_ids))

    if errors:
        raise TaskContractError(errors)


def _patch_cycle_errors(patch: TaskPlanPatch, patch_ids: set[str]) -> list[TaskFieldError]:
    """Cycle detection over the patch-internal dependency graph (DFS)."""
    edges: dict[str, list[str]] = {
        operation.step.step_id: [
            dep for dep in operation.step.depends_on if dep in patch_ids
        ]
        for operation in patch.operations
    }
    state: dict[str, int] = {}  # 1 = on stack, 2 = done
    stack: list[str] = []
    errors: list[TaskFieldError] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for dep in edges.get(node, ()):
            mark = state.get(dep)
            if mark == 1:
                cycle_start = stack.index(dep)
                cycle = " -> ".join([*stack[cycle_start:], dep])
                errors.append(
                    TaskFieldError(
                        TaskErrorCode.DEPENDENCY_CYCLE,
                        "operations",
                        f"dependency cycle in the patch graph: {cycle}",
                    )
                )
            elif mark is None:
                visit(dep)
        stack.pop()
        state[node] = 2

    for node in edges:
        if state.get(node) is None:
            visit(node)
    return errors


def validate_completion_verdict(
    verdict: TaskCompletionVerdict,
    known_criterion_ids: Collection[str],
    known_evidence_ids: Collection[str] | None = None,
) -> None:
    """Completion authority check (§9; P4/E2): a verdict can never self-grant
    verified success.

    - ``verified`` requires EVERY known criterion covered exactly once by a
      PASSED result bound to evidence — else ``completion_missing_evidence``.
    - ``partially_verified`` requires at least one passed criterion.
    - Unknown criterion IDs or fabricated evidence references are rejected.

    Raises :class:`TaskContractError`; top-level code is
    ``completion_missing_evidence`` or ``invalid_task_patch``.
    """
    errors: list[TaskFieldError] = []
    covered: dict[str, CriterionResult] = {}
    for index, result in enumerate(verdict.criterion_results):
        field = f"criterion_results[{index}]"
        if result.criterion_id not in known_criterion_ids:
            errors.append(
                TaskFieldError(
                    TaskErrorCode.UNKNOWN_CRITERION,
                    f"{field}.criterion_id",
                    "result references a criterion the task does not define",
                )
            )
            continue
        if result.criterion_id in covered:
            errors.append(
                TaskFieldError(
                    TaskErrorCode.INVALID_TASK_PATCH,
                    f"{field}.criterion_id",
                    "duplicate result for one criterion",
                )
            )
            continue
        covered[result.criterion_id] = result
        if known_evidence_ids is not None:
            for ref_index, ref in enumerate(result.evidence_refs):
                if str(ref.evidence_id) not in known_evidence_ids and (
                    ref.evidence_id not in known_evidence_ids
                ):
                    errors.append(
                        TaskFieldError(
                            TaskErrorCode.UNKNOWN_EVIDENCE_REF,
                            f"{field}.evidence_refs[{ref_index}]",
                            "evidence reference does not exist in the task scope",
                        )
                    )
        if result.result == VerificationStatus.PASSED and not result.evidence_refs:
            errors.append(
                TaskFieldError(
                    TaskErrorCode.MISSING_EVIDENCE_REFS,
                    f"{field}.evidence_refs",
                    "a passed criterion result requires evidence references",
                )
            )

    missing = [
        criterion_id
        for criterion_id in known_criterion_ids
        if criterion_id not in covered
    ]
    if verdict.task_outcome == TaskOutcome.VERIFIED:
        for criterion_id in missing:
            errors.append(
                TaskFieldError(
                    TaskErrorCode.MISSING_CRITERION,
                    "criterion_results",
                    f"verified verdict does not cover criterion '{criterion_id}'",
                )
            )
        for criterion_id, result in covered.items():
            if result.result != VerificationStatus.PASSED:
                errors.append(
                    TaskFieldError(
                        TaskErrorCode.CRITERION_NOT_PASSED,
                        "criterion_results",
                        f"verified verdict requires criterion '{criterion_id}' to pass",
                    )
                )
    elif verdict.task_outcome == TaskOutcome.PARTIALLY_VERIFIED:
        if not any(
            result.result == VerificationStatus.PASSED for result in covered.values()
        ):
            errors.append(
                TaskFieldError(
                    TaskErrorCode.PARTIAL_REQUIRES_PASSED,
                    "criterion_results",
                    "partially_verified requires at least one passed criterion",
                )
            )

    if errors:
        raise TaskContractError(errors)
