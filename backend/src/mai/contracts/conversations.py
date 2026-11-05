"""Conversation identity, intake and pagination vocabulary (frozen).

Campaign contract producer (``01-contracts``; act_CONTRACTS §1–§5, ratified):
this module owns the conversation-domain contract vocabulary that persistence
(04/11), the chat API producers (05/07/08) and runtime workers consume — no
sibling redesigns (act_CONTRACTS §11). Task-domain vocabulary (phases, plan
patches, evidence references) lives in :mod:`mai.contracts.tasks`; this module
imports it — the dependency is one-directional.

Governing rules:

- **Identity (act_CONTRACTS §1)** — ``conversation_id`` is a stable UUID for a
  chat, independent of its last run; ``message_id`` is a UUID plus a
  conversation-local monotonic sequence; assistant/tool roles can only be
  created through trusted producer paths (the intake models below carry no
  writable role field at all). One writer/executor lease per conversation;
  stale requests surface structured 409 vocabulary, never last-writer-wins.
- **Submission (§3)** — a turn submit carries ``client_request_id``
  (idempotency): same key + same content deduplicates, same key + changed
  content is the stable ``client_request_conflict``. Dispositions map onto
  ``201 created | 200 deduplicated | 202 queued_followup``.
- **Intake (§5)** — ``TurnInterpretation`` is the checkpointed discriminated
  union the planner emits once per accepted user turn:
  ``answer | clarify | plan | continue_task | new_task | blocked``. The
  schema contains NO writable ``approved``/``grants``/``execute_now``/
  ``bypass_approval`` fields — those are injected read-only facts from
  trusted services; ``extra="forbid"`` makes them unrepresentable.
- **Privacy (C05 resolution, brief AC 3)** — transcript content is bounded,
  redaction-flagged and may be carried as a safe ``content_ref`` instead of
  raw text; raw-secret exclusion happens before persistence or model
  forwarding, so these contracts never gain a field that could carry
  credential values. See ``docs/architecture/agent-task-continuity.md``.
- **Pagination (C01 rule 5; AGENTS.md invariant)** — every collection view is
  a bounded page envelope: ``DEFAULT_PAGE_LIMIT = 50``, ``MAX_PAGE_LIMIT =
  200``, and ``limit|offset|total|has_more`` on every page.

Stable error codes (brief ``01-contracts``): ``conversation_busy``,
``client_request_conflict``, ``conversation_not_found``,
``history_reconstruction_incomplete`` (plus the task-domain codes from
:mod:`mai.contracts.tasks`).

The module is pure: no process environment, filesystem, network or clock
access — validation is SSR/worker/replay safe and never leaks ambient secret
material.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Final
from typing import Literal as PyLiteral

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from pydantic_core import ValidationError as PydanticValidationError

# D01 extraction: the neutral tag/pagination vocabulary now lives in
# :mod:`mai.contracts.tags`; this module re-imports it so every existing
# ``mai.contracts.conversations`` caller keeps working until D09 deletes this
# module. The re-exports are the same objects, never copies.
from mai.contracts.tags import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    EntityTagPreviewView,
    compute_has_more,
)
from mai.contracts.tasks import (
    MAX_EVIDENCE_REFS,
    SUMMARY_MAX_CHARS,
    TITLE_MAX_CHARS,
    AcceptanceCriterion,
    ConstraintOrigin,
    ConstraintProposal,
    EvidenceRef,
    OpenQuestion,
    Phase,
    TaskContractError,
    TaskErrorCode,
    TaskFieldError,
    TaskPlanPatch,
    constraint_proposal_errors,
    validate_task_plan_patch,
)

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_MESSAGE_CHARS",
    "MAX_PAGE_LIMIT",
    "SCHEMA_VERSION",
    "BlockingReason",
    "ConversationContractError",
    "ConversationDetail",
    "ConversationErrorCode",
    "ConversationSteerRequest",
    "ConversationSummary",
    "ConversationTranscriptPage",
    "ConversationTurnSubmitRequest",
    "ConversationTurnSubmitResponse",
    "DISPOSITION_HTTP_STATUS",
    "EntityTagPreviewView",
    "FORBIDDEN_AUTHORITY_FIELDS",
    "HistoryCompleteness",
    "MessageRange",
    "MessageRole",
    "MessageSourceKind",
    "NextActionKind",
    "PageEnvelope",
    "SteerAction",
    "TranscriptMessage",
    "TurnDisposition",
    "TurnInterpretation",
    "TurnKind",
    "TurnValidationError",
    "assert_conversation_scope",
    "assert_history_available",
    "assert_writer_admission",
    "Assumption",
    "AnswerTurn",
    "BlockedTurn",
    "classify_retransmission",
    "ClarifyTurn",
    "compute_has_more",
    "content_fingerprint",
    "ContinueTaskTurn",
    "NewTaskTurn",
    "parse_turn_interpretation",
    "PlanTurn",
    "ScopeChangeProposal",
    "TurnNextAction",
    "validate_turn_references",
]

#: Payload schema version produced by this freeze (fails closed on drift).
SCHEMA_VERSION: Final[int] = 1

# NOTE: DEFAULT_PAGE_LIMIT and MAX_PAGE_LIMIT are re-exported from
# :mod:`mai.contracts.tags` (D01 neutral extraction) — see the import block
# above.

# --- intake bounds (ratified; additions require campaign reconciliation) ------

#: Understanding/reason bound for bounded model output (§5 "short").
UNDERSTANDING_MAX_CHARS: Final[int] = 1000

#: Direct answer text bound (§5 ``answer`` variant).
ANSWER_MAX_CHARS: Final[int] = 4000

#: Bounded open-question list (§5 "bounded list with stable proposal IDs").
MAX_OPEN_QUESTIONS: Final[int] = 8

#: Facts/assumptions bounds (§5: facts are sourced refs; assumptions are
#: explicitly unverified and kept separate).
MAX_FACTS: Final[int] = 16
MAX_ASSUMPTIONS: Final[int] = 8
ASSUMPTION_MAX_CHARS: Final[int] = 500

#: Scope-change proposal bounds (§5; UI-reviewable additions/removals).
SCOPE_CHANGE_MAX_ITEMS: Final[int] = 8
SCOPE_ITEM_MAX_CHARS: Final[int] = 500

#: Transcript message bound. Generous by design: scenario C3 exercises
#: histories beyond the former 15,000-character assembly cap, so the
#: per-message contract bound must not sit below realistic user turns;
#: truncation remains explicit at the context-assembly layer, never here.
MAX_MESSAGE_CHARS: Final[int] = 20_000

#: Safe out-of-band content reference bound (C05 privacy resolution).
CONTENT_REF_MAX_CHARS: Final[int] = 512

#: Profile/model reference bound on submission (§3 ``profile``).
PROFILE_MAX_CHARS: Final[int] = 128

#: Authority-shaped fields that can never appear anywhere in the intake
#: schema; ``extra="forbid"`` rejects them at every nesting level (§5).
FORBIDDEN_AUTHORITY_FIELDS: Final[frozenset[str]] = frozenset(
    {"approved", "grants", "execute_now", "bypass_approval"}
)


# --- frozen vocabularies ------------------------------------------------------


class MessageRole(StrEnum):
    """Frozen message roles (§1). Intake payloads cannot forge a role:
    assistant/tool rows are created only through trusted producer paths."""

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


class MessageSourceKind(StrEnum):
    """Where a message came from (§1 source kind; §4 legacy material)."""

    BROWSER = "browser"
    API = "api"
    LEGACY_IMPORT = "legacy_import"
    SYSTEM = "system"


class HistoryCompleteness(StrEnum):
    """History-completeness marker (§3/§4): ``partial|unavailable`` must be
    shown, never papered over with fabricated turns."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class TurnDisposition(StrEnum):
    """Run disposition of an accepted turn submit (§3; brief: 201/200/202)."""

    CREATED = "created"
    DEDUPLICATED = "deduplicated"
    QUEUED_FOLLOWUP = "queued_followup"


#: HTTP status each disposition maps to (brief AC 2: 201 | 200 | 202).
DISPOSITION_HTTP_STATUS: Final[Mapping[TurnDisposition, int]] = MappingProxyType(
    {
        TurnDisposition.CREATED: 201,
        TurnDisposition.DEDUPLICATED: 200,
        TurnDisposition.QUEUED_FOLLOWUP: 202,
    }
)


class SteerAction(StrEnum):
    """Deterministic control actions (§3): pause/cancel never wait for a
    planner; natural-language steering is interpreted at the next safe
    boundary."""

    STEER = "steer"
    PAUSE = "pause"
    CANCEL = "cancel"


class TurnKind(StrEnum):
    """Discriminated intake union variants (brief AC 1; act_CONTRACTS §5)."""

    ANSWER = "answer"
    CLARIFY = "clarify"
    PLAN = "plan"
    CONTINUE_TASK = "continue_task"
    NEW_TASK = "new_task"
    BLOCKED = "blocked"


class NextActionKind(StrEnum):
    """What the runtime should do next (§5; never executable code)."""

    ASK_USER = "ask_user"
    COLLECT_EVIDENCE = "collect_evidence"
    EXECUTE_PENDING = "execute_pending"
    WAIT = "wait"


class BlockingReason(StrEnum):
    """Why the turn is blocked (§5 ``blocked`` variant)."""

    MISSING_PREREQUISITE = "missing_prerequisite"
    MISSING_AUTHORITY = "missing_authority"
    AMBIGUOUS_SCOPE = "ambiguous_scope"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"


class ConversationErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    CONVERSATION_BUSY = "conversation_busy"
    CLIENT_REQUEST_CONFLICT = "client_request_conflict"
    CONVERSATION_NOT_FOUND = "conversation_not_found"
    HISTORY_RECONSTRUCTION_INCOMPLETE = "history_reconstruction_incomplete"


#: Discriminator values, used to strip the union tag from error field paths.
_TURN_TAG_VALUES: Final[frozenset[str]] = frozenset(
    kind.value for kind in TurnKind
)


# --- rejection plumbing (value-free errors; C01 rule 3) -----------------------


@dataclass(frozen=True, slots=True)
class ConversationFieldError:
    """One rejection: field path + stable code + value-free reason."""

    code: str
    field: str
    detail: str


class ConversationContractError(ValueError):
    """All rejections for one conversation-scope contract check.

    ``message``/``str`` contain only field paths, codes and fixed reasons —
    never message content or identifiers of other scopes — so the exception
    is safe to log, store and return in a browser-facing error body.
    """

    def __init__(self, errors: Collection[ConversationFieldError]) -> None:
        self.errors: tuple[ConversationFieldError, ...] = tuple(errors)
        summary = ", ".join(f"{e.field}: {e.code}" for e in self.errors)
        super().__init__(f"conversation contract rejected ({summary})")

    @property
    def fields(self) -> dict[str, str]:
        """Field path → stable code, ready for the C01 envelope ``fields``."""
        return {e.field: e.code for e in self.errors}


class TurnValidationError(ValueError):
    """Pydantic rejection of one ``TurnInterpretation`` payload, reduced to a
    value-free ``field path → error type`` mapping (no provider prose, no
    payload echoes — safe for display and logging)."""

    def __init__(self, fields: Mapping[str, str]) -> None:
        self.fields: dict[str, str] = dict(fields)
        summary = ", ".join(f"{loc}: {kind}" for loc, kind in self.fields.items())
        super().__init__(f"turn interpretation rejected ({summary})")

    @classmethod
    def from_pydantic(cls, exc: PydanticValidationError) -> "TurnValidationError":
        fields: dict[str, str] = {}
        for error in exc.errors():
            parts = [str(part) for part in error["loc"]]
            # Drop the leading union discriminator tag so consumers see the
            # variant-relative field path ("grants", not "answer.grants").
            if len(parts) > 1 and parts[0] in _TURN_TAG_VALUES:
                parts = parts[1:]
            loc = ".".join(parts) or "<root>"
            fields.setdefault(loc, str(error["type"]))
        return cls(fields)


# --- transcript and pagination envelopes -------------------------------------


class TranscriptMessage(BaseModel):
    """One role-preserving sanitized transcript row (§1/§3).

    Content may be carried as a safe ``content_ref`` instead of raw text
    (C05 privacy resolution); ``redacted`` marks deliberately redacted rows,
    for which no textual-fidelity claim is made.
    """

    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID = Field(description="Owning conversation identity")
    message_id: uuid.UUID = Field(description="Opaque stable message identity")
    sequence: int = Field(
        ge=1,
        description="Conversation-local monotonic sequence number",
    )
    role: MessageRole = Field(description="Trusted producer-assigned role")
    source_kind: MessageSourceKind = Field(description="Origin of the message")
    created_at: datetime = Field(description="RFC3339 UTC message timestamp")
    content: str = Field(
        default="",
        max_length=MAX_MESSAGE_CHARS,
        description="Sanitized message text when carried inline",
    )
    redacted: bool = Field(
        default=False,
        description="True when content was deliberately redacted (no fidelity claim)",
    )
    content_ref: str | None = Field(
        default=None,
        max_length=CONTENT_REF_MAX_CHARS,
        description="Safe out-of-band content reference instead of raw text",
    )


class PageEnvelope(BaseModel):
    """Bounded page metadata every collection view carries (C01 rule 5;
    AGENTS.md mandatory-pagination invariant)."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(
        ge=1,
        le=MAX_PAGE_LIMIT,
        description="Page size used for this slice (1..200)",
    )
    offset: int = Field(ge=0, description="Slice start within the collection")
    total: int = Field(ge=0, description="Pre-slice matching count")
    has_more: bool = Field(
        description="True when another page exists after this slice",
    )


class ConversationTranscriptPage(PageEnvelope):
    """One role-preserving sanitized message page (§3), ordered by
    ``(sequence, message_id)`` with the same-filter ``total``."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID = Field(description="Owning conversation identity")
    history_completeness: HistoryCompleteness = Field(
        default=HistoryCompleteness.COMPLETE,
        description="History-completeness marker for the loaded range",
    )
    items: list[TranscriptMessage] = Field(
        default_factory=list,
        description="Message rows in deterministic sequence order",
    )

    @model_validator(mode="after")
    def _page_is_consistent(self) -> "ConversationTranscriptPage":
        if len(self.items) > self.limit:
            raise ValueError("page carries more items than its limit")
        sequences = [item.sequence for item in self.items]
        if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
            raise ValueError("transcript page must be strictly sequence-ordered")
        if self.has_more != (self.offset + len(self.items) < self.total):
            raise ValueError("has_more must equal offset + len(items) < total")
        return self


# NOTE: compute_has_more is re-exported from :mod:`mai.contracts.tags`
# (D01 neutral extraction) — see the import block above.


# --- conversation header views (§3/§4) ----------------------------------------


class MessageRange(BaseModel):
    """A missing transcript range shown when history is not complete (§4)."""

    model_config = ConfigDict(extra="forbid")

    from_sequence: int = Field(ge=1, description="First missing sequence")
    to_sequence: int = Field(ge=1, description="Last missing sequence (inclusive)")

    @model_validator(mode="after")
    def _range_is_ordered(self) -> "MessageRange":
        if self.to_sequence < self.from_sequence:
            raise ValueError("missing range must satisfy to_sequence >= from_sequence")
        return self


# NOTE: EntityTagPreviewView is re-exported from :mod:`mai.contracts.tags`
# (D01 neutral extraction) — see the import block above.


class ConversationSummary(BaseModel):
    """Safe metadata row of one conversation (§3 ``GET`` list page item).

    No transcript content: summaries stay safe for generic list surfaces.
    """

    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID = Field(description="Opaque stable conversation identity")
    server_id: uuid.UUID = Field(description="Server scope pinning this conversation")
    title: str | None = Field(
        default=None,
        max_length=TITLE_MAX_CHARS,
        description="User-assigned conversation title, when set",
    )
    created_at: datetime = Field(description="RFC3339 UTC creation timestamp")
    updated_at: datetime = Field(description="RFC3339 UTC last-activity timestamp")
    message_count: int = Field(ge=0, description="Persisted message count")
    active_task_id: uuid.UUID | None = Field(
        default=None,
        description="Current active task episode, when one exists (§1)",
    )
    active_run_id: uuid.UUID | None = Field(
        default=None,
        description="Current durable run, when one is attached (§1)",
    )
    tag_preview: EntityTagPreviewView = Field(
        description="Bounded tag name preview plus honest total",
    )


class ConversationDetail(ConversationSummary):
    """Safe singleton header (§3 ``GET`` one): active task/run references plus
    the history-completeness marker and the missing range when incomplete."""

    model_config = ConfigDict(extra="forbid")

    history_completeness: HistoryCompleteness = Field(
        description="complete, partial or unavailable; never fabricated (§4)",
    )
    missing_range: MessageRange | None = Field(
        default=None,
        description="Missing transcript range when completeness is not complete",
    )


# --- submission and steering (§3) ---------------------------------------------


class ConversationTurnSubmitRequest(BaseModel):
    """One accepted user turn (§3 ``POST`` shape): idempotency key, explicit
    server scope, optional CAS revision and explicit task action."""

    model_config = ConfigDict(extra="forbid")

    server_id: uuid.UUID = Field(description="Explicit server scope (§1 rule 2)")
    client_request_id: uuid.UUID = Field(
        description="UI-authored idempotency key deduplicating retransmission",
    )
    message: str = Field(
        min_length=1,
        max_length=MAX_MESSAGE_CHARS,
        description="Safe user message text (raw-secret exclusion happens upstream)",
    )
    profile: str | None = Field(
        default=None,
        max_length=PROFILE_MAX_CHARS,
        description="Model profile reference for the next allowed call",
    )
    requested_phase: Phase | None = Field(
        default=None,
        description="Advisory requested phase; never permission (§5)",
    )
    expected_revision: int | None = Field(
        default=None,
        ge=1,
        description="Expected task revision for CAS; stale writes conflict",
    )
    task_action: PyLiteral["continue", "new_task"] | None = Field(
        default=None,
        description="Explicit episode action: continue or start a new task (§1)",
    )
    objective_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Runtime v2 objective episode (runtime-v2.md §3.3): when set, the "
            "turn selects the v2 handler explicitly and binds this durable run "
            "to the operator-owned objective; the initiating server must be an "
            "explicit scope member at submit time"
        ),
    )
    mode: PyLiteral["ask", "delegated"] | None = Field(
        default=None,
        description=(
            "Prompting mode within grants (§C03 rule 5): 'ask' prompts for an "
            "approval decision on non-read effects; omitted pins 'delegated' "
            "(the mode never widens any check)"
        ),
    )

    @field_validator("message")
    @classmethod
    def _message_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class ConversationTurnSubmitResponse(BaseModel):
    """Persisted turn identity plus run disposition (§3; brief AC 2)."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID = Field(description="Owning conversation identity")
    message_id: uuid.UUID = Field(description="Persisted message identity")
    task_id: uuid.UUID | None = Field(
        description="Task episode bound to the turn, when one exists",
    )
    run_id: uuid.UUID | None = Field(
        description="Durable run bound to the turn, when one started",
    )
    revision: int = Field(
        ge=1,
        description="Current task revision after the turn was admitted",
    )
    disposition: TurnDisposition = Field(
        description="created, deduplicated or queued_followup",
    )

    @property
    def http_status(self) -> int:
        """HTTP status this disposition maps to (201 | 200 | 202)."""
        return DISPOSITION_HTTP_STATUS[self.disposition]


class ConversationSteerRequest(BaseModel):
    """Deterministic control request (§3): pause/cancel carry no planner
    input; natural-language steering is interpreted at the next safe
    boundary and displayed as pending until applied."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID = Field(description="Target conversation identity")
    server_id: uuid.UUID = Field(description="Explicit server scope (§1 rule 2)")
    action: SteerAction = Field(description="steer, pause or cancel")
    message: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_MESSAGE_CHARS,
        description="Natural-language steering text; forbidden on pause/cancel",
    )
    client_request_id: uuid.UUID | None = Field(
        default=None,
        description="Idempotency key for repeated control submissions",
    )
    expected_revision: int | None = Field(
        default=None,
        ge=1,
        description="Expected task revision for CAS on steering, when relevant",
    )

    @model_validator(mode="after")
    def _action_message_shape(self) -> "ConversationSteerRequest":
        if self.action == SteerAction.STEER:
            if self.message is None or not self.message.strip():
                raise ValueError("steer action requires non-blank steering message")
        elif self.message is not None:
            raise ValueError(
                "pause/cancel are deterministic control requests and carry no message"
            )
        return self


# --- structured first-call intake (§5) -----------------------------------------


class Assumption(BaseModel):
    """One explicitly unverified assumption, kept separate from facts (§5)."""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(
        min_length=1,
        max_length=ASSUMPTION_MAX_CHARS,
        description="Assumption statement the plan must not treat as fact",
    )
    basis: str | None = Field(
        default=None,
        max_length=ASSUMPTION_MAX_CHARS,
        description="What the assumption was derived from, when known",
    )

    @field_validator("statement")
    @classmethod
    def _statement_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("statement must not be blank")
        return value


class TurnNextAction(BaseModel):
    """Declared next step of the turn (§5): a symbolic choice, never code."""

    model_config = ConfigDict(extra="forbid")

    kind: NextActionKind = Field(description="Symbolic next-action kind")
    step_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
        description="Plan step the action belongs to, when applicable",
    )
    detail: str = Field(
        min_length=1,
        max_length=500,
        description="Short human-readable description of the next action",
    )

    @field_validator("detail")
    @classmethod
    def _detail_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("detail must not be blank")
        return value


class ScopeChangeProposal(BaseModel):
    """Explicit scope additions/removals for UI review (§5)."""

    model_config = ConfigDict(extra="forbid")

    additions: list[str] = Field(
        default_factory=list,
        max_length=SCOPE_CHANGE_MAX_ITEMS,
        description="Proposed scope additions for user review",
    )
    removals: list[str] = Field(
        default_factory=list,
        max_length=SCOPE_CHANGE_MAX_ITEMS,
        description="Proposed scope removals for user review",
    )

    @field_validator("additions", "removals")
    @classmethod
    def _items_not_blank(cls, value: list[str]) -> list[str]:
        for item in value:
            if not item.strip():
                raise ValueError("scope change items must not be blank")
            if len(item) > SCOPE_ITEM_MAX_CHARS:
                raise ValueError(
                    f"scope change items are at most {SCOPE_ITEM_MAX_CHARS} chars"
                )
        return value


class _TurnBase(BaseModel):
    """Shared fields of every ``TurnInterpretation`` variant (§5)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(
        default=SCHEMA_VERSION,
        description="Intake schema version; consumers fail closed on drift",
    )
    source_message_id: uuid.UUID = Field(
        description="User message this interpretation was produced from",
    )
    expected_task_revision: int | None = Field(
        default=None,
        ge=1,
        description="Task revision the interpretation was produced against",
    )
    turn_kind: TurnKind = Field(description="Discriminator of the intake variant")
    understanding: str = Field(
        min_length=1,
        max_length=UNDERSTANDING_MAX_CHARS,
        description="Short user-facing understanding of the turn",
    )
    open_questions: list[OpenQuestion] = Field(
        default_factory=list,
        max_length=MAX_OPEN_QUESTIONS,
        description="Bounded questions with stable proposal IDs",
    )

    @field_validator("schema_version")
    @classmethod
    def _version_supported(cls, value: int) -> int:
        if value != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported intake schema version (supported: {SCHEMA_VERSION})"
            )
        return value

    @field_validator("understanding")
    @classmethod
    def _understanding_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("understanding must not be blank")
        return value


class AnswerTurn(_TurnBase):
    """Compact structured answer (§5, P1): no todo creation, no effects, no
    patch fields — the union shape makes them unrepresentable."""

    model_config = ConfigDict(extra="forbid")

    turn_kind: PyLiteral[TurnKind.ANSWER] = Field(
        description="Discriminator: compact self-contained answer",
    )
    answer_text: str = Field(
        min_length=1,
        max_length=ANSWER_MAX_CHARS,
        description="Answer text using only supplied context",
    )
    evidence_refs: list[EvidenceRef] = Field(
        default_factory=list,
        max_length=MAX_EVIDENCE_REFS,
        description="Optional sourced references backing the answer",
    )

    @field_validator("answer_text")
    @classmethod
    def _answer_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("answer_text must not be blank")
        return value


class ClarifyTurn(_TurnBase):
    """Ask the user (§5): requires at least one bounded open question."""

    model_config = ConfigDict(extra="forbid")

    turn_kind: PyLiteral[TurnKind.CLARIFY] = Field(
        description="Discriminator: clarification request",
    )

    @model_validator(mode="after")
    def _clarify_has_questions(self) -> "ClarifyTurn":
        if not self.open_questions:
            raise ValueError("a clarify turn requires at least one open question")
        return self


class PlanTurn(_TurnBase):
    """Full task proposal (§5): objective, criteria, constraints, bounded
    delta patch; ``requested_phase`` is advisory, never permission."""

    model_config = ConfigDict(extra="forbid")

    turn_kind: PyLiteral[TurnKind.PLAN] = Field(
        description="Discriminator: propose a task plan",
    )
    objective: str = Field(
        min_length=1,
        max_length=SUMMARY_MAX_CHARS,
        description="Proposed objective statement (≤ 1000 chars)",
    )
    acceptance_criteria: list[AcceptanceCriterion] = Field(
        min_length=1,
        max_length=12,
        description="Measurable acceptance criteria with stable IDs",
    )
    constraints_proposed: list[ConstraintProposal] = Field(
        default_factory=list,
        max_length=12,
        description="Proposed constraints with source anchors",
    )
    facts: list[EvidenceRef] = Field(
        default_factory=list,
        max_length=MAX_FACTS,
        description="Sourced observation references",
    )
    assumptions: list[Assumption] = Field(
        default_factory=list,
        max_length=MAX_ASSUMPTIONS,
        description="Explicitly unverified assumptions",
    )
    requested_phase: Phase = Field(description="Advisory phase; never permission")
    todo_patch: TaskPlanPatch = Field(
        description="Bounded plan delta (max 12 steps, ≤ 16 KiB)",
    )
    next_action: TurnNextAction | None = Field(
        default=None,
        description="Symbolic next action; never executable code",
    )
    scope_change_proposal: ScopeChangeProposal | None = Field(
        default=None,
        description="Explicit scope additions/removals for UI review",
    )

    @field_validator("objective")
    @classmethod
    def _objective_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("objective must not be blank")
        return value


class ContinueTaskTurn(_TurnBase):
    """Continuation of the current task (§5): an ordinary continuation
    carries ``todo_patch = null``; a carried patch is a plan EDIT against
    the current plan — never whole-list replacement, never a goal change,
    never a progress report (item status/verification is evidence-bound
    task state, never a model-ticked plan)."""

    model_config = ConfigDict(extra="forbid")

    turn_kind: PyLiteral[TurnKind.CONTINUE_TASK] = Field(
        description="Discriminator: continue the current task",
    )
    todo_patch: TaskPlanPatch | None = Field(
        default=None,
        description=(
            "Bounded plan EDIT against the current task plan, or null "
            "for an ordinary continuation that edits no plan items"
        ),
    )
    facts: list[EvidenceRef] = Field(
        default_factory=list,
        max_length=MAX_FACTS,
        description="New sourced observation references since the last turn",
    )
    assumptions: list[Assumption] = Field(
        default_factory=list,
        max_length=MAX_ASSUMPTIONS,
        description="Explicitly unverified assumptions",
    )
    next_action: TurnNextAction | None = Field(
        default=None,
        description="Symbolic next action; never executable code",
    )
    scope_change_proposal: ScopeChangeProposal | None = Field(
        default=None,
        description="Explicit scope additions/removals for UI review",
    )


class NewTaskTurn(_TurnBase):
    """Explicit new episode (§1/§5): a new goal episode, never a silent
    retarget of the old task; prior task stays historical."""

    model_config = ConfigDict(extra="forbid")

    turn_kind: PyLiteral[TurnKind.NEW_TASK] = Field(
        description="Discriminator: start a new task episode",
    )
    objective: str = Field(
        min_length=1,
        max_length=SUMMARY_MAX_CHARS,
        description="New episode objective statement (≤ 1000 chars)",
    )
    rationale: str = Field(
        min_length=1,
        max_length=SUMMARY_MAX_CHARS,
        description="Why this is a new episode rather than a continuation",
    )
    prior_task_id: uuid.UUID | None = Field(
        default=None,
        description="Historical task this episode follows, when any",
    )
    acceptance_criteria: list[AcceptanceCriterion] = Field(
        min_length=1,
        max_length=12,
        description="Measurable acceptance criteria with stable IDs",
    )
    constraints_proposed: list[ConstraintProposal] = Field(
        default_factory=list,
        max_length=12,
        description="Proposed constraints with source anchors",
    )
    facts: list[EvidenceRef] = Field(
        default_factory=list,
        max_length=MAX_FACTS,
        description="Sourced observation references",
    )
    assumptions: list[Assumption] = Field(
        default_factory=list,
        max_length=MAX_ASSUMPTIONS,
        description="Explicitly unverified assumptions",
    )
    requested_phase: Phase = Field(description="Advisory phase; never permission")
    todo_patch: TaskPlanPatch = Field(
        description="Initial bounded plan for the new episode",
    )
    next_action: TurnNextAction | None = Field(
        default=None,
        description="Symbolic next action; never executable code",
    )
    scope_change_proposal: ScopeChangeProposal | None = Field(
        default=None,
        description="Explicit scope additions/removals for UI review",
    )

    @field_validator("objective", "rationale")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class BlockedTurn(_TurnBase):
    """Blocked intake (§5/P4): the turn cannot proceed safely; no patch, no
    effects — the union shape makes them unrepresentable."""

    model_config = ConfigDict(extra="forbid")

    turn_kind: PyLiteral[TurnKind.BLOCKED] = Field(
        description="Discriminator: turn blocked from proceeding",
    )
    reason: str = Field(
        min_length=1,
        max_length=UNDERSTANDING_MAX_CHARS,
        description="Safe explanation of what blocks the turn",
    )
    blocked_kind: BlockingReason = Field(
        description="Stable blocking-reason vocabulary",
    )

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value


#: Checkpointed discriminated intake union (§5; brief AC 1): exactly these six
#: variants; anything else — including any authority field — is rejected.
TurnInterpretation = Annotated[
    AnswerTurn
    | ClarifyTurn
    | PlanTurn
    | ContinueTaskTurn
    | NewTaskTurn
    | BlockedTurn,
    Field(discriminator="turn_kind"),
]

_TURN_ADAPTER: Final[TypeAdapter[TurnInterpretation]] = TypeAdapter(TurnInterpretation)


def parse_turn_interpretation(payload: object) -> TurnInterpretation:
    """Validate one intake payload against the discriminated union.

    Raises :class:`TurnValidationError` carrying a value-free
    ``field path → error type`` mapping; unknown authority fields
    (``approved``/``grants``/``execute_now``/``bypass_approval``) are refused
    at any nesting depth via ``extra="forbid"``.
    """
    try:
        return _TURN_ADAPTER.validate_python(payload)
    except PydanticValidationError as exc:
        raise TurnValidationError.from_pydantic(exc) from None


def turn_evidence_refs(turn: TurnInterpretation) -> Iterator[tuple[str, EvidenceRef]]:
    """Yield ``(field_path, ref)`` for every evidence reference a variant
    carries — answer evidence, facts and patch step evidence alike."""
    if isinstance(turn, AnswerTurn):
        for index, ref in enumerate(turn.evidence_refs):
            yield f"evidence_refs[{index}]", ref
        return
    if isinstance(turn, (PlanTurn, ContinueTaskTurn, NewTaskTurn)):
        for index, ref in enumerate(turn.facts):
            yield f"facts[{index}]", ref
        patch = turn.todo_patch
        if patch is None:  # ordinary continue_task continuation
            return
        for op_index, operation in enumerate(patch.operations):
            for ref_index, ref in enumerate(operation.step.evidence_refs):
                yield (
                    f"todo_patch.operations[{op_index}].step.evidence_refs[{ref_index}]",
                    ref,
                )


def validate_turn_references(
    turn: TurnInterpretation,
    known_step_ids: Collection[str] | None = None,
    known_evidence_ids: Collection[str] | None = None,
    known_constraint_origins: Mapping[str, ConstraintOrigin | str] | None = None,
) -> None:
    """Deterministic reference/authority validation of one parsed turn (§6; P4).

    Fail-closed when live scope is provided (production passes the task's
    step IDs, evidence IDs and constraint origins):

    - evidence/source references must exist (hallucinated IDs are rejected);
    - the embedded patch passes :func:`validate_task_plan_patch`
      (duplicate keys, cycles, add-collisions, unknown refs);
    - constraint proposals never supersede a USER-origin constraint
      (§6; P4 "request to erase a user prohibition").

    Raises :class:`TaskContractError` (task-domain vocabulary — the codes are
    the ratified task codes plus field-level detail codes).
    """
    errors: list[TaskFieldError] = []
    known_evidence: set[str] | None = None
    if known_evidence_ids is not None:
        known_evidence = {str(evidence_id) for evidence_id in known_evidence_ids}
    if known_evidence is not None:
        for path, ref in turn_evidence_refs(turn):
            if str(ref.evidence_id) not in known_evidence:
                errors.append(
                    TaskFieldError(
                        TaskErrorCode.UNKNOWN_EVIDENCE_REF,
                        path,
                        "evidence reference does not exist in the task scope",
                    )
                )
    todo_patch: TaskPlanPatch | None = getattr(turn, "todo_patch", None)
    if todo_patch is not None:
        try:
            validate_task_plan_patch(todo_patch, known_step_ids)
        except TaskContractError as exc:
            errors.extend(
                TaskFieldError(error.code, f"todo_patch.{error.field}", error.detail)
                for error in exc.errors
            )
    constraints: tuple[ConstraintProposal, ...] = tuple(
        getattr(turn, "constraints_proposed", ()) or ()
    )
    if constraints:
        errors.extend(
            constraint_proposal_errors(constraints, known_constraint_origins)
        )
    if errors:
        raise TaskContractError(errors)


# --- deterministic submission/control checks (§1/§3/§4, scenarios C4/C6/C8) ---


def content_fingerprint(message: str) -> str:
    """Canonical content fingerprint for idempotent retransmission checks:
    same key + same fingerprint deduplicates; same key + a different
    fingerprint is a conflict (§1 rule 2)."""
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def classify_retransmission(
    submitted_fingerprint: str, stored_fingerprint: str
) -> TurnDisposition:
    """Classify a retransmitted submit (§1): same content deduplicates;
    changed content under the same idempotency key is the stable
    ``client_request_conflict``."""
    if submitted_fingerprint == stored_fingerprint:
        return TurnDisposition.DEDUPLICATED
    raise ConversationContractError(
        [
            ConversationFieldError(
                ConversationErrorCode.CLIENT_REQUEST_CONFLICT,
                "client_request_id",
                "idempotency key retransmitted with changed content",
            )
        ]
    )


def assert_conversation_scope(
    request_server_id: uuid.UUID, record_server_id: uuid.UUID
) -> None:
    """Cross-server scope guard (scenario C8): a mismatch fails CLOSED with
    ``conversation_not_found`` — existence of the other server's conversation
    never leaks through the error channel."""
    if request_server_id != record_server_id:
        raise ConversationContractError(
            [
                ConversationFieldError(
                    ConversationErrorCode.CONVERSATION_NOT_FOUND,
                    "server_id",
                    "no conversation in the requesting server scope",
                )
            ]
        )


def assert_writer_admission(
    lease_holder_run_id: uuid.UUID | None, *, follow_up: bool
) -> None:
    """One-writer lease admission (§1/§3): an exclusive new write cannot start
    while a run holds the conversation lease (``conversation_busy``); message
    follow-ups are admitted and queued against the active writer."""
    if lease_holder_run_id is not None and not follow_up:
        raise ConversationContractError(
            [
                ConversationFieldError(
                    ConversationErrorCode.CONVERSATION_BUSY,
                    "conversation_id",
                    "another writer holds the conversation lease",
                )
            ]
        )


def assert_history_available(
    completeness: HistoryCompleteness, *, user_confirmed: bool
) -> None:
    """Mutation gate over reconstructed history (§4): ``partial`` history
    needs explicit user scope confirmation; ``unavailable`` stays blocked
    without it. Never fabricate old turns to fill the gap."""
    if completeness == HistoryCompleteness.COMPLETE:
        return
    if completeness == HistoryCompleteness.PARTIAL and user_confirmed:
        return
    if completeness == HistoryCompleteness.UNAVAILABLE and user_confirmed:
        return
    raise ConversationContractError(
        [
            ConversationFieldError(
                ConversationErrorCode.HISTORY_RECONSTRUCTION_INCOMPLETE,
                "history_completeness",
                "history reconstruction is "
                f"{completeness.value}; explicit user scope confirmation is "
                "required before new mutations",
            )
        ]
    )


if __name__ == "__main__":  # pragma: no cover - manual smoke entrypoint
    import sys

    _sample = {
        "schema_version": SCHEMA_VERSION,
        "source_message_id": "7e6c1a34-9a2b-4c3d-8e5f-1029384756af",
        "turn_kind": "answer",
        "understanding": "greeting",
        "answer_text": "Hello!",
    }
    print(parse_turn_interpretation(_sample).model_dump_json())
    sys.exit(0)
