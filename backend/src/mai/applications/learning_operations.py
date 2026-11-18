"""Durable preservation operation admission, progression and recovery
enumeration over the frozen ``service_learning_operation`` receipt table
(ask_13, campaign D5, C1/C3/C5).

The table is the durable operation RECEIPT (NOT a second queue): DBOS
remains the single execution authority. This module:

1. admits one bounded :class:`LearnRequest` through :meth:`admit`:
   trusted source identity (actor, run, tool-call ids, timestamps) comes
   ONLY from the trusted runtime context and the database clock — never
   from the model payload; a deterministic operation/workflow identity
   is derived from ``(source_run_id, source_tool_call_id)``; an atomic
   per-run budget of :data:`MAX_PRESERVATION_OPERATIONS_PER_RUN`
   distinct operations is enforced under ``pg_advisory_xact_lock`` so
   concurrent admissions cannot overrun it; a duplicate observation of
   the SAME service + fact-key set with an IDENTICAL payload coalesces
   into the first durable receipt without consuming budget;
2. never reports ``accepted`` before the engine acknowledged the enqueue
   (C5): the row is committed ``queued`` FIRST, then the injected
   ``engine_enqueue`` seam is awaited; only a successful acknowledgment
   promotes the row to ``accepted``. A failed enqueue leaves the row
   ``queued`` (recoverable via :meth:`list_pending_recovery`) and raises
   the typed ``enqueue_failed`` refusal — replaying the same source call
   id retries the enqueue through the same deterministic workflow id.

HONEST SCOPE LIMITATION: the engine acknowledgment is an INJECTED SEAM
at this unit boundary (``engine_enqueue: Callable[[str], Awaitable[None]]``);
no DBOS client, worker or jobs handler is wired here. End-to-end durable
enqueue proof is the later integration slices' evidence, not this
module's. There is no scheduler and no polling loop here:
``list_pending_recovery`` is a bounded enumeration seam for worker
startup reconciliation, nothing else.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Final

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.applications.knowledge_contracts import (
    DEFAULT_PAGE_LIMIT,
    LearnRequest,
    MAX_PAGE_LIMIT,
    OperationPage,
    OperationReceipt,
    OperationStatus,
    ScopeMismatchError,
    SubEffectStatus,
    TrustedSourceContext,
    derive_operation_status,
    enforce_envelope_bound,
    validate_same_server_scope,
)

__all__ = [
    "AdmitOperationResult",
    "LearningOperationStore",
    "MAX_PRESERVATION_OPERATIONS_PER_RUN",
    "OperationAdmissionError",
]

#: Distinct preservation operations one source run may admit (C1, after
#: coalescing). Frozen for this campaign; defined here because
#: knowledge_contracts.py is frozen and this constant is operation-level.
MAX_PRESERVATION_OPERATIONS_PER_RUN: Final[int] = 16

#: Deterministic DBOS workflow identity prefix (frozen derivation).
_WORKFLOW_PREFIX: Final[str] = "service.learn"

#: Statuses that may never progress again (D5: honest terminal states).
_TERMINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {"applied", "failed", "partial", "unknown"}
)

#: Statuses a duplicate observation may coalesce into (terminal
#: failed/partial/unknown are NOT coalesced: re-preservation is a new op).
_COALESCABLE_STATUSES: Final[list[str]] = [
    "queued",
    "accepted",
    "applying",
    "applied",
]

#: Statuses an explicit source stop may cancel into a failed state (the
#: frozen status vocabulary has no 'cancelled' value; the error code
#: carries the semantics).
_CANCELLABLE_STATUSES: Final[list[str]] = ["queued", "accepted", "applying"]

#: Stable error code stamped on rows cancelled by an explicit source stop.
_CANCEL_ERROR_CODE: Final[str] = "source_cancelled"

#: Columns projected whenever a row is turned into a receipt.
_RECEIPT_COLUMNS: Final[str] = (
    "operation_id, server_id, application_id, generation, payload_digest, "
    "workflow_id, status, sub_effects, accepted_at"
)


# ---------------------------------------------------------------------------
# typed error family (stable codes, value-free)
# ---------------------------------------------------------------------------


class OperationAdmissionError(ValueError):
    """One value-free admission/progression refusal with a stable
    ``code`` in {payload_conflict, operation_budget_exceeded,
    enqueue_failed, status_regression, unknown_operation}."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        #: Honest budget figures, set on operation_budget_exceeded refusals.
        self.budget_used: int | None = None
        self.budget_limit: int | None = None


# ---------------------------------------------------------------------------
# result envelopes (fail-closed, < 64 KiB)
# ---------------------------------------------------------------------------


class AdmitOperationResult(BaseModel):
    """The frozen-shape result of one admission (fresh, replayed or
    coalesced) — always carries the durable receipt plus honest budget
    figures."""

    model_config = ConfigDict(extra="forbid")

    receipt: OperationReceipt = Field(
        description="Durable receipt admitted, replayed or coalesced by this call",
    )
    replayed: bool = Field(
        description="True when an existing row for the same source call id was returned",
    )
    coalesced: bool = Field(
        description="True when a duplicate identical observation merged into an existing operation",
    )
    budget_used: int = Field(
        description="Distinct operations admitted for the source run after this admission",
    )
    budget_limit: int = Field(
        description="Frozen maximum of distinct preservation operations per source run",
    )


class _PageQuery(BaseModel):
    """Bounded page-request validation for collection reads (AGENTS.md
    pagination invariant: limit 1..50, offset >= 0)."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(
        default=DEFAULT_PAGE_LIMIT,
        ge=1,
        le=MAX_PAGE_LIMIT,
        description="Maximum number of records returned inside this page",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Zero-based offset of this page into the ordered collection",
    )


# ---------------------------------------------------------------------------
# frozen derivations (pure)
# ---------------------------------------------------------------------------


def _payload_digest(request: LearnRequest) -> str:
    """Canonical payload hash: SHA-256 over the pydantic JSON of the
    accepted request (64 lowercase hex, migration CHECK compatible)."""
    return hashlib.sha256(request.model_dump_json().encode("utf-8")).hexdigest()


def _operation_id(trusted: TrustedSourceContext) -> uuid.UUID:
    """Deterministic operation identity from run + persisted tool-call
    id (D5/C5): identical retries derive the identical identity."""
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"mai:learning-operation:{trusted.run_id}:{trusted.tool_call_id}",
    )


def _workflow_id(operation_id: uuid.UUID) -> str:
    """Deterministic DBOS workflow identity (1..256, UNIQUE column)."""
    return f"{_WORKFLOW_PREFIX}:{operation_id}"


def _coalesce_key(
    server_id: uuid.UUID,
    application_id: uuid.UUID | None,
    request: LearnRequest,
) -> str:
    """Bounded (<= 256) coalescing key from the stable service identity
    plus the sorted fact ``(category, key)`` set of the request."""
    pairs = sorted({f"{delta.category}:{delta.key}" for delta in request.facts})
    tail = hashlib.sha256("|".join(pairs).encode("utf-8")).hexdigest()
    app = "new" if application_id is None else str(application_id)
    return f"{server_id}:{app}:{tail}"[:256]


def _row_to_receipt(row: Mapping[str, object]) -> OperationReceipt:
    """Project one ``service_learning_operation`` row onto the frozen
    receipt envelope (< 64 KiB enforced)."""
    raw_effects = row["sub_effects"] or {}
    sub_effects = {
        str(key): SubEffectStatus(str(value))
        for key, value in dict(raw_effects).items()
    }
    receipt = OperationReceipt(
        operation_id=uuid.UUID(str(row["operation_id"])),
        status=OperationStatus(str(row["status"])),
        payload_digest=str(row["payload_digest"]),
        sub_effects=sub_effects,
        accepted_at=row["accepted_at"],  # type: ignore[arg-type]  # DB datetime
        workflow_id=str(row["workflow_id"]),
    )
    enforce_envelope_bound(receipt)
    return receipt


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------


class LearningOperationStore:
    """Durable admission/progression seam over the 0018 receipt table
    (async session factory injected; one transaction per mutation; the
    engine enqueue is a per-call injected async seam)."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    # -- admission ---------------------------------------------------------

    async def admit(
        self,
        *,
        request: LearnRequest,
        trusted: TrustedSourceContext,
        application_id: uuid.UUID | None,
        engine_enqueue: Callable[[str], Awaitable[None]],
    ) -> AdmitOperationResult:
        """Admit one bounded learn request as a durable preservation
        operation.

        Flow (frozen): scope guard -> per-run advisory lock -> same call
        id replay (identical digest replays the stored receipt; a queued
        row retries the enqueue; a divergent digest is a typed
        ``payload_conflict`` and the stored row is untouched) -> duplicate
        coalescing BEFORE the budget -> atomic per-run budget ->
        INSERT the ``queued`` receipt and COMMIT -> await the engine
        enqueue seam -> promote to ``accepted`` ONLY on acknowledgment.
        Actor, grants and timestamps never come from the payload: the
        row's actor is ``trusted.actor`` and ``accepted_at`` is the
        database ``now()``."""
        validate_same_server_scope(None, trusted.server_id)
        digest = _payload_digest(request)
        operation_id = _operation_id(trusted)
        workflow_id = _workflow_id(operation_id)
        coalesce_key = _coalesce_key(trusted.server_id, application_id, request)

        replay_row: Mapping[str, object] | None = None
        settled: AdmitOperationResult | None = None
        budget = 0

        async with self._sessions() as session:
            async with session.begin():
                # Serialize admissions for one run: the per-run budget
                # check + insert become atomic under the transaction-
                # scoped advisory lock (D5 "atomically").
                await session.execute(
                    sa.text(
                        "SELECT pg_advisory_xact_lock("
                        "hashtextextended(CAST(:rid AS text), 0))"
                    ),
                    {"rid": str(trusted.run_id)},
                )
                existing = (
                    await session.execute(
                        sa.text(
                            f"SELECT {_RECEIPT_COLUMNS} FROM "
                            "service_learning_operation WHERE source_run_id = :r "
                            "AND source_tool_call_id = :c"
                        ),
                        {"r": trusted.run_id, "c": trusted.tool_call_id},
                    )
                ).mappings().first()
                budget = await self._run_budget(session, trusted.run_id)
                if existing is not None:
                    # Replay branch: same source call id. Scope first
                    # (reject, never substitute), then payload identity.
                    existing_server = uuid.UUID(str(existing["server_id"]))
                    if existing_server != trusted.server_id:
                        raise ScopeMismatchError(existing_server, trusted.server_id)
                    if str(existing["payload_digest"]) != digest:
                        raise OperationAdmissionError(
                            "payload_conflict",
                            "the same source call id was already admitted "
                            "with a different payload; the stored operation "
                            "is never overwritten",
                        )
                    if str(existing["status"]) == "queued":
                        # Admitted but never acknowledged: reconciliation
                        # through the SAME deterministic workflow id.
                        replay_row = existing
                    else:
                        settled = AdmitOperationResult(
                            receipt=_row_to_receipt(existing),
                            replayed=True,
                            coalesced=False,
                            budget_used=budget,
                            budget_limit=MAX_PRESERVATION_OPERATIONS_PER_RUN,
                        )
                else:
                    # Fresh or duplicate observation: coalescing happens
                    # BEFORE the budget check, so an identical duplicate
                    # never wastes a budget slot.
                    coalesced = (
                        await session.execute(
                            sa.text(
                                f"SELECT {_RECEIPT_COLUMNS} FROM "
                                "service_learning_operation WHERE "
                                "source_run_id = :r AND coalesce_key = :k "
                                "AND server_id = :s AND status = ANY(:st) "
                                "AND payload_digest = :d "
                                "ORDER BY accepted_at ASC, operation_id ASC "
                                "LIMIT 1"
                            ),
                            {
                                "r": trusted.run_id,
                                "k": coalesce_key,
                                "s": trusted.server_id,
                                "st": _COALESCABLE_STATUSES,
                                "d": digest,
                            },
                        )
                    ).mappings().first()
                    if coalesced is not None:
                        settled = AdmitOperationResult(
                            receipt=_row_to_receipt(coalesced),
                            replayed=False,
                            coalesced=True,
                            budget_used=budget,
                            budget_limit=MAX_PRESERVATION_OPERATIONS_PER_RUN,
                        )
                    else:
                        # Coalescing happened BEFORE the budget: a
                        # duplicate never wastes a budget slot.
                        if budget >= MAX_PRESERVATION_OPERATIONS_PER_RUN:
                            error = OperationAdmissionError(
                                "operation_budget_exceeded",
                                f"the source run already carries {budget} "
                                "distinct admitted preservation operations; "
                                f"the frozen per-run budget is "
                                f"{MAX_PRESERVATION_OPERATIONS_PER_RUN}",
                            )
                            error.budget_used = budget
                            error.budget_limit = MAX_PRESERVATION_OPERATIONS_PER_RUN
                            raise error
                        await session.execute(
                            sa.text(
                                "INSERT INTO service_learning_operation "
                                "(operation_id, server_id, application_id, "
                                "generation, payload_digest, workflow_id, "
                                "status, sub_effects, coalesce_key, "
                                "source_run_id, source_tool_call_id, actor) "
                                "VALUES (:o, :s, :a, :g, :d, :w, 'queued', "
                                "'{}'::jsonb, :k, :r, :c, :actor) "
                                "RETURNING accepted_at"
                            ),
                            {
                                "o": operation_id,
                                "s": trusted.server_id,
                                "a": application_id,
                                "g": (
                                    None
                                    if request.expected_generation == 0
                                    else request.expected_generation
                                ),
                                "d": digest,
                                "w": workflow_id,
                                "k": coalesce_key,
                                "r": trusted.run_id,
                                "c": trusted.tool_call_id,
                                "actor": trusted.actor,
                            },
                        )
                        budget += 1
        if settled is not None:
            enforce_envelope_bound(settled)
            return settled

        # C5: the durable request row is COMMITTED ('queued') before the
        # enqueue; the accepted claim happens only after acknowledgment.
        try:
            await engine_enqueue(workflow_id)
        except Exception as exc:
            raise OperationAdmissionError(
                "enqueue_failed",
                f"the engine did not acknowledge the enqueue of workflow "
                f"{workflow_id}; the row stays queued for recovery",
            ) from exc

        async with self._sessions() as session:
            async with session.begin():
                updated = (
                    await session.execute(
                        sa.text(
                            f"UPDATE service_learning_operation SET "
                            f"status = 'accepted', updated_at = now() WHERE "
                            f"operation_id = :o AND status = 'queued' "
                            f"RETURNING {_RECEIPT_COLUMNS}"
                        ),
                        {"o": operation_id},
                    )
                ).mappings().first()
                if updated is None:
                    # Raced ahead by another seam: report the CURRENT
                    # durable state honestly.
                    updated = (
                        await session.execute(
                            sa.text(
                                f"SELECT {_RECEIPT_COLUMNS} FROM "
                                "service_learning_operation WHERE "
                                "operation_id = :o"
                            ),
                            {"o": operation_id},
                        )
                    ).mappings().one()
        result = AdmitOperationResult(
            receipt=_row_to_receipt(updated),
            replayed=replay_row is not None,
            coalesced=False,
            budget_used=budget,
            budget_limit=MAX_PRESERVATION_OPERATIONS_PER_RUN,
        )
        enforce_envelope_bound(result)
        return result

    async def _run_budget(self, session: AsyncSession, run_id: uuid.UUID) -> int:
        """Count the distinct admitted operations of one source run."""
        row = await session.execute(
            sa.text(
                "SELECT COUNT(*) FROM service_learning_operation "
                "WHERE source_run_id = :r"
            ),
            {"r": run_id},
        )
        return int(row.scalar_one())

    # -- single receipt read -------------------------------------------------

    async def get_operation(self, operation_id: uuid.UUID) -> OperationReceipt | None:
        """Read one durable receipt by identity, or ``None`` when absent."""
        async with self._sessions() as session:
            row = (
                await session.execute(
                    sa.text(
                        f"SELECT {_RECEIPT_COLUMNS} FROM "
                        "service_learning_operation WHERE operation_id = :o"
                    ),
                    {"o": operation_id},
                )
            ).mappings().first()
        if row is None:
            return None
        return _row_to_receipt(row)

    # -- paged reads (AGENTS.md pagination invariant) ------------------------

    async def list_operations(
        self,
        server_id: uuid.UUID,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> OperationPage:
        """One bounded page of a server's durable operation receipts,
        ordered ``accepted_at DESC, operation_id DESC`` (matches the
        0018 index); ``total`` counts the same filter predicate."""
        page = _PageQuery(limit=limit, offset=offset)
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    sa.text(
                        f"SELECT {_RECEIPT_COLUMNS} FROM "
                        "service_learning_operation WHERE server_id = :s "
                        "ORDER BY accepted_at DESC, operation_id DESC "
                        "LIMIT :l OFFSET :o"
                    ),
                    {"s": server_id, "l": page.limit, "o": page.offset},
                )
            ).mappings().all()
            total = int(
                (
                    await session.execute(
                        sa.text(
                            "SELECT COUNT(*) FROM service_learning_operation "
                            "WHERE server_id = :s"
                        ),
                        {"s": server_id},
                    )
                ).scalar_one()
            )
        result = OperationPage.build(
            [_row_to_receipt(row) for row in rows],
            total=total,
            offset=page.offset,
            limit=page.limit,
        )
        enforce_envelope_bound(result)
        return result

    async def list_pending_recovery(
        self,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> OperationPage:
        """Bounded page of admitted-but-not-acked (``queued``) receipts,
        oldest first — a worker-startup reconciliation enumeration seam,
        NOT a polling scheduler."""
        page = _PageQuery(limit=limit, offset=offset)
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    sa.text(
                        f"SELECT {_RECEIPT_COLUMNS} FROM "
                        "service_learning_operation WHERE status = 'queued' "
                        "ORDER BY accepted_at ASC, operation_id ASC "
                        "LIMIT :l OFFSET :o"
                    ),
                    {"l": page.limit, "o": page.offset},
                )
            ).mappings().all()
            total = int(
                (
                    await session.execute(
                        sa.text(
                            "SELECT COUNT(*) FROM service_learning_operation "
                            "WHERE status = 'queued'"
                        ),
                    )
                ).scalar_one()
            )
        result = OperationPage.build(
            [_row_to_receipt(row) for row in rows],
            total=total,
            offset=page.offset,
            limit=page.limit,
        )
        enforce_envelope_bound(result)
        return result

    # -- honest progression --------------------------------------------------

    async def record_progress(
        self,
        operation_id: uuid.UUID,
        *,
        sub_effects: Mapping[str, SubEffectStatus | str],
        applied_fact_ids: Sequence[uuid.UUID] | None = None,
        conflict_keys: Sequence[str] | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> OperationReceipt:
        """Persist one honest progression step: ``status`` derives from
        the sub-effects (``applying`` when empty); a terminal status
        (applied/failed/partial/unknown) can never regress to a
        different status. ``None`` optional arguments leave the stored
        values unchanged."""
        derived = (
            derive_operation_status(sub_effects)
            if sub_effects
            else OperationStatus.APPLYING
        )
        detail = error_detail.strip()[:400] if error_detail and error_detail.strip() else None
        code = error_code if error_code else None
        async with self._sessions() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        sa.text(
                            "SELECT status FROM service_learning_operation "
                            "WHERE operation_id = :o FOR UPDATE"
                        ),
                        {"o": operation_id},
                    )
                ).first()
                if row is None:
                    raise OperationAdmissionError(
                        "unknown_operation",
                        "no durable operation exists under this identity",
                    )
                current = str(row[0])
                if current in _TERMINAL_STATUSES and derived.value != current:
                    raise OperationAdmissionError(
                        "status_regression",
                        f"the operation already reached terminal status "
                        f"'{current}'; progression back to "
                        f"'{derived.value}' is refused",
                    )
                updated = (
                    await session.execute(
                        sa.text(
                            f"UPDATE service_learning_operation SET "
                            f"sub_effects = CAST(:se AS jsonb), "
                            f"applied_fact_ids = COALESCE(CAST(:af AS jsonb), "
                            f"applied_fact_ids), conflict_keys = "
                            f"COALESCE(CAST(:ck AS jsonb), conflict_keys), "
                            f"error_code = COALESCE(:ec, error_code), "
                            f"error_detail = COALESCE(:ed, error_detail), "
                            f"status = :st, updated_at = now() WHERE "
                            f"operation_id = :o RETURNING {_RECEIPT_COLUMNS}"
                        ),
                        {
                            "se": json.dumps(
                                {
                                    key: SubEffectStatus(str(value)).value
                                    for key, value in sub_effects.items()
                                }
                            ),
                            "af": (
                                json.dumps([str(f) for f in applied_fact_ids])
                                if applied_fact_ids is not None
                                else None
                            ),
                            "ck": (
                                json.dumps([str(k) for k in conflict_keys])
                                if conflict_keys is not None
                                else None
                            ),
                            "ec": code,
                            "ed": detail,
                            "st": derived.value,
                            "o": operation_id,
                        },
                    )
                ).mappings().one()
        receipt = _row_to_receipt(updated)
        enforce_envelope_bound(receipt)
        return receipt

    # -- explicit source stop --------------------------------------------------

    async def cancel_pending_for_run(
        self,
        source_run_id: uuid.UUID,
        *,
        reason: str | None = None,
    ) -> int:
        """Mark every queued/accepted/applying operation of one source
        run as ``failed`` with error code ``source_cancelled`` (the
        frozen status vocabulary has no 'cancelled' value; the code
        carries the semantics). Already-applied and other terminal rows
        are untouched. Returns the affected row count."""
        detail = reason.strip()[:400] if reason and reason.strip() else None
        async with self._sessions() as session:
            async with session.begin():
                outcome = await session.execute(
                    sa.text(
                        "UPDATE service_learning_operation SET "
                        "status = 'failed', error_code = :code, "
                        "error_detail = :ed, updated_at = now() WHERE "
                        "source_run_id = :r AND status = ANY(:st)"
                    ),
                    {
                        "code": _CANCEL_ERROR_CODE,
                        "ed": detail,
                        "r": source_run_id,
                        "st": _CANCELLABLE_STATUSES,
                    },
                )
        return int(outcome.rowcount)
