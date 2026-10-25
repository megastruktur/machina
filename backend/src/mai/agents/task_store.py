"""Task-domain persistence over the 0009 schema (card 11-task-store).

App-owned storage for task headers/revisions and the todo/constraint/
criterion/evidence/outcome pages, INDEPENDENTLY of execution attempts
(act_CONTRACTS §1–§2/§5–§9, ratified). ONE storage seam for the §7 task
tools and the §8 user HTTP endpoints — no sibling redesigns
(act_CONTRACTS §11). ``conversation_runs`` (0008) links runs to task
episodes; it never re-owns task state.

Frozen-contract reuse (no second vocabulary lives here):

- scope: :func:`mai.contracts.tasks.assert_task_scope` (scenario C8 — a
  cross-server read fails CLOSED with ``task_not_found``, never leaking
  the other scope's existence);
- CAS: :func:`mai.contracts.tasks.check_revision` under
  ``SELECT ... FOR UPDATE`` on the task row (scenario C6 — two tabs
  editing the same revision serialize; the stale writer receives the
  stable ``revision_conflict``, never last-writer-wins);
- patch/verdict validation: :func:`validate_task_plan_patch`,
  :func:`validate_constraint_proposals` and
  :func:`validate_completion_verdict` are the ratified authority checks
  (§6; P3/P4 — model updates can never erase user constraints, done
  items, or self-grant verified success);
- idempotency: every accepted mutation writes an
  ``agent_task_revisions`` row keyed by the caller's ``idempotency_key``
  (UNIQUE per task) plus a canonical request fingerprint (scenario C4 —
  a retransmitted request replays onto the SAME revision; a same key
  with a different payload is the stable conflict, never a silent
  double-apply);
- pagination: deterministic ``ORDER BY ... LIMIT :l OFFSET :o`` with
  ``COUNT(*)`` under the identical filter predicate and the shared
  :func:`compute_has_more` (AGENTS.md §1 mandatory pagination).

Authority model (§6): every mutation carries an explicit ``author``
(``user | model | system``). Model-authored updates can never supersede
an active user-origin constraint and can never touch a completed step
(``executed`` status / ``passed`` verification) — completed work is
recorded, never silently dropped through a replacement summary or plan
patch (P3/P4). User-authored corrections may supersede, but the old row
stays recorded with ``superseded_at``/``superseded_by`` — nothing is
ever deleted.

Evidence honesty (P4): every evidence reference a patch/item-update/
verdict carries is existence-checked against its REAL source table
(``conversation_messages`` / ``server_observations`` /
``conversation_runs`` / ``knowledge_documents``) BEFORE storage, then
recorded in the task-scoped ``agent_task_evidence`` registry (first
registration wins). ``EvidenceKind.ARTIFACT`` has no app-owned registry
at this head, so artifact refs fail CLOSED — a limitation, never a
fabricated success. ``run_event`` refs validate against the run linkage
(``conversation_runs.run_id``), the checkable portion of run provenance.

Frozen transitions: header outcomes freeze at terminal states
(``verified``/``failed``/``cancelled``); step status freezes at
``executed``/``skipped``; verification never silently clears a ``passed``
verdict. The maps below are the store's single transition truth and are
exhaustively asserted by the focused tests.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.contracts.conversations import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    TurnDisposition,
    compute_has_more,
)
from mai.contracts.tasks import (
    MAX_CONSTRAINT_PROPOSALS,
    MAX_CRITERIA,
    MAX_PATCH_STEPS,
    STABLE_ID_PATTERN,
    SUMMARY_MAX_CHARS,
    TITLE_MAX_CHARS,
    AcceptanceCriterion,
    ActionRisk,
    ConstraintOrigin,
    ConstraintProposal,
    EvidenceKind,
    EvidenceRef,
    Phase,
    StepStatus,
    TaskCompletionVerdict,
    TaskCounts,
    TaskContractError,
    TaskErrorCode,
    TaskFieldError,
    TaskOutcome,
    TaskPlanStep,
    TaskView,
    VerificationStatus,
    assert_task_scope,
    check_revision,
    validate_completion_verdict,
    validate_constraint_proposals,
    validate_task_plan_patch,
)

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "TASK_TERMINAL_OUTCOMES",
    "STATUS_TRANSITIONS",
    "STEP_STATUS_TRANSITIONS",
    "VERIFICATION_TRANSITIONS",
    "REVISION_KINDS",
    "AUTHORS",
    "CompletionRow",
    "CompletionResult",
    "CompletionPage",
    "ConstraintRow",
    "ConstraintsPage",
    "CriterionRow",
    "EvidenceRow",
    "EvidencePage",
    "ItemUpdateResult",
    "RevisionRow",
    "RevisionsPage",
    "StepRow",
    "StepsPage",
    "TaskMutationResult",
    "TaskRow",
    "acreate_task",
    "aget_task",
    "aget_task_view",
    "alist_completions",
    "alist_constraints",
    "alist_evidence",
    "alist_revisions",
    "alist_steps",
    "apropose_completion",
    "apropose_update",
    "aupdate_item",
    "aupdate_task_header",
]


# ---------------------------------------------------------------------------
# rows (plain frozen storage rows; the HTTP models live in the contracts)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskRow:
    """One ``agent_tasks`` row (storage truth, not an HTTP view)."""

    task_id: uuid.UUID
    conversation_id: uuid.UUID
    server_id: uuid.UUID
    revision: int
    title: str
    summary: str | None
    requested_phase: Phase
    status: TaskOutcome
    active_step_id: str | None
    client_request_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StepRow:
    """One persisted ``agent_task_steps`` row."""

    task_id: uuid.UUID
    step_id: str
    title: str
    status: StepStatus
    verification: VerificationStatus
    position: int
    risk: ActionRisk
    prerequisite_unresolved: bool
    expected_check: str | None
    skip_reason: str | None
    result_summary: str | None
    evidence_refs: tuple[EvidenceRef, ...]
    depends_on: tuple[str, ...]
    superseded_at: datetime | None
    superseded_by: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ConstraintRow:
    """One persisted ``agent_task_constraints`` row."""

    task_id: uuid.UUID
    constraint_id: str
    category: str
    statement: str
    origin: ConstraintOrigin
    source_message_refs: tuple[uuid.UUID, ...]
    position: int
    superseded_at: datetime | None
    superseded_by: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CriterionRow:
    """One persisted ``agent_task_criteria`` row."""

    task_id: uuid.UUID
    criterion_id: str
    statement: str
    verification: VerificationStatus
    position: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class EvidenceRow:
    """One persisted ``agent_task_evidence`` registry row."""

    task_id: uuid.UUID
    evidence_id: uuid.UUID
    kind: EvidenceKind
    note: str | None
    origin_step_id: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RevisionRow:
    """One append-only ``agent_task_revisions`` row (§8 audit log)."""

    task_id: uuid.UUID
    revision: int
    kind: str
    author: str
    idempotency_key: uuid.UUID | None
    request_fingerprint: str | None
    source_message_id: uuid.UUID | None
    item_id: str | None
    payload: dict[str, object] | None
    applied_at: datetime


@dataclass(frozen=True, slots=True)
class CompletionRow:
    """One persisted ``agent_task_completions`` receipt (§9)."""

    completion_id: uuid.UUID
    task_id: uuid.UUID
    base_revision: int
    revision: int
    idempotency_key: uuid.UUID
    task_outcome: TaskOutcome
    verdict: dict[str, object]
    created_at: datetime


# ---------------------------------------------------------------------------
# pages (AGENTS.md §1 mandatory pagination envelopes)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StepsPage:
    """One bounded plan-step page, ordered ``(position, step_id)``."""

    task_id: uuid.UUID
    limit: int
    offset: int
    total: int
    has_more: bool
    items: tuple[StepRow, ...]


@dataclass(frozen=True, slots=True)
class ConstraintsPage:
    """One bounded constraint page, ordered ``(position, constraint_id)``."""

    task_id: uuid.UUID
    limit: int
    offset: int
    total: int
    has_more: bool
    items: tuple[ConstraintRow, ...]


@dataclass(frozen=True, slots=True)
class EvidencePage:
    """One bounded evidence-registry page, ordered ``(created_at, id)``."""

    task_id: uuid.UUID
    limit: int
    offset: int
    total: int
    has_more: bool
    items: tuple[EvidenceRow, ...]


@dataclass(frozen=True, slots=True)
class RevisionsPage:
    """One bounded revision-log page, newest revision first."""

    task_id: uuid.UUID
    limit: int
    offset: int
    total: int
    has_more: bool
    items: tuple[RevisionRow, ...]


@dataclass(frozen=True, slots=True)
class CompletionPage:
    """One bounded completion-receipt page, newest first."""

    task_id: uuid.UUID
    limit: int
    offset: int
    total: int
    has_more: bool
    items: tuple[CompletionRow, ...]


@dataclass(frozen=True, slots=True)
class TaskMutationResult:
    """Outcome of one accepted/replayed task mutation."""

    task: TaskRow
    revision: RevisionRow
    disposition: TurnDisposition


@dataclass(frozen=True, slots=True)
class ItemUpdateResult:
    """Outcome of one ``task.update_item`` mutation (§7)."""

    task: TaskRow
    step: StepRow
    revision: RevisionRow
    disposition: TurnDisposition


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """Outcome of one ``task.propose_completion`` mutation (§9)."""

    task: TaskRow
    completion: CompletionRow
    revision: RevisionRow
    disposition: TurnDisposition


# ---------------------------------------------------------------------------
# frozen transition truth (asserted exhaustively by the focused tests)
# ---------------------------------------------------------------------------

#: Header outcomes that close a task episode (§9); no mutation reopens one.
TASK_TERMINAL_OUTCOMES: Final[frozenset[TaskOutcome]] = frozenset(
    {
        TaskOutcome.VERIFIED,
        TaskOutcome.FAILED,
        TaskOutcome.CANCELLED,
    }
)

#: Legal header status transitions (§9): terminal states are frozen; every
#: open state may move to any state, including closing (cancel/complete).
#: One map, one truth.
STATUS_TRANSITIONS: Final[Mapping[TaskOutcome, frozenset[TaskOutcome]]] = {
    outcome: (
        frozenset({outcome})
        if outcome in TASK_TERMINAL_OUTCOMES
        else frozenset(TaskOutcome)
    )
    for outcome in TaskOutcome
}

#: Legal step status transitions (§7): ``executed``/``skipped`` are frozen
#: (completed work and recorded skips are never reopened); every open
#: status may move to any status.
STEP_STATUS_TRANSITIONS: Final[Mapping[StepStatus, frozenset[StepStatus]]] = {
    status: (
        frozenset({status})
        if status in (StepStatus.EXECUTED, StepStatus.SKIPPED)
        else frozenset(StepStatus)
    )
    for status in StepStatus
}

#: Legal verification transitions (§7): verification is evidence-bound —
#: ``not_checked`` may receive any verdict; ``unavailable`` may be retried;
#: a ``failed`` check may later pass with new evidence; a ``passed`` check
#: is never silently cleared or overturned.
VERIFICATION_TRANSITIONS: Final[
    Mapping[VerificationStatus, frozenset[VerificationStatus]]
] = {
    VerificationStatus.NOT_CHECKED: frozenset(VerificationStatus),
    VerificationStatus.UNAVAILABLE: frozenset(
        {
            VerificationStatus.PASSED,
            VerificationStatus.FAILED,
            VerificationStatus.UNAVAILABLE,
        }
    ),
    VerificationStatus.FAILED: frozenset(
        {VerificationStatus.FAILED, VerificationStatus.PASSED}
    ),
    VerificationStatus.PASSED: frozenset({VerificationStatus.PASSED}),
}

#: Frozen revision-log kinds (0009 CHECK mirror).
REVISION_KINDS: Final[frozenset[str]] = frozenset(
    {"create", "plan_patch", "item_update", "metadata", "completion"}
)

#: Frozen revision authors (0009 CHECK mirror; §6 authority).
AUTHORS: Final[frozenset[str]] = frozenset({"user", "model", "system"})

_ACTIVE_STATUSES_SQL: Final[str] = (
    "('in_progress', 'needs_input', 'partially_verified')"
)

_TASK_COLUMNS: Final[str] = (
    "task_id, conversation_id, server_id, revision, title, summary, "
    "requested_phase, status, active_step_id, client_request_id, "
    "created_at, updated_at"
)

_STEP_COLUMNS: Final[str] = (
    "task_id, step_id, title, status, verification, position, risk, "
    "prerequisite_unresolved, expected_check, skip_reason, "
    "result_summary, evidence_refs, depends_on, superseded_at, "
    "superseded_by, created_at, updated_at"
)

_CONSTRAINT_COLUMNS: Final[str] = (
    "task_id, constraint_id, category, statement, origin, "
    "source_message_refs, position, superseded_at, superseded_by, "
    "created_at, updated_at"
)

_CRITERION_COLUMNS: Final[str] = (
    "task_id, criterion_id, statement, verification, position, "
    "created_at, updated_at"
)

_EVIDENCE_COLUMNS: Final[str] = (
    "task_id, evidence_id, kind, note, origin_step_id, created_at"
)

_REVISION_COLUMNS: Final[str] = (
    "task_id, revision, kind, author, idempotency_key, "
    "request_fingerprint, source_message_id, item_id, payload, "
    "applied_at"
)

_COMPLETION_COLUMNS: Final[str] = (
    "completion_id, task_id, base_revision, revision, idempotency_key, "
    "task_outcome, verdict, created_at"
)

#: Real-source existence predicate per evidence kind (P4). ``artifact`` has
#: no app-owned registry at this head → always closed (documented
#: limitation). ``run_event`` validates the checkable portion of run
#: provenance: the linkage row in the task's conversation.
_EVIDENCE_SOURCE_SQL: Final[Mapping[EvidenceKind, str | None]] = {
    EvidenceKind.MESSAGE: (
        "SELECT 1 FROM conversation_messages "
        "WHERE message_id = :eid AND conversation_id = :cid"
    ),
    EvidenceKind.OBSERVATION: (
        "SELECT 1 FROM server_observations WHERE observation_id = :eid"
    ),
    EvidenceKind.RUN_EVENT: (
        "SELECT 1 FROM conversation_runs "
        "WHERE run_id = :eid AND conversation_id = :cid"
    ),
    EvidenceKind.ARTIFACT: None,
    EvidenceKind.DOCUMENT: (
        "SELECT 1 FROM knowledge_documents WHERE doc_id = :eid"
    ),
}

_STABLE_ID_RE: Final[re.Pattern[str]] = re.compile(STABLE_ID_PATTERN)


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------


def _not_found() -> TaskContractError:
    """The single closed error for a missing/out-of-scope task (C8)."""
    return TaskContractError(
        [
            TaskFieldError(
                TaskErrorCode.TASK_NOT_FOUND,
                "task_id",
                "no task in the requesting server scope",
            )
        ]
    )


def _invalid(field: str, detail: str) -> TaskContractError:
    """One ``invalid_task_patch`` rejection with a single field error."""
    return TaskContractError(
        [TaskFieldError(TaskErrorCode.INVALID_TASK_PATCH, field, detail)]
    )


def _check_page_bounds(limit: int, offset: int) -> None:
    """Bounded-page validation (AGENTS.md §1): fail closed, never clamp."""
    limit_ok = isinstance(limit, int) and not isinstance(limit, bool)
    if not limit_ok or not 1 <= limit <= MAX_PAGE_LIMIT:
        raise ValueError(f"limit must be an integer in [1, {MAX_PAGE_LIMIT}]")
    offset_ok = isinstance(offset, int) and not isinstance(offset, bool)
    if not offset_ok or offset < 0:
        raise ValueError("offset must be an integer >= 0")


def _fingerprint(payload: Mapping[str, object]) -> str:
    """Canonical request fingerprint (C4 idempotent-replay discriminator)."""
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _row_to_task(row: Mapping[str, object]) -> TaskRow:
    return TaskRow(
        task_id=row["task_id"],  # type: ignore[arg-type]
        conversation_id=row["conversation_id"],  # type: ignore[arg-type]
        server_id=row["server_id"],  # type: ignore[arg-type]
        revision=int(row["revision"]),  # type: ignore[arg-type]
        title=str(row["title"]),
        summary=row["summary"],  # type: ignore[arg-type]
        requested_phase=Phase(str(row["requested_phase"])),
        status=TaskOutcome(str(row["status"])),
        active_step_id=row["active_step_id"],  # type: ignore[arg-type]
        client_request_id=row["client_request_id"],  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
        updated_at=row["updated_at"],  # type: ignore[arg-type]
    )


def _json_str_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value)
    return [str(entry) for entry in value]


def _row_to_step(row: Mapping[str, object]) -> StepRow:
    refs = row["evidence_refs"]
    if isinstance(refs, str):
        refs = json.loads(refs)
    return StepRow(
        task_id=row["task_id"],  # type: ignore[arg-type]
        step_id=str(row["step_id"]),
        title=str(row["title"]),
        status=StepStatus(str(row["status"])),
        verification=VerificationStatus(str(row["verification"])),
        position=int(row["position"]),  # type: ignore[arg-type]
        risk=ActionRisk(str(row["risk"])),
        prerequisite_unresolved=bool(row["prerequisite_unresolved"]),
        expected_check=row["expected_check"],  # type: ignore[arg-type]
        skip_reason=row["skip_reason"],  # type: ignore[arg-type]
        result_summary=row["result_summary"],  # type: ignore[arg-type]
        evidence_refs=tuple(
            EvidenceRef.model_validate(entry) for entry in (refs or [])
        ),
        depends_on=tuple(_json_str_list(row["depends_on"])),
        superseded_at=row["superseded_at"],  # type: ignore[arg-type]
        superseded_by=row["superseded_by"],  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
        updated_at=row["updated_at"],  # type: ignore[arg-type]
    )


def _row_to_constraint(row: Mapping[str, object]) -> ConstraintRow:
    raw_refs = row["source_message_refs"]
    if isinstance(raw_refs, str):
        raw_refs = json.loads(raw_refs)
    return ConstraintRow(
        task_id=row["task_id"],  # type: ignore[arg-type]
        constraint_id=str(row["constraint_id"]),
        category=str(row["category"]),
        statement=str(row["statement"]),
        origin=ConstraintOrigin(str(row["origin"])),
        source_message_refs=tuple(
            uuid.UUID(str(entry)) for entry in (raw_refs or [])
        ),
        position=int(row["position"]),  # type: ignore[arg-type]
        superseded_at=row["superseded_at"],  # type: ignore[arg-type]
        superseded_by=row["superseded_by"],  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
        updated_at=row["updated_at"],  # type: ignore[arg-type]
    )


def _row_to_criterion(row: Mapping[str, object]) -> CriterionRow:
    return CriterionRow(
        task_id=row["task_id"],  # type: ignore[arg-type]
        criterion_id=str(row["criterion_id"]),
        statement=str(row["statement"]),
        verification=VerificationStatus(str(row["verification"])),
        position=int(row["position"]),  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
        updated_at=row["updated_at"],  # type: ignore[arg-type]
    )


def _row_to_evidence(row: Mapping[str, object]) -> EvidenceRow:
    return EvidenceRow(
        task_id=row["task_id"],  # type: ignore[arg-type]
        evidence_id=row["evidence_id"],  # type: ignore[arg-type]
        kind=EvidenceKind(str(row["kind"])),
        note=row["note"],  # type: ignore[arg-type]
        origin_step_id=row["origin_step_id"],  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
    )


def _row_to_revision(row: Mapping[str, object]) -> RevisionRow:
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return RevisionRow(
        task_id=row["task_id"],  # type: ignore[arg-type]
        revision=int(row["revision"]),  # type: ignore[arg-type]
        kind=str(row["kind"]),
        author=str(row["author"]),
        idempotency_key=row["idempotency_key"],  # type: ignore[arg-type]
        request_fingerprint=row["request_fingerprint"],  # type: ignore[arg-type]
        source_message_id=row["source_message_id"],  # type: ignore[arg-type]
        item_id=row["item_id"],  # type: ignore[arg-type]
        payload=payload,  # type: ignore[arg-type]
        applied_at=row["applied_at"],  # type: ignore[arg-type]
    )


def _row_to_completion(row: Mapping[str, object]) -> CompletionRow:
    verdict = row["verdict"]
    if isinstance(verdict, str):
        verdict = json.loads(verdict)
    return CompletionRow(
        completion_id=row["completion_id"],  # type: ignore[arg-type]
        task_id=row["task_id"],  # type: ignore[arg-type]
        base_revision=int(row["base_revision"]),  # type: ignore[arg-type]
        revision=int(row["revision"]),  # type: ignore[arg-type]
        idempotency_key=row["idempotency_key"],  # type: ignore[arg-type]
        task_outcome=TaskOutcome(str(row["task_outcome"])),
        verdict=verdict,  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
    )


async def _alocked_task(
    session: AsyncSession, task_id: uuid.UUID
) -> Mapping[str, object]:
    """Fetch one task row under ``FOR UPDATE`` (C6 serialization).

    Raises the closed ``task_not_found`` error when the row does not
    exist; scope is checked by the caller against the request's
    ``server_id``.
    """
    row = (
        await session.execute(
            text(
                f"SELECT {_TASK_COLUMNS} FROM agent_tasks "
                "WHERE task_id = :tid FOR UPDATE"
            ),
            {"tid": task_id},
        )
    ).mappings().first()
    if row is None:
        raise _not_found()
    return row


async def _achecked_task(
    sessions: async_sessionmaker[AsyncSession],
    task_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
) -> TaskRow:
    """Scope-checked read of one task (C8; no lock needed)."""
    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    f"SELECT {_TASK_COLUMNS} FROM agent_tasks "
                    "WHERE task_id = :tid"
                ),
                {"tid": task_id},
            )
        ).mappings().first()
    if row is None:
        raise _not_found()
    task = _row_to_task(row)
    assert_task_scope(server_id, task.server_id)
    return task


async def _arevision_row(
    session: AsyncSession, task_id: uuid.UUID, revision: int
) -> RevisionRow:
    row = (
        await session.execute(
            text(
                f"SELECT {_REVISION_COLUMNS} FROM agent_task_revisions "
                "WHERE task_id = :tid AND revision = :rev"
            ),
            {"tid": task_id, "rev": revision},
        )
    ).mappings().one()
    return _row_to_revision(row)


async def _aidempotent_revision(
    session: AsyncSession,
    task_id: uuid.UUID,
    idempotency_key: uuid.UUID,
) -> RevisionRow | None:
    """The durable replay row for this caller key, when one exists (C4)."""
    row = (
        await session.execute(
            text(
                f"SELECT {_REVISION_COLUMNS} FROM agent_task_revisions "
                "WHERE task_id = :tid AND idempotency_key = :key"
            ),
            {"tid": task_id, "key": idempotency_key},
        )
    ).mappings().first()
    return None if row is None else _row_to_revision(row)


def _replay_conflict(field: str) -> TaskContractError:
    """Same idempotency key, different request — never a double-apply."""
    return TaskContractError(
        [
            TaskFieldError(
                TaskErrorCode.INVALID_TASK_PATCH,
                field,
                "idempotency key was already used for a different request",
            )
        ]
    )


async def _aevidence_exists(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    ref: EvidenceRef,
) -> bool:
    """Existence check of one evidence ref against its real source (P4)."""
    source_sql = _EVIDENCE_SOURCE_SQL[ref.kind]
    if source_sql is None:
        return False
    row = (
        await session.execute(
            text(source_sql), {"eid": ref.evidence_id, "cid": conversation_id}
        )
    ).first()
    return row is not None


async def _acheck_and_register_evidence(
    session: AsyncSession,
    task: Mapping[str, object],
    refs: Sequence[EvidenceRef],
    field: str,
    origin_step_id: str | None = None,
) -> None:
    """Fail closed on unknown evidence, then record it in the registry.

    Every referenced record must EXIST in its real source table before
    the mutation is accepted (P4: hallucinated evidence/source IDs are
    rejected with zero effects — the caller's transaction rolls back).
    Registration is an upsert (first registration wins on kind/note).
    """
    errors: list[TaskFieldError] = []
    for index, ref in enumerate(refs):
        if not await _aevidence_exists(session, task["conversation_id"], ref):  # type: ignore[arg-type]
            errors.append(
                TaskFieldError(
                    TaskErrorCode.UNKNOWN_EVIDENCE_REF,
                    f"{field}[{index}]",
                    "evidence reference does not exist in the task scope",
                )
            )
            continue
        await session.execute(
            text(
                "INSERT INTO agent_task_evidence "
                "    (task_id, evidence_id, kind, note, origin_step_id) "
                "VALUES (:tid, :eid, :kind, :note, :ostep) "
                "ON CONFLICT (task_id, evidence_id) DO NOTHING"
            ),
            {
                "tid": task["task_id"],
                "eid": ref.evidence_id,
                "kind": ref.kind.value,
                "note": ref.note,
                "ostep": origin_step_id,
            },
        )
    if errors:
        raise TaskContractError(errors)


async def _aactive_steps(
    session: AsyncSession, task_id: uuid.UUID
) -> list[StepRow]:
    rows = (
        await session.execute(
            text(
                f"SELECT {_STEP_COLUMNS} FROM agent_task_steps "
                "WHERE task_id = :tid AND superseded_at IS NULL "
                "ORDER BY position, step_id"
            ),
            {"tid": task_id},
        )
    ).mappings().all()
    return [_row_to_step(row) for row in rows]


async def _aactive_constraints(
    session: AsyncSession, task_id: uuid.UUID
) -> list[ConstraintRow]:
    rows = (
        await session.execute(
            text(
                f"SELECT {_CONSTRAINT_COLUMNS} FROM agent_task_constraints "
                "WHERE task_id = :tid AND superseded_at IS NULL "
                "ORDER BY position, constraint_id"
            ),
            {"tid": task_id},
        )
    ).mappings().all()
    return [_row_to_constraint(row) for row in rows]


def _step_set_errors(steps: Sequence[TaskPlanStep]) -> list[TaskFieldError]:
    """Creation-time structural validation of the initial step set (§6).

    Mirrors :func:`validate_task_plan_patch` semantics for a fresh task:
    duplicate stable IDs, dependencies outside the set, dependency
    cycles. (The patch validator needs a patch envelope; creation has
    none, so the three checks are restated here over the plain set.)
    """
    errors: list[TaskFieldError] = []
    seen: set[str] = set()
    for index, step in enumerate(steps):
        field = f"steps[{index}].step_id"
        if step.step_id in seen:
            errors.append(
                TaskFieldError(
                    TaskErrorCode.DUPLICATE_STEP_ID,
                    field,
                    f"duplicate step key '{step.step_id}' in the initial plan",
                )
            )
        seen.add(step.step_id)
    ids = frozenset(seen)
    for index, step in enumerate(steps):
        for position, dep in enumerate(step.depends_on):
            if dep not in ids:
                errors.append(
                    TaskFieldError(
                        TaskErrorCode.UNKNOWN_DEPENDENCY,
                        f"steps[{index}].depends_on[{position}]",
                        f"dependency '{dep}' is not in the initial plan",
                    )
                )
    errors.extend(_graph_cycle_errors(ids, {s.step_id: s.depends_on for s in steps}))
    return errors


def _graph_cycle_errors(
    ids: Collection[str], edges: Mapping[str, Sequence[str]]
) -> list[TaskFieldError]:
    """Cycle detection over one dependency graph (DFS; §7 no cycles)."""
    state: dict[str, int] = {}  # 1 = on stack, 2 = done
    stack: list[str] = []
    errors: list[TaskFieldError] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for dep in edges.get(node, ()):
            if dep not in ids:
                continue
            mark = state.get(dep)
            if mark == 1:
                cycle_start = stack.index(dep)
                cycle = " -> ".join([*stack[cycle_start:], dep])
                errors.append(
                    TaskFieldError(
                        TaskErrorCode.DEPENDENCY_CYCLE,
                        "steps",
                        f"dependency cycle in the plan graph: {cycle}",
                    )
                )
            elif mark is None:
                visit(dep)
        stack.pop()
        state[node] = 2

    for node in sorted(ids):
        if state.get(node) is None:
            visit(node)
    return errors


def _post_patch_graph_errors(
    active: Sequence[StepRow | _StepState],
) -> list[TaskFieldError]:
    """Post-application DAG check over the resulting active plan (§7).

    The ratified patch validator checks the patch-internal graph; after
    an update/supersede the FULL active graph must still be acyclic and
    dangling-reference free (e.g. an update pointing a dependency at a
    step the same patch supersedes).
    """
    ids = {step.step_id for step in active}
    errors: list[TaskFieldError] = []
    for step in active:
        for position, dep in enumerate(step.depends_on):
            if dep not in ids:
                errors.append(
                    TaskFieldError(
                        TaskErrorCode.UNKNOWN_DEPENDENCY,
                        f"steps[{step.step_id}].depends_on[{position}]",
                        f"dependency '{dep}' is not an active step "
                        "after the patch",
                    )
                )
    errors.extend(
        _graph_cycle_errors(ids, {s.step_id: s.depends_on for s in active})
    )
    return errors


def _done_step_conflicts(
    patch: TaskPlanPatch,
    active: Mapping[str, StepRow],
) -> list[TaskFieldError]:
    """Model-authority guard (§6, P3): a plan patch can never rewrite a
    completed step (``executed`` status or ``passed`` verification).
    Completed work is recorded; a replan must supersede it with an
    explicit USER correction, and even then the old row stays recorded.
    """
    errors: list[TaskFieldError] = []
    for index, operation in enumerate(patch.operations):
        if operation.op == "add":
            continue
        target = (
            operation.supersedes_step_id
            if operation.op == "supersede"
            else operation.step.step_id
        )
        current = active.get(target) if target is not None else None
        if current is None:
            continue
        if current.status is StepStatus.EXECUTED or (
            current.verification is VerificationStatus.PASSED
        ):
            errors.append(
                TaskFieldError(
                    TaskErrorCode.INVALID_TASK_PATCH,
                    f"operations[{index}].step.step_id",
                    f"step '{target}' is completed work "
                    "(executed/passed) and cannot be rewritten by a plan "
                    "patch; an explicit user correction is required",
                )
            )
    return errors


def _validate_text_bound(
    value: str | None, bound: int, name: str
) -> None:
    """Plain-shape bound checks for non-Pydantic store parameters."""
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string when set")
    if len(value) > bound:
        raise ValueError(f"{name} must be at most {bound} characters")


def _validate_step_id(step_id: str) -> None:
    if not isinstance(step_id, str) or not _STABLE_ID_RE.fullmatch(step_id):
        raise ValueError("step_id must match the stable ID pattern")


def _evidence_json(refs: Sequence[EvidenceRef]) -> str:
    return json.dumps(
        [
            {
                "kind": ref.kind.value,
                "evidence_id": str(ref.evidence_id),
                "note": ref.note,
            }
            for ref in refs
        ]
    )


def _merge_evidence(
    current: Sequence[EvidenceRef], incoming: Sequence[EvidenceRef]
) -> list[EvidenceRef]:
    """Union by ``(kind, evidence_id)``, current order first (§7: an item's
    evidence accumulates across its lifecycle; a verdict never erases the
    execution evidence that came before it)."""
    merged: list[EvidenceRef] = list(current)
    seen = {(ref.kind, ref.evidence_id) for ref in current}
    for ref in incoming:
        if (ref.kind, ref.evidence_id) not in seen:
            merged.append(ref)
            seen.add((ref.kind, ref.evidence_id))
    return merged


# ---------------------------------------------------------------------------
# task identity (§1)
# ---------------------------------------------------------------------------


async def acreate_task(
    sessions: async_sessionmaker[AsyncSession],
    *,
    server_id: uuid.UUID,
    conversation_id: uuid.UUID,
    title: str,
    summary: str | None = None,
    requested_phase: Phase = Phase.PLAN,
    steps: Sequence[TaskPlanStep] = (),
    constraints: Sequence[ConstraintProposal] = (),
    criteria: Sequence[AcceptanceCriterion] = (),
    source_message_id: uuid.UUID | None = None,
    client_request_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
) -> TaskMutationResult:
    """Create one task episode pinned to its conversation scope (§1).

    V1 keeps ONE active task per conversation: creation serializes under
    the conversation-row lock (the same lock chat submissions use) and
    fails when another open episode exists. A retransmitted intake (same
    ``client_request_id``) replays onto the SAME task row (C4) — a same
    key with a different initial payload is the stable conflict.

    The initial plan/constraints/criteria sets are validated with the
    same deterministic rules as patches (duplicate IDs, unknown
    dependencies, cycles, supersede-at-create rejection) and every
    initial evidence reference is existence-checked (P4).
    """
    _validate_text_bound(title, TITLE_MAX_CHARS, "title")
    _validate_text_bound(summary, SUMMARY_MAX_CHARS, "summary")
    if not isinstance(requested_phase, Phase):
        raise ValueError("requested_phase must be a Phase value")
    if len(steps) > MAX_PATCH_STEPS:
        raise ValueError(f"the initial plan carries at most {MAX_PATCH_STEPS} steps")
    if len(constraints) > MAX_CONSTRAINT_PROPOSALS:
        raise ValueError(
            f"the initial constraints carry at most {MAX_CONSTRAINT_PROPOSALS} entries"
        )
    if len(criteria) > MAX_CRITERIA:
        raise ValueError(f"the task carries at most {MAX_CRITERIA} criteria")
    for index, proposal in enumerate(constraints):
        if proposal.suggested != "add":
            raise _invalid(
                f"constraints[{index}].suggested",
                "task creation admits add proposals only",
            )
    structural = _step_set_errors(steps)
    if structural:
        raise TaskContractError(structural)

    payload = {
        "title": title,
        "summary": summary,
        "requested_phase": requested_phase.value,
        "steps": [step.model_dump(mode="json") for step in steps],
        "constraints": [c.model_dump(mode="json") for c in constraints],
        "criteria": [c.model_dump(mode="json") for c in criteria],
        "source_message_id": (
            str(source_message_id) if source_message_id else None
        ),
    }
    fingerprint = _fingerprint(payload)
    tid = task_id or uuid.uuid4()
    async with sessions() as session, session.begin():
        # Serialize with chat submissions and sibling task creations on
        # the same conversation (one active task per conversation, §1).
        conversation = (
            await session.execute(
                text(
                    "SELECT conversation_id, server_id FROM conversations "
                    "WHERE conversation_id = :cid FOR UPDATE"
                ),
                {"cid": conversation_id},
            )
        ).mappings().first()
        if conversation is None:
            raise _not_found()
        assert_task_scope(server_id, conversation["server_id"])  # type: ignore[arg-type]
        if client_request_id is not None:
            existing = (
                await session.execute(
                    text(
                        f"SELECT {_TASK_COLUMNS} FROM agent_tasks "
                        "WHERE conversation_id = :cid "
                        "AND client_request_id = :crid"
                    ),
                    {"cid": conversation_id, "crid": client_request_id},
                )
            ).mappings().first()
            if existing is not None:
                prior = await _aidempotent_revision(
                    session, existing["task_id"], client_request_id  # type: ignore[arg-type]
                )
                if prior is None or prior.request_fingerprint != fingerprint:
                    raise _replay_conflict("client_request_id")
                return TaskMutationResult(
                    task=_row_to_task(existing),
                    revision=prior,
                    disposition=TurnDisposition.DEDUPLICATED,
                )
        clash = (
            await session.execute(
                text(
                    "SELECT task_id FROM agent_tasks WHERE conversation_id "
                    "= :cid AND status IN "
                    f"{_ACTIVE_STATUSES_SQL} LIMIT 1"
                ),
                {"cid": conversation_id},
            )
        ).first()
        if clash is not None:
            raise ValueError(
                "the conversation already has an active task episode; "
                "close it before starting a new one"
            )
        if source_message_id is not None:
            known = (
                await session.execute(
                    text(
                        "SELECT 1 FROM conversation_messages "
                        "WHERE message_id = :mid AND conversation_id = :cid"
                    ),
                    {"mid": source_message_id, "cid": conversation_id},
                )
            ).first()
            if known is None:
                raise _invalid(
                    "source_message_id",
                    "source message does not exist in the task's conversation",
                )
        inserted = (
            await session.execute(
                text(
                    f"""
                    INSERT INTO agent_tasks
                        ({_TASK_COLUMNS})
                    VALUES (:tid, :cid, :sid, 1, :title, :summary,
                            :phase, 'in_progress', NULL, :crid, now(), now())
                    RETURNING {_TASK_COLUMNS}
                    """
                ),
                {
                    "tid": tid,
                    "cid": conversation_id,
                    "sid": server_id,
                    "title": title,
                    "summary": summary,
                    "phase": requested_phase.value,
                    "crid": client_request_id,
                },
            )
        ).mappings().one()
        task_map: Mapping[str, object] = dict(inserted)
        for position, step in enumerate(steps):
            await session.execute(
                text(
                    f"""
                    INSERT INTO agent_task_steps
                        ({_STEP_COLUMNS})
                    VALUES (:tid, :sid, :title, :status, :verification,
                            :position, :risk, :prereq, :check, NULL, NULL,
                            CAST(:evidence AS jsonb), CAST(:deps AS jsonb),
                            NULL, NULL, now(), now())
                    """
                ),
                {
                    "tid": tid,
                    "sid": step.step_id,
                    "title": step.title,
                    "status": step.status.value,
                    "verification": step.verification.value,
                    "position": position,
                    "risk": step.risk.value,
                    "prereq": step.prerequisite_unresolved,
                    "check": step.expected_check,
                    "evidence": _evidence_json(step.evidence_refs),
                    "deps": json.dumps(list(step.depends_on)),
                },
            )
            if step.evidence_refs:
                await _acheck_and_register_evidence(
                    session,
                    task_map,
                    step.evidence_refs,
                    f"steps[{position}].evidence_refs",
                    origin_step_id=step.step_id,
                )
        for index, proposal in enumerate(constraints):
            for ref_index, ref in enumerate(proposal.source_message_refs):
                known_ref = (
                    await session.execute(
                        text(
                            "SELECT 1 FROM conversation_messages "
                            "WHERE message_id = :mid "
                            "AND conversation_id = :cid"
                        ),
                        {"mid": ref, "cid": conversation_id},
                    )
                ).first()
                if known_ref is None:
                    raise _invalid(
                        f"constraints[{index}].source_message_refs[{ref_index}]",
                        "constraint source message does not exist in the "
                        "task's conversation",
                    )
            await session.execute(
                text(
                    f"""
                    INSERT INTO agent_task_constraints
                        ({_CONSTRAINT_COLUMNS})
                    VALUES (:tid, :cid2, :category, :statement, :origin,
                            CAST(:refs AS jsonb), :position, NULL, NULL,
                            now(), now())
                    """
                ),
                {
                    "tid": tid,
                    "cid2": proposal.constraint_id,
                    "category": proposal.category,
                    "statement": proposal.statement,
                    "origin": proposal.origin.value,
                    "refs": json.dumps(
                        [str(ref) for ref in proposal.source_message_refs]
                    ),
                    "position": index,
                },
            )
        for position, criterion in enumerate(criteria):
            await session.execute(
                text(
                    f"""
                    INSERT INTO agent_task_criteria
                        ({_CRITERION_COLUMNS})
                    VALUES (:tid, :cid3, :statement, :verification,
                            :position, now(), now())
                    """
                ),
                {
                    "tid": tid,
                    "cid3": criterion.criterion_id,
                    "statement": criterion.statement,
                    "verification": criterion.verification.value,
                    "position": position,
                },
            )
        revision_row = await _ainsert_revision(
            session,
            task_id=tid,
            revision=1,
            kind="create",
            author="system",
            idempotency_key=client_request_id,
            request_fingerprint=fingerprint,
            source_message_id=source_message_id,
            payload={"kind": "create", "title": title},
        )
        await session.execute(
            text(
                "UPDATE conversations SET active_task_id = :tid, "
                "updated_at = now() WHERE conversation_id = :cid"
            ),
            {"tid": tid, "cid": conversation_id},
        )
    return TaskMutationResult(
        task=await _achecked_task(sessions, tid, server_id=server_id),
        revision=revision_row,
        disposition=TurnDisposition.CREATED,
    )


async def _ainsert_revision(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    revision: int,
    kind: str,
    author: str,
    idempotency_key: uuid.UUID | None,
    request_fingerprint: str | None,
    source_message_id: uuid.UUID | None = None,
    item_id: str | None = None,
    payload: Mapping[str, object] | None = None,
) -> RevisionRow:
    """Insert one append-only revision-log row and bump the header."""
    if kind not in REVISION_KINDS:
        raise ValueError(f"unknown revision kind {kind!r}")
    if author not in AUTHORS:
        raise ValueError(f"unknown revision author {author!r}")
    inserted = (
        await session.execute(
            text(
                f"""
                INSERT INTO agent_task_revisions
                    ({_REVISION_COLUMNS})
                VALUES (:tid, :rev, :kind, :author, :key, :fingerprint,
                        :smid, :item, CAST(:payload AS jsonb), now())
                RETURNING {_REVISION_COLUMNS}
                """
            ),
            {
                "tid": task_id,
                "rev": revision,
                "kind": kind,
                "author": author,
                "key": idempotency_key,
                "fingerprint": request_fingerprint,
                "smid": source_message_id,
                "item": item_id,
                "payload": (
                    json.dumps(dict(payload), default=str)
                    if payload is not None
                    else None
                ),
            },
        )
    ).mappings().one()
    return _row_to_revision(inserted)


# ---------------------------------------------------------------------------
# reads (§7 task.read / §8 GET task; C8 fails closed)
# ---------------------------------------------------------------------------


async def aget_task(
    sessions: async_sessionmaker[AsyncSession],
    task_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
) -> TaskRow:
    """Scope-checked singleton read (C8 fails closed on scope mismatch)."""
    return await _achecked_task(sessions, task_id, server_id=server_id)


async def aget_task_view(
    sessions: async_sessionmaker[AsyncSession],
    task_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
) -> TaskView:
    """Compact §8 header view: revision, phase, status and list counts —
    item/constraint/evidence collections are fetched as explicit pages,
    never embedded."""
    task = await _achecked_task(sessions, task_id, server_id=server_id)
    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM agent_task_steps
                         WHERE task_id = :tid AND superseded_at IS NULL)
                            AS steps_total,
                        (SELECT COUNT(*) FROM agent_task_steps
                         WHERE task_id = :tid AND superseded_at IS NULL
                               AND status = 'executed') AS steps_executed,
                        (SELECT COUNT(*) FROM agent_task_constraints
                         WHERE task_id = :tid AND superseded_at IS NULL)
                            AS constraints_total,
                        (SELECT COUNT(*) FROM agent_task_evidence
                         WHERE task_id = :tid) AS evidence_total,
                        (SELECT COUNT(*) FROM agent_task_criteria
                         WHERE task_id = :tid) AS criteria_total,
                        (SELECT COUNT(*) FROM agent_task_criteria
                         WHERE task_id = :tid AND verification = 'passed')
                            AS criteria_verified
                    """
                ),
                {"tid": task.task_id},
            )
        ).mappings().one()
    counts = TaskCounts(
        steps_total=int(row["steps_total"]),
        steps_executed=int(row["steps_executed"]),
        constraints_total=int(row["constraints_total"]),
        evidence_total=int(row["evidence_total"]),
        criteria_total=int(row["criteria_total"]),
        criteria_verified=int(row["criteria_verified"]),
    )
    return TaskView(
        task_id=task.task_id,
        conversation_id=task.conversation_id,
        server_id=task.server_id,
        revision=task.revision,
        title=task.title,
        summary=task.summary,
        requested_phase=task.requested_phase,
        status=task.status,
        active_step_id=task.active_step_id,
        counts=counts,
        created_at=task.created_at.isoformat(),
        updated_at=task.updated_at.isoformat(),
    )


async def alist_steps(
    sessions: async_sessionmaker[AsyncSession],
    task_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    include_superseded: bool = False,
) -> StepsPage:
    """Deterministic plan-step page (§8 ``GET /tasks/{id}/items``).

    Ordered ``(position, step_id)``; ``total`` is the ``COUNT(*)`` under
    the identical predicate. Superseded rows (recorded history, P3) are
    excluded unless ``include_superseded`` is set.
    """
    _check_page_bounds(limit, offset)
    task = await _achecked_task(sessions, task_id, server_id=server_id)
    predicate = "WHERE task_id = :tid"
    if not include_superseded:
        predicate += " AND superseded_at IS NULL"
    params: dict[str, object] = {"tid": task.task_id, "l": limit, "o": offset}
    async with sessions() as session:
        total = int(
            (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM agent_task_steps " + predicate
                    ),
                    params,
                )
            ).scalar_one()
        )
        rows = (
            await session.execute(
                text(
                    f"SELECT {_STEP_COLUMNS} FROM agent_task_steps "
                    f"{predicate} ORDER BY position, step_id "
                    "LIMIT :l OFFSET :o"
                ),
                params,
            )
        ).mappings().all()
    items = tuple(_row_to_step(row) for row in rows)
    return StepsPage(
        task_id=task.task_id,
        limit=limit,
        offset=offset,
        total=total,
        has_more=compute_has_more(offset, len(items), total),
        items=items,
    )


async def alist_constraints(
    sessions: async_sessionmaker[AsyncSession],
    task_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    include_superseded: bool = False,
) -> ConstraintsPage:
    """Deterministic constraint page (§8 ``GET /tasks/{id}/constraints``),
    ordered ``(position, constraint_id)`` with the same-predicate total."""
    _check_page_bounds(limit, offset)
    task = await _achecked_task(sessions, task_id, server_id=server_id)
    predicate = "WHERE task_id = :tid"
    if not include_superseded:
        predicate += " AND superseded_at IS NULL"
    params: dict[str, object] = {"tid": task.task_id, "l": limit, "o": offset}
    async with sessions() as session:
        total = int(
            (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM agent_task_constraints "
                        + predicate
                    ),
                    params,
                )
            ).scalar_one()
        )
        rows = (
            await session.execute(
                text(
                    f"SELECT {_CONSTRAINT_COLUMNS} FROM "
                    f"agent_task_constraints {predicate} "
                    "ORDER BY position, constraint_id LIMIT :l OFFSET :o"
                ),
                params,
            )
        ).mappings().all()
    items = tuple(_row_to_constraint(row) for row in rows)
    return ConstraintsPage(
        task_id=task.task_id,
        limit=limit,
        offset=offset,
        total=total,
        has_more=compute_has_more(offset, len(items), total),
        items=items,
    )


async def alist_evidence(
    sessions: async_sessionmaker[AsyncSession],
    task_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
) -> EvidencePage:
    """Deterministic evidence-registry page (§8 ``GET /tasks/{id}/evidence``),
    ordered ``(created_at, evidence_id)`` with the same-predicate total."""
    _check_page_bounds(limit, offset)
    task = await _achecked_task(sessions, task_id, server_id=server_id)
    params: dict[str, object] = {"tid": task.task_id, "l": limit, "o": offset}
    async with sessions() as session:
        total = int(
            (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM agent_task_evidence "
                        "WHERE task_id = :tid"
                    ),
                    params,
                )
            ).scalar_one()
        )
        rows = (
            await session.execute(
                text(
                    f"SELECT {_EVIDENCE_COLUMNS} FROM agent_task_evidence "
                    "WHERE task_id = :tid "
                    "ORDER BY created_at, evidence_id LIMIT :l OFFSET :o"
                ),
                params,
            )
        ).mappings().all()
    items = tuple(_row_to_evidence(row) for row in rows)
    return EvidencePage(
        task_id=task.task_id,
        limit=limit,
        offset=offset,
        total=total,
        has_more=compute_has_more(offset, len(items), total),
        items=items,
    )


async def alist_revisions(
    sessions: async_sessionmaker[AsyncSession],
    task_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
) -> RevisionsPage:
    """Deterministic revision-log page (§8), newest revision first."""
    _check_page_bounds(limit, offset)
    task = await _achecked_task(sessions, task_id, server_id=server_id)
    params: dict[str, object] = {"tid": task.task_id, "l": limit, "o": offset}
    async with sessions() as session:
        total = int(
            (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM agent_task_revisions "
                        "WHERE task_id = :tid"
                    ),
                    params,
                )
            ).scalar_one()
        )
        rows = (
            await session.execute(
                text(
                    f"SELECT {_REVISION_COLUMNS} FROM agent_task_revisions "
                    "WHERE task_id = :tid "
                    "ORDER BY revision DESC LIMIT :l OFFSET :o"
                ),
                params,
            )
        ).mappings().all()
    items = tuple(_row_to_revision(row) for row in rows)
    return RevisionsPage(
        task_id=task.task_id,
        limit=limit,
        offset=offset,
        total=total,
        has_more=compute_has_more(offset, len(items), total),
        items=items,
    )


async def alist_completions(
    sessions: async_sessionmaker[AsyncSession],
    task_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
) -> CompletionPage:
    """Deterministic completion-receipt page (§9), newest first."""
    _check_page_bounds(limit, offset)
    task = await _achecked_task(sessions, task_id, server_id=server_id)
    params: dict[str, object] = {"tid": task.task_id, "l": limit, "o": offset}
    async with sessions() as session:
        total = int(
            (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM agent_task_completions "
                        "WHERE task_id = :tid"
                    ),
                    params,
                )
            ).scalar_one()
        )
        rows = (
            await session.execute(
                text(
                    f"SELECT {_COMPLETION_COLUMNS} FROM "
                    "agent_task_completions WHERE task_id = :tid "
                    "ORDER BY created_at, completion_id LIMIT :l OFFSET :o"
                ),
                params,
            )
        ).mappings().all()
    items = tuple(_row_to_completion(row) for row in rows)
    return CompletionPage(
        task_id=task.task_id,
        limit=limit,
        offset=offset,
        total=total,
        has_more=compute_has_more(offset, len(items), total),
        items=items,
    )


# ---------------------------------------------------------------------------
# plan mutations (§7 task.propose_update; C4/C6/P3/P4)
# ---------------------------------------------------------------------------


@dataclass
class _StepState:
    """Mutable in-memory plan state while one patch is applied."""

    step_id: str
    title: str
    status: StepStatus
    verification: VerificationStatus
    position: int
    risk: ActionRisk
    prerequisite_unresolved: bool
    expected_check: str | None
    skip_reason: str | None
    result_summary: str | None
    evidence_refs: list[EvidenceRef]
    depends_on: list[str]
    is_new: bool = False
    fields_dirty: bool = False


class _PlanEditor:
    """Applies one bounded patch to the active plan in memory, then the
    transaction flushes the diff. Supersede never deletes: the old row is
    marked and stays recorded (P3)."""

    def __init__(self, rows: Sequence[StepRow]) -> None:
        self.order: list[str] = [row.step_id for row in rows]
        self.states: dict[str, _StepState] = {
            row.step_id: _StepState(
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
            )
            for row in rows
        }
        self.supersede_pairs: list[tuple[str, str]] = []

    def active(self) -> list[_StepState]:
        return [self.states[step_id] for step_id in self.order]

    def apply(self, patch: TaskPlanPatch) -> None:
        for index, operation in enumerate(patch.operations):
            step = operation.step
            if operation.op == "add":
                state = _StepState(
                    step_id=step.step_id,
                    title=step.title,
                    status=step.status,
                    verification=step.verification,
                    position=(
                        operation.position
                        if operation.position is not None
                        else len(self.order)
                    ),
                    risk=step.risk,
                    prerequisite_unresolved=step.prerequisite_unresolved,
                    expected_check=step.expected_check,
                    skip_reason=step.skip_reason,
                    result_summary=None,
                    evidence_refs=list(step.evidence_refs),
                    depends_on=list(step.depends_on),
                    is_new=True,
                )
                insert_at = min(state.position, len(self.order))
                self.order.insert(insert_at, state.step_id)
                self.states[state.step_id] = state
                continue
            target_id = (
                operation.supersedes_step_id
                if operation.op == "supersede"
                else step.step_id
            )
            current = self.states.get(target_id)
            if current is None:
                raise _invalid(
                    f"operations[{index}].step.step_id",
                    f"operation target step '{target_id}' does not "
                    "exist in the active plan",
                )
            if operation.op == "update":
                if step.status is not current.status:
                    raise _invalid(
                        f"operations[{index}].step.status",
                        "a plan patch cannot change item status; status "
                        "changes go through task.update_item",
                    )
                if step.verification is not current.verification:
                    raise _invalid(
                        f"operations[{index}].step.verification",
                        "a plan patch cannot change item verification; "
                        "verification changes are evidence-bound and go "
                        "through task.update_item or a completion verdict",
                    )
                current.title = step.title
                current.risk = step.risk
                current.prerequisite_unresolved = step.prerequisite_unresolved
                current.expected_check = step.expected_check
                current.evidence_refs = _merge_evidence(
                    current.evidence_refs, step.evidence_refs
                )
                current.depends_on = list(step.depends_on)
                if operation.position is not None:
                    current.position = operation.position
                current.fields_dirty = True
                continue
            # supersede: the new payload replaces the old item; the old
            # row is marked, never deleted (P3).
            new_state = _StepState(
                step_id=step.step_id,
                title=step.title,
                status=step.status,
                verification=step.verification,
                position=(
                    operation.position
                    if operation.position is not None
                    else current.position
                ),
                risk=step.risk,
                prerequisite_unresolved=step.prerequisite_unresolved,
                expected_check=step.expected_check,
                skip_reason=step.skip_reason,
                result_summary=None,
                evidence_refs=list(step.evidence_refs),
                depends_on=list(step.depends_on),
                is_new=True,
            )
            self.supersede_pairs.append((current.step_id, new_state.step_id))
            if current.is_new:
                # Superseding a step this same patch added: no DB row yet.
                self.order[self.order.index(current.step_id)] = (
                    new_state.step_id
                )
                del self.states[current.step_id]
            else:
                self.order[self.order.index(current.step_id)] = (
                    new_state.step_id
                )
            self.states[new_state.step_id] = new_state


async def apropose_update(
    sessions: async_sessionmaker[AsyncSession],
    task_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    author: Literal["user", "model"],
    patch: TaskPlanPatch,
    constraint_proposals: Sequence[ConstraintProposal] = (),
) -> TaskMutationResult:
    """Apply one bounded plan delta under CAS + idempotency (§7; C4/C6).

    One transaction: lock the task row, check scope, replay the caller's
    idempotency key, CAS the base revision, run the ratified patch and
    constraint-authority validators, guard completed work (model author),
    apply the delta, re-check the FULL dependency DAG, existence-check
    every evidence/source reference, then write header + rows + revision
    log atomically. A stale revision is the structured
    ``revision_conflict`` (C6) — never last-writer-wins.
    """
    if author not in ("user", "model"):
        raise ValueError("author must be 'user' or 'model'")
    fingerprint = _fingerprint(
        {
            "kind": "plan_patch",
            "author": author,
            "patch": patch.model_dump(mode="json"),
            "constraints": [
                proposal.model_dump(mode="json")
                for proposal in constraint_proposals
            ],
        }
    )
    async with sessions() as session, session.begin():
        row = await _alocked_task(session, task_id)
        assert_task_scope(server_id, row["server_id"])  # type: ignore[arg-type]
        prior = await _aidempotent_revision(session, task_id, patch.idempotency_key)
        if prior is not None:
            if prior.kind != "plan_patch" or prior.request_fingerprint != fingerprint:
                raise _replay_conflict("idempotency_key")
            return TaskMutationResult(
                task=_row_to_task(row),
                revision=prior,
                disposition=TurnDisposition.DEDUPLICATED,
            )
        check_revision(int(row["revision"]), patch.base_revision)
        active_rows = await _aactive_steps(session, task_id)
        active_constraints = await _aactive_constraints(session, task_id)
        known_step_ids = [row_.step_id for row_ in active_rows]
        validate_task_plan_patch(patch, known_step_ids)
        validate_constraint_proposals(
            constraint_proposals,
            {c.constraint_id: c.origin for c in active_constraints},
        )
        if author == "model":
            conflicts = _done_step_conflicts(
                patch, {r.step_id: r for r in active_rows}
            )
            if conflicts:
                raise TaskContractError(conflicts)
        editor = _PlanEditor(active_rows)
        editor.apply(patch)
        active_states = editor.active()
        for position, state in enumerate(active_states):
            state.position = position
        graph_errors = _post_patch_graph_errors(active_states)
        if graph_errors:
            raise TaskContractError(graph_errors)

        # Evidence honesty (P4): every referenced record must exist
        # BEFORE any mutation is written; the registry is then upserted.
        for index, operation in enumerate(patch.operations):
            if operation.step.evidence_refs:
                await _acheck_and_register_evidence(
                    session,
                    row,
                    operation.step.evidence_refs,
                    f"operations[{index}].step.evidence_refs",
                    origin_step_id=operation.step.step_id,
                )
        if patch.source_message_id is not None:
            known_source = (
                await session.execute(
                    text(
                        "SELECT 1 FROM conversation_messages "
                        "WHERE message_id = :mid AND conversation_id = :cid"
                    ),
                    {
                        "mid": patch.source_message_id,
                        "cid": row["conversation_id"],
                    },
                )
            ).first()
            if known_source is None:
                raise _invalid(
                    "source_message_id",
                    "patch source message does not exist in the task's "
                    "conversation",
                )
        for index, proposal in enumerate(constraint_proposals):
            for ref_index, ref in enumerate(proposal.source_message_refs):
                known_ref = (
                    await session.execute(
                        text(
                            "SELECT 1 FROM conversation_messages "
                            "WHERE message_id = :mid "
                            "AND conversation_id = :cid"
                        ),
                        {"mid": ref, "cid": row["conversation_id"]},
                    )
                ).first()
                if known_ref is None:
                    raise _invalid(
                        f"constraints_proposed[{index}]"
                        f".source_message_refs[{ref_index}]",
                        "constraint source message does not exist in the "
                        "task's conversation",
                    )

        # Two-phase position rewrite: move every active row far out of
        # the final range first (the partial UNIQUE index would otherwise
        # reject intermediate swaps), then write the dense finals.
        await session.execute(
            text(
                "UPDATE agent_task_steps SET position = position + 1000000 "
                "WHERE task_id = :tid AND superseded_at IS NULL"
            ),
            {"tid": task_id},
        )
        for old_id, new_id in editor.supersede_pairs:
            await session.execute(
                text(
                    "UPDATE agent_task_steps SET superseded_at = now(), "
                    "superseded_by = :new_id, updated_at = now() "
                    "WHERE task_id = :tid AND step_id = :old_id"
                ),
                {"tid": task_id, "old_id": old_id, "new_id": new_id},
            )
        for state in active_states:
            if state.is_new:
                continue
            await session.execute(
                text(
                    """
                    UPDATE agent_task_steps SET
                        title = :title, risk = :risk,
                        prerequisite_unresolved = :prereq,
                        expected_check = :check,
                        evidence_refs = CAST(:evidence AS jsonb),
                        depends_on = CAST(:deps AS jsonb),
                        position = :position, updated_at = now()
                    WHERE task_id = :tid AND step_id = :sid
                    """
                ),
                {
                    "tid": task_id,
                    "sid": state.step_id,
                    "title": state.title,
                    "risk": state.risk.value,
                    "prereq": state.prerequisite_unresolved,
                    "check": state.expected_check,
                    "evidence": _evidence_json(state.evidence_refs),
                    "deps": json.dumps(state.depends_on),
                    "position": state.position,
                },
            )
        for state in active_states:
            if not state.is_new:
                continue
            await session.execute(
                text(
                    f"""
                    INSERT INTO agent_task_steps ({_STEP_COLUMNS})
                    VALUES (:tid, :sid, :title, :status, :verification,
                            :position, :risk, :prereq, :check, :skip,
                            NULL, CAST(:evidence AS jsonb),
                            CAST(:deps AS jsonb), NULL, NULL, now(), now())
                    """
                ),
                {
                    "tid": task_id,
                    "sid": state.step_id,
                    "title": state.title,
                    "status": state.status.value,
                    "verification": state.verification.value,
                    "position": state.position,
                    "risk": state.risk.value,
                    "prereq": state.prerequisite_unresolved,
                    "check": state.expected_check,
                    "skip": state.skip_reason,
                    "evidence": _evidence_json(state.evidence_refs),
                    "deps": json.dumps(state.depends_on),
                },
            )

        active_constraint_count = len(active_constraints)
        for index, proposal in enumerate(constraint_proposals):
            if proposal.suggested == "supersede":
                assert proposal.supersedes_constraint_id is not None
                await session.execute(
                    text(
                        "UPDATE agent_task_constraints "
                        "SET superseded_at = now(), superseded_by = :new_id, "
                        "updated_at = now() "
                        "WHERE task_id = :tid AND constraint_id = :old_id "
                        "AND superseded_at IS NULL"
                    ),
                    {
                        "tid": task_id,
                        "old_id": proposal.supersedes_constraint_id,
                        "new_id": proposal.constraint_id,
                    },
                )
            await session.execute(
                text(
                    f"""
                    INSERT INTO agent_task_constraints
                        ({_CONSTRAINT_COLUMNS})
                    VALUES (:tid, :cid2, :category, :statement, :origin,
                            CAST(:refs AS jsonb), :position, NULL, NULL,
                            now(), now())
                    """
                ),
                {
                    "tid": task_id,
                    "cid2": proposal.constraint_id,
                    "category": proposal.category,
                    "statement": proposal.statement,
                    "origin": proposal.origin.value,
                    "refs": json.dumps(
                        [str(ref) for ref in proposal.source_message_refs]
                    ),
                    "position": active_constraint_count + index,
                },
            )

        header_updates = ["revision = revision + 1", "updated_at = now()"]
        params: dict[str, object] = {"tid": task_id}
        if patch.title is not None:
            header_updates.append("title = :title")
            params["title"] = patch.title
        if patch.summary is not None:
            header_updates.append("summary = :summary")
            params["summary"] = patch.summary
        updated = (
            await session.execute(
                text(
                    f"UPDATE agent_tasks SET {', '.join(header_updates)} "
                    f"WHERE task_id = :tid RETURNING {_TASK_COLUMNS}"
                ),
                params,
            )
        ).mappings().one()
        revision_row = await _ainsert_revision(
            session,
            task_id=task_id,
            revision=int(updated["revision"]),
            kind="plan_patch",
            author=author,
            idempotency_key=patch.idempotency_key,
            request_fingerprint=fingerprint,
            source_message_id=patch.source_message_id,
            payload={
                "kind": "plan_patch",
                "rationale": patch.rationale,
                "operations": [
                    {"op": op.op.value, "step_id": op.step.step_id}
                    for op in patch.operations
                ],
                "constraints": [
                    {
                        "suggested": p.suggested,
                        "constraint_id": p.constraint_id,
                        "supersedes_constraint_id": p.supersedes_constraint_id,
                    }
                    for p in constraint_proposals
                ],
            },
        )
        return TaskMutationResult(
            task=_row_to_task(updated),
            revision=revision_row,
            disposition=TurnDisposition.CREATED,
        )


# ---------------------------------------------------------------------------
# item mutations (§7 task.update_item)
# ---------------------------------------------------------------------------


async def aupdate_item(
    sessions: async_sessionmaker[AsyncSession],
    task_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    step_id: str,
    expected_revision: int,
    idempotency_key: uuid.UUID,
    status: StepStatus | None = None,
    verification: VerificationStatus | None = None,
    result_summary: str | None = None,
    evidence_refs: Sequence[EvidenceRef] = (),
    source_message_id: uuid.UUID | None = None,
    author: Literal["user", "model"] = "model",
) -> ItemUpdateResult:
    """Advance one plan item under CAS + idempotency (§7; C4/C6).

    Status and verification are EVIDENCE-BOUND: an executed/skipped item
    is frozen (completed work and recorded skips are never reopened, P3);
    a ``passed`` verification is never silently cleared; ``executed`` and
    ``passed``/``failed`` require evidence references (§7 — "done" is
    rendered from agreed checks, never a ticked boolean). Evidence is
    merged (union by record identity), so a verdict never erases the
    execution evidence that came before it. Every referenced record is
    existence-checked before any write (P4).
    """
    _validate_step_id(step_id)
    if author not in ("user", "model"):
        raise ValueError("author must be 'user' or 'model'")
    _validate_text_bound(result_summary, SUMMARY_MAX_CHARS, "result_summary")
    changes = [
        status is not None,
        verification is not None,
        result_summary is not None,
        bool(evidence_refs),
    ]
    if not any(changes):
        raise ValueError(
            "update_item requires at least one of status, verification, "
            "result_summary or evidence_refs"
        )
    if status is not None and not isinstance(status, StepStatus):
        raise ValueError("status must be a StepStatus value")
    if verification is not None and not isinstance(
        verification, VerificationStatus
    ):
        raise ValueError("verification must be a VerificationStatus value")
    fingerprint = _fingerprint(
        {
            "kind": "item_update",
            "author": author,
            "step_id": step_id,
            "status": status.value if status else None,
            "verification": verification.value if verification else None,
            "result_summary": result_summary,
            "evidence": [
                [ref.kind.value, str(ref.evidence_id), ref.note]
                for ref in evidence_refs
            ],
            "source_message_id": (
                str(source_message_id) if source_message_id else None
            ),
        }
    )
    async with sessions() as session, session.begin():
        row = await _alocked_task(session, task_id)
        assert_task_scope(server_id, row["server_id"])  # type: ignore[arg-type]
        prior = await _aidempotent_revision(session, task_id, idempotency_key)
        if prior is not None:
            if (
                prior.kind != "item_update"
                or prior.item_id != step_id
                or prior.request_fingerprint != fingerprint
            ):
                raise _replay_conflict("idempotency_key")
            step_row = await _astep_row(session, task_id, step_id)
            if step_row is None:
                raise _invalid("step_id", f"no step '{step_id}' in the task scope")
            return ItemUpdateResult(
                task=_row_to_task(row),
                step=step_row,
                revision=prior,
                disposition=TurnDisposition.DEDUPLICATED,
            )
        check_revision(int(row["revision"]), expected_revision)
        step = await _aactive_step(session, task_id, step_id)
        new_status = status if status is not None else step.status
        new_verification = (
            verification if verification is not None else step.verification
        )
        if new_status not in STEP_STATUS_TRANSITIONS[step.status]:
            raise _invalid(
                "status",
                f"status transition {step.status.value} -> "
                f"{new_status.value} is not legal (executed/skipped items "
                "are frozen recorded work)",
            )
        if (
            new_status is StepStatus.SKIPPED
            and not (step.skip_reason or "").strip()
        ):
            raise _invalid("status", "skipped requires a skip_reason")
        if new_verification not in VERIFICATION_TRANSITIONS[step.verification]:
            raise _invalid(
                "verification",
                f"verification transition {step.verification.value} -> "
                f"{new_verification.value} is not legal (a passed check is "
                "never silently cleared)",
            )
        merged = _merge_evidence(step.evidence_refs, evidence_refs)
        if new_status is StepStatus.EXECUTED and not merged:
            raise _invalid(
                "evidence_refs",
                "an executed item requires at least one evidence reference",
            )
        if new_verification in (
            VerificationStatus.PASSED,
            VerificationStatus.FAILED,
        ) and not merged:
            raise _invalid(
                "evidence_refs",
                "a passed/failed verification requires at least one "
                "evidence reference",
            )
        if source_message_id is not None:
            known = (
                await session.execute(
                    text(
                        "SELECT 1 FROM conversation_messages "
                        "WHERE message_id = :mid AND conversation_id = :cid"
                    ),
                    {"mid": source_message_id, "cid": row["conversation_id"]},
                )
            ).first()
            if known is None:
                raise _invalid(
                    "source_message_id",
                    "source message does not exist in the task's conversation",
                )
        await _acheck_and_register_evidence(
            session,
            row,
            evidence_refs,
            "evidence_refs",
            origin_step_id=step_id,
        )
        clear_active = (
            row["active_step_id"] == step_id
            and new_status in (StepStatus.EXECUTED, StepStatus.SKIPPED)
        )
        updated = (
            await session.execute(
                text(
                    f"""
                    UPDATE agent_tasks SET
                        revision = revision + 1, updated_at = now()
                        {", active_step_id = NULL" if clear_active else ""}
                    WHERE task_id = :tid RETURNING {_TASK_COLUMNS}
                    """
                ),
                {"tid": task_id},
            )
        ).mappings().one()
        new_revision = int(updated["revision"])
        step_updated = (
            await session.execute(
                text(
                    f"""
                    UPDATE agent_task_steps SET
                        status = :status, verification = :verification,
                        result_summary = COALESCE(:summary, result_summary),
                        evidence_refs = CAST(:evidence AS jsonb),
                        updated_at = now()
                    WHERE task_id = :tid AND step_id = :sid
                    RETURNING {_STEP_COLUMNS}
                    """
                ),
                {
                    "tid": task_id,
                    "sid": step_id,
                    "status": new_status.value,
                    "verification": new_verification.value,
                    "summary": result_summary,
                    "evidence": _evidence_json(merged),
                },
            )
        ).mappings().one()
        revision_row = await _ainsert_revision(
            session,
            task_id=task_id,
            revision=new_revision,
            kind="item_update",
            author=author,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            source_message_id=source_message_id,
            item_id=step_id,
            payload={
                "kind": "item_update",
                "step_id": step_id,
                "status": new_status.value,
                "verification": new_verification.value,
                "evidence_count": len(merged),
            },
        )
        return ItemUpdateResult(
            task=_row_to_task(updated),
            step=_row_to_step(step_updated),
            revision=revision_row,
            disposition=TurnDisposition.CREATED,
        )


async def _aactive_step(
    session: AsyncSession, task_id: uuid.UUID, step_id: str
) -> StepRow:
    """One active step row, fail-closed on a missing/unknown ID."""
    step = await _astep_row(session, task_id, step_id)
    if step is None or step.superseded_at is not None:
        raise _invalid(
            "step_id",
            f"no active step '{step_id}' in the task scope",
        )
    return step


async def _astep_row(
    session: AsyncSession, task_id: uuid.UUID, step_id: str
) -> StepRow:
    row = (
        await session.execute(
            text(
                f"SELECT {_STEP_COLUMNS} FROM agent_task_steps "
                "WHERE task_id = :tid AND step_id = :sid"
            ),
            {"tid": task_id, "sid": step_id},
        )
    ).mappings().first()
    return None if row is None else _row_to_step(row)


# ---------------------------------------------------------------------------
# completion verdicts (§9 task.propose_completion; no self-granted success)
# ---------------------------------------------------------------------------


async def apropose_completion(
    sessions: async_sessionmaker[AsyncSession],
    task_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    verdict: TaskCompletionVerdict,
    author: Literal["user", "model"] = "model",
) -> CompletionResult:
    """Record one validated completion verdict under CAS + idempotency
    (§9; P4/E2).

    ``verified`` can never be self-granted: every acceptance criterion
    must be covered by a PASSED, evidence-bound result — otherwise the
    stable ``completion_missing_evidence``/field rejections apply and
    the transaction has zero effects. Criterion verification advances
    only through legal transitions (a passed criterion is never
    overturned). Terminal outcomes close the episode and release the
    conversation's active-task pointer.
    """
    if author not in ("user", "model"):
        raise ValueError("author must be 'user' or 'model'")
    fingerprint = _fingerprint(
        {
            "kind": "completion",
            "author": author,
            "verdict": verdict.model_dump(mode="json"),
        }
    )
    async with sessions() as session, session.begin():
        row = await _alocked_task(session, task_id)
        assert_task_scope(server_id, row["server_id"])  # type: ignore[arg-type]
        prior = await _aidempotent_revision(
            session, task_id, verdict.idempotency_key
        )
        if prior is not None:
            if (
                prior.kind != "completion"
                or prior.request_fingerprint != fingerprint
            ):
                raise _replay_conflict("idempotency_key")
            completion = await _acompletion_row(
                session, task_id, verdict.idempotency_key
            )
            return CompletionResult(
                task=_row_to_task(row),
                completion=completion,
                revision=prior,
                disposition=TurnDisposition.DEDUPLICATED,
            )
        check_revision(int(row["revision"]), verdict.base_revision)
        criteria = (
            await session.execute(
                text(
                    f"SELECT {_CRITERION_COLUMNS} FROM agent_task_criteria "
                    "WHERE task_id = :tid ORDER BY position, criterion_id"
                ),
                {"tid": task_id},
            )
        ).mappings().all()
        criterion_rows = [_row_to_criterion(entry) for entry in criteria]
        evidence_rows = (
            await session.execute(
                text(
                    "SELECT evidence_id FROM agent_task_evidence "
                    "WHERE task_id = :tid"
                ),
                {"tid": task_id},
            )
        ).scalars().all()
        validate_completion_verdict(
            verdict,
            [criterion.criterion_id for criterion in criterion_rows],
            {str(entry) for entry in evidence_rows},
        )
        current_status = TaskOutcome(str(row["status"]))
        if verdict.task_outcome not in STATUS_TRANSITIONS[current_status]:
            raise _invalid(
                "task_outcome",
                f"task outcome transition {current_status.value} -> "
                f"{verdict.task_outcome.value} is not legal (terminal "
                "episodes are frozen)",
            )
        by_id = {criterion.criterion_id: criterion for criterion in criterion_rows}
        for index, result in enumerate(verdict.criterion_results):
            criterion = by_id[result.criterion_id]
            if result.result not in VERIFICATION_TRANSITIONS[criterion.verification]:
                raise _invalid(
                    f"criterion_results[{index}].result",
                    f"criterion '{result.criterion_id}' verification "
                    f"{criterion.verification.value} -> {result.result.value} "
                    "is not legal (a passed criterion is never overturned)",
                )
        clear_active = verdict.task_outcome in TASK_TERMINAL_OUTCOMES
        updated = (
            await session.execute(
                text(
                    f"""
                    UPDATE agent_tasks SET
                        revision = revision + 1, status = :outcome,
                        updated_at = now()
                        {", active_step_id = NULL" if clear_active else ""}
                    WHERE task_id = :tid RETURNING {_TASK_COLUMNS}
                    """
                ),
                {"tid": task_id, "outcome": verdict.task_outcome.value},
            )
        ).mappings().one()
        new_revision = int(updated["revision"])
        for result in verdict.criterion_results:
            await session.execute(
                text(
                    "UPDATE agent_task_criteria SET verification = :result, "
                    "updated_at = now() "
                    "WHERE task_id = :tid AND criterion_id = :cid"
                ),
                {
                    "tid": task_id,
                    "cid": result.criterion_id,
                    "result": result.result.value,
                },
            )
        completion_id = uuid.uuid4()
        await session.execute(
            text(
                f"""
                INSERT INTO agent_task_completions
                    ({_COMPLETION_COLUMNS})
                VALUES (:compid, :tid, :base, :rev, :key, :outcome,
                        CAST(:verdict AS jsonb), now())
                """
            ),
            {
                "compid": completion_id,
                "tid": task_id,
                "base": verdict.base_revision,
                "rev": new_revision,
                "key": verdict.idempotency_key,
                "outcome": verdict.task_outcome.value,
                "verdict": verdict.model_dump_json(),
            },
        )
        if clear_active:
            await session.execute(
                text(
                    "UPDATE conversations SET active_task_id = NULL, "
                    "updated_at = now() "
                    "WHERE active_task_id = :tid"
                ),
                {"tid": task_id},
            )
        revision_row = await _ainsert_revision(
            session,
            task_id=task_id,
            revision=new_revision,
            kind="completion",
            author=author,
            idempotency_key=verdict.idempotency_key,
            request_fingerprint=fingerprint,
            payload={
                "kind": "completion",
                "task_outcome": verdict.task_outcome.value,
                "criteria_covered": len(verdict.criterion_results),
            },
        )
        completion = await _acompletion_row(
            session, task_id, verdict.idempotency_key
        )
        return CompletionResult(
            task=_row_to_task(updated),
            completion=completion,
            revision=revision_row,
            disposition=TurnDisposition.CREATED,
        )


async def _acompletion_row(
    session: AsyncSession, task_id: uuid.UUID, idempotency_key: uuid.UUID
) -> CompletionRow:
    row = (
        await session.execute(
            text(
                f"SELECT {_COMPLETION_COLUMNS} FROM agent_task_completions "
                "WHERE task_id = :tid AND idempotency_key = :key"
            ),
            {"tid": task_id, "key": idempotency_key},
        )
    ).mappings().one()
    return _row_to_completion(row)


# ---------------------------------------------------------------------------
# header metadata mutations (§6: legal in every phase; §8 user controls)
# ---------------------------------------------------------------------------


async def aupdate_task_header(
    sessions: async_sessionmaker[AsyncSession],
    task_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    expected_revision: int,
    requested_phase: Phase | None = None,
    status: TaskOutcome | None = None,
    active_step_id: str | None = None,
    client_request_id: uuid.UUID | None = None,
    author: Literal["user", "system"] = "user",
) -> TaskMutationResult:
    """Metadata-only header update under CAS (§6/§8).

    Phase changes are advisory (never permission); status follows the
    frozen transition map (terminal episodes stay closed); the active
    step must be an active plan item. Metadata updates never touch
    grants, approvals or item states.
    """
    if author not in ("user", "system"):
        raise ValueError("author must be 'user' or 'system'")
    if requested_phase is None and status is None and active_step_id is None:
        raise ValueError(
            "header update requires at least one of requested_phase, "
            "status or active_step_id"
        )
    if requested_phase is not None and not isinstance(requested_phase, Phase):
        raise ValueError("requested_phase must be a Phase value")
    if status is not None and not isinstance(status, TaskOutcome):
        raise ValueError("status must be a TaskOutcome value")
    if active_step_id is not None:
        _validate_step_id(active_step_id)
    fingerprint = _fingerprint(
        {
            "kind": "metadata",
            "author": author,
            "requested_phase": (
                requested_phase.value if requested_phase else None
            ),
            "status": status.value if status else None,
            "active_step_id": active_step_id,
        }
    )
    async with sessions() as session, session.begin():
        row = await _alocked_task(session, task_id)
        assert_task_scope(server_id, row["server_id"])  # type: ignore[arg-type]
        if client_request_id is not None:
            prior = await _aidempotent_revision(
                session, task_id, client_request_id
            )
            if prior is not None:
                if (
                    prior.kind != "metadata"
                    or prior.request_fingerprint != fingerprint
                ):
                    raise _replay_conflict("client_request_id")
                return TaskMutationResult(
                    task=_row_to_task(row),
                    revision=prior,
                    disposition=TurnDisposition.DEDUPLICATED,
                )
        check_revision(int(row["revision"]), expected_revision)
        if status is not None:
            current_status = TaskOutcome(str(row["status"]))
            if status not in STATUS_TRANSITIONS[current_status]:
                raise _invalid(
                    "status",
                    f"status transition {current_status.value} -> "
                    f"{status.value} is not legal (terminal episodes are "
                    "frozen)",
                )
        if active_step_id is not None:
            await _aactive_step(session, task_id, active_step_id)
        assignments = ["revision = revision + 1", "updated_at = now()"]
        params: dict[str, object] = {"tid": task_id}
        if requested_phase is not None:
            assignments.append("requested_phase = :phase")
            params["phase"] = requested_phase.value
        if status is not None:
            assignments.append("status = :status")
            params["status"] = status.value
        if active_step_id is not None:
            assignments.append("active_step_id = :astep")
            params["astep"] = active_step_id
        updated = (
            await session.execute(
                text(
                    f"UPDATE agent_tasks SET {', '.join(assignments)} "
                    f"WHERE task_id = :tid RETURNING {_TASK_COLUMNS}"
                ),
                params,
            )
        ).mappings().one()
        if status is not None and status in TASK_TERMINAL_OUTCOMES:
            await session.execute(
                text(
                    "UPDATE conversations SET active_task_id = NULL, "
                    "updated_at = now() WHERE active_task_id = :tid"
                ),
                {"tid": task_id},
            )
        revision_row = await _ainsert_revision(
            session,
            task_id=task_id,
            revision=int(updated["revision"]),
            kind="metadata",
            author=author,
            idempotency_key=client_request_id,
            request_fingerprint=fingerprint,
            payload={
                "kind": "metadata",
                "requested_phase": (
                    requested_phase.value if requested_phase else None
                ),
                "status": status.value if status else None,
                "active_step_id": active_step_id,
            },
        )
        return TaskMutationResult(
            task=_row_to_task(updated),
            revision=revision_row,
            disposition=TurnDisposition.CREATED,
        )
