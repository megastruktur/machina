"""Authenticated task read/correction/decision HTTP surface (15-task-api).

The user-facing half of the ratified task contract (act_CONTRACTS §6/§8)
over the ONE task service (:mod:`mai.agents.task_store` — card
11-task-store; this surface composes it, never re-queries around it for
domain truth and never opens a second revision/CAS path):

- ``GET /api/v1/chat/conversations/{conversation_id}/task`` — the compact
  §8 header view (revision, phase, status, list counts; no unbounded
  arrays) for the conversation's ONE active episode.
- ``GET /api/v1/chat/tasks/{task_id}/items`` — the deterministic,
  explicitly server-scoped, paginated plan-step page
  (``limit|offset|total|has_more`` per the AGENTS.md §1 invariant;
  ``COUNT(*)`` under the identical filter predicate; superseded rows are
  recorded history, served only when explicitly requested, P3).
- ``GET /api/v1/chat/tasks/{task_id}/constraints`` — the constraint page
  (the §1/§6 safety boundary is read like every other collection:
  bounded, ordered, never truncated silently).
- ``GET /api/v1/chat/tasks/{task_id}/evidence`` — the evidence-registry
  page (§9 provenance records only — never re-fetched source content).
- ``GET /api/v1/chat/tasks/{task_id}/revisions`` — the append-only
  revision ledger page (§8 audit; P5: a reconnecting UI re-derives the
  pending/applied distinction from the SAME ledger the tools write).
- ``POST /api/v1/chat/tasks/{task_id}/revisions`` — the authenticated
  user correction/proposal (§8): ONE shared revision service call with
  CAS (``base_revision``), idempotency (``idempotency_key``) and a
  mandatory source message (the revision is SOURCE-LINKED to the user
  message it corrects and is always authored ``user``, never the model).
- ``POST /api/v1/chat/tasks/{task_id}/decisions`` — the explicit user
  decision route (§8): phase selection (advisory, never permission) and
  pause/cancel via the frozen outcome transitions. It is DISTINCT from
  plan review (never applies a plan delta) and from operation approval
  (the C03 approvals surface owns effect authorization; a decision here
  never touches it — §6: metadata updates never change grants).

Boundary discipline (identical to the conversation history surface,
C01/C08): the principal comes from the validated session only (401
before any body); mutating routes add the exact-origin and double-submit
CSRF layers; every route takes its explicit ``server_id`` argument (C01
rule 2); cross-server access fails CLOSED with the scope-blind
``task_not_found`` 404 — another scope's existence never leaks through
the error channel (C8). A stale ``expected_revision``/``base_revision``
is the structured ``409 revision_conflict`` — never last-writer-wins
(C6). Store rejections map exhaustively onto the five ratified task
envelope codes; nothing else escapes the C01 envelope.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Final, Literal

from fastapi import APIRouter, FastAPI, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.status import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_401_UNAUTHORIZED,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_422_UNPROCESSABLE_CONTENT,
)

from mai.agents.conversation_store import aget_conversation
from mai.contracts.conversations import ConversationContractError, TurnDisposition
from mai.agents.task_store import (
    ConstraintRow,
    EvidenceRow,
    RevisionRow,
    StepRow,
    aget_task_view,
    alist_constraints,
    alist_evidence,
    alist_revisions,
    alist_steps,
    apropose_update,
    aupdate_task_header,
)
from mai.api.auth import (
    ErrorEnvelope,
    _require_allowed_origin,
    _require_csrf,
    _require_session,
)
from mai.api.jobs import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    _error,
    _jobs_services,
    _sessions,
)
from mai.contracts.tasks import (
    MAX_CONSTRAINT_PROPOSALS,
    ActionRisk,
    ConstraintOrigin,
    ConstraintProposal,
    EvidenceKind,
    EvidenceRef,
    Phase,
    StepStatus,
    TaskContractError,
    TaskErrorCode,
    TaskOutcome,
    TaskPlanPatch,
    TaskView,
    VerificationStatus,
)

__all__ = ["register_tasks"]


# ---------------------------------------------------------------------------
# Wire shapes (transport only; truth is the store + frozen contracts)
# ---------------------------------------------------------------------------


class TaskStepView(BaseModel):
    """One plan item (§7 vocabulary mirror of the storage row).

    ``executed``/``skipped`` stay visible with their evidence references
    and skip reasons — "done" is rendered from agreed checks, never a
    ticked boolean (§7); superseded rows stay RECORDED (P3).
    """

    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID = Field(description="Owning task identity (§1)")
    step_id: str = Field(description="Stable item ID that survives replanning")
    title: str = Field(description="Short step title safe for browser display")
    status: StepStatus = Field(description="Frozen todo status of this step")
    verification: VerificationStatus = Field(
        description="Separate verification state bound to evidence",
    )
    position: int = Field(ge=0, description="Deterministic list position")
    risk: ActionRisk = Field(description="Declared risk/action class of the step")
    prerequisite_unresolved: bool = Field(
        description="True when a required prerequisite is still missing",
    )
    expected_check: str | None = Field(
        description="The check that will decide this step is done, when declared",
    )
    skip_reason: str | None = Field(
        description="Recorded reason when the step is skipped, otherwise null",
    )
    result_summary: str | None = Field(
        description="Safe bounded result summary, when one was recorded",
    )
    evidence_refs: list[EvidenceRef] = Field(
        description="Sourced evidence supporting executed/verified state",
    )
    depends_on: list[str] = Field(
        description="Stable step IDs this step depends on (no cycles)",
    )
    superseded_at: str | None = Field(
        description="RFC3339 UTC supersede timestamp for recorded history, else null",
    )
    superseded_by: str | None = Field(
        description="Step ID that replaced this row, else null (P3 history)",
    )
    created_at: str = Field(description="RFC3339 UTC creation timestamp")
    updated_at: str = Field(description="RFC3339 UTC last-revision timestamp")


class TaskStepsPage(BaseModel):
    """One bounded plan-step page (§8 ``GET items``; AGENTS.md §1)."""

    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID = Field(description="Owning task identity (§1)")
    limit: int = Field(
        ge=1,
        le=MAX_PAGE_LIMIT,
        description="Page size used for this slice (1..200)",
    )
    offset: int = Field(ge=0, description="Slice start within the collection")
    total: int = Field(
        ge=0,
        description="Pre-slice matching count under the identical filter",
    )
    has_more: bool = Field(
        description="True when another page exists after this slice",
    )
    items: list[TaskStepView] = Field(
        default_factory=list,
        description="Step rows in deterministic (position, step_id) order",
    )

    @model_validator(mode="after")
    def _page_is_consistent(self) -> "TaskStepsPage":
        if len(self.items) > self.limit:
            raise ValueError("page carries more items than its limit")
        if self.has_more != (self.offset + len(self.items) < self.total):
            raise ValueError("has_more must equal offset + len(items) < total")
        return self


class TaskConstraintView(BaseModel):
    """One constraint row with its source anchors (§5/§6).

    ``origin`` distinguishes user requirements from model inferences —
    the authority fact that keeps a model correction from ever erasing a
    user prohibition (P4).
    """

    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID = Field(description="Owning task identity (§1)")
    constraint_id: str = Field(description="Stable slug of the constraint")
    category: str = Field(description="Constraint category slug, e.g. safety")
    statement: str = Field(description="Safe constraint statement; no secret material")
    origin: ConstraintOrigin = Field(
        description="Whether the user stated this or the model inferred it",
    )
    source_message_refs: list[uuid.UUID] = Field(
        description="User messages this constraint is anchored to",
    )
    position: int = Field(ge=0, description="Deterministic list position")
    superseded_at: str | None = Field(
        description="RFC3339 UTC supersede timestamp for recorded history, else null",
    )
    superseded_by: str | None = Field(
        description="Constraint ID that replaced this row, else null (P3 history)",
    )
    created_at: str = Field(description="RFC3339 UTC creation timestamp")
    updated_at: str = Field(description="RFC3339 UTC last-revision timestamp")


class TaskConstraintsPage(BaseModel):
    """One bounded constraint page (§8 ``GET constraints``; AGENTS.md §1)."""

    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID = Field(description="Owning task identity (§1)")
    limit: int = Field(
        ge=1,
        le=MAX_PAGE_LIMIT,
        description="Page size used for this slice (1..200)",
    )
    offset: int = Field(ge=0, description="Slice start within the collection")
    total: int = Field(
        ge=0,
        description="Pre-slice matching count under the identical filter",
    )
    has_more: bool = Field(
        description="True when another page exists after this slice",
    )
    items: list[TaskConstraintView] = Field(
        default_factory=list,
        description="Constraint rows in deterministic (position, id) order",
    )

    @model_validator(mode="after")
    def _page_is_consistent(self) -> "TaskConstraintsPage":
        if len(self.items) > self.limit:
            raise ValueError("page carries more items than its limit")
        if self.has_more != (self.offset + len(self.items) < self.total):
            raise ValueError("has_more must equal offset + len(items) < total")
        return self


class TaskEvidenceView(BaseModel):
    """One evidence-registry row (§9 provenance identities only).

    The registry stores WHICH sourced record backs the task (kind + ID +
    optional safe note), never a copy of the referenced content.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID = Field(description="Owning task identity (§1)")
    evidence_id: uuid.UUID = Field(description="Opaque UUID of the referenced record")
    kind: EvidenceKind = Field(description="Kind of sourced record referenced")
    note: str | None = Field(
        description="Short safe annotation for UI display, when recorded",
    )
    origin_step_id: str | None = Field(
        description="Step that first registered this evidence, when known",
    )
    created_at: str = Field(description="RFC3339 UTC registration timestamp")


class TaskEvidencePage(BaseModel):
    """One bounded evidence page (§8 ``GET evidence``; AGENTS.md §1)."""

    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID = Field(description="Owning task identity (§1)")
    limit: int = Field(
        ge=1,
        le=MAX_PAGE_LIMIT,
        description="Page size used for this slice (1..200)",
    )
    offset: int = Field(ge=0, description="Slice start within the collection")
    total: int = Field(
        ge=0,
        description="Pre-slice matching count under the identical filter",
    )
    has_more: bool = Field(
        description="True when another page exists after this slice",
    )
    items: list[TaskEvidenceView] = Field(
        default_factory=list,
        description="Evidence rows in deterministic (created_at, id) order",
    )

    @model_validator(mode="after")
    def _page_is_consistent(self) -> "TaskEvidencePage":
        if len(self.items) > self.limit:
            raise ValueError("page carries more items than its limit")
        if self.has_more != (self.offset + len(self.items) < self.total):
            raise ValueError("has_more must equal offset + len(items) < total")
        return self


class TaskRevisionView(BaseModel):
    """One append-only revision-log row (§8 audit; P5 shared truth).

    ``author`` is the stored authority fact (``user`` for every correction
    this surface accepts — never the model) and ``source_message_id`` is
    the user message the revision was produced from, so a disputed
    interpretation always explains itself through its origin (§8 UI
    contract).
    """

    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID = Field(description="Owning task identity (§1)")
    revision: int = Field(ge=1, description="The optimistic revision token (§1)")
    kind: str = Field(
        description="Frozen revision kind (create|plan_patch|item_update|metadata|completion)",
    )
    author: str = Field(description="Stored authority: user, model or system (§6)")
    source_message_id: uuid.UUID | None = Field(
        description="User message this revision was produced from, when linked",
    )
    item_id: str | None = Field(
        description="Stable item ID for item-scoped revisions, else null",
    )
    payload: dict[str, object] | None = Field(
        description="Bounded revision summary (operation/constraint identities)",
    )
    applied_at: str = Field(description="RFC3339 UTC application timestamp")


class TaskRevisionsPage(BaseModel):
    """One bounded revision-ledger page (§8; AGENTS.md §1), newest first."""

    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID = Field(description="Owning task identity (§1)")
    limit: int = Field(
        ge=1,
        le=MAX_PAGE_LIMIT,
        description="Page size used for this slice (1..200)",
    )
    offset: int = Field(ge=0, description="Slice start within the collection")
    total: int = Field(
        ge=0,
        description="Pre-slice matching count under the identical filter",
    )
    has_more: bool = Field(
        description="True when another page exists after this slice",
    )
    items: list[TaskRevisionView] = Field(
        default_factory=list,
        description="Revision rows in deterministic revision-descending order",
    )

    @model_validator(mode="after")
    def _page_is_consistent(self) -> "TaskRevisionsPage":
        if len(self.items) > self.limit:
            raise ValueError("page carries more items than its limit")
        if self.has_more != (self.offset + len(self.items) < self.total):
            raise ValueError("has_more must equal offset + len(items) < total")
        return self


class TaskCorrectionBody(BaseModel):
    """One authenticated user correction/proposal (§8 ``POST revisions``).

    The body embeds the FROZEN ratified patch model — the exact same
    validated delta the model tools submit — so CAS (``base_revision``),
    idempotency (``idempotency_key``) and the mandatory source message
    (``source_message_id``) are contract-owned, not re-implemented here.
    The route always stores ``author="user"``.
    """

    model_config = ConfigDict(extra="forbid")

    server_id: uuid.UUID = Field(
        description="Explicit server scope (C01 rule 2); other servers' "
        "tasks are invisible, not forbidden",
    )
    patch: TaskPlanPatch = Field(
        description="The bounded plan delta (user correction/proposal) with "
        "CAS base_revision, idempotency key and source message reference",
    )
    constraint_proposals: list[ConstraintProposal] = Field(
        default_factory=list,
        max_length=MAX_CONSTRAINT_PROPOSALS,
        description="Bounded constraint add/supersede proposals anchored to "
        "user messages",
    )


class TaskDecisionBody(BaseModel):
    """One explicit user decision (§8 ``POST decisions``).

    Phase selection is ADVISORY (never permission, §6) and pause/cancel
    move only the domain outcome layer through its frozen transition map.
    A decision NEVER applies a plan delta (that is the revisions route)
    and NEVER authorizes or terminates a pending operation (that is the
    C03 approvals surface; §6: metadata decisions cannot change grants).
    """

    model_config = ConfigDict(extra="forbid")

    server_id: uuid.UUID = Field(
        description="Explicit server scope (C01 rule 2); other servers' "
        "tasks are invisible, not forbidden",
    )
    expected_revision: int = Field(
        ge=1,
        description="The named revision this decision reviewed; a stale "
        "value is the structured 409 revision_conflict (C6)",
    )
    client_request_id: uuid.UUID = Field(
        description="Caller key so retransmitted decisions deduplicate (§1)",
    )
    requested_phase: Phase | None = Field(
        default=None,
        description="The user's chosen next phase (plan|inspect|act); "
        "advisory only, never permission (§6)",
    )
    task_action: Literal["pause", "cancel"] | None = Field(
        default=None,
        description="Explicit deterministic control: pause parks the open "
        "episode as needs_input, cancel closes it (terminal)",
    )

    @model_validator(mode="after")
    def _carries_a_decision(self) -> "TaskDecisionBody":
        if self.requested_phase is None and self.task_action is None:
            raise ValueError("a decision requires requested_phase or task_action")
        return self


class TaskMutationOutcome(BaseModel):
    """The shared outcome of one accepted/replayed task mutation (§1).

    The fresh header view (revision, phase, status, counts), the
    append-only revision row this mutation produced (source-linked,
    authority-recorded) and the honest idempotency disposition
    (``created`` | ``deduplicated``).
    """

    model_config = ConfigDict(extra="forbid")

    task: TaskView = Field(
        description="The fresh compact header view after the mutation (§8)",
    )
    revision: TaskRevisionView = Field(
        description="The revision-ledger row this mutation produced/replayed",
    )
    disposition: Literal["created", "deduplicated"] = Field(
        description="created for a first application, deduplicated for an "
        "idempotent replay (C4)",
    )


# ---------------------------------------------------------------------------
# Rejections (the scope-blind C01 envelope; mirrors the chat surface)
# ---------------------------------------------------------------------------


def _task_not_found(request: Request) -> Response:
    """The scope-blind 404 (C8): existence of another server's task (or
    conversation) never leaks through the error channel."""
    return _error(
        request,
        HTTP_404_NOT_FOUND,
        TaskErrorCode.TASK_NOT_FOUND.value,
        "no task in the requesting server scope",
    )


def _task_error(request: Request, exc: TaskContractError) -> Response:
    """Map the ONE store's closed rejections onto the C01 envelope.

    Exhaustive over the ratified top-level codes; the store's single
    scope-mismatch/absent code becomes the scope-blind 404, a stale CAS
    the structured 409 (C6), and contract-invalid payloads the 422 with
    the store's field-level detail codes (value-free, C01 rule 3).
    """
    codes = set(exc.fields.values())
    if TaskErrorCode.TASK_NOT_FOUND.value in codes:
        return _task_not_found(request)
    if TaskErrorCode.REVISION_CONFLICT.value in codes:
        return _error(
            request,
            HTTP_409_CONFLICT,
            TaskErrorCode.REVISION_CONFLICT.value,
            "expected task revision does not match the current revision",
            fields=exc.fields,
        )
    if TaskErrorCode.COMPLETION_MISSING_EVIDENCE.value in codes:
        return _error(
            request,
            HTTP_409_CONFLICT,
            TaskErrorCode.COMPLETION_MISSING_EVIDENCE.value,
            "the completion verdict is not covered by passed evidence-bound criteria",
            fields=exc.fields,
        )
    if TaskErrorCode.PHASE_NOT_ALLOWED.value in codes:
        return _error(
            request,
            HTTP_409_CONFLICT,
            TaskErrorCode.PHASE_NOT_ALLOWED.value,
            "the requested action is not allowed in the task's phase",
            fields=exc.fields,
        )
    return _error(
        request,
        HTTP_422_UNPROCESSABLE_CONTENT,
        TaskErrorCode.INVALID_TASK_PATCH.value,
        "task contract rejected",
        fields=exc.fields,
    )


# ---------------------------------------------------------------------------
# row → wire projection (transport only)
# ---------------------------------------------------------------------------


def _wire_ts(value: datetime) -> str:
    """Render one stored UTC timestamp in the frozen ``…Z`` wire form."""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _step_view(row: StepRow) -> TaskStepView:
    return TaskStepView(
        task_id=row.task_id,
        step_id=row.step_id,
        title=row.title,
        status=row.status,
        verification=row.verification,
        position=row.position,
        risk=row.risk,
        prerequisite_unresolved=row.prerequisite_unresolved,
        expected_check=row.expected_check,
        skip_reason=row.skip_reason,
        result_summary=row.result_summary,
        evidence_refs=list(row.evidence_refs),
        depends_on=list(row.depends_on),
        superseded_at=(
            None if row.superseded_at is None else _wire_ts(row.superseded_at)
        ),
        superseded_by=row.superseded_by,
        created_at=_wire_ts(row.created_at),
        updated_at=_wire_ts(row.updated_at),
    )


def _constraint_view(row: ConstraintRow) -> TaskConstraintView:
    return TaskConstraintView(
        task_id=row.task_id,
        constraint_id=row.constraint_id,
        category=row.category,
        statement=row.statement,
        origin=row.origin,
        source_message_refs=list(row.source_message_refs),
        position=row.position,
        superseded_at=(
            None if row.superseded_at is None else _wire_ts(row.superseded_at)
        ),
        superseded_by=row.superseded_by,
        created_at=_wire_ts(row.created_at),
        updated_at=_wire_ts(row.updated_at),
    )


def _evidence_view(row: EvidenceRow) -> TaskEvidenceView:
    return TaskEvidenceView(
        task_id=row.task_id,
        evidence_id=row.evidence_id,
        kind=row.kind,
        note=row.note,
        origin_step_id=row.origin_step_id,
        created_at=_wire_ts(row.created_at),
    )


def _revision_view(row: RevisionRow) -> TaskRevisionView:
    return TaskRevisionView(
        task_id=row.task_id,
        revision=row.revision,
        kind=row.kind,
        author=row.author,
        source_message_id=row.source_message_id,
        item_id=row.item_id,
        payload=row.payload,
        applied_at=_wire_ts(row.applied_at),
    )


async def _amutation_outcome(
    request: Request,
    task_id: uuid.UUID,
    server_id: uuid.UUID,
    revision: RevisionRow,
    disposition: TurnDisposition,
) -> TaskMutationOutcome:
    """The fresh header view + revision row for one accepted mutation."""
    factory = _sessions(request)
    view = await aget_task_view(factory, task_id, server_id=server_id)
    return TaskMutationOutcome(
        task=view,
        revision=_revision_view(revision),
        disposition=disposition,  # StrEnum members ARE the wire literals
    )


# ---------------------------------------------------------------------------
# routes (session boundary; explicit server scope; bounded everywhere)
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/chat", tags=["chat-tasks"])

_SERVER_ID_QUERY: Final = Query(
    ...,
    description="Explicit server scope (C01 rule 2); other servers' tasks "
    "are invisible, not forbidden",
)

_NOT_FOUND_RESPONSES: Final[dict[int, dict[str, str]]] = {
    HTTP_401_UNAUTHORIZED: {"model": ErrorEnvelope, "description": "No valid session"},
    HTTP_404_NOT_FOUND: {
        "model": ErrorEnvelope,
        "description": "No task in the requesting server scope",
    },
}


@router.get(
    "/conversations/{conversation_id}/task",
    status_code=HTTP_200_OK,
    response_model=TaskView,
    summary="The conversation's ONE active task header (§8; no unbounded arrays)",
    responses=_NOT_FOUND_RESPONSES,
)
async def read_conversation_task(
    request: Request,
    conversation_id: uuid.UUID,
    server_id: Annotated[uuid.UUID, _SERVER_ID_QUERY],
) -> TaskView | Response:
    """The compact §8 header view for the conversation's active episode.

    Resolution goes through the conversation row's ``active_task_id`` —
    the ONE task pointer §1 defines — so a completed episode's history
    never masquerades as the current task. Absent conversation, foreign
    scope and absent episode all collapse into the SAME scope-blind 404:
    existence never leaks (C8).
    """
    await _require_session(request)
    _lifecycle, _recovery = _jobs_services(request)  # fail closed when absent
    factory = _sessions(request)
    try:
        conversation = await aget_conversation(
            factory, conversation_id, server_id=server_id
        )
    except ConversationContractError:
        return _task_not_found(request)
    if conversation.active_task_id is None:
        return _task_not_found(request)
    try:
        return await aget_task_view(
            factory, conversation.active_task_id, server_id=server_id
        )
    except TaskContractError as exc:
        return _task_error(request, exc)


@router.get(
    "/tasks/{task_id}/items",
    status_code=HTTP_200_OK,
    response_model=TaskStepsPage,
    summary="One bounded server-scoped plan-step page (§8; AGENTS.md §1)",
    responses=_NOT_FOUND_RESPONSES,
)
async def list_task_steps(
    request: Request,
    task_id: uuid.UUID,
    server_id: Annotated[uuid.UUID, _SERVER_ID_QUERY],
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
    include_superseded: bool = Query(
        default=False,
        description="Include superseded rows (recorded P3 history) in the page",
    ),
) -> TaskStepsPage | Response:
    """The deterministic plan-step page, ordered ``(position, step_id)``.

    ``total`` is the ``COUNT(*)`` under the identical filter predicate;
    superseded rows stay recorded and are served only when explicitly
    requested (P3). Cross-server access is the scope-blind 404 (C8).
    """
    await _require_session(request)
    _lifecycle, _recovery = _jobs_services(request)  # fail closed when absent
    factory = _sessions(request)
    try:
        page = await alist_steps(
            factory,
            task_id,
            server_id=server_id,
            limit=limit,
            offset=offset,
            include_superseded=include_superseded,
        )
    except TaskContractError as exc:
        return _task_error(request, exc)
    return TaskStepsPage(
        task_id=page.task_id,
        limit=page.limit,
        offset=page.offset,
        total=page.total,
        has_more=page.has_more,
        items=[_step_view(row) for row in page.items],
    )


@router.get(
    "/tasks/{task_id}/constraints",
    status_code=HTTP_200_OK,
    response_model=TaskConstraintsPage,
    summary="One bounded server-scoped constraint page (§8; AGENTS.md §1)",
    responses=_NOT_FOUND_RESPONSES,
)
async def list_task_constraints(
    request: Request,
    task_id: uuid.UUID,
    server_id: Annotated[uuid.UUID, _SERVER_ID_QUERY],
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
    include_superseded: bool = Query(
        default=False,
        description="Include superseded rows (recorded P3 history) in the page",
    ),
) -> TaskConstraintsPage | Response:
    """The deterministic constraint page, ordered ``(position, id)``.

    The §1/§6 safety boundary is read like every other collection:
    bounded, deterministic, never silently truncated — every row carries
    its ``origin`` (user requirement vs model inference) and its source
    message anchors.
    """
    await _require_session(request)
    _lifecycle, _recovery = _jobs_services(request)  # fail closed when absent
    factory = _sessions(request)
    try:
        page = await alist_constraints(
            factory,
            task_id,
            server_id=server_id,
            limit=limit,
            offset=offset,
            include_superseded=include_superseded,
        )
    except TaskContractError as exc:
        return _task_error(request, exc)
    return TaskConstraintsPage(
        task_id=page.task_id,
        limit=page.limit,
        offset=page.offset,
        total=page.total,
        has_more=page.has_more,
        items=[_constraint_view(row) for row in page.items],
    )


@router.get(
    "/tasks/{task_id}/evidence",
    status_code=HTTP_200_OK,
    response_model=TaskEvidencePage,
    summary="One bounded server-scoped evidence-registry page (§8/§9)",
    responses=_NOT_FOUND_RESPONSES,
)
async def list_task_evidence(
    request: Request,
    task_id: uuid.UUID,
    server_id: Annotated[uuid.UUID, _SERVER_ID_QUERY],
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> TaskEvidencePage | Response:
    """The deterministic evidence-registry page, ordered ``(created_at, id)``.

    Provenance IDENTITIES only (kind + referenced record ID + safe note):
    the route never re-fetches or inlines the referenced source content.
    """
    await _require_session(request)
    _lifecycle, _recovery = _jobs_services(request)  # fail closed when absent
    factory = _sessions(request)
    try:
        page = await alist_evidence(
            factory,
            task_id,
            server_id=server_id,
            limit=limit,
            offset=offset,
        )
    except TaskContractError as exc:
        return _task_error(request, exc)
    return TaskEvidencePage(
        task_id=page.task_id,
        limit=page.limit,
        offset=page.offset,
        total=page.total,
        has_more=page.has_more,
        items=[_evidence_view(row) for row in page.items],
    )


@router.get(
    "/tasks/{task_id}/revisions",
    status_code=HTTP_200_OK,
    response_model=TaskRevisionsPage,
    summary="One bounded revision-ledger page (§8 audit; P5 shared truth)",
    responses=_NOT_FOUND_RESPONSES,
)
async def list_task_revisions(
    request: Request,
    task_id: uuid.UUID,
    server_id: Annotated[uuid.UUID, _SERVER_ID_QUERY],
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> TaskRevisionsPage | Response:
    """The append-only revision ledger, newest revision first (§8).

    The SAME ledger the agent tools write, so a reconnecting UI (P5)
    re-derives the shared server revision and the pending/applied
    distinction from one authoritative history — never from browser
    state.
    """
    await _require_session(request)
    _lifecycle, _recovery = _jobs_services(request)  # fail closed when absent
    factory = _sessions(request)
    try:
        page = await alist_revisions(
            factory,
            task_id,
            server_id=server_id,
            limit=limit,
            offset=offset,
        )
    except TaskContractError as exc:
        return _task_error(request, exc)
    return TaskRevisionsPage(
        task_id=page.task_id,
        limit=page.limit,
        offset=page.offset,
        total=page.total,
        has_more=page.has_more,
        items=[_revision_view(row) for row in page.items],
    )


@router.post(
    "/tasks/{task_id}/revisions",
    status_code=HTTP_201_CREATED,
    response_model=TaskMutationOutcome,
    summary="One authenticated user correction/proposal under CAS + idempotency (§8)",
    responses={
        HTTP_200_OK: {
            "model": TaskMutationOutcome,
            "description": "Idempotent replay of an already-applied correction (C4)",
        },
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No task in the requesting server scope",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Stale base revision (C6) or contract rejection",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "The proposed patch is contract-invalid",
        },
    },
)
async def submit_task_correction(
    request: Request,
    task_id: uuid.UUID,
    payload: TaskCorrectionBody,
) -> TaskMutationOutcome | Response:
    """The user correction route (§8): ONE shared revision service call.

    Always ``author="user"`` — a user correction is never authored as the
    model — and always SOURCE-LINKED: the frozen patch model requires the
    user message being corrected, and the store existence-checks it
    against the task's conversation (P4) before recording the revision.
    A stale tab's ``base_revision`` is the structured
    ``409 revision_conflict`` — never last-writer-wins (C6); a
    retransmitted ``idempotency_key`` replays onto the SAME revision (C4,
    ``200 deduplicated``). Plan review never touches the phase, the
    status or any approval (§6).
    """
    _store, _token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    _lifecycle, _recovery = _jobs_services(request)  # fail closed when absent
    factory = _sessions(request)
    try:
        result = await apropose_update(
            factory,
            task_id,
            server_id=payload.server_id,
            author="user",
            patch=payload.patch,
            constraint_proposals=payload.constraint_proposals,
        )
    except TaskContractError as exc:
        return _task_error(request, exc)
    outcome = await _amutation_outcome(
        request, task_id, payload.server_id, result.revision, result.disposition
    )
    return Response(
        status_code=HTTP_201_CREATED
        if outcome.disposition == "created"
        else HTTP_200_OK,
        content=outcome.model_dump_json(),
        media_type="application/json",
    )


@router.post(
    "/tasks/{task_id}/decisions",
    status_code=HTTP_201_CREATED,
    response_model=TaskMutationOutcome,
    summary="One explicit user decision: phase selection or pause/cancel (§8)",
    responses={
        HTTP_200_OK: {
            "model": TaskMutationOutcome,
            "description": "Idempotent replay of an already-applied decision (C4)",
        },
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No task in the requesting server scope",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Stale named revision (C6) or frozen-transition rejection",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "The decision is contract-invalid",
        },
    },
)
async def submit_task_decision(
    request: Request,
    task_id: uuid.UUID,
    payload: TaskDecisionBody,
) -> TaskMutationOutcome | Response:
    """The user decision route (§8): DISTINCT from plan review and from
    operation approval.

    The route performs metadata-only header updates through the ONE
    store's CAS seam: phase selection is advisory (never permission) and
    pause/cancel follow the frozen outcome transitions (a cancelled
    episode closes and releases the conversation's active-task pointer).
    It NEVER applies a plan delta, NEVER authorizes a pending operation
    and NEVER terminates one — effect authorization stays on the C03
    approvals surface, unchanged (§6: metadata decisions cannot change
    grants). A stale ``expected_revision`` (the named revision the user
    reviewed) is the structured ``409 revision_conflict`` (C6); a
    retransmitted ``client_request_id`` replays (``200 deduplicated``).
    """
    _store, _token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    _lifecycle, _recovery = _jobs_services(request)  # fail closed when absent
    factory = _sessions(request)
    status = (
        TaskOutcome.NEEDS_INPUT
        if payload.task_action == "pause"
        else TaskOutcome.CANCELLED
        if payload.task_action == "cancel"
        else None
    )
    try:
        result = await aupdate_task_header(
            factory,
            task_id,
            server_id=payload.server_id,
            expected_revision=payload.expected_revision,
            requested_phase=payload.requested_phase,
            status=status,
            client_request_id=payload.client_request_id,
            author="user",
        )
    except TaskContractError as exc:
        return _task_error(request, exc)
    outcome = await _amutation_outcome(
        request, task_id, payload.server_id, result.revision, result.disposition
    )
    return Response(
        status_code=HTTP_201_CREATED
        if outcome.disposition == "created"
        else HTTP_200_OK,
        content=outcome.model_dump_json(),
        media_type="application/json",
    )


# ---------------------------------------------------------------------------
# Registration (api-wiring exclusive resource)
# ---------------------------------------------------------------------------


def register_tasks(app: FastAPI) -> None:
    """Register the task read/correction/decision surface (C01/C08)."""
    app.include_router(router)
