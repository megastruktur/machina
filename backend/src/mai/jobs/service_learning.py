"""The DBOS background ``service.learn`` handler (ask_22, D4/D5, C4/C5/C7).

Applies one admitted durable preservation operation: canonical adoption
and map metadata through the :class:`MetadataMutationCoordinator` lease
seam, learned-fact deltas through the
:class:`mai.applications.knowledge_write.KnowledgeWriteStore` single
mutation seam. The durable admission receipt
(:class:`mai.applications.learning_operations.LearningOperationStore`)
stays the authority: this handler NEVER invents work — it applies
exactly the admitted request, honestly checkpointing every sub-effect so
a worker crash at ANY boundary reconciles without duplicate side
effects.

Execution-time authority (C5): ``begin_apply`` re-fences on the CURRENT
grant intersection, the identity tombstone and the admission generation
BEFORE any effect runs. Foreground run completion is NOT cancellation:
a completed source run lets the handler apply normally. An explicit
operator stop (the wired ``run_stopped_checker``) cancels the pending
operation (``source_cancelled``) instead of applying it.

Execution vocabulary (frozen): the durable status vocabulary has no
``conflict``/``cancelled`` values — refusal semantics travel as the
terminal ``failed`` status plus the stable ``error_code``
(``selector_conflict``, ``fact_conflict``, ``authority_revoked``,
``generation_superseded``, ``source_cancelled``); the frozen sub-effect
vocabulary has no ``skipped`` value — a refused-before-attempt effect
records ``failed`` with the refusal detail.

Grant normalization at the ``begin_apply`` boundary: the admitted
``app.learn_service`` capability implies the execution-side fact grant
``app.learn_service_facts``; ``app.adopt`` is required ONLY when the
payload carries an adoption proposal and is satisfied only when BOTH the
admitted and the current-authority grant sets hold it. The store
intersects ADMITTED with CURRENT grants, so the required set passed to
``begin_apply`` is expressed in the admitted vocabulary while the
effective-vocabulary facts grant is mirrored on the resume path.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.applications.adopt import (
    ABSENT_REVISION,
    AdoptionRequest,
    ApplicationManifestStore,
)
from mai.applications.discover import ApplicationOrigin
from mai.applications.knowledge_contracts import (
    LearnRequest,
    OperationStatus,
    SubEffectStatus,
    TrustedSourceContext,
)
from mai.applications.knowledge_write import KnowledgeWriteStore
from mai.applications.learning_operations import (
    DEFAULT_REQUIRED_GRANTS,
    LearningOperationStore,
    OperationAdmissionError,
)
from mai.applications.metadata_mutations import MetadataMutationCoordinator
from mai.applications.service_map import ServiceMapStore
from mai.jobs.engine import EngineClient, register_job_handler

__all__ = [
    "SERVICE_LEARN_HANDLER_NAME",
    "WORKFLOW_PREFIX",
    "AdoptionProposal",
    "ServiceLearningHandler",
    "ServiceLearningPayload",
    "build_engine_enqueue",
    "engine_run_id_for_operation",
    "parse_workflow_operation_id",
    "register_service_learning_handler",
    "workflow_id_for_operation",
]

SERVICE_LEARN_HANDLER_NAME: Final[str] = "service.learn"

WORKFLOW_PREFIX: Final[str] = "service.learn"

#: The admitted capability that implies the execution-side facts grant.
_LEARN_CAPABILITY: Final[str] = "app.learn_service"

#: The execution-side facts grant (store ``DEFAULT_REQUIRED_GRANTS``).
_LEARN_FACTS_GRANT: Final[str] = "app.learn_service_facts"

#: The adoption grant (never implied by ``app.learn_service``).
_ADOPT_GRANT: Final[str] = "app.adopt"

#: Stable refusal codes this handler records on the durable receipt.
_CODE_SELECTOR_CONFLICT: Final[str] = "selector_conflict"
_CODE_FACT_CONFLICT: Final[str] = "fact_conflict"
_CODE_DOCS_SYNC_FAILED: Final[str] = "docs_sync_failed"
_CODE_AUTHORITY_REVOKED: Final[str] = "authority_revoked"
_CODE_GENERATION_SUPERSEDED: Final[str] = "generation_superseded"
_CODE_UNKNOWN_OPERATION: Final[str] = "unknown_operation"
_CODE_INVALID_PAYLOAD: Final[str] = "invalid_payload"
_CODE_ADOPTION_FAILED: Final[str] = "adoption_failed"

#: Statuses a crashed-but-in-flight operation may be resumed from.
_RESUMABLE_STATUSES: Final[frozenset[str]] = frozenset({"applying"})

#: Full-row columns the handler reads (the receipt shape carries no
#: ``error_code``/``actor``/``source_run_id``; the executor needs them).
_ROW_COLUMNS: Final[str] = (
    "operation_id, server_id, application_id, generation, payload_digest, "
    "workflow_id, status, sub_effects, accepted_at, error_code, actor, "
    "source_run_id"
)


def workflow_id_for_operation(operation_id: uuid.UUID) -> str:
    """The deterministic DBOS workflow id for one operation (matches
    ``LearningOperationStore._workflow_id``)."""
    return f"{WORKFLOW_PREFIX}:{operation_id}"


def engine_run_id_for_operation(operation_id: uuid.UUID) -> str:
    """The engine run id for one operation: the canonical lowercase
    hyphenated UUID string (``EngineClient.enqueue_run`` contract)."""
    return str(operation_id)


def parse_workflow_operation_id(workflow_or_run_id: str) -> uuid.UUID:
    """Parse ``"service.learn:<uuid>"`` or a bare ``"<uuid>"`` into the
    canonical operation identity (garbage is a ``ValueError``)."""
    value = workflow_or_run_id.strip()
    prefix = f"{WORKFLOW_PREFIX}:"
    if value.startswith(prefix):
        value = value[len(prefix):]
    return uuid.UUID(value)


class AdoptionProposal(BaseModel):
    """One proposed canonical/map adoption carried by a learning
    payload (strict; selector identity drives the replay/conflict
    reconciliation)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=120,
        description="Human-visible service name for the map projection",
    )
    origin: Literal["compose", "systemd_unit", "manual"] = Field(
        description="Adoption origin selector kind this proposal claims",
    )
    category: str = Field(
        default="system",
        min_length=1,
        max_length=64,
        description="Bounded taxonomy category for the map projection",
    )
    compose_project: str | None = Field(
        default=None,
        description="Canonical compose project selector, when compose-origin",
    )
    working_dir: str | None = Field(
        default=None,
        description="Canonical working directory selector, when present",
    )
    unit_name: str | None = Field(
        default=None,
        description="Canonical systemd unit selector, when systemd-origin",
    )
    description: str = Field(
        default="",
        max_length=2000,
        description="Bounded human-readable description for the projection",
    )
    custom_instructions: str | None = Field(
        default=None,
        max_length=8000,
        description="Server-specific operator instructions for the projection",
    )
    installed: bool = Field(
        default=True,
        description="Whether the service is currently installed on the host",
    )


class ServiceLearningPayload(BaseModel):
    """The typed durable workflow payload for one ``service.learn`` run
    (strict; identifiers only — never secret values).

    ``request``/``trusted`` may be omitted in a minimal recovery enqueue
    payload; the handler then resolves them from the in-process
    admission context registry (same-process recovery) or the payload of
    a full enqueue (slice 36 binding)."""

    model_config = ConfigDict(extra="forbid")

    operation_id: uuid.UUID = Field(
        description="Canonical identity of the durable operation to apply",
    )
    server_id: uuid.UUID | None = Field(
        default=None,
        description="Server scope; resolved from the durable row when omitted",
    )
    application_id: uuid.UUID | None = Field(
        default=None,
        description="Learned-service identity; resolved from the row when omitted",
    )
    request: LearnRequest | None = Field(
        default=None,
        description="The admitted learn request; resolved from the admission "
        "context when omitted",
    )
    trusted: TrustedSourceContext | None = Field(
        default=None,
        description="The admitted trusted runtime snapshot; resolved from the "
        "admission context when omitted",
    )
    adoption: AdoptionProposal | None = Field(
        default=None,
        description="Proposed canonical/map adoption to apply before the facts",
    )


def build_engine_enqueue(
    client: EngineClient,
    *,
    payload_resolver: Callable[
        [uuid.UUID], str | Mapping[str, Any] | ServiceLearningPayload | None
    ]
    | None = None,
) -> Callable[[str], Awaitable[None]]:
    """Build the ``LearningOperationStore.admit`` engine-enqueue seam
    over a real :class:`EngineClient`.

    Given the deterministic workflow id, the seam derives the engine run
    id, builds the durable payload JSON (from ``payload_resolver`` or
    the minimal ``{"operation_id": ...}`` document the handler resolves
    against the admission context) and submits through the engine's
    blocking client off the event loop."""

    async def _enqueue(workflow_id: str) -> None:
        operation_id = parse_workflow_operation_id(workflow_id)
        run_id = engine_run_id_for_operation(operation_id)
        resolved = (
            payload_resolver(operation_id) if payload_resolver is not None else None
        )
        if resolved is None:
            payload_json = json.dumps({"operation_id": run_id}, sort_keys=True)
        elif isinstance(resolved, ServiceLearningPayload):
            payload_json = resolved.model_dump_json()
        elif isinstance(resolved, Mapping):
            payload_json = json.dumps(dict(resolved), sort_keys=True)
        else:
            payload_json = str(resolved)
        await asyncio.to_thread(
            client.enqueue_run, run_id, SERVICE_LEARN_HANDLER_NAME, payload_json
        )

    return _enqueue


def register_service_learning_handler(handler: "ServiceLearningHandler") -> None:
    """Register ``handler`` as the durable ``service.learn`` run handler.

    The engine resolves handlers by name at run time and drives async
    handlers only when ``inspect`` sees a coroutine FUNCTION, so the
    instance is bound behind a module-level async entrypoint."""
    if not isinstance(handler, ServiceLearningHandler):
        raise ValueError("handler must be a ServiceLearningHandler")

    async def _run_service_learn(payload_json: str) -> dict[str, Any]:
        return await handler(payload_json)

    register_job_handler(SERVICE_LEARN_HANDLER_NAME, _run_service_learn)


class ServiceLearningHandler:
    """The ``service.learn`` executor (one instance per worker wiring).

    All adoption/map mutations go through the coordinator lease seam;
    all fact mutations go through the knowledge write seam; every
    sub-effect is checkpointed on the durable receipt so replay after a
    crash reconciles from ``sub_effects`` AND durable domain state
    without duplicate side effects."""

    def __init__(
        self,
        *,
        operations: LearningOperationStore,
        coordinator: MetadataMutationCoordinator,
        knowledge_write: KnowledgeWriteStore,
        map_store: ServiceMapStore,
        sessions: async_sessionmaker[AsyncSession],
        manifest_store: ApplicationManifestStore | None = None,
        docs_indexer: Callable[
            [uuid.UUID, uuid.UUID | None, tuple[uuid.UUID, ...]], Awaitable[None]
        ]
        | None = None,
        run_stopped_checker: Callable[
            [uuid.UUID, uuid.UUID], Awaitable[bool] | bool
        ]
        | None = None,
        current_grants_resolver: Callable[
            [uuid.UUID, str], Awaitable[set[str]] | set[str]
        ]
        | None = None,
    ) -> None:
        if not isinstance(operations, LearningOperationStore):
            raise ValueError("operations must be a LearningOperationStore")
        if not isinstance(coordinator, MetadataMutationCoordinator):
            raise ValueError("coordinator must be a MetadataMutationCoordinator")
        if not isinstance(knowledge_write, KnowledgeWriteStore):
            raise ValueError("knowledge_write must be a KnowledgeWriteStore")
        if not isinstance(map_store, ServiceMapStore):
            raise ValueError("map_store must be a ServiceMapStore")
        if sessions is None:
            raise ValueError("sessions is required for executor row reads")
        self._operations = operations
        self._coordinator = coordinator
        self._knowledge_write = knowledge_write
        self._map_store = map_store
        self._sessions = sessions
        self._manifest_store = manifest_store
        self._docs_indexer = docs_indexer
        self._run_stopped_checker = run_stopped_checker
        self._current_grants_resolver = current_grants_resolver

    # -- row access (receipt shape lacks executor-only columns) -------------

    async def _load_row(self, operation_id: uuid.UUID) -> dict[str, Any] | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT {_ROW_COLUMNS} FROM "
                        "service_learning_operation WHERE operation_id = :o"
                    ),
                    {"o": operation_id},
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def _fail_pending(
        self,
        operation_id: uuid.UUID,
        *,
        error_code: str,
        error_detail: str,
    ) -> None:
        """Commit a terminal ``failed`` refusal on a non-terminal row
        (the same discipline ``begin_apply`` uses for its refusals)."""
        async with self._sessions() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE service_learning_operation SET status = "
                        "'failed', error_code = :c, error_detail = :ed, "
                        "updated_at = now() WHERE operation_id = :o AND "
                        "status = ANY(:st)"
                    ),
                    {
                        "c": error_code,
                        "ed": error_detail.strip()[:400],
                        "o": operation_id,
                        "st": ["queued", "accepted", "applying"],
                    },
                )

    async def _identity_state(
        self, server_id: uuid.UUID, application_id: uuid.UUID
    ) -> dict[str, Any] | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT generation, auto_adopt_suppressed FROM "
                        "service_knowledge_identity WHERE server_id = :s "
                        "AND application_id = :a"
                    ),
                    {"s": server_id, "a": application_id},
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    # -- trusted context + grants -------------------------------------------

    def _stored_context(
        self, operation_id: uuid.UUID
    ) -> tuple[LearnRequest | None, TrustedSourceContext | None]:
        """Resolve the admitted (request, trusted) from the store's
        in-process admission context registry (fail-closed empty on a
        miss; full payloads carry them cross-process from slice 36)."""
        registry = getattr(self._operations, "_contexts", None)
        if isinstance(registry, dict):
            stored = registry.get(operation_id)
            if stored is not None:
                return stored[0], stored[1]
        return None, None

    @staticmethod
    def _expanded(grants: set[str]) -> set[str]:
        """Execution-vocabulary expansion: the admitted/current
        ``app.learn_service`` capability implies ``app.learn_service_facts``."""
        expanded = set(grants)
        if _LEARN_CAPABILITY in expanded:
            expanded.add(_LEARN_FACTS_GRANT)
        return expanded

    async def _grant_sets(
        self,
        *,
        payload_trusted: TrustedSourceContext | None,
        stored_trusted: TrustedSourceContext | None,
        row: dict[str, Any],
    ) -> tuple[set[str], set[str]]:
        """Return (admitted, current) raw grant sets. Current grants come
        from the wired resolver (execution-time authority, C5); without a
        resolver the admitted grants stand in (unchallenged authority)."""
        trusted = payload_trusted or stored_trusted
        admitted = set(trusted.grants) if trusted is not None else set()
        actor = str(row.get("actor") or (trusted.actor if trusted else ""))
        if self._current_grants_resolver is not None:
            resolved = self._current_grants_resolver(
                uuid.UUID(str(row["server_id"])), actor
            )
            if isinstance(resolved, Awaitable):
                resolved = await resolved
            current = set(resolved)
        else:
            current = set(admitted)
        return admitted, current

    # -- serialization -------------------------------------------------------

    @staticmethod
    def _serialize(row: dict[str, Any]) -> dict[str, Any]:
        """JSON-safe receipt dict (the handler's workflow return value)."""
        return {
            "operation_id": str(row["operation_id"]),
            "workflow_id": str(row["workflow_id"]),
            "status": str(row["status"]),
            "sub_effects": {
                str(key): str(value)
                for key, value in (row.get("sub_effects") or {}).items()
            },
            "error_code": row.get("error_code"),
            "payload_digest": str(row["payload_digest"]),
            "accepted_at": row["accepted_at"].isoformat()
            if hasattr(row["accepted_at"], "isoformat")
            else str(row["accepted_at"]),
            "server_id": str(row["server_id"]),
            "application_id": (
                str(row["application_id"])
                if row.get("application_id") is not None
                else None
            ),
        }

    # -- main entry -----------------------------------------------------------

    async def __call__(self, payload_json: str) -> dict[str, Any]:
        try:
            payload = ServiceLearningPayload.model_validate_json(payload_json)
        except ValueError as error:
            return {
                "status": OperationStatus.FAILED.value,
                "error_code": _CODE_INVALID_PAYLOAD,
                "detail": str(error)[:400],
            }
        operation_id = payload.operation_id
        row = await self._load_row(operation_id)
        if row is None:
            return {
                "status": OperationStatus.FAILED.value,
                "error_code": _CODE_UNKNOWN_OPERATION,
                "detail": "no durable operation exists under this identity",
            }

        # Boundary (a): a terminal receipt is authoritative — reconcile
        # by returning it untouched.
        if str(row["status"]) in {s.value for s in (
            OperationStatus.APPLIED,
            OperationStatus.PARTIAL,
            OperationStatus.FAILED,
            OperationStatus.UNKNOWN,
        )}:
            return self._serialize(row)

        server_id = payload.server_id or uuid.UUID(str(row["server_id"]))
        application_id = (
            payload.application_id
            or (
                uuid.UUID(str(row["application_id"]))
                if row.get("application_id") is not None
                else None
            )
        )
        source_run_id = (
            uuid.UUID(str(row["source_run_id"]))
            if row.get("source_run_id") is not None
            else None
        )

        # An explicit operator stop of the source run cancels the
        # pending operation instead of applying it. Foreground COMPLETION
        # is not cancellation: a completed run reports False here.
        if self._run_stopped_checker is not None and source_run_id is not None:
            stopped = self._run_stopped_checker(server_id, source_run_id)
            if isinstance(stopped, Awaitable):
                stopped = await stopped
            if stopped:
                await self._operations.cancel_pending_for_run(
                    source_run_id,
                    reason="the source run was explicitly stopped by the operator",
                )
                fresh = await self._load_row(operation_id)
                return self._serialize(fresh or row)

        stored_request, stored_trusted = self._stored_context(operation_id)
        request = payload.request or stored_request
        trusted = payload.trusted or stored_trusted
        admitted, current = await self._grant_sets(
            payload_trusted=payload.trusted,
            stored_trusted=stored_trusted,
            row=row,
        )

        has_facts = request is not None and len(request.facts) > 0
        has_adoption = payload.adoption is not None
        # Required set in the ADMITTED vocabulary (the store intersects
        # admitted with current): facts require the learn capability,
        # adoption additionally requires the adopt grant — which is
        # satisfied only when both sides actually hold it.
        required_admitted: set[str] = set()
        if has_facts:
            required_admitted.add(_LEARN_CAPABILITY)
        if has_adoption:
            required_admitted.add(_ADOPT_GRANT)

        status = str(row["status"])
        if status in _RESUMABLE_STATUSES:
            # Crash resume: the authorization already committed before
            # the crash; re-run the cheap execution-time fences and
            # continue reconciling from the checkpointed sub-effects.
            return await self._resume(
                row=row,
                payload=payload,
                server_id=server_id,
                application_id=application_id,
                request=request,
                trusted=trusted,
                admitted=admitted,
                current=current,
                required_admitted=required_admitted,
            )

        current_expanded = self._expanded(current)
        try:
            authorization = await self._operations.begin_apply(
                server_id,
                operation_id,
                current_grants=current_expanded,
                required_grants=(
                    required_admitted if required_admitted else DEFAULT_REQUIRED_GRANTS
                ),
            )
        except OperationAdmissionError:
            # begin_apply already committed the terminal refusal on the
            # durable row; reconcile by returning it.
            fresh = await self._load_row(operation_id)
            return self._serialize(fresh or row)

        generation = authorization.generation
        request = authorization.request or request
        trusted = authorization.trusted or trusted
        sub_effects: dict[str, SubEffectStatus] = {
            str(key): SubEffectStatus(str(value))
            for key, value in (row.get("sub_effects") or {}).items()
        }
        return await self._apply_effects(
            row=row,
            payload=payload,
            server_id=server_id,
            application_id=application_id,
            request=request,
            trusted=trusted,
            generation=generation,
            sub_effects=sub_effects,
            has_facts=has_facts,
        )

    # -- resume path (crash after begin_apply committed `applying`) ----------

    async def _resume(
        self,
        *,
        row: dict[str, Any],
        payload: ServiceLearningPayload,
        server_id: uuid.UUID,
        application_id: uuid.UUID | None,
        request: LearnRequest | None,
        trusted: TrustedSourceContext | None,
        admitted: set[str],
        current: set[str],
        required_admitted: set[str],
    ) -> dict[str, Any]:
        operation_id = uuid.UUID(str(row["operation_id"]))
        if application_id is not None:
            identity = await self._identity_state(server_id, application_id)
            if identity is not None:
                row_generation = (
                    int(row["generation"]) if row.get("generation") is not None else None
                )
                if (
                    bool(identity["auto_adopt_suppressed"])
                    or (
                        row_generation is not None
                        and int(identity["generation"]) != row_generation
                    )
                ):
                    await self._fail_pending(
                        operation_id,
                        error_code=_CODE_GENERATION_SUPERSEDED,
                        error_detail=(
                            "the service identity is tombstoned or moved to a "
                            "newer generation; stale preservation can never apply"
                        ),
                    )
                    fresh = await self._load_row(operation_id)
                    return self._serialize(fresh or row)
        effective = self._expanded(admitted) & self._expanded(current)
        required_effective: set[str] = set()
        if request is not None and len(request.facts) > 0:
            required_effective.add(_LEARN_FACTS_GRANT)
        if payload.adoption is not None:
            required_effective.add(_ADOPT_GRANT)
        if required_effective - effective:
            await self._fail_pending(
                operation_id,
                error_code=_CODE_AUTHORITY_REVOKED,
                error_detail=(
                    "required capability grants are missing from the "
                    "current-authority intersection: "
                    + ",".join(sorted(required_effective - effective))
                ),
            )
            fresh = await self._load_row(operation_id)
            return self._serialize(fresh or row)
        generation = (
            int(row["generation"]) if row.get("generation") is not None else 1
        )
        sub_effects: dict[str, SubEffectStatus] = {
            str(key): SubEffectStatus(str(value))
            for key, value in (row.get("sub_effects") or {}).items()
        }
        return await self._apply_effects(
            row=row,
            payload=payload,
            server_id=server_id,
            application_id=application_id,
            request=request,
            trusted=trusted,
            generation=generation,
            sub_effects=sub_effects,
            has_facts=request is not None and len(request.facts) > 0,
        )

    # -- effect execution + reconciliation ------------------------------------

    async def _apply_effects(
        self,
        *,
        row: dict[str, Any],
        payload: ServiceLearningPayload,
        server_id: uuid.UUID,
        application_id: uuid.UUID | None,
        request: LearnRequest | None,
        trusted: TrustedSourceContext | None,
        generation: int,
        sub_effects: dict[str, SubEffectStatus],
        has_facts: bool,
    ) -> dict[str, Any]:
        operation_id = uuid.UUID(str(row["operation_id"]))

        def _pending_markers() -> dict[str, SubEffectStatus]:
            """Planned-but-unapplied effects stay explicit ``pending`` so
            intermediate checkpoints derive a non-terminal status."""
            markers: dict[str, SubEffectStatus] = {}
            if has_facts and sub_effects.get("facts") != SubEffectStatus.APPLIED:
                markers["facts"] = SubEffectStatus.PENDING
            if (
                self._docs_indexer is not None
                and sub_effects.get("docs") != SubEffectStatus.APPLIED
            ):
                markers["docs"] = SubEffectStatus.PENDING
            return markers

        # Boundary (b): adoption / map sub-effect.
        adoption = payload.adoption
        write_result = None
        if adoption is not None and application_id is not None:
            if sub_effects.get("adoption") != SubEffectStatus.APPLIED:
                outcome = await self._apply_adoption(
                    row=row,
                    server_id=server_id,
                    application_id=application_id,
                    adoption=adoption,
                    sub_effects=sub_effects,
                    has_facts=has_facts,
                )
                if outcome is not None:
                    return outcome  # refusal envelope (selector conflict / failure)
            # Checkpoint the reconciled-or-executed adoption (facts stay
            # explicit pending so the derived status stays non-terminal).
            checkpoint = {
                **sub_effects,
                **_pending_markers(),
            }
            await self._operations.record_progress(
                operation_id, sub_effects=checkpoint
            )

        # Boundary (c): fact deltas (the write seam is idempotent for
        # identical active proposals at the expected generation).
        if has_facts and sub_effects.get("facts") != SubEffectStatus.APPLIED:
            if request is None or trusted is None or application_id is None:
                await self._fail_pending(
                    operation_id,
                    error_code=_CODE_AUTHORITY_REVOKED,
                    error_detail=(
                        "the admitted request/context is not resolvable in "
                        "this process; re-enqueue with the full durable payload"
                    ),
                )
                fresh = await self._load_row(operation_id)
                return self._serialize(fresh or row)
            write_result = await self._knowledge_write.apply_facts(
                server_id=server_id,
                application_id=application_id,
                request=request,
                trusted=trusted,
                expected_generation=generation,
            )
            if write_result.conflict_keys:
                sub_effects["facts"] = SubEffectStatus.FAILED
                await self._operations.record_progress(
                    operation_id,
                    sub_effects=sub_effects,
                    applied_fact_ids=write_result.applied_fact_ids,
                    conflict_keys=write_result.conflict_keys,
                    error_code=_CODE_FACT_CONFLICT,
                    error_detail=(
                        "divergent same-key proposals were refused; the "
                        "per-fact CAS owns corrections"
                    ),
                )
                fresh = await self._load_row(operation_id)
                return self._serialize(fresh or row)
            if write_result.status is not OperationStatus.APPLIED:
                sub_effects["facts"] = SubEffectStatus.FAILED
                await self._operations.record_progress(
                    operation_id,
                    sub_effects=sub_effects,
                    error_code=write_result.error_code or _CODE_FACT_CONFLICT,
                    error_detail=write_result.error_detail,
                )
                fresh = await self._load_row(operation_id)
                return self._serialize(fresh or row)
            sub_effects["facts"] = SubEffectStatus.APPLIED
            await self._operations.record_progress(
                operation_id,
                sub_effects={**sub_effects, **_pending_markers()},
                applied_fact_ids=write_result.applied_fact_ids,
            )

        # Boundary (d): optional docs/index sync; a failure NEVER loses
        # the already-committed facts (final status derives partial).
        if (
            self._docs_indexer is not None
            and sub_effects.get("docs") != SubEffectStatus.APPLIED
        ):
            try:
                await self._docs_indexer(
                    server_id,
                    application_id,
                    (
                        tuple(write_result.applied_fact_ids)
                        if write_result is not None
                        else ()
                    ),
                )
                sub_effects["docs"] = SubEffectStatus.APPLIED
            except Exception as error:  # noqa: BLE001 - recorded honestly
                sub_effects["docs"] = SubEffectStatus.FAILED
                await self._operations.record_progress(
                    operation_id,
                    sub_effects=sub_effects,
                    error_code=_CODE_DOCS_SYNC_FAILED,
                    error_detail=f"docs/index sync failed: {error}"[:400],
                )
                fresh = await self._load_row(operation_id)
                return self._serialize(fresh or row)

        await self._operations.record_progress(
            operation_id, sub_effects=sub_effects
        )
        fresh = await self._load_row(operation_id)
        return self._serialize(fresh or row)

    async def _apply_adoption(
        self,
        *,
        row: dict[str, Any],
        server_id: uuid.UUID,
        application_id: uuid.UUID,
        adoption: AdoptionProposal,
        sub_effects: dict[str, SubEffectStatus],
        has_facts: bool,
    ) -> dict[str, Any] | None:
        """Execute or reconcile the adoption/map sub-effect. Returns a
        refusal envelope when the operation must stop here; ``None``
        when the sub-effect is applied/reconciled."""
        operation_id = uuid.UUID(str(row["operation_id"]))
        canonical_origins = ("compose", "systemd_unit")
        existing = self._map_store.get_entry(server_id, application_id)
        if (
            adoption.origin in canonical_origins
            and existing is not None
            and existing.origin in canonical_origins
        ):
            selector_matches = (
                existing.origin == adoption.origin
                and (existing.compose_project or None)
                == (adoption.compose_project or None)
                and (existing.working_dir or None)
                == (adoption.working_dir or None)
                and (existing.unit_name or None) == (adoption.unit_name or None)
            )
            if selector_matches:
                # Reconciled replay: the durable domain state already
                # matches the proposal — no write, no revision bump.
                sub_effects["adoption"] = SubEffectStatus.APPLIED
                return None
            sub_effects["adoption"] = SubEffectStatus.FAILED
            if has_facts:
                sub_effects["facts"] = SubEffectStatus.FAILED
            await self._operations.record_progress(
                operation_id,
                sub_effects=sub_effects,
                error_code=_CODE_SELECTOR_CONFLICT,
                error_detail=(
                    "an already-adopted canonical service carries a "
                    "different selector; facts were skipped and nothing "
                    "was overwritten"
                ),
            )
            fresh = await self._load_row(operation_id)
            return self._serialize(fresh or row)

        try:
            if (
                adoption.origin in canonical_origins
                and self._manifest_store is not None
                and self._coordinator._adoption is not None
            ):
                await self._coordinator.adopt(
                    self._manifest_store,
                    AdoptionRequest(
                        server_id=server_id,
                        application_id=application_id,
                        name=adoption.name,
                        origin=ApplicationOrigin(adoption.origin),
                        compose_project=adoption.compose_project,
                        working_dir=adoption.working_dir,
                        unit_name=adoption.unit_name,
                        expected_revision=ABSENT_REVISION,
                    ),
                )
            await self._coordinator.upsert_map_entry(
                server_id,
                application_id,
                name=adoption.name,
                category=adoption.category,
                origin=adoption.origin,
                compose_project=adoption.compose_project,
                working_dir=adoption.working_dir,
                unit_name=adoption.unit_name,
                description=adoption.description,
                custom_instructions=adoption.custom_instructions,
                installed=adoption.installed,
                check_suppression=True,
                explicit_operator_write=False,
            )
        except Exception as error:  # noqa: BLE001 - recorded honestly
            code = getattr(error, "code", None)
            code_value = getattr(code, "value", None) or _CODE_ADOPTION_FAILED
            sub_effects["adoption"] = SubEffectStatus.FAILED
            if has_facts and sub_effects.get("facts") != SubEffectStatus.APPLIED:
                sub_effects["facts"] = SubEffectStatus.FAILED
            await self._operations.record_progress(
                operation_id,
                sub_effects=sub_effects,
                error_code=str(code_value),
                error_detail=str(error)[:400],
            )
            fresh = await self._load_row(operation_id)
            return self._serialize(fresh or row)
        sub_effects["adoption"] = SubEffectStatus.APPLIED
        return None
