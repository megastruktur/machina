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

HONEST SCOPE LIMITATION: the engine acknowledgment is an INJECTED SEAM at
this unit boundary (``engine_enqueue: Callable[[str], Awaitable[None]]``);
no DBOS client, worker or jobs handler is wired here. End-to-end durable
enqueue proof is the later integration slices' evidence, not this
module's. There is no scheduler and no polling loop here:
``list_pending_recovery`` is a bounded enumeration seam for worker
startup reconciliation, nothing else.

Slice 14 (ask_14-operation-recovery) adds the identity lifecycle and
execution fence: ``suppress_for_unadopt`` tombstones a service identity
BEFORE removal and cancels pending preservation; ``readopt_service_identity``
starts a NEW generation (facts/receipts stay as history); ``admit`` refuses
tombstoned identities (background work can never silently re-adopt);
``begin_apply`` fences on receipt status, tombstone/generation and the
CURRENT-authority grant intersection before transitioning to ``applying``;
``record_progress`` reconciles already-applied effects of an in-flight
operation that raced an explicit cancellation (the binding review-13
design constraint) instead of losing them to ``status_regression``;
``retry_operation`` and ``recover_pending_batch`` never retry an
``unknown`` outcome; ``recover_pending_batch`` is a bounded keyset-cursor
startup sweep (queued rows re-enqueue through the same deterministic
workflow id, stale-generation rows fail as ``generation_superseded``) —
NOT a polling scheduler.

Second honest limitation: the frozen 0018 schema carries no payload or
grants column, so the admission-time ``LearnRequest`` and trusted grants
snapshot are carried to ``begin_apply`` through an IN-PROCESS registry
(populated by ``admit``; fail-closed empty intersection on a miss). The
durable carrier for a real executor is the DBOS workflow payload in the
later wiring slice.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
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
from mai.jobs.engine import RunIdInUseError

__all__ = [
    "AdmitOperationResult",
    "ExecutionAuthorization",
    "LearningOperationStore",
    "MAX_PRESERVATION_OPERATIONS_PER_RUN",
    "OperationAdmissionError",
    "PendingRecoveryResult",
    "ServiceAutoAdoptSuppressedError",
    "ServiceIdentityState",
    "UnknownOutcomeRetryForbiddenError",
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

#: The capability grants an execution must still hold at apply time (C5
#: current-authority intersection) when no stricter set is requested.
DEFAULT_REQUIRED_GRANTS: Final[frozenset[str]] = frozenset(
    {"app.learn_service_facts"}
)

#: The recoverable (not yet terminal) operation statuses a recovery sweep
#: may visit (D5 pending recovery set).
_PENDING_STATUSES: Final[list[str]] = ["queued", "accepted", "applying"]

#: Bounded error detail stamped on rows superseded by the identity fence.
_SUPERSEDED_DETAIL: Final[str] = (
    "the service identity moved to a newer generation or is tombstoned; "
    "stale preservation can never apply"
)

#: In-process bound for the admission -> execution context registry.
_CONTEXT_REGISTRY_LIMIT: Final[int] = 1024

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
    enqueue_failed, status_regression, unknown_operation} plus the
    slice-14 execution-fence codes {source_cancelled,
    operation_not_runnable, generation_superseded, authority_revoked}."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        #: Honest budget figures, set on operation_budget_exceeded refusals.
        self.budget_used: int | None = None
        self.budget_limit: int | None = None


class ServiceAutoAdoptSuppressedError(ValueError):
    """The service identity carries an auto-adopt tombstone (D4): any
    background/automatic learning admission is refused — preserved work
    can never silently re-adopt a tombstoned service."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.code = "auto_adopt_suppressed"
        self.detail = detail


class UnknownOutcomeRetryForbiddenError(ValueError):
    """The operation's outcome is (partially) UNKNOWN: automatic retry
    or re-enqueue is FORBIDDEN (C5: unknown effects reconcile, never
    retry blind)."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.code = "retry_unknown_forbidden"
        self.detail = detail


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


class ServiceIdentityState(BaseModel):
    """The durable state of one service knowledge identity (D4): the
    generation fence plus the no-auto-adopt tombstone, read back after
    suppression or explicit readoption."""

    model_config = ConfigDict(extra="forbid")

    server_id: uuid.UUID = Field(
        description="Authorized server identity owning this service identity",
    )
    application_id: uuid.UUID = Field(
        description="Canonical application identity of the service",
    )
    generation: int = Field(
        description="Identity generation fence this service currently enforces",
    )
    service_revision: int = Field(
        description="Observed service revision at the last knowledge write",
    )
    knowledge_revision: int = Field(
        description="Monotone knowledge revision for bounded reads",
    )
    manual_identity: bool = Field(
        description="True when the identity is an evidenced map-only manual identity",
    )
    auto_adopt_suppressed: bool = Field(
        description="True when the no-auto-adopt tombstone is active",
    )
    suppressed_at: datetime | None = Field(
        default=None,
        description="Trusted timestamp when auto-adoption was suppressed",
    )
    suppressed_generation: int | None = Field(
        default=None,
        description="Generation that was current when suppression happened",
    )


class ExecutionAuthorization(BaseModel):
    """The authorized execution context returned by ``begin_apply``
    (C5): the operation may apply its effects under THIS generation and
    THIS effective grant intersection only."""

    model_config = ConfigDict(extra="forbid")

    operation_id: uuid.UUID = Field(
        description="Canonical identity of the operation authorized to apply",
    )
    generation: int = Field(
        description="Identity generation the execution is authorized against",
    )
    effective_grants: list[str] = Field(
        description="Sorted intersection of admitted and currently held grants",
    )
    request: LearnRequest | None = Field(
        default=None,
        description="Accepted learn request carried over from admission, when available",
    )
    trusted: TrustedSourceContext | None = Field(
        default=None,
        description="Trusted runtime snapshot carried over from admission, when available",
    )


class PendingRecoveryResult(BaseModel):
    """One bounded keyset-cursor page of the startup recovery sweep
    (D5) with honest action counters — a reconciliation enumeration, NOT
    a polling scheduler."""

    model_config = ConfigDict(extra="forbid")

    receipts: list[OperationReceipt] = Field(
        description="Current durable receipts of the visited page after the sweep actions",
    )
    limit: int = Field(
        description="Requested maximum number of receipts visited in this page",
    )
    total: int = Field(
        description="Total number of pending operations matching the sweep filter",
    )
    has_more: bool = Field(
        description="True when pending operations remain beyond this keyset page",
    )
    next_after_accepted_at: datetime | None = Field(
        default=None,
        description="Accepted-at keyset cursor of the last visited row, when has_more",
    )
    next_after_operation_id: uuid.UUID | None = Field(
        default=None,
        description="Operation-id keyset cursor of the last visited row, when has_more",
    )
    promoted: int = Field(
        description="Queued operations re-enqueued and promoted to accepted by this sweep",
    )
    superseded: int = Field(
        description="Pending operations failed as generation_superseded by this sweep",
    )
    in_flight: int = Field(
        description="Accepted or applying operations left untouched for their executor",
    )
    enqueue_failed: int = Field(
        description="Queued operations whose re-enqueue was not acknowledged and stay queued",
    )
    skipped_unknown: int = Field(
        description="Operations skipped because their outcome is partially unknown",
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
        #: In-process admission -> execution context registry (the frozen
        #: 0018 schema has no payload/grants column; ``begin_apply`` reads
        #: the accepted request and trusted grants snapshot from here and
        #: FAILS CLOSED with an empty grant intersection on a miss). The
        #: durable carrier for a real executor is the DBOS workflow
        #: payload in the later wiring slice.
        self._contexts: dict[uuid.UUID, tuple[LearnRequest, TrustedSourceContext]] = {}

    def _register_context(
        self,
        operation_id: uuid.UUID,
        request: LearnRequest,
        trusted: TrustedSourceContext,
    ) -> None:
        """Carry one accepted (request, trusted snapshot) to the future
        executor. First writer wins (a coalesced duplicate keeps the
        FIRST row's identity); bounded oldest-first eviction."""
        if operation_id not in self._contexts:
            while len(self._contexts) >= _CONTEXT_REGISTRY_LIMIT:
                oldest = next(iter(self._contexts))
                del self._contexts[oldest]
            self._contexts[operation_id] = (request, trusted)

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
        coalescing BEFORE the budget (a coalesced duplicate of a QUEUED
        unacknowledged operation reconciles that same deterministic
        workflow and reports ``coalesced=True, replayed=False``) ->
        atomic per-run budget ->
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

        settled: AdmitOperationResult | None = None
        retry_operation_id: uuid.UUID | None = None
        retry_workflow_id: str | None = None
        retry_queued = False
        coalesced_queued = False
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
                if application_id is not None:
                    # D4 tombstone fence: background preservation can
                    # NEVER silently re-adopt a suppressed identity. The
                    # identity row is locked so a concurrent suppression
                    # serializes (identity -> operations lock order).
                    tombstone = (
                        await session.execute(
                            sa.text(
                                "SELECT auto_adopt_suppressed FROM "
                                "service_knowledge_identity WHERE "
                                "server_id = :s AND application_id = :a "
                                "FOR UPDATE"
                            ),
                            {"s": trusted.server_id, "a": application_id},
                        )
                    ).first()
                    if tombstone is not None and bool(tombstone[0]):
                        raise ServiceAutoAdoptSuppressedError(
                            "the service identity carries an auto-adopt "
                            "tombstone; background learning admission is "
                            "refused until an explicit operator readopt"
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
                        retry_queued = True
                        retry_operation_id = uuid.UUID(str(existing["operation_id"]))
                        retry_workflow_id = str(existing["workflow_id"])
                        self._register_context(
                            retry_operation_id, request, trusted
                        )
                    else:
                        settled = AdmitOperationResult(
                            receipt=_row_to_receipt(existing),
                            replayed=True,
                            coalesced=False,
                            budget_used=budget,
                            budget_limit=MAX_PRESERVATION_OPERATIONS_PER_RUN,
                        )
                        self._register_context(
                            uuid.UUID(str(existing["operation_id"])),
                            request,
                            trusted,
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
                        if str(coalesced["status"]) == "queued":
                            # A duplicate observation of an UNACKNOWLEDGED
                            # operation reconciles the SAME deterministic
                            # workflow and is reported honestly as
                            # coalesced (never as a replay).
                            retry_queued = True
                            coalesced_queued = True
                            retry_operation_id = uuid.UUID(
                                str(coalesced["operation_id"])
                            )
                            retry_workflow_id = str(coalesced["workflow_id"])
                            self._register_context(
                                retry_operation_id, request, trusted
                            )
                        else:
                            settled = AdmitOperationResult(
                                receipt=_row_to_receipt(coalesced),
                                replayed=False,
                                coalesced=True,
                                budget_used=budget,
                                budget_limit=MAX_PRESERVATION_OPERATIONS_PER_RUN,
                            )
                            self._register_context(
                                uuid.UUID(str(coalesced["operation_id"])),
                                request,
                                trusted,
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
                        self._register_context(operation_id, request, trusted)
                        budget += 1
        if settled is not None:
            enforce_envelope_bound(settled)
            return settled

        # C5: the durable request row is COMMITTED ('queued') before the
        # enqueue; the accepted claim happens only after acknowledgment.
        # A queued replay/coalesce retry targets the FIRST row's
        # deterministic workflow, never a fresh identity.
        target_workflow = retry_workflow_id if retry_queued else workflow_id
        promoted_operation_id = (
            retry_operation_id if retry_queued else operation_id
        )
        try:
            await engine_enqueue(target_workflow)
        except Exception as exc:
            raise OperationAdmissionError(
                "enqueue_failed",
                f"the engine did not acknowledge the enqueue of workflow "
                f"{target_workflow}; the row stays queued for recovery",
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
                        {"o": promoted_operation_id},
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
                            {"o": promoted_operation_id},
                        )
                    ).mappings().one()
        result = AdmitOperationResult(
            receipt=_row_to_receipt(updated),
            replayed=retry_queued and not coalesced_queued,
            coalesced=coalesced_queued,
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

    # -- identity lifecycle (D4: tombstone before removal) -------------------

    @staticmethod
    def _identity_state(row: Mapping[str, object]) -> ServiceIdentityState:
        state = ServiceIdentityState(
            server_id=uuid.UUID(str(row["server_id"])),
            application_id=uuid.UUID(str(row["application_id"])),
            generation=int(row["generation"]),  # type: ignore[arg-type]
            service_revision=int(row["service_revision"]),  # type: ignore[arg-type]
            knowledge_revision=int(row["knowledge_revision"]),  # type: ignore[arg-type]
            manual_identity=bool(row["manual_identity"]),
            auto_adopt_suppressed=bool(row["auto_adopt_suppressed"]),
            suppressed_at=row["suppressed_at"],  # type: ignore[arg-type]
            suppressed_generation=row["suppressed_generation"],  # type: ignore[arg-type]
        )
        enforce_envelope_bound(state)
        return state

    @staticmethod
    def _identity_columns() -> str:
        return (
            "server_id, application_id, generation, service_revision, "
            "knowledge_revision, manual_identity, auto_adopt_suppressed, "
            "suppressed_at, suppressed_generation"
        )

    async def suppress_for_unadopt(
        self,
        server_id: uuid.UUID,
        application_id: uuid.UUID,
    ) -> ServiceIdentityState:
        """Persist the no-auto-adopt tombstone for one service identity
        (D4: tombstone BEFORE any map removal) and cancel every pending
        (queued/accepted/applying) preservation operation of that
        identity as ``generation_superseded`` so stale background work
        can never resurrect or re-adopt the service. Creates the
        tombstone row (generation 1, suppressed) when no identity exists
        yet. Facts and receipts stay intact as history."""
        async with self._sessions() as session:
            async with session.begin():
                # Lock order: identity row FIRST, then operation rows
                # (mirrored by admit/begin_apply; no cycle).
                current = (
                    await session.execute(
                        sa.text(
                            f"SELECT {self._identity_columns()} FROM "
                            "service_knowledge_identity WHERE server_id = :s "
                            "AND application_id = :a FOR UPDATE"
                        ),
                        {"s": server_id, "a": application_id},
                    )
                ).mappings().first()
                if current is None:
                    await session.execute(
                        sa.text(
                            "INSERT INTO service_knowledge_identity "
                            "(server_id, application_id, generation, "
                            "auto_adopt_suppressed, suppressed_at, "
                            "suppressed_generation) VALUES (:s, :a, 1, true, "
                            "now(), 1)"
                        ),
                        {"s": server_id, "a": application_id},
                    )
                else:
                    await session.execute(
                        sa.text(
                            "UPDATE service_knowledge_identity SET "
                            "auto_adopt_suppressed = true, suppressed_at = "
                            "now(), suppressed_generation = generation, "
                            "updated_at = now() WHERE server_id = :s AND "
                            "application_id = :a"
                        ),
                        {"s": server_id, "a": application_id},
                    )
                await session.execute(
                    sa.text(
                        "UPDATE service_learning_operation SET status = "
                        "'failed', error_code = 'generation_superseded', "
                        "error_detail = :ed, updated_at = now() WHERE "
                        "server_id = :s AND application_id = :a AND status "
                        "= ANY(:st)"
                    ),
                    {
                        "ed": (
                            "the service identity was tombstoned for "
                            "unadoption; pending preservation is cancelled"
                        )[:400],
                        "s": server_id,
                        "a": application_id,
                        "st": _PENDING_STATUSES,
                    },
                )
                state = (
                    await session.execute(
                        sa.text(
                            f"SELECT {self._identity_columns()} FROM "
                            "service_knowledge_identity WHERE server_id = :s "
                            "AND application_id = :a"
                        ),
                        {"s": server_id, "a": application_id},
                    )
                ).mappings().one()
        return self._identity_state(state)

    async def readopt_service_identity(
        self,
        server_id: uuid.UUID,
        application_id: uuid.UUID,
        *,
        manual_identity: bool = False,
    ) -> ServiceIdentityState:
        """Explicit operator/trusted re-adoption (D4): starts a NEW
        generation (``+1``, or 1 when no prior row existed), clears the
        auto-adopt tombstone, resets ``knowledge_revision`` and bumps
        ``service_revision``; prior-generation facts and terminal
        receipts stay intact as history. Pending (queued/accepted/
        applying) operations of PRIOR generations are cancelled as
        ``generation_superseded`` so a direct readopt never leaves
        stale-generation work runnable; late operations carrying the old
        generation are additionally fenced out at ``begin_apply``."""
        async with self._sessions() as session:
            async with session.begin():
                current = (
                    await session.execute(
                        sa.text(
                            f"SELECT {self._identity_columns()} FROM "
                            "service_knowledge_identity WHERE server_id = :s "
                            "AND application_id = :a FOR UPDATE"
                        ),
                        {"s": server_id, "a": application_id},
                    )
                ).mappings().first()
                new_generation = (
                    1 if current is None else int(current["generation"]) + 1
                )
                if current is None:
                    await session.execute(
                        sa.text(
                            "INSERT INTO service_knowledge_identity "
                            "(server_id, application_id, generation, "
                            "service_revision, knowledge_revision, "
                            "manual_identity, auto_adopt_suppressed) VALUES "
                            "(:s, :a, 1, 1, 0, :m, false)"
                        ),
                        {"s": server_id, "a": application_id, "m": manual_identity},
                    )
                else:
                    await session.execute(
                        sa.text(
                            "UPDATE service_knowledge_identity SET "
                            "generation = generation + 1, "
                            "auto_adopt_suppressed = false, suppressed_at = "
                            "NULL, suppressed_generation = NULL, "
                            "knowledge_revision = 0, service_revision = "
                            "service_revision + 1, manual_identity = :m, "
                            "updated_at = now() WHERE server_id = :s AND "
                            "application_id = :a"
                        ),
                        {"s": server_id, "a": application_id, "m": manual_identity},
                    )
                # Cancel stale-generation pending rows (terminal receipts
                # and NULL-generation new-service rows are NOT touched
                # here; the apply-time fence owns those).
                await session.execute(
                    sa.text(
                        "UPDATE service_learning_operation SET status = "
                        "'failed', error_code = 'generation_superseded', "
                        "error_detail = :ed, updated_at = now() WHERE "
                        "server_id = :s AND application_id = :a AND status "
                        "= ANY(:st) AND generation < :g"
                    ),
                    {
                        "ed": _SUPERSEDED_DETAIL[:400],
                        "s": server_id,
                        "a": application_id,
                        "st": _PENDING_STATUSES,
                        "g": new_generation,
                    },
                )
                state = (
                    await session.execute(
                        sa.text(
                            f"SELECT {self._identity_columns()} FROM "
                            "service_knowledge_identity WHERE server_id = :s "
                            "AND application_id = :a"
                        ),
                        {"s": server_id, "a": application_id},
                    )
                ).mappings().one()
        state_result = self._identity_state(state)
        return state_result

    # -- execution fence (C5: no accepted work applies without authority) ----

    async def begin_apply(
        self,
        server_id: uuid.UUID,
        operation_id: uuid.UUID,
        *,
        current_grants: frozenset[str] | set[str] | Sequence[str],
        required_grants: frozenset[str] | set[str] | Sequence[str] = DEFAULT_REQUIRED_GRANTS,
    ) -> ExecutionAuthorization:
        """Authorize one admitted operation to apply its effects (the
        slice-22 executor seam), fencing IN ORDER on: receipt scope, an
        UNKNOWN outcome (retry/reconcile only, never blind execution), an
        explicit cancellation, a non-runnable status, the identity
        tombstone/generation fence, and the CURRENT-authority grant
        intersection. Refusals transition the row to terminal ``failed``
        with the matching stable code; success transitions the row to
        ``applying`` and returns the authorized context.

        Admission-time grants come from the in-process context registry
        (fail-closed empty intersection on a miss) because the frozen
        schema carries no grants column."""
        required = frozenset(required_grants)
        current = frozenset(current_grants)
        async with self._sessions() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        sa.text(
                            f"SELECT {_RECEIPT_COLUMNS}, error_code FROM "
                            "service_learning_operation WHERE operation_id "
                            "= :o FOR UPDATE"
                        ),
                        {"o": operation_id},
                    )
                ).mappings().first()
                if row is None:
                    raise OperationAdmissionError(
                        "unknown_operation",
                        "no durable operation exists under this identity",
                    )
                row_server = uuid.UUID(str(row["server_id"]))
                if row_server != server_id:
                    raise ScopeMismatchError(row_server, server_id)
                status = str(row["status"])
                sub_effects = dict(row["sub_effects"] or {})
                if status == OperationStatus.UNKNOWN.value or any(
                    value == SubEffectStatus.UNKNOWN.value
                    for value in sub_effects.values()
                ):
                    # Unknown outcomes reconcile; they NEVER execute or
                    # retry blind (C5).
                    raise UnknownOutcomeRetryForbiddenError(
                        "the operation outcome is partially unknown; "
                        "execution and retry are forbidden until the "
                        "authoritative state is reconciled"
                    )
                if str(row["error_code"] or "") == _CANCEL_ERROR_CODE:
                    raise OperationAdmissionError(
                        _CANCEL_ERROR_CODE,
                        "the operation was cancelled by an explicit source "
                        "stop and can no longer begin applying",
                    )
                if status not in ("queued", "accepted"):
                    raise OperationAdmissionError(
                        "operation_not_runnable",
                        f"the operation is in status '{status}' and cannot "
                        "begin applying",
                    )
                application_id = (
                    uuid.UUID(str(row["application_id"]))
                    if row["application_id"] is not None
                    else None
                )
                row_generation = (
                    int(row["generation"]) if row["generation"] is not None else None
                )
                identity = None
                if application_id is not None:
                    identity = (
                        await session.execute(
                            sa.text(
                                "SELECT generation, auto_adopt_suppressed FROM "
                                "service_knowledge_identity WHERE server_id "
                                "= :s AND application_id = :a"
                            ),
                            {"s": server_id, "a": application_id},
                        )
                    ).mappings().first()

                refusal: OperationAdmissionError | None = None
                if identity is not None:
                    if bool(identity["auto_adopt_suppressed"]):
                        refusal = OperationAdmissionError(
                            "generation_superseded",
                            "the service identity is tombstoned; stale "
                            "preservation can never apply",
                        )
                    elif row_generation != int(identity["generation"]):
                        refusal = OperationAdmissionError(
                            "generation_superseded", _SUPERSEDED_DETAIL
                        )
                    # An ABSENT identity row never fences here: a
                    # brand-new service has no generation to compare
                    # against; the per-fact CAS in the write seam owns
                    # that case.
                stored = self._contexts.get(operation_id)
                admitted_grants = (
                    frozenset(stored[1].grants) if stored is not None else frozenset()
                )
                effective = admitted_grants & current
                if refusal is None:
                    missing = required - effective
                    if missing:
                        refusal = OperationAdmissionError(
                            "authority_revoked",
                            "required capability grants are missing from "
                            "the current-authority intersection: "
                            + ",".join(sorted(missing)),
                        )
                if refusal is not None:
                    # The refusal transition MUST survive: commit the
                    # terminal failed row, then raise outside the block.
                    await session.execute(
                        sa.text(
                            "UPDATE service_learning_operation SET status "
                            "= 'failed', error_code = :c, error_detail = "
                            ":ed, updated_at = now() WHERE operation_id = "
                            ":o AND status = ANY(:st)"
                        ),
                        {
                            "c": refusal.code,
                            "ed": refusal.detail[:400],
                            "o": operation_id,
                            "st": _PENDING_STATUSES,
                        },
                    )
                else:
                    authorized = (
                        await session.execute(
                            sa.text(
                                "UPDATE service_learning_operation SET "
                                "status = 'applying', updated_at = now() "
                                "WHERE operation_id = :o AND status = "
                                "ANY(:st) "
                                f"RETURNING {_RECEIPT_COLUMNS}"
                            ),
                            {"o": operation_id, "st": ["queued", "accepted"]},
                        )
                    ).mappings().first()
                    if authorized is None:
                        refusal = OperationAdmissionError(
                            "operation_not_runnable",
                            "the operation left the runnable set before "
                            "the authorization committed",
                        )
        if refusal is not None:
            raise refusal
        generation = (
            int(identity["generation"])
            if identity is not None
            else (row_generation if row_generation is not None else 1)
        )
        context = ExecutionAuthorization(
            operation_id=operation_id,
            generation=generation,
            effective_grants=sorted(effective),
            request=stored[0] if stored is not None else None,
            trusted=stored[1] if stored is not None else None,
        )
        enforce_envelope_bound(context)
        return context

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
        after_accepted_at: datetime | None = None,
        after_operation_id: uuid.UUID | None = None,
    ) -> OperationPage:
        """Bounded page of admitted-but-not-acked (``queued``) receipts,
        oldest first — a worker-startup reconciliation enumeration seam,
        NOT a polling scheduler. When the keyset cursor
        ``(after_accepted_at, after_operation_id)`` is supplied, paging
        is snapshot-stable: completing/mutating rows from earlier pages
        never shifts later pages (offset must then be 0)."""
        page = _PageQuery(limit=limit, offset=offset)
        cursor_given = after_accepted_at is not None or after_operation_id is not None
        if cursor_given and (after_accepted_at is None or after_operation_id is None):
            raise ValueError(
                "the keyset cursor requires BOTH after_accepted_at and "
                "after_operation_id"
            )
        if cursor_given and page.offset != 0:
            raise ValueError(
                "offset pagination cannot be combined with the keyset cursor"
            )
        keyset = (
            "AND (accepted_at, operation_id) > (CAST(:ca AS timestamptz), "
            "CAST(:co AS uuid))" if cursor_given else ""
        )
        params: dict[str, object] = {"l": page.limit, "o": page.offset}
        if cursor_given:
            params["ca"] = after_accepted_at
            params["co"] = after_operation_id
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    sa.text(
                        f"SELECT {_RECEIPT_COLUMNS} FROM "
                        "service_learning_operation WHERE status = 'queued' "
                        f"{keyset} "
                        "ORDER BY accepted_at ASC, operation_id ASC "
                        "LIMIT :l OFFSET :o"
                    ),
                    params,
                )
            ).mappings().all()
            total = int(
                (
                    await session.execute(
                        sa.text(
                            "SELECT COUNT(*) FROM service_learning_operation "
                            f"WHERE status = 'queued' {keyset}",
                        ),
                        params,
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
                            "SELECT status, error_code FROM "
                            "service_learning_operation WHERE operation_id "
                            "= :o FOR UPDATE"
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
                was_cancelled = str(row[1] or "") == _CANCEL_ERROR_CODE
                # Review-13 binding constraint: an in-flight executor that
                # raced an explicit cancellation still records its
                # actually-applied (or unknown) portion durably instead of
                # losing it to status_regression; the source_cancelled
                # code is preserved on the receipt.
                reconciliation = (
                    current == OperationStatus.FAILED.value
                    and was_cancelled
                    and any(
                        SubEffectStatus(str(value))
                        in (SubEffectStatus.APPLIED, SubEffectStatus.UNKNOWN)
                        for value in sub_effects.values()
                    )
                )
                if (
                    current in _TERMINAL_STATUSES
                    and derived.value != current
                    and not reconciliation
                ):
                    raise OperationAdmissionError(
                        "status_regression",
                        f"the operation already reached terminal status "
                        f"'{current}'; progression back to "
                        f"'{derived.value}' is refused",
                    )
                if reconciliation:
                    code = _CANCEL_ERROR_CODE
                    detail = None
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

    # -- explicit retry (C5: unknown outcomes are never retried) --------------

    async def retry_operation(
        self,
        operation_id: uuid.UUID,
        *,
        engine_enqueue: Callable[[str], Awaitable[None]],
    ) -> OperationReceipt:
        """Retry one UNACKNOWLEDGED (``queued``) operation through its
        same deterministic workflow id. An UNKNOWN outcome (status or
        any sub-effect) is FORBIDDEN to retry; cancelled and otherwise
        terminal or in-flight rows are refused as not runnable; a
        tombstoned or newer-generation identity fails the row as
        ``generation_superseded`` BEFORE any enqueue is attempted."""
        refusal: OperationAdmissionError | None = None
        workflow_id: str | None = None
        async with self._sessions() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        sa.text(
                            f"SELECT {_RECEIPT_COLUMNS}, error_code FROM "
                            "service_learning_operation WHERE operation_id "
                            "= :o FOR UPDATE"
                        ),
                        {"o": operation_id},
                    )
                ).mappings().first()
                if row is None:
                    raise OperationAdmissionError(
                        "unknown_operation",
                        "no durable operation exists under this identity",
                    )
                sub_effects = dict(row["sub_effects"] or {})
                if str(row["status"]) == OperationStatus.UNKNOWN.value or any(
                    value == SubEffectStatus.UNKNOWN.value for value in sub_effects.values()
                ):
                    raise UnknownOutcomeRetryForbiddenError(
                        "the operation outcome is partially unknown; retry "
                        "is forbidden until the authoritative state is "
                        "reconciled"
                    )
                if str(row["error_code"] or "") == _CANCEL_ERROR_CODE:
                    raise OperationAdmissionError(
                        _CANCEL_ERROR_CODE,
                        "a cancelled operation is never retried automatically",
                    )
                if str(row["status"]) != OperationStatus.QUEUED.value:
                    raise OperationAdmissionError(
                        "operation_not_runnable",
                        f"only unacknowledged queued operations are "
                        f"retryable here; the row is in status "
                        f"'{row['status']}'",
                    )
                workflow_id = str(row["workflow_id"])
                application_id = (
                    uuid.UUID(str(row["application_id"]))
                    if row["application_id"] is not None
                    else None
                )
                row_generation = (
                    int(row["generation"]) if row["generation"] is not None else None
                )
                if application_id is not None:
                    identity = (
                        await session.execute(
                            sa.text(
                                "SELECT generation, auto_adopt_suppressed FROM "
                                "service_knowledge_identity WHERE server_id "
                                "= :s AND application_id = :a"
                            ),
                            {
                                "s": uuid.UUID(str(row["server_id"])),
                                "a": application_id,
                            },
                        )
                    ).mappings().first()
                    if identity is not None and (
                        bool(identity["auto_adopt_suppressed"])
                        or row_generation != int(identity["generation"])
                    ):
                        # Fence BEFORE any enqueue: the row transitions to
                        # terminal failed inside this committed transaction.
                        await session.execute(
                            sa.text(
                                "UPDATE service_learning_operation SET "
                                "status = 'failed', error_code = "
                                "'generation_superseded', error_detail = "
                                ":ed, updated_at = now() WHERE operation_id "
                                "= :o AND status = 'queued'"
                            ),
                            {"ed": _SUPERSEDED_DETAIL[:400], "o": operation_id},
                        )
                        refusal = OperationAdmissionError(
                            "generation_superseded", _SUPERSEDED_DETAIL
                        )
        if refusal is not None:
            raise refusal
        assert workflow_id is not None
        try:
            await engine_enqueue(workflow_id)
        except RunIdInUseError:
            pass  # duplicate workflow id == already enqueued (ack)
        except Exception as exc:
            raise OperationAdmissionError(
                "enqueue_failed",
                f"the engine did not acknowledge the retry enqueue of "
                f"workflow {workflow_id}",
            ) from exc
        async with self._sessions() as session:
            async with session.begin():
                promoted = (
                    await session.execute(
                        sa.text(
                            "UPDATE service_learning_operation SET status "
                            "= 'accepted', updated_at = now() WHERE "
                            "operation_id = :o AND status = 'queued' "
                            f"RETURNING {_RECEIPT_COLUMNS}"
                        ),
                        {"o": operation_id},
                    )
                ).mappings().first()
                if promoted is None:
                    promoted = (
                        await session.execute(
                            sa.text(
                                f"SELECT {_RECEIPT_COLUMNS} FROM "
                                "service_learning_operation WHERE "
                                "operation_id = :o"
                            ),
                            {"o": operation_id},
                        )
                    ).mappings().one()
        receipt = _row_to_receipt(promoted)
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

    # -- startup recovery sweep (D5: enumeration seam, NOT a scheduler) ------

    async def recover_pending_batch(
        self,
        server_id: uuid.UUID | None = None,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        after_accepted_at: datetime | None = None,
        after_operation_id: uuid.UUID | None = None,
        engine_enqueue: Callable[[str], Awaitable[None]],
    ) -> PendingRecoveryResult:
        """One bounded, keyset-cursor-stable sweep over the pending set
        (``queued``/``accepted``/``applying``), oldest first:

        - rows with an UNKNOWN sub-effect are counted and NEVER touched;
        - ``queued`` rows re-enqueue through their SAME deterministic
          workflow id (``RunIdInUseError`` counts as the
          already-enqueued acknowledgment) and promote to ``accepted``;
        - rows fenced by a tombstoned or newer-generation identity fail
          as ``generation_superseded``;
        - ``accepted``/``applying`` rows stay untouched (their in-flight
          executor owns them).

        Unacknowledged re-enqueue failures leave the row ``queued`` for
        the next sweep. There is NO polling loop here — callers invoke
        this at startup/on demand and continue via the returned keyset
        cursor."""
        page = _PageQuery(limit=limit, offset=0)
        cursor_given = after_accepted_at is not None or after_operation_id is not None
        if cursor_given and (after_accepted_at is None or after_operation_id is None):
            raise ValueError(
                "the keyset cursor requires BOTH after_accepted_at and "
                "after_operation_id"
            )
        keyset = (
            "AND (accepted_at, operation_id) > (CAST(:ca AS timestamptz), "
            "CAST(:co AS uuid))" if cursor_given else ""
        )
        scope = "AND server_id = :srv" if server_id is not None else ""
        params: dict[str, object] = {"l": page.limit + 1, "st": _PENDING_STATUSES}
        if server_id is not None:
            params["srv"] = server_id
        if cursor_given:
            params["ca"] = after_accepted_at
            params["co"] = after_operation_id
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    sa.text(
                        f"SELECT {_RECEIPT_COLUMNS} FROM "
                        "service_learning_operation WHERE status = "
                        "ANY(:st) "
                        f"{scope} {keyset} "
                        "ORDER BY accepted_at ASC, operation_id ASC "
                        "LIMIT :l"
                    ),
                    params,
                )
            ).mappings().all()
            total = int(
                (
                    await session.execute(
                        sa.text(
                            "SELECT COUNT(*) FROM service_learning_operation "
                            "WHERE status = ANY(:st)" + (f" {scope}" if scope else "")
                        ),
                        params,
                    )
                ).scalar_one()
            )
        has_more = len(rows) > page.limit
        visited = rows[: page.limit]
        promoted = superseded = in_flight = enqueue_failed = skipped_unknown = 0
        receipts: list[OperationReceipt] = []
        for row in visited:
            operation_id = uuid.UUID(str(row["operation_id"]))
            effects = dict(row["sub_effects"] or {})
            if any(
                value == SubEffectStatus.UNKNOWN.value for value in effects.values()
            ):
                # Unknown outcomes reconcile; a sweep NEVER touches them.
                skipped_unknown += 1
                receipts.append(_row_to_receipt(row))
                continue
            application_id = (
                uuid.UUID(str(row["application_id"]))
                if row["application_id"] is not None
                else None
            )
            row_generation = (
                int(row["generation"]) if row["generation"] is not None else None
            )
            fenced = False
            if application_id is not None:
                async with self._sessions() as session:
                    identity = (
                        await session.execute(
                            sa.text(
                                "SELECT generation, auto_adopt_suppressed FROM "
                                "service_knowledge_identity WHERE server_id "
                                "= :s AND application_id = :a"
                            ),
                            {
                                "s": uuid.UUID(str(row["server_id"])),
                                "a": application_id,
                            },
                        )
                    ).mappings().first()
                if identity is not None:
                    fenced = bool(identity["auto_adopt_suppressed"]) or (
                        row_generation != int(identity["generation"])
                    )
                # An ABSENT identity row never fences (new-service work;
                # the write seam's CAS owns that case).
            if fenced:
                async with self._sessions() as session:
                    async with session.begin():
                        await session.execute(
                            sa.text(
                                "UPDATE service_learning_operation SET "
                                "status = 'failed', error_code = "
                                "'generation_superseded', error_detail = "
                                ":ed, updated_at = now() WHERE operation_id "
                                "= :o AND status = ANY(:st)"
                            ),
                            {
                                "ed": _SUPERSEDED_DETAIL[:400],
                                "o": operation_id,
                                "st": _PENDING_STATUSES,
                            },
                        )
                superseded += 1
                receipts.append(await self.get_operation(operation_id) or _row_to_receipt(row))
                continue
            if str(row["status"]) == OperationStatus.QUEUED.value:
                workflow_id = str(row["workflow_id"])
                try:
                    await engine_enqueue(workflow_id)
                except RunIdInUseError:
                    pass  # duplicate workflow id == already enqueued (ack)
                except Exception:
                    enqueue_failed += 1
                    receipts.append(_row_to_receipt(row))
                    continue
                async with self._sessions() as session:
                    async with session.begin():
                        updated = (
                            await session.execute(
                                sa.text(
                                    "UPDATE service_learning_operation SET "
                                    "status = 'accepted', updated_at = now() "
                                    "WHERE operation_id = :o AND status = "
                                    f"'queued' RETURNING {_RECEIPT_COLUMNS}"
                                ),
                                {"o": operation_id},
                            )
                        ).mappings().first()
                if updated is not None:
                    promoted += 1
                    receipts.append(_row_to_receipt(updated))
                else:
                    in_flight += 1
                    receipts.append(
                        await self.get_operation(operation_id) or _row_to_receipt(row)
                    )
                continue
            in_flight += 1
            receipts.append(_row_to_receipt(row))
        result = PendingRecoveryResult(
            receipts=receipts,
            limit=page.limit,
            total=total,
            has_more=has_more,
            next_after_accepted_at=(
                visited[-1]["accepted_at"] if has_more and visited else None
            ),
            next_after_operation_id=(
                uuid.UUID(str(visited[-1]["operation_id"]))
                if has_more and visited
                else None
            ),
            promoted=promoted,
            superseded=superseded,
            in_flight=in_flight,
            enqueue_failed=enqueue_failed,
            skipped_unknown=skipped_unknown,
        )
        enforce_envelope_bound(result)
        return result
