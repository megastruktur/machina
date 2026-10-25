"""Task-state agent tools (act ``14-task-tools``; act_CONTRACTS §7).

The model-facing seam over the ONE task service
(:mod:`mai.agents.task_store`): a bounded read of the ACTIVE task plus the
model-author proposal and item-update tools. There is no parallel write
path and no arbitrary cross-task selection:

- **Scope binding (§7):** the target task is derived from the bound run
  identity in the :class:`~mai.policy.evaluate.ExecutionContext` —
  ``run_id`` → ``conversation_runs`` → conversation (+ its task link) —
  and scope-checked with :func:`mai.contracts.tasks.assert_task_scope`
  (scenario C8: a cross-server task is invisible, never leaked). The
  payloads carry NO ``task_id``/``server_id``: the model can never select
  another task or server scope.
- **Same service, same rules (§7):** reads go through
  ``task_store.aget_task_view``/``alist_*`` (deterministic ordering,
  same-predicate counts, ``limit``/``offset``/``total``/``has_more``);
  ``task.propose_update`` routes through
  :func:`mai.agents.intake.apropose_model_task_update` — the R-11
  author↔origin defense — and ``task.update_item`` through
  ``task_store.aupdate_item`` with a HARDWIRED ``author="model"``. CAS
  revisions, idempotency replay, constraint-authority and
  evidence-honesty rules are the store's ratified validators; nothing is
  re-implemented here.
- **No authority self-grant (§6):** the tool lane is model output only —
  a proposal claiming ``origin=USER`` must anchor to verified user
  messages of the conversation and can never supersede (user constraint
  removal stays on the user revision API); executed/skipped items stay
  frozen; a ``passed`` verification is evidence-bound and never silently
  cleared.
- **Bounded output (AGENTS.md §1):** every collection is an explicit
  page under the shared envelope bound; an over-budget page is honestly
  re-bounded (halved with an explicit marker) rather than silently
  sliced by the dispatcher's 64 KiB cap.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from typing import Any, Final, Literal

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents.intake import apropose_model_task_update
from mai.agents.task_store import (
    aget_task_view,
    alist_constraints,
    alist_evidence,
    alist_steps,
    aupdate_item,
)
from mai.capabilities.dispatch import CapabilityCall
from mai.capabilities.internal_api import (
    InternalApiComposition,
    InternalApiError,
    InternalApiOperation,
    InternalApiSpec,
    build_descriptor,
    build_executor,
    internal_api_tool,
)
from mai.contracts.conversations import TurnDisposition
from mai.plugins.registry import CapabilityRegistry
from mai.contracts.tasks import (
    MAX_CONSTRAINT_PROPOSALS,
    MAX_EVIDENCE_REFS,
    STABLE_ID_PATTERN,
    SUMMARY_MAX_CHARS,
    EvidenceRef,
    StepStatus,
    TaskContractError,
    TaskErrorCode,
    TaskOutcome,
    TaskPlanPatch,
    TaskView,
    VerificationStatus,
    assert_task_scope,
    ConstraintProposal,
)

__all__ = [
    "ENVELOPE_MAX_BYTES",
    "TASK_PROPOSE_UPDATE_CAPABILITY",
    "TASK_READ_CAPABILITY",
    "TASK_STATE_CAPABILITY_IDS",
    "TASK_UPDATE_ITEM_CAPABILITY",
    "TaskProposeUpdatePayload",
    "TaskReadPayload",
    "TaskUpdateItemPayload",
    "build_task_state_capabilities",
]


# ---------------------------------------------------------------------------
# Capability identifiers (frozen §7 vocabulary)
# ---------------------------------------------------------------------------

TASK_READ_CAPABILITY: Final[str] = "task.read"
TASK_PROPOSE_UPDATE_CAPABILITY: Final[str] = "task.propose_update"
TASK_UPDATE_ITEM_CAPABILITY: Final[str] = "task.update_item"

#: The task-state tool set (the §7 registry identifiers this module owns).
TASK_STATE_CAPABILITY_IDS: Final[tuple[str, ...]] = (
    TASK_READ_CAPABILITY,
    TASK_PROPOSE_UPDATE_CAPABILITY,
    TASK_UPDATE_ITEM_CAPABILITY,
)


# ---------------------------------------------------------------------------
# Bounded page defaults (AGENTS.md §1; the app.* tool precedent)
# ---------------------------------------------------------------------------

DEFAULT_READ_LIMIT: Final[int] = 25
MAX_READ_LIMIT: Final[int] = 100

#: Strict envelope budget (bytes): under the dispatcher's 64 KiB default
#: output budget so the JSON is never sliced mid-document.
ENVELOPE_MAX_BYTES: Final[int] = 60000

#: Open task outcomes (§1: one active episode per conversation). The
#: closed outcomes (verified/failed/cancelled) are historical.
_TASK_ACTIVE_OUTCOMES: Final[tuple[TaskOutcome, ...]] = (
    TaskOutcome.IN_PROGRESS,
    TaskOutcome.NEEDS_INPUT,
    TaskOutcome.PARTIALLY_VERIFIED,
)

_ACTIVE_TASK_STATUSES_SQL: Final[str] = "(" + ", ".join(
    f"'{outcome.value}'" for outcome in _TASK_ACTIVE_OUTCOMES
) + ")"

#: §5/§6 top-level task rejection codes, in declaration order — the first
#: match becomes the stable envelope ``code``.
_TOP_LEVEL_CODE_ORDER: Final[tuple[str, ...]] = (
    TaskErrorCode.TASK_NOT_FOUND.value,
    TaskErrorCode.REVISION_CONFLICT.value,
    TaskErrorCode.INVALID_TASK_PATCH.value,
    TaskErrorCode.PHASE_NOT_ALLOWED.value,
    TaskErrorCode.COMPLETION_MISSING_EVIDENCE.value,
)


# ---------------------------------------------------------------------------
# Payload models (every field documented; §7 contract)
# ---------------------------------------------------------------------------


class TaskReadPayload(BaseModel):
    """Read the ACTIVE task: the bounded header or one section page."""

    model_config = {"extra": "forbid"}

    section: Literal["header", "steps", "constraints", "evidence"] = Field(
        default="header",
        description=(
            "Which bounded section to read: the compact header view with "
            "counts, or one page of plan steps, constraints or evidence."
        ),
    )
    limit: int = Field(
        default=DEFAULT_READ_LIMIT,
        ge=1,
        le=MAX_READ_LIMIT,
        description=(
            "Maximum items to return per page for section reads "
            f"(default {DEFAULT_READ_LIMIT}, max {MAX_READ_LIMIT})."
        ),
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Zero-based pagination offset for section reads (default 0).",
    )


class TaskProposeUpdatePayload(BaseModel):
    """Propose one bounded plan delta on the ACTIVE task (model author).

    The delta is validated with the store's ratified CAS/idempotency and
    constraint-authority rules; a proposal claiming ``origin=USER`` must
    anchor to verified user messages and can never supersede (§6).
    """

    model_config = {"extra": "forbid"}

    patch: TaskPlanPatch = Field(
        description=(
            "Bounded plan delta: source message, base revision for "
            "compare-and-swap, idempotency key and add/update/supersede "
            "step operations (max 12, ≤ 16 KiB serialized)."
        ),
    )
    constraints_proposed: list[ConstraintProposal] = Field(
        default_factory=list,
        max_length=MAX_CONSTRAINT_PROPOSALS,
        description=(
            "Bounded add/supersede constraint proposals riding this "
            "delta; a user-origin claim must anchor to verified user "
            "messages and can never supersede a user constraint."
        ),
    )


class TaskUpdateItemPayload(BaseModel):
    """Advance one plan item of the ACTIVE task (model author, §7)."""

    model_config = {"extra": "forbid"}

    step_id: str = Field(
        pattern=STABLE_ID_PATTERN,
        description="Stable plan item ID to advance (from task.read steps).",
    )
    expected_revision: int = Field(
        ge=1,
        description=(
            "Expected task revision for compare-and-swap; a stale value "
            "is the stable revision_conflict, never last-writer-wins."
        ),
    )
    idempotency_key: uuid.UUID = Field(
        description=(
            "Caller-generated key so a retransmitted update deduplicates "
            "onto the SAME revision instead of double-applying."
        ),
    )
    status: StepStatus | None = Field(
        default=None,
        description=(
            "New todo status; executed/skipped items are frozen recorded "
            "work and can never be reopened."
        ),
    )
    verification: VerificationStatus | None = Field(
        default=None,
        description=(
            "New evidence-bound verification verdict; a passed check is "
            "never silently cleared."
        ),
    )
    result_summary: str | None = Field(
        default=None,
        max_length=SUMMARY_MAX_CHARS,
        description=(
            "Short safe result summary recorded on the item (no secret "
            "material)."
        ),
    )
    evidence_refs: list[EvidenceRef] = Field(
        default_factory=list,
        max_length=MAX_EVIDENCE_REFS,
        description=(
            "Sourced evidence references merged into the item; executed "
            "and passed/failed verdicts require at least one."
        ),
    )
    source_message_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Optional user message of this conversation the update was "
            "produced from (existence-checked, P4)."
        ),
    )


# ---------------------------------------------------------------------------
# rejection plumbing (value-free; the store's codes are the vocabulary)
# ---------------------------------------------------------------------------


def _refuse_contract(exc: TaskContractError) -> InternalApiError:
    """Translate one store contract rejection into the refused envelope.

    ``TaskContractError`` details are value-free by construction; the
    first matching top-level §5 code becomes the stable envelope code.
    """
    codes = {error.code for error in exc.errors}
    code = next(
        (candidate for candidate in _TOP_LEVEL_CODE_ORDER if candidate in codes),
        TaskErrorCode.INVALID_TASK_PATCH.value,
    )
    detail = "; ".join(
        f"{error.field}: {error.detail}" for error in exc.errors
    )
    return InternalApiError(code, detail)


def _bound_page_envelope(
    envelope: dict[str, object], max_bytes: int = ENVELOPE_MAX_BYTES
) -> dict[str, object]:
    """Keep one page envelope strictly under the dispatcher output budget.

    The page is ALREADY bounded by ``limit`` (AGENTS.md §1); this is the
    defense-in-depth for pathological row sizes: items are halved with an
    explicit marker and ``has_more`` until the serialized document fits,
    so the dispatcher never slices the JSON mid-structure and the
    truncation is never hidden success.
    """
    raw = json.dumps(envelope, sort_keys=True).encode("utf-8")
    if len(raw) <= max_bytes:
        return envelope
    items = envelope.get("items")
    if isinstance(items, list):
        while (
            len(items) > 1
            and len(json.dumps(envelope, sort_keys=True).encode("utf-8"))
            > max_bytes
        ):
            envelope["items"] = items[: max(1, len(items) // 2)]
            items = envelope["items"]
            envelope["has_more"] = True
            envelope["items_rebounded"] = True
    return envelope


# ---------------------------------------------------------------------------
# scope binding (§7: derived from the run identity, never the payload)
# ---------------------------------------------------------------------------


async def _aresolve_task_context(
    sessions: async_sessionmaker[AsyncSession],
    *,
    server_id: uuid.UUID,
    run_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID, TaskView]:
    """Resolve ``(conversation_id, task_id, view)`` from the bound run.

    ``run_id`` → ``conversation_runs`` → conversation (scope-checked with
    :func:`assert_task_scope`) → the run's linked task, else the
    conversation's ONE active episode (§1). Every failure is closed and
    value-free; another server's task is ``task_not_found`` (C8).
    """
    async with sessions() as session:
        link = (
            (
                await session.execute(
                    text(
                        "SELECT lr.conversation_id AS cid, lr.task_id AS tid, "
                        "c.server_id AS cserver FROM conversation_runs lr "
                        "JOIN conversations c "
                        "ON c.conversation_id = lr.conversation_id "
                        "WHERE lr.run_id = :rid LIMIT 1"
                    ),
                    {"rid": run_id},
                )
            )
            .mappings()
            .first()
        )
    if link is None:
        raise InternalApiError(
            "not_found",
            "no conversation run is bound to this execution context",
        )
    assert_task_scope(server_id, link["cserver"])
    conversation_id = uuid.UUID(str(link["cid"]))
    if link["tid"] is not None:
        task_id = uuid.UUID(str(link["tid"]))
    else:
        async with sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT task_id FROM agent_tasks "
                        "WHERE conversation_id = :cid AND status IN "
                        f"{_ACTIVE_TASK_STATUSES_SQL} "
                        "ORDER BY created_at DESC, task_id LIMIT 1"
                    ),
                    {"cid": conversation_id},
                )
            ).first()
        if row is None:
            raise InternalApiError(
                "no_active_task",
                "this conversation has no active task episode",
            )
        task_id = uuid.UUID(str(row[0]))
    view = await aget_task_view(sessions, task_id, server_id=server_id)
    return conversation_id, task_id, view


def _step_document(step: object) -> dict[str, object]:
    """One bounded plan-step document (active view; recorded evidence)."""
    return {
        "step_id": step.step_id,
        "title": step.title,
        "status": step.status.value,
        "verification": step.verification.value,
        "position": step.position,
        "risk": step.risk.value,
        "prerequisite_unresolved": step.prerequisite_unresolved,
        "expected_check": step.expected_check,
        "skip_reason": step.skip_reason,
        "result_summary": step.result_summary,
        "evidence_refs": [
            {
                "kind": ref.kind.value,
                "evidence_id": str(ref.evidence_id),
                "note": ref.note,
            }
            for ref in step.evidence_refs
        ],
        "depends_on": list(step.depends_on),
    }


def _constraint_document(constraint: object) -> dict[str, object]:
    """One bounded constraint document (origin anchors kept verbatim)."""
    return {
        "constraint_id": constraint.constraint_id,
        "category": constraint.category,
        "statement": constraint.statement,
        "origin": constraint.origin.value,
        "source_message_refs": [
            str(ref) for ref in constraint.source_message_refs
        ],
        "position": constraint.position,
    }


def _evidence_document(evidence: object) -> dict[str, object]:
    """One bounded evidence-registry document."""
    return {
        "evidence_id": str(evidence.evidence_id),
        "kind": evidence.kind.value,
        "note": evidence.note,
        "origin_step_id": evidence.origin_step_id,
        "created_at": evidence.created_at.isoformat(),
    }


def _disposition_value(disposition: TurnDisposition) -> str:
    return disposition.value


# ---------------------------------------------------------------------------
# the composition builder (app.py consumes the SAME result shape as the
# internal-API builder: descriptors + executors, fail-closed at build)
# ---------------------------------------------------------------------------


def build_task_state_capabilities(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> InternalApiComposition:
    """Build every ``task.*`` descriptor + executor over the task store.

    ``session_factory=None`` (the fail-closed no-database composition)
    produces executors whose every call answers
    ``{"status": "unavailable", ...}`` — the internal-API composition
    contract. Descriptors are validated fail-closed at build time
    (``assert_agent_ready_descriptor`` via :func:`build_descriptor`).
    """
    sessions = session_factory
    operations: list[tuple[InternalApiSpec, InternalApiOperation]] = []

    # -- task.read -----------------------------------------------------------

    @internal_api_tool(
        capability_id=TASK_READ_CAPABILITY,
        version="1.0.0",
        effect_class="read",
        resource_scope=["tasks"],
        environments=["controller"],
        payload_model=TaskReadPayload,
        description=(
            "Read the ACTIVE task of this conversation: the compact "
            "header view (objective, revision, phase, status, list "
            "counts) or one bounded page of plan steps, constraints or "
            "evidence. The target is bound to this run's conversation — "
            "another server's or another conversation's task is "
            "invisible."
        ),
    )
    async def _task_read(
        call: CapabilityCall, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        if sessions is None:
            raise InternalApiError("unavailable", "task store unavailable")
        if not hasattr(call.context, "run_id"):
            raise InternalApiError(
                "invalid_request", "executor input must carry a run context"
            )
        read = TaskReadPayload.model_validate(payload)
        try:
            _conversation_id, task_id, view = await _aresolve_task_context(
                sessions,
                server_id=call.context.server_id,
                run_id=call.context.run_id,
            )
            if read.section == "header":
                # ``status`` is the executor envelope discriminator
                # ({"status": "ok"}); the task outcome rides ``task_status``.
                document = view.model_dump(mode="json")
                document["task_status"] = document.pop("status")
                document["status"] = "ok"
                return document
            if read.section == "steps":
                page = await alist_steps(
                    sessions,
                    task_id,
                    server_id=call.context.server_id,
                    limit=read.limit,
                    offset=read.offset,
                )
                items: list[dict[str, object]] = [
                    _step_document(step) for step in page.items
                ]
            elif read.section == "constraints":
                page = await alist_constraints(
                    sessions,
                    task_id,
                    server_id=call.context.server_id,
                    limit=read.limit,
                    offset=read.offset,
                )
                items = [_constraint_document(c) for c in page.items]
            else:
                evidence_page = await alist_evidence(
                    sessions,
                    task_id,
                    server_id=call.context.server_id,
                    limit=read.limit,
                    offset=read.offset,
                )
                items = [_evidence_document(e) for e in evidence_page.items]
                page = evidence_page
            return _bound_page_envelope(
                {
                    "status": "ok",
                    "task_id": str(task_id),
                    "revision": view.revision,
                    "section": read.section,
                    "items": items,
                    "limit": page.limit,
                    "offset": page.offset,
                    "total": page.total,
                    "has_more": page.has_more,
                }
            )
        except TaskContractError as exc:
            raise _refuse_contract(exc) from None

    operations.append(_task_read)

    # -- task.propose_update ---------------------------------------------------

    @internal_api_tool(
        capability_id=TASK_PROPOSE_UPDATE_CAPABILITY,
        version="1.0.0",
        effect_class="mutate",
        resource_scope=["tasks"],
        environments=["controller"],
        payload_model=TaskProposeUpdatePayload,
        description=(
            "Propose one bounded plan delta on the ACTIVE task of this "
            "conversation under compare-and-swap revision and "
            "idempotency rules: add/update/supersede plan items (max 12) "
            "and optionally add/supersede constraints. Model proposals "
            "can never supersede a user-origin constraint, erase "
            "completed work, or claim user authority without verified "
            "user-message anchors."
        ),
    )
    async def _task_propose_update(
        call: CapabilityCall, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        if sessions is None:
            raise InternalApiError("unavailable", "task store unavailable")
        if not hasattr(call.context, "run_id"):
            raise InternalApiError(
                "invalid_request", "executor input must carry a run context"
            )
        proposal = TaskProposeUpdatePayload.model_validate(payload)
        try:
            conversation_id, task_id, _view = await _aresolve_task_context(
                sessions,
                server_id=call.context.server_id,
                run_id=call.context.run_id,
            )
            result = await apropose_model_task_update(
                sessions,
                task_id,
                server_id=call.context.server_id,
                conversation_id=conversation_id,
                patch=proposal.patch,
                constraint_proposals=tuple(proposal.constraints_proposed),
            )
        except TaskContractError as exc:
            raise _refuse_contract(exc) from None
        except ValueError as exc:
            raise InternalApiError("invalid_request", str(exc)) from None
        return {
            "status": "ok",
            "task_id": str(result.task.task_id),
            "revision": result.task.revision,
            "disposition": _disposition_value(result.disposition),
            "revision_kind": result.revision.kind,
            "author": result.revision.author,
        }

    operations.append(_task_propose_update)

    # -- task.update_item ------------------------------------------------------

    @internal_api_tool(
        capability_id=TASK_UPDATE_ITEM_CAPABILITY,
        version="1.0.0",
        effect_class="mutate",
        resource_scope=["tasks"],
        environments=["controller"],
        payload_model=TaskUpdateItemPayload,
        description=(
            "Advance one plan item of the ACTIVE task of this "
            "conversation under compare-and-swap revision and "
            "idempotency rules: set its todo status, evidence-bound "
            "verification verdict, safe result summary and evidence "
            "references. Executed/skipped items stay frozen; a passed "
            "verdict requires evidence and is never silently cleared."
        ),
    )
    async def _task_update_item(
        call: CapabilityCall, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        if sessions is None:
            raise InternalApiError("unavailable", "task store unavailable")
        if not hasattr(call.context, "run_id"):
            raise InternalApiError(
                "invalid_request", "executor input must carry a run context"
            )
        update = TaskUpdateItemPayload.model_validate(payload)
        try:
            _conversation_id, task_id, _view = await _aresolve_task_context(
                sessions,
                server_id=call.context.server_id,
                run_id=call.context.run_id,
            )
            result = await aupdate_item(
                sessions,
                task_id,
                server_id=call.context.server_id,
                step_id=update.step_id,
                expected_revision=update.expected_revision,
                idempotency_key=update.idempotency_key,
                status=update.status,
                verification=update.verification,
                result_summary=update.result_summary,
                evidence_refs=tuple(update.evidence_refs),
                source_message_id=update.source_message_id,
                author="model",
            )
        except TaskContractError as exc:
            raise _refuse_contract(exc) from None
        except ValueError as exc:
            raise InternalApiError("invalid_request", str(exc)) from None
        return {
            "status": "ok",
            "task_id": str(result.task.task_id),
            "revision": result.task.revision,
            "disposition": _disposition_value(result.disposition),
            "revision_kind": result.revision.kind,
            "author": result.revision.author,
            "step_id": result.step.step_id,
            "step_status": result.step.status.value,
            "step_verification": result.step.verification.value,
        }

    operations.append(_task_update_item)

    # -- registration ----------------------------------------------------------

    registry = CapabilityRegistry()
    executors: dict[str, Callable[[CapabilityCall], Any]] = {}
    for spec, operation in operations:
        descriptor = build_descriptor(spec)
        registry.register(descriptor)
        executors[spec.capability_id] = build_executor(spec, operation)
    return InternalApiComposition(
        descriptors=tuple(registry.list_capabilities()),
        executors=executors,
    )
