"""Runtime v2 objective/scope/journal contracts (journal-store slice).

Frozen vocabulary for the v2 objective episode (runtime-v2.md §3, §4, §5,
§8, §11, §14):

- **Actor provenance (§4):** every activity event and scope mutation carries
  an explicit :class:`ActorKind` — provenance labels only, never principals;
  a ``child_agent`` is never an approver and never a scope authority.
- **Objective identity (§3):** verbatim operator request + operator
  constraints (never model-reworded), one status, one explicit operator-owned
  server scope, one bounded budget, one CAS ``revision``.
- **Canonical ``objective.*`` ledger family (§5.1/§14):** the six frozen
  event types, dotted-slug grammar, versioned object payloads. Events are
  value-free: identities, counts and sanitized references — never task prose
  or secrets.
- **Redaction bounds (§5.3):** the frozen stream budgets from the legacy
  executor carry over — per-text 4096 chars (:data:`EVENT_TEXT_MAX_CHARS`,
  mirrors ``mai.agents.run.STREAM_DELTA_MAX_CHARS``) and whole-payload 32768
  chars (:data:`EVENT_PAYLOAD_MAX_CHARS`, mirrors
  ``mai.agents.run.STREAM_TOOL_DETAIL_MAX_CHARS``). Text is projected BEFORE
  persistence; oversized structured payloads are rejected, never silently
  cut (a silent cut would corrupt a typed payload).
- **Pagination bounds (§5.2):** reuse the frozen ``DEFAULT_PAGE_LIMIT``/``
  MAX_PAGE_LIMIT`` of :mod:`mai.contracts.conversations`; ``has_more`` is
  derived, never fabricated.
- **Evidence-bound completion (§8.3):** the ``TaskCompletionVerdict``
  discipline carries over — a ``verified`` outcome requires passed results
  bound to evidence, else the structured ``completion_missing_evidence``;
  ``domain_outcome`` stays honestly ``unknown`` for arbitrary shell work.

The HTTP views live with the conversation-api slice; this module freezes the
store-level contract (the one vocabulary every producer consumes).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mai.contracts.conversations import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    PageEnvelope,
    compute_has_more,
)
from mai.contracts.tasks import (
    CriterionResult,
    EvidenceRef,
    DomainOutcome,
    EffectCertainty,
    ExecutionStatus,
    TaskOutcome,
    TITLE_MAX_CHARS,
    VerificationStatus,
)

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "ACTOR_KINDS",
    "CHECKLIST_ITEM_MAX_CHARS",
    "CONSTRAINT_TEXT_MAX_CHARS",
    "EVENT_TEXT_MAX_CHARS",
    "EVENT_PAYLOAD_MAX_CHARS",
    "MAX_CHECKLIST_ITEMS",
    "MAX_JOURNAL_ACTIONS",
    "MAX_JOURNAL_CHILDREN",
    "MAX_JOURNAL_RECEIPTS",
    "MAX_JOURNAL_UNKNOWNS",
    "MAX_OBJECTIVE_CONSTRAINTS",
    "MAX_SCOPE_SERVERS",
    "OBJECTIVE_CREATED_EVENT_TYPE",
    "OBJECTIVE_STATUS_CHANGED_EVENT_TYPE",
    "OBJECTIVE_SCOPE_CHANGED_EVENT_TYPE",
    "OBJECTIVE_JOURNAL_UPDATED_EVENT_TYPE",
    "OBJECTIVE_CHECKLIST_UPDATED_EVENT_TYPE",
    "OBJECTIVE_COMPLETED_EVENT_TYPE",
    "OBJECTIVE_EVENT_TYPES",
    "OBJECTIVE_EVENT_PREFIX",
    "OBJECTIVE_COMPLETED_EVENT_TYPE",
    "ORIGINAL_REQUEST_MAX_CHARS",
    "ActorKind",
    "ChecklistDocument",
    "ChecklistItem",
    "JournalActionApplied",
    "JournalChildLinked",
    "JournalMutation",
    "JournalReceiptRecorded",
    "JournalUnknownNoted",
    "JournalVerificationRecorded",
    "ObjectiveBudget",
    "ObjectiveCompletionVerdict",
    "ObjectiveContractError",
    "ObjectiveCreatedPayload",
    "ObjectiveDetail",
    "ObjectiveEvent",
    "ObjectiveEventPage",
    "ObjectiveErrorCode",
    "ObjectiveFieldError",
    "ObjectiveChecklistUpdatedPayload",
    "ObjectiveCompletedPayload",
    "ObjectiveJournalUpdatedPayload",
    "ObjectiveListPage",
    "ObjectiveRunLink",
    "ObjectiveRunPage",
    "ObjectiveScopeChangedPayload",
    "ObjectiveScopeServer",
    "ObjectiveStatus",
    "ObjectiveStatusChangedPayload",
    "ObjectiveSummary",
    "WorkJournal",
    "WorkJournalChecklist",
    "assert_objective_scope",
    "compute_has_more",
    "objective_event_payload_type",
    "parse_objective_event_payload",
    "validate_objective_completion",
    "validate_scope_change",
]


# ---------------------------------------------------------------------------
# frozen bounds (AGENTS.md §1 pagination; runtime-v2.md §5.2/§5.3)
# ---------------------------------------------------------------------------

#: Per-text bound applied BEFORE persistence (§5.3; mirrors the legacy
#: executor's ``STREAM_DELTA_MAX_CHARS`` pin — cross-checked by tests).
EVENT_TEXT_MAX_CHARS: Final[int] = 4096

#: Whole-event serialized payload bound (§5.3; mirrors the legacy executor's
#: ``STREAM_TOOL_DETAIL_MAX_CHARS`` pin). Exceeding it is a structured
#: rejection: big raw content belongs behind a safe ``content_ref``.
EVENT_PAYLOAD_MAX_CHARS: Final[int] = 32768

#: Verbatim operator request bound (mirrors the conversation message bound).
ORIGINAL_REQUEST_MAX_CHARS: Final[int] = 20_000

#: Operator constraints: verbatim, bounded count and length.
MAX_OBJECTIVE_CONSTRAINTS: Final[int] = 12
CONSTRAINT_TEXT_MAX_CHARS: Final[int] = 2_000

#: Explicit operator-owned server scope bound.
MAX_SCOPE_SERVERS: Final[int] = 16

#: Advisory checklist bounds (§8.2 — optional, never mandatory).
MAX_CHECKLIST_ITEMS: Final[int] = 50
CHECKLIST_ITEM_MAX_CHARS: Final[int] = 500

#: Journal projection section bounds (§8.1) with explicit truncation flags —
#: the projection is bounded, never unbounded history.
MAX_JOURNAL_ACTIONS: Final[int] = 200
MAX_JOURNAL_RECEIPTS: Final[int] = 200
MAX_JOURNAL_UNKNOWNS: Final[int] = 64
MAX_JOURNAL_CHILDREN: Final[int] = 64


# ---------------------------------------------------------------------------
# frozen vocabularies
# ---------------------------------------------------------------------------


class ObjectiveStatus(StrEnum):
    """Objective episode status (§3; terminal states are frozen)."""

    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ActorKind(StrEnum):
    """Actor provenance for audit records (§4; labels, never principals)."""

    OPERATOR = "operator"
    AGENT = "agent"
    CHILD_AGENT = "child_agent"
    AUTOMATION = "automation"


ACTOR_KINDS: Final[frozenset[str]] = frozenset(
    kind.value for kind in ActorKind
)

#: The frozen ``objective.*`` family (§14; additions stay in-family and
#: additive — never a rename, never a reused meaning).
OBJECTIVE_CREATED_EVENT_TYPE: Final[str] = "objective.created"
OBJECTIVE_SCOPE_CHANGED_EVENT_TYPE: Final[str] = "objective.scope_changed"
OBJECTIVE_STATUS_CHANGED_EVENT_TYPE: Final[str] = "objective.status_changed"
OBJECTIVE_JOURNAL_UPDATED_EVENT_TYPE: Final[str] = "objective.journal_updated"
OBJECTIVE_CHECKLIST_UPDATED_EVENT_TYPE: Final[str] = "objective.checklist_updated"
OBJECTIVE_COMPLETED_EVENT_TYPE: Final[str] = "objective.completed"

OBJECTIVE_EVENT_TYPES: Final[tuple[str, ...]] = (
    OBJECTIVE_CREATED_EVENT_TYPE,
    OBJECTIVE_SCOPE_CHANGED_EVENT_TYPE,
    OBJECTIVE_STATUS_CHANGED_EVENT_TYPE,
    OBJECTIVE_JOURNAL_UPDATED_EVENT_TYPE,
    OBJECTIVE_CHECKLIST_UPDATED_EVENT_TYPE,
    OBJECTIVE_COMPLETED_EVENT_TYPE,
)

OBJECTIVE_EVENT_PREFIX: Final[str] = "objective."

#: Payload version of every ``objective.*`` event document this slice emits.
OBJECTIVE_EVENT_PAYLOAD_VERSION: Final[int] = 1


class ObjectiveErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    OBJECTIVE_NOT_FOUND = "objective_not_found"
    REVISION_CONFLICT = "revision_conflict"
    OPERATOR_REQUIRED = "operator_required"
    SCOPE_VIOLATION = "scope_violation"
    INVALID_STATUS_TRANSITION = "invalid_status_transition"
    COMPLETION_MISSING_EVIDENCE = "completion_missing_evidence"
    LEGACY_RUN_NOT_CONVERTIBLE = "legacy_run_not_convertible"
    INVALID_SCOPE_CHANGE = "invalid_scope_change"
    RUN_NOT_IN_SCOPE = "run_not_in_scope"
    EVENT_TOO_LARGE = "event_too_large"
    INVALID_PAYLOAD = "invalid_payload"
    CLIENT_REQUEST_CONFLICT = "client_request_conflict"


@dataclass(frozen=True, slots=True)
class ObjectiveFieldError:
    """One rejection: field path + stable code + value-free reason."""

    code: str
    field: str
    detail: str


class ObjectiveContractError(ValueError):
    """All rejections for one objective-scope contract check.

    ``str`` contains only field paths, codes and fixed reasons — never
    request content or other-scope identifiers — so the exception is safe
    to log, store and return in an error body.
    """

    def __init__(self, errors: Collection[ObjectiveFieldError]) -> None:
        self.errors: tuple[ObjectiveFieldError, ...] = tuple(errors)
        summary = ", ".join(f"{e.field}: {e.code}" for e in self.errors)
        super().__init__(f"objective contract rejected ({summary})")

    @property
    def fields(self) -> dict[str, str]:
        """Field path → stable code, ready for the C01 envelope ``fields``."""
        return {e.field: e.code for e in self.errors}


def _error(
    code: ObjectiveErrorCode, field: str, detail: str
) -> ObjectiveContractError:
    return ObjectiveContractError(
        [ObjectiveFieldError(code.value, field, detail)]
    )


# ---------------------------------------------------------------------------
# scope authority (§3.2; AC5)
# ---------------------------------------------------------------------------


def assert_objective_scope(
    request_server_id: uuid.UUID, scope_member_server_ids: Collection[uuid.UUID]
) -> None:
    """Cross-server scope guard (C8 discipline): a request whose server is
    not in the objective's explicit scope fails CLOSED with
    ``objective_not_found`` — the not-found code intentionally hides
    existence so nothing about the other scope's objective leaks through
    the error channel.
    """
    if request_server_id not in scope_member_server_ids:
        raise _error(
            ObjectiveErrorCode.OBJECTIVE_NOT_FOUND,
            "server_id",
            "no objective in the requesting server scope",
        )


def validate_scope_change(
    *,
    add_server_ids: Collection[uuid.UUID],
    remove_server_ids: Collection[uuid.UUID],
    current_scope: Collection[uuid.UUID],
    home_server_id: uuid.UUID,
    actor_kind: ActorKind,
    expected_revision: int,
    current_revision: int,
) -> tuple[tuple[uuid.UUID, ...], tuple[uuid.UUID, ...]]:
    """Operator-only scope-change authority check (§3.2; AC5).

    Returns the effective ``(added, removed)`` tuples after idempotent
    filtering. Raises a structured rejection when:

    - ``actor_kind`` is not :attr:`ActorKind.OPERATOR` — no agent, child or
      model input may add or remove a server;
    - ``expected_revision`` is stale — CAS, never last-writer-wins;
    - the same server is added and removed in one call;
    - the home (initiating) server is removed — the objective's scope pin
      always stays a scope member;
    - the resulting scope would be empty or exceed
      :data:`MAX_SCOPE_SERVERS`.
    """
    if actor_kind is not ActorKind.OPERATOR:
        raise _error(
            ObjectiveErrorCode.OPERATOR_REQUIRED,
            "actor_kind",
            "only the operator may change an objective's server scope",
        )
    if expected_revision != current_revision:
        raise _error(
            ObjectiveErrorCode.REVISION_CONFLICT,
            "expected_revision",
            "expected revision does not match the current objective revision",
        )
    add_set = set(add_server_ids)
    remove_set = set(remove_server_ids)
    overlap = add_set & remove_set
    if overlap:
        raise _error(
            ObjectiveErrorCode.INVALID_SCOPE_CHANGE,
            "server_ids",
            "the same server cannot be added and removed in one scope change",
        )
    if home_server_id in remove_set:
        raise _error(
            ObjectiveErrorCode.SCOPE_VIOLATION,
            "remove_server_ids",
            "the initiating server always remains in the objective scope",
        )
    effective_add = tuple(sorted(add_set - current_scope, key=str))
    effective_remove = tuple(sorted(remove_set & set(current_scope), key=str))
    projected = (set(current_scope) - remove_set) | add_set
    if not projected:
        raise _error(
            ObjectiveErrorCode.SCOPE_VIOLATION,
            "remove_server_ids",
            "the objective scope cannot become empty",
        )
    if len(projected) > MAX_SCOPE_SERVERS:
        raise _error(
            ObjectiveErrorCode.SCOPE_VIOLATION,
            "add_server_ids",
            f"the objective scope is bounded at {MAX_SCOPE_SERVERS} servers",
        )
    return effective_add, effective_remove


# ---------------------------------------------------------------------------
# evidence-bound completion (§8.3)
# ---------------------------------------------------------------------------


class ObjectiveCompletionVerdict(BaseModel):
    """Compact evidence-bound completion projection for one objective.

    The legacy ``TaskCompletionVerdict`` discipline (runtime-v2.md §8.3):
    ``verified`` can never be self-granted —
    :func:`validate_objective_completion` requires passed criterion results
    bound to evidence, else the structured
    ``completion_missing_evidence``. ``domain_outcome`` stays honestly
    ``unknown`` for arbitrary shell work; model prose never inflates
    evidence.
    """

    model_config = ConfigDict(extra="forbid")

    objective_id: uuid.UUID = Field(
        description="Opaque stable objective identity this verdict closes",
    )
    base_revision: int = Field(
        ge=1,
        description="Objective revision this verdict was produced against",
    )
    idempotency_key: uuid.UUID = Field(
        description="Caller key so retransmitted verdicts deduplicate",
    )
    task_outcome: TaskOutcome = Field(
        description="Domain outcome; verified requires evidence-bound coverage",
    )
    execution_status: ExecutionStatus = Field(
        description="Technical status of the closing attempt",
    )
    domain_outcome: DomainOutcome = Field(
        description="Honest domain outcome; unknown stays unknown for shell work",
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
        description="Advice only; policy decides what may execute",
    )
    evidence_refs: list[EvidenceRef] = Field(
        default_factory=list,
        max_length=16,
        description="Aggregate evidence supporting the verdict",
    )
    criterion_results: list[CriterionResult] = Field(
        default_factory=list,
        max_length=12,
        description="Requirement-result map bound to evidence",
    )

    @field_validator("error_code", "retry_hint")
    @classmethod
    def _no_prose_secrets(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("verdict free-text must not contain NUL bytes")
        return value


def validate_objective_completion(verdict: ObjectiveCompletionVerdict) -> bool:
    """Completion authority check (§8.3): a verdict can never self-grant
    verified success.

    - ``verified`` requires at least one criterion result PASSED and bound
      to at least one evidence reference, and every reported result must be
      passed-criterion-covered — else the structured
      ``completion_missing_evidence`` rejection.
    - ``partially_verified`` requires at least one passed criterion.
    - ``rejected``/``failed`` verdicts need no evidence (an honest failure
      is not an achievement claim).

    Returns ``True`` when the verdict is evidence-complete. Raises
    :class:`ObjectiveContractError` (``completion_missing_evidence``)
    otherwise — the caller records the honest missing-evidence state, it
    never silently completes.
    """
    passed = [
        result
        for result in verdict.criterion_results
        if result.result is VerificationStatus.PASSED
    ]
    if verdict.task_outcome is TaskOutcome.PARTIALLY_VERIFIED:
        if not passed:
            raise _error(
                ObjectiveErrorCode.COMPLETION_MISSING_EVIDENCE,
                "criterion_results",
                "a partially-verified outcome needs at least one passed result",
            )
        return False
    if verdict.task_outcome is not TaskOutcome.VERIFIED:
        return False
    if not passed or not all(
        result.evidence_refs for result in verdict.criterion_results
    ):
        raise _error(
            ObjectiveErrorCode.COMPLETION_MISSING_EVIDENCE,
            "criterion_results",
            "a verified outcome requires every reported criterion passed "
            "and bound to evidence",
        )
    if not verdict.evidence_refs:
        raise _error(
            ObjectiveErrorCode.COMPLETION_MISSING_EVIDENCE,
            "evidence_refs",
            "a verified outcome requires aggregate evidence references",
        )
    return True


# ---------------------------------------------------------------------------
# event payloads (value-free, versioned; §5.1/§14)
# ---------------------------------------------------------------------------


class ObjectiveCreatedPayload(BaseModel):
    """``objective.created`` payload: identities and counts, no prose."""

    model_config = ConfigDict(extra="forbid")

    server_id: uuid.UUID = Field(description="Initiating (home) server identity")
    title: str | None = Field(
        default=None,
        max_length=TITLE_MAX_CHARS,
        description="Operator-assigned objective title when set",
    )
    scope_server_ids: list[uuid.UUID] = Field(
        min_length=1,
        max_length=MAX_SCOPE_SERVERS,
        description="Initial explicit operator-owned server scope",
    )
    constraint_count: int = Field(
        ge=0,
        description="Number of verbatim operator constraints stored",
    )
    budget: "ObjectiveBudget | None" = Field(
        default=None,
        description="Bounded budget caps when the operator set any",
    )


class ObjectiveScopeChangedPayload(BaseModel):
    """``objective.scope_changed`` payload: the exact effective delta."""

    model_config = ConfigDict(extra="forbid")

    added_server_ids: list[uuid.UUID] = Field(
        default_factory=list,
        max_length=MAX_SCOPE_SERVERS,
        description="Servers added to the scope by this change",
    )
    removed_server_ids: list[uuid.UUID] = Field(
        default_factory=list,
        max_length=MAX_SCOPE_SERVERS,
        description="Servers removed from the scope by this change",
    )
    revision: int = Field(ge=1, description="Objective revision after the change")


class ObjectiveStatusChangedPayload(BaseModel):
    """``objective.status_changed`` payload: the exact transition."""

    model_config = ConfigDict(extra="forbid")

    from_status: ObjectiveStatus = Field(description="Status before the change")
    to_status: ObjectiveStatus = Field(description="Status after the change")
    revision: int = Field(ge=1, description="Objective revision after the change")


class ObjectiveChecklistUpdatedPayload(BaseModel):
    """``objective.checklist_updated`` payload: the bounded advisory list."""

    model_config = ConfigDict(extra="forbid")

    items: list["ChecklistItem"] = Field(
        max_length=MAX_CHECKLIST_ITEMS,
        description="The full advisory checklist after this update",
    )
    checklist_revision: int = Field(
        ge=1,
        description="Checklist revision after this update",
    )


class ObjectiveCompletedPayload(BaseModel):
    """``objective.completed`` payload: the settled outcome summary."""

    model_config = ConfigDict(extra="forbid")

    from_status: ObjectiveStatus = Field(description="Status before completion")
    revision: int = Field(ge=1, description="Objective revision after completion")
    idempotency_key: uuid.UUID = Field(
        description="The verdict's dedup key; retransmitted completions "
        "deduplicate onto this settled event",
    )
    task_outcome: TaskOutcome = Field(description="Evidence-bound domain outcome")
    execution_status: ExecutionStatus = Field(
        description="Technical status of the closing attempt",
    )
    domain_outcome: DomainOutcome = Field(
        description="Honest domain outcome; unknown stays unknown",
    )
    effect_certainty: EffectCertainty = Field(
        description="Certainty of the observed effects",
    )
    missing_evidence: bool = Field(
        description="True when evidence discipline downgraded the claim",
    )
    evidence_count: int = Field(
        ge=0,
        description="Number of aggregate evidence references",
    )
    criterion_count: int = Field(
        ge=0,
        description="Number of reported criterion results",
    )
    error_code: str | None = Field(
        default=None,
        max_length=64,
        description="Stable machine-readable error code, when failed",
    )


class ObjectiveJournalUpdatedPayload(BaseModel):
    """``objective.journal_updated`` payload: one trusted-record mutation.

    ``kind`` selects the record; the remaining fields are that record.
    Malformed payloads are bookkeeping trouble for the journal fold (§8.2)
    — they never erase applied effects and never fail a run.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "action", "receipt", "verification", "unknown", "child"
    ] = Field(description="Which trusted journal record this payload mutates")
    action: "JournalActionApplied | None" = Field(
        default=None,
        description="Action attempt record when kind=action",
    )
    receipt: "JournalReceiptRecorded | None" = Field(
        default=None,
        description="Receipt record when kind=receipt",
    )
    verification: "JournalVerificationRecorded | None" = Field(
        default=None,
        description="Verification verdict when kind=verification",
    )
    unknown: "JournalUnknownNoted | None" = Field(
        default=None,
        description="Unknown-effect record when kind=unknown",
    )
    child: "JournalChildLinked | None" = Field(
        default=None,
        description="Delegation link record when kind=child",
    )


ObjectiveEventPayload = Annotated[
    ObjectiveCreatedPayload
    | ObjectiveScopeChangedPayload
    | ObjectiveStatusChangedPayload
    | ObjectiveChecklistUpdatedPayload
    | ObjectiveCompletedPayload
    | ObjectiveJournalUpdatedPayload,
    Field(discriminator=None),
]

_PAYLOAD_TYPES: Final[Mapping[str, type[BaseModel]]] = {
    OBJECTIVE_CREATED_EVENT_TYPE: ObjectiveCreatedPayload,
    OBJECTIVE_SCOPE_CHANGED_EVENT_TYPE: ObjectiveScopeChangedPayload,
    OBJECTIVE_STATUS_CHANGED_EVENT_TYPE: ObjectiveStatusChangedPayload,
    OBJECTIVE_JOURNAL_UPDATED_EVENT_TYPE: ObjectiveJournalUpdatedPayload,
    OBJECTIVE_CHECKLIST_UPDATED_EVENT_TYPE: ObjectiveChecklistUpdatedPayload,
    OBJECTIVE_COMPLETED_EVENT_TYPE: ObjectiveCompletedPayload,
}


def objective_event_payload_type(event_type: str) -> type[BaseModel] | None:
    """The payload model for one ledger type; ``None`` for unknown types
    (unknown in-family additions pass through display untouched)."""
    return _PAYLOAD_TYPES.get(event_type)


def parse_objective_event_payload(
    event_type: str, data: Mapping[str, Any]
) -> tuple[BaseModel | None, str | None]:
    """Strict typed parse of one ledger payload (fold input).

    Returns ``(model, None)`` on success, ``(None, None)`` for an unknown
    type (additive in-family types are display-only for this slice), and
    ``(None, reason)`` for a KNOWN type whose payload does not satisfy the
    frozen shape — the journal fold turns that reason into a visible
    bookkeeping marker without touching any other section (§8.2).
    """
    model = objective_event_payload_type(event_type)
    if model is None:
        return None, None
    try:
        return model.model_validate(dict(data)), None
    except ValidationError as exc:
        fields = ", ".join(
            ".".join(str(part) for part in error["loc"]) or "<root>"
            for error in exc.errors()[:4]
        )
        return None, f"invalid {model.__name__}: {fields}"


# ---------------------------------------------------------------------------
# structured records (objective header, scope, runs)
# ---------------------------------------------------------------------------


class ObjectiveBudget(BaseModel):
    """Bounded objective budget caps (§3; the budget LEDGER vocabulary of
    ``mai.agents.budgets`` stays the spend-tracking authority — these are
    the operator-set caps stored with the objective)."""

    model_config = ConfigDict(extra="forbid")

    max_active_runs: int | None = Field(
        default=None,
        ge=1,
        le=32,
        description="Aggregate active-slot cap (1..32, the ledger bound)",
    )
    max_total_steps: int | None = Field(
        default=None,
        ge=1,
        le=10_000,
        description="Aggregate durable-step cap (1..10000, the ledger bound)",
    )
    max_total_tokens: int | None = Field(
        default=None,
        ge=1,
        le=10_000_000,
        description="Aggregate model-token cap (1..10000000, the ledger bound)",
    )


class ObjectiveSummary(BaseModel):
    """One list-page objective row (safe metadata; no request text)."""

    model_config = ConfigDict(extra="forbid")

    objective_id: uuid.UUID = Field(description="Opaque stable objective identity")
    server_id: uuid.UUID = Field(
        description="Initiating (home) server identity",
    )
    title: str | None = Field(
        default=None,
        max_length=TITLE_MAX_CHARS,
        description="Operator-assigned title when set",
    )
    status: ObjectiveStatus = Field(description="Current objective status")
    revision: int = Field(ge=1, description="Current CAS revision")
    created_at: object = Field(description="RFC3339 UTC creation timestamp")
    updated_at: object = Field(description="RFC3339 UTC last-update timestamp")


class ObjectiveDetail(ObjectiveSummary):
    """One scope-checked objective read (§3 store truth)."""

    model_config = ConfigDict(extra="forbid")

    original_request: str = Field(
        max_length=ORIGINAL_REQUEST_MAX_CHARS,
        description="Verbatim operator request (projected; provenance-carrying)",
    )
    constraints: list[str] = Field(
        max_length=MAX_OBJECTIVE_CONSTRAINTS,
        description="Verbatim operator constraints (never model-reworded)",
    )
    budget: ObjectiveBudget | None = Field(
        default=None,
        description="Bounded budget caps when set",
    )
    scope: list["ObjectiveScopeServer"] = Field(
        min_length=1,
        max_length=MAX_SCOPE_SERVERS,
        description="The explicit operator-owned server scope",
    )


class ObjectiveScopeServer(BaseModel):
    """One explicit in-scope server (§3.2; operator-owned set member)."""

    model_config = ConfigDict(extra="forbid")

    server_id: uuid.UUID = Field(description="In-scope server identity")
    added_by_actor_kind: ActorKind = Field(
        description="Actor provenance of the scope admission",
    )
    added_at: object = Field(description="RFC3339 UTC admission timestamp")


class ObjectiveRunLink(BaseModel):
    """One run ↔ objective binding (§3.3; the v2-handler CHECK is the
    durable 'no in-flight legacy run becomes v2' guard)."""

    model_config = ConfigDict(extra="forbid")

    objective_id: uuid.UUID = Field(description="Owning objective identity")
    run_id: uuid.UUID = Field(description="Bound durable run identity")
    parent_run_id: uuid.UUID | None = Field(
        default=None,
        description="Parent delegated run when this binding is a child",
    )
    initiating_server_id: uuid.UUID = Field(
        description="The run's one initiating server (a scope member at submit)",
    )
    handler: Literal["agent.run_v2"] = Field(
        description="The v2 run handler identity (legacy handlers are rejected)",
    )
    actor_kind: ActorKind = Field(
        description="Actor provenance of the binding",
    )
    created_at: object = Field(description="RFC3339 UTC binding timestamp")


# ---------------------------------------------------------------------------
# ledger row + bounded pages (§5.2)
# ---------------------------------------------------------------------------


class ObjectiveEvent(BaseModel):
    """One canonical ``objective.*`` ledger row (C05 rule 4 envelope shape
    at objective scope, plus the §4 actor-kind provenance field)."""

    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID = Field(description="Opaque stable event identity")
    objective_id: uuid.UUID = Field(description="Owning objective identity")
    sequence: int = Field(
        ge=1,
        description="Objective-local monotonic sequence number",
    )
    ts: object = Field(description="RFC3339 UTC event timestamp")
    type: str = Field(
        min_length=1,
        max_length=100,
        description="Dotted-slug event type inside the objective.* family",
    )
    actor_kind: ActorKind = Field(description="Actor provenance (§4)")
    v: int = Field(
        ge=1,
        description="Payload schema version",
    )
    data: dict[str, Any] = Field(
        description="Value-free projected payload (never secrets, never prose)",
    )
    run_id: uuid.UUID | None = Field(
        default=None,
        description="Producing run when the event originated inside a run",
    )


class ObjectiveEventPage(PageEnvelope):
    """One deterministic ledger page ordered by ``sequence`` with the
    same-filter ``total`` and derived ``has_more`` (§5.2)."""

    model_config = ConfigDict(extra="forbid")

    objective_id: uuid.UUID = Field(description="Owning objective identity")
    items: list[ObjectiveEvent] = Field(
        default_factory=list,
        description="Ledger rows in deterministic sequence order",
    )

    @field_validator("items")
    @classmethod
    def _strictly_ordered(cls, items: list[ObjectiveEvent]) -> list[ObjectiveEvent]:
        sequences = [item.sequence for item in items]
        if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
            raise ValueError("ledger page must be strictly sequence-ordered")
        return items


class ObjectiveListPage(PageEnvelope):
    """One deterministic objective list page (safe summaries only)."""

    model_config = ConfigDict(extra="forbid")

    items: list[ObjectiveSummary] = Field(
        default_factory=list,
        description="Objective summaries in deterministic order",
    )


class ObjectiveRunPage(PageEnvelope):
    """One deterministic objective-run linkage page."""

    model_config = ConfigDict(extra="forbid")

    objective_id: uuid.UUID = Field(description="Owning objective identity")
    items: list[ObjectiveRunLink] = Field(
        default_factory=list,
        description="Run bindings in deterministic order",
    )


# ---------------------------------------------------------------------------
# advisory checklist (§8.2)
# ---------------------------------------------------------------------------


class ChecklistItem(BaseModel):
    """One advisory checklist entry — never authority, never evidence."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        min_length=1,
        max_length=CHECKLIST_ITEM_MAX_CHARS,
        description="Projected checklist item text (advisory)",
    )
    done: bool = Field(
        default=False,
        description="Advisory tick; grants no authority and completes nothing",
    )


class ChecklistDocument(BaseModel):
    """The bounded advisory checklist projection with its CAS revision."""

    model_config = ConfigDict(extra="forbid")

    objective_id: uuid.UUID = Field(description="Owning objective identity")
    items: list[ChecklistItem] = Field(
        max_length=MAX_CHECKLIST_ITEMS,
        description="The full advisory checklist",
    )
    revision: int = Field(ge=1, description="Checklist CAS revision")
    updated_at: object = Field(description="RFC3339 UTC last-update timestamp")


# ---------------------------------------------------------------------------
# trusted journal records + projection (§8)
# ---------------------------------------------------------------------------


class JournalActionApplied(BaseModel):
    """One trusted action attempt (§8.1): sanitized arguments summary,
    dispatcher decision, operation reference."""

    model_config = ConfigDict(extra="forbid")

    action_id: uuid.UUID = Field(description="Opaque action-attempt identity")
    capability_id: str = Field(
        min_length=1,
        max_length=200,
        description="Dispatched capability identity",
    )
    server_id: uuid.UUID = Field(
        description="The effect's exactly-one explicit target server",
    )
    decision: str = Field(
        min_length=1,
        max_length=64,
        description="Dispatcher decision vocabulary value",
    )
    operation_id: str | None = Field(
        default=None,
        max_length=128,
        description="Operation reference when the effect was attempted",
    )
    detail: str | None = Field(
        default=None,
        max_length=EVENT_TEXT_MAX_CHARS,
        description="Projected sanitized argument summary when carried",
    )


class JournalReceiptRecorded(BaseModel):
    """One trusted dispatcher receipt reference (§8.1)."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(
        min_length=1,
        max_length=128,
        description="Operation identity the receipt closes",
    )
    outcome: str = Field(
        min_length=1,
        max_length=64,
        description="Receipt outcome vocabulary value",
    )
    effect_class: str = Field(
        min_length=1,
        max_length=64,
        description="Effect class vocabulary value",
    )
    server_id: uuid.UUID = Field(
        description="The effect's exactly-one explicit target server",
    )


class JournalVerificationRecorded(BaseModel):
    """One trusted verification record (§8.3): the evidence-bound verdict,
    with the honest ``missing_evidence`` downgrade flag."""

    model_config = ConfigDict(extra="forbid")

    verdict: ObjectiveCompletionVerdict = Field(
        description="The evidence-bound completion verdict",
    )
    missing_evidence: bool = Field(
        description="True when evidence discipline downgraded the claim",
    )


class JournalUnknownNoted(BaseModel):
    """One unknown/unreconciled effect record (§9: unknowns stay explicit)."""

    model_config = ConfigDict(extra="forbid")

    effect_ref: str = Field(
        min_length=1,
        max_length=200,
        description="Stable reference of the uncertain effect",
    )
    server_id: uuid.UUID | None = Field(
        default=None,
        description="Target server when classified enough to name one",
    )
    detail: str | None = Field(
        default=None,
        max_length=EVENT_TEXT_MAX_CHARS,
        description="Projected bounded detail when carried",
    )
    reconciled: bool = Field(
        default=False,
        description="True once explicit classification resolved the effect",
    )


class JournalChildLinked(BaseModel):
    """One delegation link (§8.1): parent/child linkage + bounded budget."""

    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID = Field(description="Child run identity")
    parent_run_id: uuid.UUID = Field(description="Parent run identity")
    server_ids: list[uuid.UUID] = Field(
        min_length=1,
        max_length=MAX_SCOPE_SERVERS,
        description="The child's explicit server subset",
    )
    budget: ObjectiveBudget | None = Field(
        default=None,
        description="The child's bounded budget when set",
    )


JournalMutation = Annotated[
    JournalActionApplied
    | JournalReceiptRecorded
    | JournalVerificationRecorded
    | JournalUnknownNoted
    | JournalChildLinked,
    Field(discriminator=None),
]


class WorkJournalChecklist(BaseModel):
    """The checklist section of the derived journal (§8.2): advisory,
    with an honest malformed marker that never erases other sections."""

    model_config = ConfigDict(extra="forbid")

    items: list[ChecklistItem] = Field(
        default_factory=list,
        max_length=MAX_CHECKLIST_ITEMS,
        description="The last valid advisory checklist seen",
    )
    revision: int | None = Field(
        default=None,
        ge=1,
        description="Checklist revision of the last valid update",
    )
    malformed: bool = Field(
        default=False,
        description="True when a checklist update failed its typed parse",
    )
    detail: str | None = Field(
        default=None,
        max_length=200,
        description="Value-free reason when malformed",
    )


class WorkJournal(BaseModel):
    """The derived work-journal projection (§8.1): trusted records only —
    verbatim goal/constraints live on the objective row and are referenced,
    never re-owned here. ``derived_seq`` is the exact ledger cursor this
    projection was derived at, so crash/restart catch-up is honest."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = Field(
        default=1,
        description="Projection document schema version",
    )
    objective_id: uuid.UUID = Field(description="Owning objective identity")
    derived_seq: int = Field(
        default=0,
        ge=0,
        description="Ledger sequence this projection was derived through",
    )
    checklist: WorkJournalChecklist = Field(
        default_factory=WorkJournalChecklist,
        description="Advisory checklist section",
    )
    actions: list[JournalActionApplied] = Field(
        default_factory=list,
        description="Action attempts, oldest first (bounded, drop-oldest)",
    )
    actions_truncated: bool = Field(
        default=False,
        description="True when old action rows were dropped at the bound",
    )
    receipts: list[JournalReceiptRecorded] = Field(
        default_factory=list,
        description="Receipts, oldest first (bounded, drop-oldest)",
    )
    receipts_truncated: bool = Field(
        default=False,
        description="True when old receipt rows were dropped at the bound",
    )
    verification: JournalVerificationRecorded | None = Field(
        default=None,
        description="Latest verification verdict when any",
    )
    unknowns: list[JournalUnknownNoted] = Field(
        default_factory=list,
        description="Unknown/unreconciled effects (bounded, drop-oldest)",
    )
    unknowns_truncated: bool = Field(
        default=False,
        description="True when old unknown rows were dropped at the bound",
    )
    children: list[JournalChildLinked] = Field(
        default_factory=list,
        description="Delegation links (bounded, drop-oldest)",
    )
    children_truncated: bool = Field(
        default=False,
        description="True when old child links were dropped at the bound",
    )
    status: ObjectiveStatus = Field(
        default=ObjectiveStatus.ACTIVE,
        description="Objective status as derived from the ledger",
    )


@dataclass(frozen=True, slots=True)
class JournalFoldResult:
    """Outcome of folding ledger events into the projection."""

    journal: WorkJournal
    #: Value-free bookkeeping markers for events that failed their typed
    #: parse or were unknown — visible trouble, never a failure (§8.2).
    issues: tuple[str, ...]


def fold_journal(
    state: WorkJournal | None, events: Sequence[ObjectiveEvent]
) -> JournalFoldResult:
    """Fold ``objective.*`` ledger events into the journal projection.

    Pure and deterministic: replaying the same events from an empty state
    yields exactly the same projection. Trusted-record sections are only
    ever appended or replaced — a malformed advisory checklist or a
    malformed journal payload CANNOT erase recorded effects, and an
    unknown in-family type degrades to a visible marker (§8.2, AC3).
    """
    journal = (
        state.model_copy(deep=True)
        if state is not None
        else WorkJournal(objective_id=uuid.UUID(int=0))
    )
    issues: list[str] = []
    for event in events:
        payload, issue = parse_objective_event_payload(event.type, event.data)
        if issue is not None:
            if event.type == OBJECTIVE_CHECKLIST_UPDATED_EVENT_TYPE:
                # Malformed advisory checklist: honest marker, prior items
                # stay visible, NOTHING else in the projection changes.
                journal = journal.model_copy(
                    update={
                        "checklist": journal.checklist.model_copy(
                            update={
                                "malformed": True,
                                "detail": issue[:200],
                            }
                        )
                    }
                )
            else:
                issues.append(f"seq {event.sequence}: {issue}")
            continue
        if payload is None:
            issues.append(f"seq {event.sequence}: unknown event type")
            continue
        journal = _apply_payload(journal, event, payload)
    return JournalFoldResult(journal=journal, issues=tuple(issues))


def _apply_payload(
    journal: WorkJournal, event: ObjectiveEvent, payload: BaseModel
) -> WorkJournal:
    journal = journal.model_copy(
        update={
            "objective_id": event.objective_id,
            "derived_seq": event.sequence,
        }
    )
    if isinstance(payload, ObjectiveCreatedPayload):
        return journal
    if isinstance(payload, ObjectiveScopeChangedPayload):
        return journal
    if isinstance(payload, ObjectiveStatusChangedPayload):
        return journal.model_copy(update={"status": payload.to_status})
    if isinstance(payload, ObjectiveChecklistUpdatedPayload):
        return journal.model_copy(
            update={
                "checklist": WorkJournalChecklist(
                    items=payload.items,
                    revision=payload.checklist_revision,
                    malformed=False,
                    detail=None,
                )
            }
        )
    if isinstance(payload, ObjectiveCompletedPayload):
        verdict = JournalVerificationRecorded(
            verdict=ObjectiveCompletionVerdict(
                objective_id=event.objective_id,
                base_revision=payload.revision,
                idempotency_key=payload.idempotency_key,
                task_outcome=payload.task_outcome,
                execution_status=payload.execution_status,
                domain_outcome=payload.domain_outcome,
                effect_certainty=payload.effect_certainty,
                error_code=payload.error_code,
            ),
            missing_evidence=payload.missing_evidence,
        )
        return journal.model_copy(
            update={
                "status": ObjectiveStatus.COMPLETED,
                "verification": verdict,
            }
        )
    if isinstance(payload, ObjectiveJournalUpdatedPayload):
        return _apply_journal_mutation(journal, payload)
    return journal


def _apply_journal_mutation(
    journal: WorkJournal, payload: ObjectiveJournalUpdatedPayload
) -> WorkJournal:
    update: dict[str, Any] = {}
    if payload.kind == "action" and payload.action is not None:
        actions = [
            action
            for action in journal.actions
            if action.action_id != payload.action.action_id  # type: ignore[union-attr]
        ]
        actions.append(payload.action)
        truncated = journal.actions_truncated or len(actions) > MAX_JOURNAL_ACTIONS
        update = {
            "actions": actions[-MAX_JOURNAL_ACTIONS:],
            "actions_truncated": truncated,
        }
    elif payload.kind == "receipt" and payload.receipt is not None:
        receipts = [
            receipt
            for receipt in journal.receipts
            if receipt.operation_id != payload.receipt.operation_id  # type: ignore[union-attr]
        ]
        receipts.append(payload.receipt)
        truncated = (
            journal.receipts_truncated or len(receipts) > MAX_JOURNAL_RECEIPTS
        )
        update = {
            "receipts": receipts[-MAX_JOURNAL_RECEIPTS:],
            "receipts_truncated": truncated,
        }
    elif payload.kind == "verification" and payload.verification is not None:
        update = {"verification": payload.verification}
    elif payload.kind == "unknown" and payload.unknown is not None:
        unknowns = [
            note
            for note in journal.unknowns
            if note.effect_ref != payload.unknown.effect_ref  # type: ignore[union-attr]
        ]
        unknowns.append(payload.unknown)
        truncated = (
            journal.unknowns_truncated or len(unknowns) > MAX_JOURNAL_UNKNOWNS
        )
        update = {
            "unknowns": unknowns[-MAX_JOURNAL_UNKNOWNS:],
            "unknowns_truncated": truncated,
        }
    elif payload.kind == "child" and payload.child is not None:
        children = [
            child
            for child in journal.children
            if child.run_id != payload.child.run_id  # type: ignore[union-attr]
        ]
        children.append(payload.child)
        truncated = (
            journal.children_truncated or len(children) > MAX_JOURNAL_CHILDREN
        )
        update = {
            "children": children[-MAX_JOURNAL_CHILDREN:],
            "children_truncated": truncated,
        }
    return journal.model_copy(update=update)


# pydantic forward references (payload models reference each other)
ObjectiveCreatedPayload.model_rebuild()
ObjectiveChecklistUpdatedPayload.model_rebuild()
ObjectiveJournalUpdatedPayload.model_rebuild()
