"""Session-first domain vocabulary: one session, subordinate runs (frozen).

Campaign contract producer (``machina-session-first-chat`` D01; sfc_PLAN.md
D01, sfc_INTENT.md, sfc_RESEARCH.md): this module owns the public agent
vocabulary of the session-first replacement — a **session** is the stable
identity and the only authoritative domain history; a **run** is a subordinate
execution child that never becomes a conversation identity of its own.

Governing rules:

- **Identity (sfc_INTENT §1)** — ``session_id`` owns scope, ordered domain
  entries and compaction metadata; ``run_id`` identifies one execution
  attempt. Reusing a ``run_id`` as a ``session_id`` (or as a parent identity)
  is rejected fail-closed (:func:`assert_session_not_run` and model
  validators). Server scope is explicit; a cross-server request fails closed
  with ``session_not_found``, never leaking cross-server existence
  (:func:`assert_session_scope`).
- **Bounded history (sfc_PLAN frozen parameters; AGENTS.md §1)** — the
  initial tail carries at most :data:`INITIAL_TAIL_MAX_ENTRIES` entries, an
  older/replay page at most :data:`HISTORY_PAGE_MAX_ENTRIES`, and a serialized
  page at most :data:`MAX_PAGE_BYTES` bytes with explicit
  ``truncated_by_bytes`` honesty. Entry text and tool previews carry their own
  bounds; large artifacts live behind references, never inline.
- **Typed entries, atomic tool groups (sfc_RESEARCH §2)** — the domain log is
  a versioned discriminated union (:class:`SessionEntryPayload`), not
  ``role + content``. An assistant tool-call message and all matching tool
  results form one atomic model-continuity group
  (:func:`validate_atomic_tool_group`); incomplete/pending groups stay
  visible with an honest badge but are excluded from native historical replay
  (:func:`partition_replayable_tool_groups`). Pending/unknown results are
  never inferred successful.
- **Authority (C01/C03)** — client admission/control request models carry no
  writable authority field: :data:`FORBIDDEN_AUTHORITY_FIELDS` is
  unrepresentable via ``extra="forbid"``. Approvals, grants and sequence
  allocation belong to trusted producers only.
- **Value-free errors (C01 rule 3)** — :class:`SessionContractError` carries
  field paths, stable codes and fixed reasons only; it is safe to log, store
  and return in a browser-facing envelope.

The module is pure: no process environment, filesystem, network or clock
access — validation is SSR/worker/replay safe and never leaks ambient secret
material.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from mai.contracts.tool_results import DomainOutcome, EffectCertainty, ExecutionStatus

__all__ = [
    "MAX_ENTRY_TEXT_CHARS",
    "MAX_PAGE_BYTES",
    "MAX_TOOL_OUTPUT_PREVIEW_CHARS",
    "HISTORY_PAGE_MAX_ENTRIES",
    "INITIAL_TAIL_MAX_ENTRIES",
    "SESSION_LIST_DEFAULT_LIMIT",
    "SESSION_LIST_MAX_LIMIT",
    "SCHEMA_VERSION",
    "FORBIDDEN_AUTHORITY_FIELDS",
    "AssistantMessageEntry",
    "CompactionEntry",
    "ControlActionKind",
    "ControlEntry",
    "DelegationEntry",
    "DelegationResult",
    "DelegationSpec",
    "DocumentAttachAdmissionRequest",
    "DocumentAttachRef",
    "EntryCursor",
    "EntryKind",
    "ProviderReasoningArtifact",
    "RunStateEntry",
    "RunbookRef",
    "RunbookSessionExecuteRequest",
    "SessionCompactionRecord",
    "SessionContractError",
    "SessionControlRequest",
    "SessionControlResponse",
    "SessionCreateRequest",
    "SessionDetail",
    "SessionEntryPayload",
    "SessionEntryView",
    "SessionErrorCode",
    "SessionFieldError",
    "SessionHistoryPage",
    "SessionListPage",
    "SessionRunRecord",
    "SessionRunState",
    "SessionSourceKind",
    "SessionStatus",
    "SessionSummary",
    "SessionTurnSubmitRequest",
    "SessionTurnSubmitResponse",
    "ToolCallItem",
    "ToolGroupPartition",
    "ToolResultEntry",
    "ToolStatus",
    "UserMessageEntry",
    "assert_session_not_run",
    "assert_session_scope",
    "partition_replayable_tool_groups",
    "validate_atomic_tool_group",
]

#: Payload schema version produced by this freeze. Consumers fail closed on
#: any other value (server-contract pattern): a newer producer must bump and
#: reconcile, not silently drift.
SCHEMA_VERSION: Final[int] = 1

# --- pagination and byte bounds (sfc_PLAN frozen parameters; AGENTS.md §1) -----

#: Newest entries returned by the initial open/snapshot window (hard cap).
INITIAL_TAIL_MAX_ENTRIES: Final[int] = 30

#: Older/replay page size cap (keyset ``before_seq`` pages).
HISTORY_PAGE_MAX_ENTRIES: Final[int] = 50

#: Default page size of the session list collection.
SESSION_LIST_DEFAULT_LIMIT: Final[int] = 20

#: Hard upper bound any session-list page may request.
SESSION_LIST_MAX_LIMIT: Final[int] = 100

#: Serialized page budget: every history page declares its byte size and a
#: page can never exceed the product-wide 64 KiB cap (dispatcher discipline).
MAX_PAGE_BYTES: Final[int] = 64 * 1024

#: Per-entry text bound (user/assistant message bodies).
MAX_ENTRY_TEXT_CHARS: Final[int] = 20_000

#: Tool output preview bound inside a tool-result entry.
MAX_TOOL_OUTPUT_PREVIEW_CHARS: Final[int] = 4_000

# --- module-owned string bounds (entry and control field lengths) --------------

#: Identity-shaped strings (call ids, tool/capability names, model profiles,
#: durable source ids).
MAX_IDENTITY_CHARS: Final[int] = 128

#: Content-hash strings (payload hashes; SHA-256 hex is 64 chars, bound is
#: deliberately larger so hash-algorithm bumps reconcile explicitly).
MAX_HASH_CHARS: Final[int] = 128

#: Safe out-of-band content/artifact references (C05 privacy resolution).
MAX_ARTIFACT_REF_CHARS: Final[int] = 512

#: Stable machine-readable reason/error codes.
MAX_REASON_CODE_CHARS: Final[int] = 64

#: Value-free error details (never echoing message contents or secrets).
MAX_ERROR_DETAIL_CHARS: Final[int] = 200

#: Run-state details and control notes.
MAX_DETAIL_CHARS: Final[int] = 500

#: Compaction summaries, delegation goals and bounded child result summaries.
MAX_SUMMARY_TEXT_CHARS: Final[int] = 4_000

#: Session titles.
MAX_TITLE_CHARS: Final[int] = 240

#: Tool calls per assistant message / provider artifacts per message.
MAX_TOOL_CALLS: Final[int] = 16
MAX_PROVIDER_ARTIFACTS: Final[int] = 8

#: Delegation collection bounds (allowed capabilities, explicit refs).
MAX_ALLOWED_CAPABILITIES: Final[int] = 16
MAX_CONTEXT_REFS: Final[int] = 16

#: Delegation execution budgets (child depth/fan-out caps live in the plan:
#: depth 1, max 3 children/parent, max 4 descendants/root).
MAX_DELEGATION_ROUNDS: Final[int] = 16
MAX_DELEGATION_TOOL_CALLS: Final[int] = 100
MAX_DELEGATION_TIMEOUT_SECONDS: Final[float] = 86_400.0

#: Authority-shaped fields that can never appear anywhere in a client
#: admission/control schema; ``extra="forbid"`` rejects them at every
#: nesting level (C01/C03; sfc_RESEARCH §2 "control events cannot become
#: user authority").
FORBIDDEN_AUTHORITY_FIELDS: Final[frozenset[str]] = frozenset(
    {"approved", "grants", "execute_now", "bypass_approval", "role", "seq", "is_system"}
)

# --- bounded string aliases -----------------------------------------------------

IdentityStr = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_IDENTITY_CHARS),
]

RefStr = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_ARTIFACT_REF_CHARS),
]

#: Frozen tool-call status vocabulary (declared as a literal mirror of the
#: ``mai.agents.tools.ToolStatus`` values so this pure contract module never
#: imports the PydanticAI-carrying bridge module; D03 reconciles any drift).
ToolStatus = Literal["executed", "denied", "approval_required", "unknown_effect"]


# --- frozen vocabularies --------------------------------------------------------


class SessionSourceKind(StrEnum):
    """How a session came into existence (sfc_INTENT §7 runbook/document
    decisions): interactive chat, runbook execution, document attach, API."""

    INTERACTIVE = "interactive"
    RUNBOOK = "runbook"
    DOCUMENT_ATTACH = "document_attach"
    API = "api"


class SessionStatus(StrEnum):
    """Coarse session-level status the browser/operator sees (derived by the
    trusted projection from run states; never written by clients)."""

    IDLE = "idle"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SessionRunState(StrEnum):
    """Frozen run lifecycle state (sfc_PLAN: honest pending/unknown states;
    ``outcome_unknown`` is honest after a crash — never silently terminal)."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_CHILD = "waiting_child"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"


class EntryKind(StrEnum):
    """Discriminated domain-entry kinds (sfc_RESEARCH §2 core entry kinds)."""

    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_RESULT = "tool_result"
    RUN_STATE = "run_state"
    DELEGATION = "delegation"
    CONTROL = "control"
    COMPACTION = "compaction"


class ControlActionKind(StrEnum):
    """Deterministic session control actions (sfc_RESEARCH §6 frame families;
    controls acknowledge — they are never interpreted as user authority)."""

    STEER = "steer"
    PAUSE = "pause"
    CANCEL = "cancel"
    APPROVE = "approve"
    DENY_APPROVAL = "deny_approval"
    RECONCILE = "reconcile"


class SessionErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    SESSION_NOT_FOUND = "session_not_found"
    SESSION_RUN_IDENTITY_CONFLICT = "session_run_identity_conflict"
    CROSS_SESSION_ENTRY = "cross_session_entry"
    CROSS_SCOPE_TOOL_GROUP = "cross_scope_tool_group"
    INVALID_TOOL_GROUP = "invalid_tool_group"
    INVALID_CURSOR = "invalid_cursor"
    INVALID_PAGINATION = "invalid_pagination"
    BYTE_BUDGET_EXCEEDED = "byte_budget_exceeded"
    FORBIDDEN_FIELD = "forbidden_field"


# --- rejection plumbing (server-contract pattern: value-free errors) -----------


@dataclass(frozen=True, slots=True)
class SessionFieldError:
    """One rejection: field path + stable code + value-free reason."""

    code: str
    field: str
    detail: str


class SessionContractError(ValueError):
    """All rejections for one session-scope contract check.

    ``message``/``str`` contain only field paths, codes and fixed reasons —
    never field values — so the exception is safe to log, store and return in
    a browser-facing error body (C01 rule 3).
    """

    def __init__(self, errors: Collection[SessionFieldError]) -> None:
        self.errors: tuple[SessionFieldError, ...] = tuple(errors)
        summary = ", ".join(f"{e.field}: {e.code}" for e in self.errors)
        super().__init__(f"session contract rejected ({summary})")

    @property
    def fields(self) -> dict[str, str]:
        """Field path → stable code, ready for the C01 envelope ``fields``."""
        return {e.field: e.code for e in self.errors}


# --- deterministic identity/scope checks ----------------------------------------


def assert_session_not_run(session_id: uuid.UUID, run_id: uuid.UUID) -> None:
    """Session/run identity guard (sfc_INTENT §1): a ``run_id`` is a
    subordinate execution child and may never be reused as the ``session_id``
    conversation identity.

    Raises :class:`SessionContractError` with code
    ``session_run_identity_conflict``.
    """
    if session_id == run_id:
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.SESSION_RUN_IDENTITY_CONFLICT,
                    "session_id",
                    "session identity may never reuse a run id; runs are "
                    "subordinate execution children",
                )
            ]
        )


def assert_session_scope(
    request_server_id: uuid.UUID, record_server_id: uuid.UUID
) -> None:
    """Cross-server scope guard (sfc_RESEARCH security analysis): a mismatch
    fails CLOSED with ``session_not_found`` — the same value-free code whether
    the session exists in another scope or not, so cross-server existence is
    never leaked.

    Raises :class:`SessionContractError` with code ``session_not_found``.
    """
    if request_server_id != record_server_id:
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.SESSION_NOT_FOUND,
                    "server_id",
                    "no session with this identity in the requesting server scope",
                )
            ]
        )


# --- shared reference models -----------------------------------------------------


class DocumentAttachRef(BaseModel):
    """Provenance reference of a document-attach admission (sfc_INTENT §8):
    the excerpt text travels as bounded untrusted user content; this ref
    carries only the source identity and a safe out-of-band artifact
    reference — never inline document bytes."""

    model_config = ConfigDict(extra="forbid")

    document_id: uuid.UUID = Field(
        description="Opaque stable document identity the excerpt came from",
    )
    artifact_ref: RefStr = Field(
        description="Safe out-of-band reference to the stored excerpt artifact",
    )


class RunbookRef(BaseModel):
    """Provenance reference of a runbook-session admission (sfc_INTENT §7):
    runbook execution is a fresh non-interactive session carrying immutable
    service/runbook references."""

    model_config = ConfigDict(extra="forbid")

    service_id: IdentityStr = Field(
        description="Opaque adopted-service identity owning the runbook",
    )
    runbook_id: IdentityStr = Field(
        description="Stable runbook identity inside the adopted service",
    )


class ToolCallItem(BaseModel):
    """One structured assistant tool-call part (sfc_RESEARCH §2 assistant
    entries carry ordered safe text and structured tool-call parts)."""

    model_config = ConfigDict(extra="forbid")

    call_id: IdentityStr = Field(
        description="Provider tool-call id; pairs the call with its result",
    )
    tool_name: IdentityStr = Field(
        description="Registered capability tool name that was invoked",
    )
    capability_id: IdentityStr = Field(
        description="Capability descriptor id behind the invoked tool name",
    )
    args: dict[str, Any] = Field(
        description="Bounded tool-call arguments as produced by the model",
    )
    target_server_id: uuid.UUID | None = Field(
        default=None,
        description="Explicit target server scope for multi-target capabilities",
    )

    @model_validator(mode="after")
    def _args_are_bounded(self) -> "ToolCallItem":
        if len(json.dumps(self.args)) > MAX_ENTRY_TEXT_CHARS:
            raise ValueError(
                "tool-call arguments exceed the bounded entry payload budget"
            )
        return self


class ProviderReasoningArtifact(BaseModel):
    """Explicit safe provider reasoning artifact (sfc_INTENT clarification 5:
    raw unvalidated chain-of-thought is never authoritative history; only
    provider-supported summary/signature/opaque references are carried, and
    only when actually required and reviewed)."""

    model_config = ConfigDict(extra="forbid")

    provider: IdentityStr = Field(
        description="Provider/model family the artifact is compatible with",
    )
    artifact_kind: Literal["summary", "signature", "opaque_ref"] = Field(
        description="Artifact class: bounded summary, provider signature or "
        "opaque reference token",
    )
    content: IdentityStr = Field(
        description="Bounded artifact payload or reference token",
    )
    redacted: bool = Field(
        default=False,
        description="True when content was deliberately redacted (no fidelity claim)",
    )


# --- domain entry payloads (discriminated union on ``kind``) ---------------------


class UserMessageEntry(BaseModel):
    """One admitted user turn (sfc_RESEARCH §2): bounded safe text, client
    request id for idempotency, source metadata and optional provenance
    references."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[EntryKind.USER_MESSAGE] = Field(
        description="Discriminator: this payload is a user message entry",
    )
    text: str = Field(
        max_length=MAX_ENTRY_TEXT_CHARS,
        description="Bounded user message text (untrusted content, never authority)",
    )
    client_request_id: uuid.UUID = Field(
        description="Client idempotency key of the admitted turn submission",
    )
    source: SessionSourceKind = Field(
        description="How this turn entered the session (interactive, runbook, "
        "document attach or API)",
    )
    document_ref: DocumentAttachRef | None = Field(
        default=None,
        description="Document provenance when the turn was a document attach",
    )
    runbook_ref: RunbookRef | None = Field(
        default=None,
        description="Runbook provenance when the turn was a runbook execution",
    )


class AssistantMessageEntry(BaseModel):
    """One assistant model response (sfc_RESEARCH §2): bounded safe text
    and/or ordered structured tool-call parts, plus the model metadata needed
    for native continuation and explicit provider reasoning artifacts."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[EntryKind.ASSISTANT_MESSAGE] = Field(
        description="Discriminator: this payload is an assistant message entry",
    )
    text: str = Field(
        default="",
        max_length=MAX_ENTRY_TEXT_CHARS,
        description="Bounded assistant text (empty when the round was pure tool use)",
    )
    tool_calls: list[ToolCallItem] = Field(
        default_factory=list,
        max_length=MAX_TOOL_CALLS,
        description="Ordered structured tool-call parts of this assistant round",
    )
    model_profile: IdentityStr = Field(
        description="Model profile identity that produced this assistant round",
    )
    provider_artifacts: list[ProviderReasoningArtifact] = Field(
        default_factory=list,
        max_length=MAX_PROVIDER_ARTIFACTS,
        description="Explicit safe provider reasoning artifacts (never raw "
        "chain-of-thought)",
    )
    tool_group_state: Literal["none", "pending", "complete", "unresolved"] = Field(
        default="none",
        description="Atomic tool-group status: none, pending, complete or unresolved",
    )

    @model_validator(mode="after")
    def _entry_is_meaningful(self) -> "AssistantMessageEntry":
        if not self.text.strip() and not self.tool_calls:
            raise ValueError(
                "assistant entry must carry non-empty text or at least one tool call"
            )
        call_ids = [call.call_id for call in self.tool_calls]
        if len(set(call_ids)) != len(call_ids):
            raise ValueError("tool_calls carry duplicate call_id values")
        if not self.tool_calls and self.tool_group_state != "none":
            raise ValueError(
                "tool_group_state must be 'none' when there are no tool calls"
            )
        return self


class ToolResultEntry(BaseModel):
    """One tool result (sfc_RESEARCH §2): call identity, the frozen bridge
    status and the three-layer execution/domain/certainty projection —
    pending/unknown results are never inferred successful (C03/C06)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[EntryKind.TOOL_RESULT] = Field(
        description="Discriminator: this payload is a tool result entry",
    )
    call_id: IdentityStr = Field(
        description="Provider tool-call id this result answers",
    )
    tool_name: IdentityStr = Field(
        description="Registered capability tool name that was invoked",
    )
    capability_id: IdentityStr = Field(
        description="Capability descriptor id behind the invoked tool name",
    )
    status: ToolStatus = Field(
        description="Bridge lifecycle status: executed, denied, approval_required "
        "or unknown_effect",
    )
    execution_status: ExecutionStatus = Field(
        description="Compact technical wrapper execution status of the attempt",
    )
    domain_outcome: DomainOutcome = Field(
        description="Domain-level outcome; unknown is honest for arbitrary work",
    )
    effect_certainty: EffectCertainty = Field(
        description="How certain the effect evidence is (certain/uncertain/unknown)",
    )
    payload_hash: IdentityStr = Field(
        description="Canonical payload hash binding the result to its approval/"
        "dispatch envelope",
    )
    output_preview: str | None = Field(
        default=None,
        max_length=MAX_TOOL_OUTPUT_PREVIEW_CHARS,
        description="Bounded redacted output preview; large output lives behind "
        "artifact references",
    )
    artifact_ref: RefStr | None = Field(
        default=None,
        description="Safe out-of-band reference to the full stored tool result",
    )
    error_code: IdentityStr | None = Field(
        default=None,
        description="Stable machine-readable error code when the call failed",
    )
    error_detail: str | None = Field(
        default=None,
        max_length=MAX_ERROR_DETAIL_CHARS,
        description="Value-free fixed failure reason (never echoing contents)",
    )
    approval_id: uuid.UUID | None = Field(
        default=None,
        description="Approval record id when the call parked on the approval seam",
    )


class RunStateEntry(BaseModel):
    """One deterministic run-state transition (sfc_RESEARCH §2: execution
    status and safe reason linked to a run — never model prose)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[EntryKind.RUN_STATE] = Field(
        description="Discriminator: this payload is a run-state transition",
    )
    state: SessionRunState = Field(
        description="New frozen run lifecycle state after this transition",
    )
    reason_code: IdentityStr | None = Field(
        default=None,
        description="Stable machine-readable transition reason code when present",
    )
    detail: str | None = Field(
        default=None,
        max_length=MAX_DETAIL_CHARS,
        description="Value-free bounded transition detail for operators",
    )


class DelegationEntry(BaseModel):
    """One delegation relation (sfc_RESEARCH §4): child linkage, bounded
    goal/status and explicit allowed capabilities — the child receives a
    bounded context, never ambient parent state."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[EntryKind.DELEGATION] = Field(
        description="Discriminator: this payload is a delegation entry",
    )
    child_run_id: uuid.UUID = Field(
        description="Run identity of the delegated child execution",
    )
    child_session_id: uuid.UUID | None = Field(
        default=None,
        description="Session identity of the child when it owns a distinct session",
    )
    goal: str = Field(
        min_length=1,
        max_length=MAX_SUMMARY_TEXT_CHARS,
        description="Bounded explicit goal handed to the child",
    )
    status: SessionRunState = Field(
        description="Current frozen lifecycle state of the child run",
    )
    allowed_capabilities: list[IdentityStr] = Field(
        max_length=MAX_ALLOWED_CAPABILITIES,
        description="Explicit narrowed capability grants of the child execution",
    )
    result_summary: str | None = Field(
        default=None,
        max_length=MAX_SUMMARY_TEXT_CHARS,
        description="Bounded immutable child result summary once the child settles",
    )
    error_code: IdentityStr | None = Field(
        default=None,
        description="Stable machine-readable delegation error code when refused",
    )

    @model_validator(mode="after")
    def _child_identity_is_distinct(self) -> "DelegationEntry":
        assert_session_not_run(self.child_session_id, self.child_run_id)
        return self


class ControlEntry(BaseModel):
    """One acknowledged control action (sfc_RESEARCH §2: approval/cancel/
    reconcile/steer acknowledgements — never interpreted as user authority)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[EntryKind.CONTROL] = Field(
        description="Discriminator: this payload is a control acknowledgement",
    )
    action: ControlActionKind = Field(
        description="Deterministic control action that was acknowledged",
    )
    client_request_id: uuid.UUID = Field(
        description="Client idempotency key of the control request",
    )
    target_run_id: uuid.UUID | None = Field(
        default=None,
        description="Run the control action targeted, when scoped to one run",
    )
    approval_id: uuid.UUID | None = Field(
        default=None,
        description="Approval record id when the action decided an approval",
    )
    note: str | None = Field(
        default=None,
        max_length=MAX_DETAIL_CHARS,
        description="Value-free bounded operator note carried with the action",
    )


class CompactionEntry(BaseModel):
    """One compaction marker (sfc_RESEARCH §5: metadata/pointer, not a hidden
    user message — originals stay stored and addressable; ``session_id``
    never rotates)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[EntryKind.COMPACTION] = Field(
        description="Discriminator: this payload is a compaction marker",
    )
    compaction_id: uuid.UUID = Field(
        description="Stable identity of this compaction generation",
    )
    from_seq: int = Field(
        ge=1,
        description="First covered session sequence number (inclusive)",
    )
    to_seq: int = Field(
        ge=1,
        description="Last covered session sequence number (inclusive)",
    )
    summary_text: str = Field(
        min_length=1,
        max_length=MAX_SUMMARY_TEXT_CHARS,
        description="Bounded summary of the covered entry range with provenance",
    )
    model_profile: IdentityStr = Field(
        description="Model profile identity the summary was produced for",
    )
    token_usage_anchor: int | None = Field(
        default=None,
        ge=0,
        description="Actual provider usage anchor of the summarized source range",
    )

    @model_validator(mode="after")
    def _range_is_ordered(self) -> "CompactionEntry":
        if self.to_seq < self.from_seq:
            raise ValueError("compaction range must satisfy to_seq >= from_seq")
        return self


#: Checkpointed discriminated domain-entry union (sfc_RESEARCH §2): exactly
#: these seven variants; anything else — including any authority field — is
#: rejected by ``extra="forbid"``.
SessionEntryPayload = Annotated[
    UserMessageEntry
    | AssistantMessageEntry
    | ToolResultEntry
    | RunStateEntry
    | DelegationEntry
    | ControlEntry
    | CompactionEntry,
    Field(discriminator="kind"),
]


class SessionEntryView(BaseModel):
    """One committed domain entry as projected to every consumer (sfc_PLAN
    D01): versioned envelope identity plus the discriminated payload. The
    trusted session writer is the sole producer; clients never assign
    ``seq`` or entry identity."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(
        default=SCHEMA_VERSION,
        description="Contract schema version of this entry envelope",
    )
    entry_id: uuid.UUID = Field(
        description="Stable domain-entry identity inside its session",
    )
    session_id: uuid.UUID = Field(
        description="Owning session identity (the only conversation root)",
    )
    seq: int = Field(
        ge=1,
        description="Session-local monotonic sequence number of this entry",
    )
    run_id: uuid.UUID | None = Field(
        default=None,
        description="Subordinate run that produced this entry, when run-scoped",
    )
    durable_source_id: IdentityStr = Field(
        description="Stable producer source id backing idempotent re-appends",
    )
    created_at: datetime = Field(
        description="UTC commit timestamp of this entry",
    )
    payload: SessionEntryPayload = Field(
        description="Discriminated domain payload (union discriminated on `kind`)",
    )

    @model_validator(mode="after")
    def _identity_and_version(self) -> "SessionEntryView":
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported entry schema version {self.schema_version!r}; "
                f"this contract freezes version {SCHEMA_VERSION}"
            )
        if self.run_id is not None:
            assert_session_not_run(self.session_id, self.run_id)
        return self


# --- pagination envelopes (keyset model; AGENTS.md §1 + sfc_PLAN bounds) --------


class EntryCursor(BaseModel):
    """Opaque keyset cursor (sfc_RESEARCH §8): ``(seq, entry_id)`` positional
    identity; ``entry_id`` disambiguates gaps left by deleted originals."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(
        ge=1,
        description="Session-local sequence number the cursor points at",
    )
    entry_id: uuid.UUID | None = Field(
        default=None,
        description="Entry identity at ``seq`` when pinned, else sequence-only",
    )


class SessionHistoryPage(BaseModel):
    """One bounded, byte-capped history page (sfc_PLAN D01 acceptance 3):
    chronological items with honest continuation metadata — count bounds,
    byte bounds and cursor consistency all fail closed."""

    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID = Field(
        description="Owning session identity (the only conversation root)",
    )
    items: list[SessionEntryView] = Field(
        default_factory=list,
        max_length=HISTORY_PAGE_MAX_ENTRIES,
        description="Committed entries in strict chronological sequence order",
    )
    limit: int = Field(
        ge=1,
        le=HISTORY_PAGE_MAX_ENTRIES,
        description=f"Page size used for this slice (1..{HISTORY_PAGE_MAX_ENTRIES})",
    )
    total: int = Field(
        ge=0,
        description="Pre-slice matching count of the whole session history",
    )
    has_more: bool = Field(
        description="True when newer committed entries exist above this page",
    )
    has_older: bool = Field(
        description="True when older committed entries exist below this page",
    )
    oldest_seq: int | None = Field(
        default=None,
        ge=1,
        description="Lowest sequence number carried by this page, when non-empty",
    )
    latest_seq: int | None = Field(
        default=None,
        ge=1,
        description="Highest sequence number carried by this page, when non-empty",
    )
    next_before_seq: int | None = Field(
        default=None,
        ge=1,
        description="Exclusive keyset cursor for the next older page, when "
        "``has_older``",
    )
    byte_size: int = Field(
        ge=0,
        le=MAX_PAGE_BYTES,
        description="Declared serialized size of ``items`` in bytes",
    )
    truncated_by_bytes: bool = Field(
        default=False,
        description="True when the byte budget cut items before the count limit",
    )

    @model_validator(mode="after")
    def _page_is_consistent(self) -> "SessionHistoryPage":
        if len(self.items) > self.limit:
            raise ValueError("page carries more items than its limit")
        seqs = [item.seq for item in self.items]
        if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
            raise ValueError("history page must be strictly sequence-ordered")
        if self.items:
            if self.oldest_seq != seqs[0] or self.latest_seq != seqs[-1]:
                raise ValueError(
                    "oldest_seq/latest_seq must bound the carried items"
                )
        elif self.oldest_seq is not None or self.latest_seq is not None:
            raise ValueError("empty page cannot declare oldest_seq/latest_seq")
        if self.has_older:
            if not self.items:
                raise ValueError("has_older requires a non-empty page")
            if self.next_before_seq != self.oldest_seq:
                raise ValueError(
                    "next_before_seq must equal oldest_seq when older pages exist"
                )
        elif self.next_before_seq is not None:
            raise ValueError("next_before_seq requires has_older")
        if self.has_more and not self.items:
            raise ValueError("has_more requires a non-empty page")
        return self


class SessionSummary(BaseModel):
    """Safe metadata row of one session (list page item): identity, scope,
    source, coarse status — never transcript content."""

    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID = Field(
        description="Stable session identity (the only conversation root)",
    )
    server_id: uuid.UUID = Field(
        description="Server scope pinning this session",
    )
    source: SessionSourceKind = Field(
        description="How the session came into existence",
    )
    status: SessionStatus = Field(
        description="Coarse session status projected from its runs",
    )
    title: str | None = Field(
        default=None,
        max_length=MAX_TITLE_CHARS,
        description="User-assigned session title, when set",
    )
    active_run_id: uuid.UUID | None = Field(
        default=None,
        description="Subordinate run currently executing, when one exists",
    )
    created_at: datetime = Field(
        description="UTC creation timestamp of the session",
    )
    updated_at: datetime = Field(
        description="UTC timestamp of the latest committed change",
    )


class SessionDetail(SessionSummary):
    """Safe singleton session header: summary metadata plus bounded history
    pointers (no transcript content)."""

    model_config = ConfigDict(extra="forbid")

    latest_seq: int | None = Field(
        default=None,
        ge=1,
        description="Highest committed entry sequence, when any history exists",
    )
    total_entries: int = Field(
        default=0,
        ge=0,
        description="Total committed domain entries in the session",
    )
    compaction_count: int = Field(
        default=0,
        ge=0,
        description="Number of compaction generations recorded for the session",
    )


class SessionListPage(BaseModel):
    """Bounded session list page (C01 rule 5; AGENTS.md mandatory-pagination
    invariant) with the canonical ``limit|offset|total|has_more`` envelope."""

    model_config = ConfigDict(extra="forbid")

    items: list[SessionSummary] = Field(
        default_factory=list,
        max_length=SESSION_LIST_MAX_LIMIT,
        description="Session summary rows in deterministic order",
    )
    limit: int = Field(
        ge=1,
        le=SESSION_LIST_MAX_LIMIT,
        description=f"Page size used for this slice (1..{SESSION_LIST_MAX_LIMIT})",
    )
    offset: int = Field(
        ge=0,
        description="Slice start within the collection",
    )
    total: int = Field(
        ge=0,
        description="Pre-slice matching count of the whole collection",
    )
    has_more: bool = Field(
        description="True when another page exists after this slice",
    )

    @model_validator(mode="after")
    def _page_is_consistent(self) -> "SessionListPage":
        if len(self.items) > self.limit:
            raise ValueError("page carries more items than its limit")
        if self.has_more != (self.offset + len(self.items) < self.total):
            raise ValueError("has_more must equal offset + len(items) < total")
        return self


# --- atomic tool-group validation (sfc_RESEARCH §2) ------------------------------


def _reject_inadmissible_tool_calls(
    assistant: AssistantMessageEntry, *, session_server_id: uuid.UUID
) -> None:
    """Fail-closed metadata checks shared by the atomic-group validators.

    Duplicate call ids are malformed tool-group metadata; a call targeting
    another server is a cross-scope tool group. Both raise immediately —
    incomplete replay state never excuses them.
    """
    call_ids = [call.call_id for call in assistant.tool_calls]
    if len(set(call_ids)) != len(call_ids):
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.INVALID_TOOL_GROUP,
                    "tool_calls",
                    "assistant tool group carries duplicate call_id values",
                )
            ]
        )
    for call in assistant.tool_calls:
        if (
            call.target_server_id is not None
            and call.target_server_id != session_server_id
        ):
            raise SessionContractError(
                [
                    SessionFieldError(
                        SessionErrorCode.CROSS_SCOPE_TOOL_GROUP,
                        "tool_calls",
                        "tool group targets a server outside the session scope",
                    )
                ]
            )


def validate_atomic_tool_group(
    assistant_entry: SessionEntryView,
    tool_result_entries: Sequence[SessionEntryView],
    *,
    session_server_id: uuid.UUID,
) -> tuple[str, ...]:
    """Validate one atomic native model tool group (sfc_PLAN D01 acceptance 2).

    The assistant entry must carry tool calls, and ``tool_result_entries``
    must answer **exactly** that call set: same session, same run, one result
    per call, no unmatched results, no duplicates, and no cross-scope target.
    Structural failures raise ``invalid_tool_group``; a ``ToolCallItem``
    targeting another server raises ``cross_scope_tool_group`` (fail-closed,
    never silently replayed).

    Returns the matched call ids in assistant tool-call order.

    Raises:
        SessionContractError: on malformed/unmatched/duplicate/cross-session
            or cross-scope tool calls or results.
    """
    assistant = assistant_entry.payload
    if not isinstance(assistant, AssistantMessageEntry) or not assistant.tool_calls:
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.INVALID_TOOL_GROUP,
                    "assistant_entry",
                    "tool groups start from an assistant entry carrying tool calls",
                )
            ]
        )
    _reject_inadmissible_tool_calls(assistant, session_server_id=session_server_id)
    expected_ids = [call.call_id for call in assistant.tool_calls]

    errors: list[SessionFieldError] = []
    seen_ids: set[str] = set()
    for result_view in tool_result_entries:
        if result_view.session_id != assistant_entry.session_id:
            errors.append(
                SessionFieldError(
                    SessionErrorCode.CROSS_SESSION_ENTRY,
                    "session_id",
                    "tool group entries must share one session identity",
                )
            )
            continue
        if result_view.run_id != assistant_entry.run_id:
            errors.append(
                SessionFieldError(
                    SessionErrorCode.INVALID_TOOL_GROUP,
                    "run_id",
                    "tool results must belong to the assistant entry's run",
                )
            )
        result = result_view.payload
        if not isinstance(result, ToolResultEntry):
            errors.append(
                SessionFieldError(
                    SessionErrorCode.INVALID_TOOL_GROUP,
                    "payload.kind",
                    "tool groups close with tool result entries only",
                )
            )
            continue
        if result.call_id not in set(expected_ids):
            errors.append(
                SessionFieldError(
                    SessionErrorCode.INVALID_TOOL_GROUP,
                    "call_id",
                    "tool result answers no call of this assistant group",
                )
            )
        if result.call_id in seen_ids:
            errors.append(
                SessionFieldError(
                    SessionErrorCode.INVALID_TOOL_GROUP,
                    "call_id",
                    "tool group carries a duplicate result for one call",
                )
            )
        seen_ids.add(result.call_id)
    missing = [call_id for call_id in expected_ids if call_id not in seen_ids]
    for call_id in missing:
        errors.append(
            SessionFieldError(
                SessionErrorCode.INVALID_TOOL_GROUP,
                "call_id",
                "assistant tool call has no matching tool result",
            )
        )
    if errors:
        raise SessionContractError(errors)
    return tuple(expected_ids)


@dataclass(frozen=True, slots=True)
class ToolGroupPartition:
    """Deterministic partition of an entry window for native replay
    (sfc_PLAN D01): complete groups stay replayable; incomplete groups stay
    visible as marked pending entries but are excluded from native
    historical replay — safe assistant text is never hidden, and pending
    results are never inferred successful."""

    #: Entries that may enter native model replay: non-tool-group traffic
    #: (user/run/control/compaction/delegation), text-only assistant entries
    #: and complete, validated tool groups — in chronological order.
    replayable: tuple[SessionEntryView, ...]
    #: Incomplete/pending tool-group members: assistant entries marked
    #: ``tool_group_state="pending"|"unresolved"`` plus their partial or
    #: orphan results. The UI renders their safe text with an honest badge.
    pending: tuple[SessionEntryView, ...]


def partition_replayable_tool_groups(
    entries: Sequence[SessionEntryView],
    *,
    session_server_id: uuid.UUID,
) -> ToolGroupPartition:
    """Split one same-session entry window into replayable and pending parts.

    Walks ``entries`` chronologically. Complete assistant tool groups are
    validated with :func:`validate_atomic_tool_group` (malformed metadata and
    cross-scope targets fail closed) and kept replayable. A group interrupted
    by a non-tool-result entry or closed before every call is answered stays
    **visible but excluded**: the assistant copy is marked
    ``tool_group_state="pending"`` (no results yet) or ``"unresolved"``
    (partial/orphan results), and the partial results are excluded too. An
    orphan ``ToolResultEntry`` — no preceding matching assistant call group
    in this window (R01 O1: straddling a window boundary or corrupted) — is
    likewise NEVER replayable: it is classified into ``pending`` as
    unresolved so a corrupt window can never yield an unmatched
    ``ToolReturnPart`` in native model history.

    Lifecycle transparency (D04): ``RunStateEntry`` / ``ControlEntry`` /
    ``DelegationEntry`` / ``CompactionEntry`` records written BETWEEN an
    assistant call and its results (e.g. the dual-authority
    ``waiting_approval``/``running`` transitions of an approval park) are
    part of the group's own lifecycle — they never split the group and are
    replayed in their chronological positions. Only a ``UserMessageEntry``
    (or another assistant entry) splits an incomplete group.

    Raises:
        SessionContractError: on entries spanning multiple sessions
            (``cross_session_entry``), malformed tool-group metadata or
            cross-scope targets (``invalid_tool_group`` /
            ``cross_scope_tool_group``).
    """
    if not entries:
        return ToolGroupPartition(replayable=(), pending=())
    session_id = entries[0].session_id
    for entry in entries:
        if entry.session_id != session_id:
            raise SessionContractError(
                [
                    SessionFieldError(
                        SessionErrorCode.CROSS_SESSION_ENTRY,
                        "session_id",
                        "replay window must carry one session identity",
                    )
                ]
            )

    replayable: list[SessionEntryView] = []
    pending: list[SessionEntryView] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        payload = entry.payload
        if isinstance(payload, AssistantMessageEntry) and payload.tool_calls:
            # Fail closed on malformed metadata and cross-scope targets even
            # for groups that would otherwise only be marked pending.
            _reject_inadmissible_tool_calls(
                payload, session_server_id=session_server_id
            )
            expected_ids = {call.call_id for call in payload.tool_calls}
            matched: list[SessionEntryView] = []
            walk = index + 1
            while (
                walk < len(entries)
                and {r.payload.call_id for r in matched if isinstance(
                    r.payload, ToolResultEntry
                )} != expected_ids
            ):
                candidate = entries[walk]
                if isinstance(candidate.payload, ToolResultEntry):
                    matched.append(candidate)
                    walk += 1
                    continue
                if isinstance(
                    candidate.payload,
                    (RunStateEntry, ControlEntry, DelegationEntry, CompactionEntry),
                ):
                    # Lifecycle transparency (D04): the group's own park/
                    # resume transitions and domain records never split it.
                    walk += 1
                    continue
                break  # a user/assistant entry splits the atomic group
            complete = (
                {r.payload.call_id for r in matched if isinstance(
                    r.payload, ToolResultEntry
                )} == expected_ids
            )
            if complete:
                validate_atomic_tool_group(
                    entry, matched, session_server_id=session_server_id
                )
                replayable.append(entry)
                replayable.extend(matched)
                index = walk
                continue
            answered = {
                r.payload.call_id
                for r in matched
                if isinstance(r.payload, ToolResultEntry)
            }
            marked_state: Literal["pending", "unresolved"] = (
                "unresolved" if answered else "pending"
            )
            marked_payload = payload.model_copy(
                update={"tool_group_state": marked_state}
            )
            pending.append(entry.model_copy(update={"payload": marked_payload}))
            pending.extend(matched)
            index = walk if matched else index + 1
            continue
        if isinstance(payload, ToolResultEntry):
            # R01 O1 (pinned by tests): a tool result with NO preceding
            # matching assistant call group in this window — straddling a
            # window boundary or corrupted — is an orphan. It is NEVER
            # replayable (an unmatched ToolReturnPart would poison the
            # native model request); it stays visible as unresolved.
            pending.append(entry)
            index += 1
            continue
        replayable.append(entry)
        index += 1
    return ToolGroupPartition(replayable=tuple(replayable), pending=tuple(pending))


# --- admission, run, compaction and delegation contracts -------------------------


class SessionCreateRequest(BaseModel):
    """One idempotent session creation request (sfc_RESEARCH §1: admitting a
    turn allocates a run against the session; creation carries no authority
    field — ``extra="forbid"`` makes :data:`FORBIDDEN_AUTHORITY_FIELDS`
    unrepresentable)."""

    model_config = ConfigDict(extra="forbid")

    server_id: uuid.UUID = Field(
        description="Server scope the new session is pinned to",
    )
    source: SessionSourceKind = Field(
        default=SessionSourceKind.INTERACTIVE,
        description="How the session came into existence",
    )
    title: str | None = Field(
        default=None,
        max_length=MAX_TITLE_CHARS,
        description="Optional user-assigned session title",
    )
    model_profile: IdentityStr | None = Field(
        default=None,
        description="Preferred model profile identity for the session runs",
    )
    client_request_id: uuid.UUID = Field(
        description="Client idempotency key; retries return the same admission",
    )


class SessionTurnSubmitRequest(BaseModel):
    """One accepted user turn submission (sfc_RESEARCH §1: every accepted
    turn allocates a new run id and appends exactly one user entry before
    model work; same idempotency key + same content deduplicates)."""

    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID = Field(
        description="Session the turn is admitted into",
    )
    text: str = Field(
        min_length=1,
        max_length=MAX_ENTRY_TEXT_CHARS,
        description="Bounded user message text (untrusted content, never authority)",
    )
    client_request_id: uuid.UUID = Field(
        description="Client idempotency key of this submission",
    )
    model_profile: IdentityStr | None = Field(
        default=None,
        description="Preferred model profile identity for the new run",
    )


class SessionTurnSubmitResponse(BaseModel):
    """Durable admission result (sfc_RESEARCH §6: the server ack reports the
    durable admission, never optimistic success)."""

    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID = Field(
        description="Session the turn was admitted into",
    )
    run_id: uuid.UUID = Field(
        description="Subordinate run identity allocated for the turn",
    )
    entry_id: uuid.UUID = Field(
        description="Committed user entry identity of the admitted turn",
    )
    seq: int = Field(
        ge=1,
        description="Session-local sequence number of the committed user entry",
    )
    run_state: SessionRunState = Field(
        description="Frozen lifecycle state of the newly allocated run",
    )
    deduplicated: bool = Field(
        default=False,
        description="True when the same idempotency key replayed an earlier "
        "admission",
    )

    @model_validator(mode="after")
    def _identity_is_distinct(self) -> "SessionTurnSubmitResponse":
        assert_session_not_run(self.session_id, self.run_id)
        return self


class SessionControlRequest(BaseModel):
    """One deterministic control request (sfc_RESEARCH §6: steer/pause/
    cancel/approve/deny/reconcile acknowledge real state; no planner prose,
    no authority fields)."""

    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID = Field(
        description="Session the control action targets",
    )
    action: ControlActionKind = Field(
        description="Deterministic control action requested",
    )
    client_request_id: uuid.UUID = Field(
        description="Client idempotency key of this control request",
    )
    target_run_id: uuid.UUID | None = Field(
        default=None,
        description="Run the action targets, when scoped to one run",
    )
    approval_id: uuid.UUID | None = Field(
        default=None,
        description="Approval record id when deciding an approval action",
    )
    note: str | None = Field(
        default=None,
        max_length=MAX_DETAIL_CHARS,
        description="Value-free bounded operator note for the action",
    )


class SessionControlResponse(BaseModel):
    """Durable control acknowledgement (sfc_RESEARCH failure behavior: stale
    or conflicting control returns the structured current state, never a
    fabricated success)."""

    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID = Field(
        description="Session the control action targeted",
    )
    action: ControlActionKind = Field(
        description="Control action that was processed",
    )
    client_request_id: uuid.UUID = Field(
        description="Client idempotency key echoed from the request",
    )
    target_run_id: uuid.UUID | None = Field(
        default=None,
        description="Run the action targeted, when scoped to one run",
    )
    accepted: bool = Field(
        description="True when the control request was durably accepted",
    )
    run_state: SessionRunState | None = Field(
        default=None,
        description="Observed frozen run state after processing, when known",
    )


class RunbookSessionExecuteRequest(BaseModel):
    """One non-interactive runbook admission (sfc_INTENT §7: runbook
    execution opens a fresh scoped session with immutable service/runbook
    references; the API response returns explicit ``session_id`` +
    ``run_id``)."""

    model_config = ConfigDict(extra="forbid")

    server_id: uuid.UUID = Field(
        description="Server scope the runbook executes against",
    )
    runbook_ref: RunbookRef = Field(
        description="Immutable adopted service and runbook references",
    )
    client_request_id: uuid.UUID = Field(
        description="Client idempotency key of the execution request",
    )
    model_profile: IdentityStr | None = Field(
        default=None,
        description="Preferred model profile identity for the execution run",
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Bounded runbook parameters for the execution",
    )

    @model_validator(mode="after")
    def _parameters_are_bounded(self) -> "RunbookSessionExecuteRequest":
        if len(json.dumps(self.parameters)) > MAX_ENTRY_TEXT_CHARS:
            raise ValueError("runbook parameters exceed the bounded entry budget")
        return self


class DocumentAttachAdmissionRequest(BaseModel):
    """One document-attach admission (sfc_INTENT §8: an explicit destination
    session or an explicit create-new-session action — never an ambient
    global chat; provenance stays content, not authority)."""

    model_config = ConfigDict(extra="forbid")

    server_id: uuid.UUID = Field(
        description="Server scope the destination session belongs to",
    )
    session_id: uuid.UUID | None = Field(
        default=None,
        description="Explicit destination session, or null for the explicit "
        "create-new-session action",
    )
    document_id: uuid.UUID = Field(
        description="Stable document identity the excerpt comes from",
    )
    artifact_ref: RefStr = Field(
        description="Safe out-of-band reference to the stored excerpt artifact",
    )
    excerpt: str | None = Field(
        default=None,
        max_length=MAX_ENTRY_TEXT_CHARS,
        description="Bounded excerpt text attached as untrusted user content",
    )
    model_profile: IdentityStr | None = Field(
        default=None,
        description="Preferred model profile identity for the admission run",
    )
    client_request_id: uuid.UUID = Field(
        description="Client idempotency key of the attach request",
    )


class SessionRunRecord(BaseModel):
    """Durable run record (sfc_RESEARCH §1: parent/child edges, budgets and
    lifecycle state are execution truth subordinate to the session)."""

    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID = Field(
        description="Subordinate run identity of this execution attempt",
    )
    session_id: uuid.UUID = Field(
        description="Owning session identity (the only conversation root)",
    )
    server_id: uuid.UUID = Field(
        description="Server scope pinning this run",
    )
    parent_run_id: uuid.UUID | None = Field(
        default=None,
        description="Parent run identity for delegated child executions",
    )
    state: SessionRunState = Field(
        description="Current frozen lifecycle state of the run",
    )
    model_profile: IdentityStr = Field(
        description="Model profile identity the run executes with",
    )
    client_request_id: uuid.UUID = Field(
        description="Client idempotency key that admitted the run",
    )
    created_at: datetime = Field(
        description="UTC creation timestamp of the run",
    )
    updated_at: datetime = Field(
        description="UTC timestamp of the latest committed run change",
    )
    completed_at: datetime | None = Field(
        default=None,
        description="UTC terminal timestamp, when the run settled",
    )

    @model_validator(mode="after")
    def _identity_edges_are_distinct(self) -> "SessionRunRecord":
        assert_session_not_run(self.session_id, self.run_id)
        if self.parent_run_id == self.run_id:
            raise SessionContractError(
                [
                    SessionFieldError(
                        SessionErrorCode.SESSION_RUN_IDENTITY_CONFLICT,
                        "parent_run_id",
                        "a run may never be its own parent",
                    )
                ]
            )
        if self.parent_run_id is not None:
            assert_session_not_run(self.session_id, self.parent_run_id)
        return self


class SessionCompactionRecord(BaseModel):
    """Durable compaction record (sfc_RESEARCH §5: stable session id, exact
    source range, usage anchor; originals are retained, never deleted)."""

    model_config = ConfigDict(extra="forbid")

    compaction_id: uuid.UUID = Field(
        description="Stable identity of this compaction generation",
    )
    session_id: uuid.UUID = Field(
        description="Compacted session identity (never rotates)",
    )
    from_seq: int = Field(
        ge=1,
        description="First covered session sequence number (inclusive)",
    )
    to_seq: int = Field(
        ge=1,
        description="Last covered session sequence number (inclusive)",
    )
    summary_text: str = Field(
        min_length=1,
        max_length=MAX_SUMMARY_TEXT_CHARS,
        description="Bounded summary of the covered entry range with provenance",
    )
    model_profile: IdentityStr = Field(
        description="Model profile identity the summary was produced for",
    )
    source_usage_tokens: int | None = Field(
        default=None,
        ge=0,
        description="Actual provider usage tokens of the summarized source range",
    )
    created_at: datetime = Field(
        description="UTC creation timestamp of the compaction",
    )

    @model_validator(mode="after")
    def _range_is_ordered(self) -> "SessionCompactionRecord":
        if self.to_seq < self.from_seq:
            raise ValueError("compaction range must satisfy to_seq >= from_seq")
        return self


class DelegationSpec(BaseModel):
    """Explicit child delegation specification (sfc_RESEARCH §4: bounded
    goal, explicit context/artifact references, narrowed capability grants
    and aggregate budgets — never an unrestricted parent copy)."""

    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID = Field(
        description="Root session identity the child execution belongs to",
    )
    parent_run_id: uuid.UUID = Field(
        description="Parent run identity requesting the delegation",
    )
    goal: str = Field(
        min_length=1,
        max_length=MAX_SUMMARY_TEXT_CHARS,
        description="Bounded explicit goal handed to the child",
    )
    context_refs: list[RefStr] = Field(
        default_factory=list,
        max_length=MAX_CONTEXT_REFS,
        description="Explicit bounded context references the child may read",
    )
    artifact_refs: list[RefStr] = Field(
        default_factory=list,
        max_length=MAX_CONTEXT_REFS,
        description="Explicit artifact references handed to the child",
    )
    allowed_capabilities: list[IdentityStr] = Field(
        max_length=MAX_ALLOWED_CAPABILITIES,
        description="Narrowed capability grants of the child (intersection "
        "with parent policy)",
    )
    max_rounds: int = Field(
        ge=1,
        le=MAX_DELEGATION_ROUNDS,
        description=f"Maximum model rounds of the child (1..{MAX_DELEGATION_ROUNDS})",
    )
    max_tool_calls: int = Field(
        ge=1,
        le=MAX_DELEGATION_TOOL_CALLS,
        description=(
            f"Maximum tool calls of the child (1..{MAX_DELEGATION_TOOL_CALLS})"
        ),
    )
    timeout_seconds: float = Field(
        gt=0,
        le=MAX_DELEGATION_TIMEOUT_SECONDS,
        description="Hard wall-clock budget of the child execution in seconds",
    )

    @model_validator(mode="after")
    def _identity_is_distinct(self) -> "DelegationSpec":
        assert_session_not_run(self.session_id, self.parent_run_id)
        return self


class DelegationResult(BaseModel):
    """Bounded immutable child delegation result (sfc_RESEARCH §4: the
    terminal result is written once; unknown children are never replaced
    speculatively)."""

    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID = Field(
        description="Root session identity the child execution belongs to",
    )
    parent_run_id: uuid.UUID = Field(
        description="Parent run identity that requested the delegation",
    )
    child_run_id: uuid.UUID = Field(
        description="Run identity of the delegated child execution",
    )
    child_session_id: uuid.UUID | None = Field(
        default=None,
        description="Session identity of the child when it owns a distinct session",
    )
    status: SessionRunState = Field(
        description="Terminal frozen lifecycle state of the child run",
    )
    result_summary: str | None = Field(
        default=None,
        max_length=MAX_SUMMARY_TEXT_CHARS,
        description="Bounded immutable summary of the settled child outcome",
    )
    error_code: IdentityStr | None = Field(
        default=None,
        description="Stable machine-readable child failure code, when failed",
    )
    child_usage_tokens: int | None = Field(
        default=None,
        ge=0,
        description="Actual provider usage tokens rolled up from the child",
    )

    @model_validator(mode="after")
    def _edges_are_distinct(self) -> "DelegationResult":
        if self.child_run_id == self.parent_run_id:
            raise SessionContractError(
                [
                    SessionFieldError(
                        SessionErrorCode.SESSION_RUN_IDENTITY_CONFLICT,
                        "child_run_id",
                        "a child run may never equal its parent run",
                    )
                ]
            )
        assert_session_not_run(self.session_id, self.child_run_id)
        if self.child_session_id is not None:
            assert_session_not_run(self.child_session_id, self.child_run_id)
        return self
