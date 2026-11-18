"""The scoped service-knowledge HTTP API (ask_26, D7, C1/C3/C4).

Five routes under
``/api/v1/servers/{server_id}/applications/{application_id}/knowledge``:

- ``GET  ""`` — the bounded current knowledge read: operator map state,
  catalog runbook, the ACTIVE learned-fact page (paginated
  ``limit``/``offset``) and the identity/revision scalars;
- ``GET  /history`` — the paginated append-only fact revision history
  (optional ``fact_id`` filter);
- ``GET  /operations`` — the paginated durable preservation-operation
  receipts (optional ``status`` filter);
- ``POST /facts/{fact_id}/retract`` — OPERATOR retraction by exact
  compare-and-set (``expected_revision`` + ``expected_generation``,
  optional bounded ``reason``) through
  :meth:`KnowledgeWriteStore.retract_fact` under the
  :class:`MetadataMutationCoordinator` C4 lease set;
- ``POST /operations/{operation_id}/retry`` — explicit authorized retry
  of an unacknowledged (``queued``) preservation operation through
  ``LearningOperationStore.retry_operation`` with the composed learn
  enqueue seam.

Strict same-server scope (C3): the path identities are the ONLY target —
an unknown or cross-server application answers ``404 not_found`` (never a
substituted scope, never an existence leak). Mutations run in the frozen
session → allowed-origin → CSRF order. Collection reads are paginated
with ``COUNT(*)`` under the exact filter and envelope-bounded (< 64 KiB
by the stores themselves). Every mutation goes through the domain stores
— no raw SQL here. Error answers use the C01 structured envelope.

Composition: the router reads the dependency family the API composition
root created (``app.state.knowledge_read`` / ``learning_operations`` /
``metadata_coordinator`` / ``learn_engine_enqueue`` — ask_24) and fails
closed with ``503 unavailable`` when a dependency is absent.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from mai.api.auth import (
    _require_allowed_origin,
    _require_csrf,
    _require_session,
    render_error_envelope,
)
from mai.applications.knowledge_contracts import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    OperationStatus,
    ScopeMismatchError,
)
from mai.applications.knowledge_read import (
    KnowledgeReadStore,
    ServiceKnowledgeNotFoundError,
)
from mai.applications.knowledge_write import KnowledgeWriteStore
from mai.applications.learning_operations import (
    LearningOperationStore,
    OperationAdmissionError,
    UnknownOutcomeRetryForbiddenError,
)
from mai.applications.metadata_mutations import (
    MetadataMutationCoordinator,
    MetadataMutationError,
)

__all__ = ["register_service_knowledge"]

router = APIRouter(
    prefix=(
        "/api/v1/servers/{server_id}/applications/{application_id}"
        "/knowledge"
    ),
    tags=["service-knowledge"],
)


# ---------------------------------------------------------------------------
# dependency access (fail-closed 503 when the composition omitted one)
# ---------------------------------------------------------------------------


def _unavailable(detail: str) -> HTTPException:
    return HTTPException(status_code=HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


def _knowledge_read(request: Request) -> KnowledgeReadStore:
    store = getattr(request.app.state, "knowledge_read", None)
    if store is None:
        raise _unavailable("service knowledge read service is unavailable")
    return store


def _learning_operations(request: Request) -> LearningOperationStore:
    store = getattr(request.app.state, "learning_operations", None)
    if store is None:
        raise _unavailable("learning operation service is unavailable")
    return store


def _coordinator(request: Request) -> MetadataMutationCoordinator:
    coordinator = getattr(request.app.state, "metadata_coordinator", None)
    if coordinator is None:
        raise _unavailable("service metadata coordinator is unavailable")
    return coordinator


def _knowledge_write(request: Request) -> KnowledgeWriteStore:
    sessions = getattr(request.app.state, "session_factory", None)
    if sessions is None:
        raise _unavailable("service knowledge write service is unavailable")
    return KnowledgeWriteStore(sessions)


def _learn_enqueue(request: Request) -> Any:
    enqueue = getattr(request.app.state, "learn_engine_enqueue", None)
    if enqueue is None:
        raise _unavailable("learning enqueue seam is unavailable")
    return enqueue


# ---------------------------------------------------------------------------
# error helpers (C01 structured envelope)
# ---------------------------------------------------------------------------


def _not_found(request: Request, message: str) -> JSONResponse:
    return render_error_envelope(request, HTTP_404_NOT_FOUND, "not_found", message)


def _conflict(request: Request, code: str, message: str) -> JSONResponse:
    return render_error_envelope(request, HTTP_409_CONFLICT, code, message)


def _bad_request(request: Request, message: str) -> JSONResponse:
    return render_error_envelope(request, HTTP_400_BAD_REQUEST, "invalid_request", message)


#: Stable store refusal code → (HTTP status, C01 code). Anything absent
#: falls back to a 409 conflict with the store's own code.
_ADMISSION_ERROR_STATUS: dict[str, tuple[int, str]] = {
    "unknown_operation": (HTTP_404_NOT_FOUND, "not_found"),
    "source_cancelled": (HTTP_409_CONFLICT, "source_cancelled"),
    "operation_not_runnable": (HTTP_409_CONFLICT, "operation_not_runnable"),
    "generation_superseded": (HTTP_409_CONFLICT, "generation_superseded"),
    "payload_conflict": (HTTP_409_CONFLICT, "payload_conflict"),
    "enqueue_failed": (HTTP_503_SERVICE_UNAVAILABLE, "enqueue_failed"),
    "operation_budget_exceeded": (HTTP_409_CONFLICT, "operation_budget_exceeded"),
}


def _admission_error(
    request: Request, error: OperationAdmissionError
) -> JSONResponse:
    status, code = _ADMISSION_ERROR_STATUS.get(
        error.code, (HTTP_409_CONFLICT, error.code)
    )
    return render_error_envelope(request, status, code, error.detail)


# ---------------------------------------------------------------------------
# scope guard (strict same-server C3: unknown/cross-server → 404)
# ---------------------------------------------------------------------------


async def _ensure_scope(
    request: Request,
    knowledge_read: KnowledgeReadStore,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
) -> JSONResponse | None:
    """A 404 not_found envelope when the application is unknown on this
    server (no map entry AND no knowledge identity); ``None`` when the
    scope is owned. C3 never substitutes or confirms a foreign scope."""
    try:
        await knowledge_read.get_service_knowledge(
            server_id=server_id,
            application_id=application_id,
            limit=1,
            offset=0,
        )
    except ServiceKnowledgeNotFoundError:
        return _not_found(
            request,
            f"no service knowledge exists for application {application_id} "
            f"on server {server_id}",
        )
    except ScopeMismatchError as error:  # pragma: no cover - defensive
        return _not_found(
            request,
            f"scope mismatch between {error} identities",
        )
    return None


# ---------------------------------------------------------------------------
# payload models
# ---------------------------------------------------------------------------


class RetractBody(BaseModel):
    """Operator retraction compare-and-set values (exact, stale-proof)."""

    expected_revision: int = Field(
        ge=0,
        description="The fact revision the operator observed before retracting",
    )
    expected_generation: int = Field(
        ge=0,
        description="The knowledge generation the operator observed before retracting",
    )
    reason: str | None = Field(
        default=None,
        max_length=400,
        description="Bounded value-free operator reason (not persisted)",
    )


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


@router.get("")
async def read_knowledge(
    request: Request,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> JSONResponse:
    """The bounded current knowledge read (D2/D3): identity/revision
    scalars, operator map state, catalog runbook and the ACTIVE
    learned-fact page (``limit``/``offset`` paginated)."""
    await _require_session(request)
    knowledge_read = _knowledge_read(request)
    try:
        read = await knowledge_read.get_service_knowledge(
            server_id=server_id,
            application_id=application_id,
            limit=limit,
            offset=offset,
        )
    except ServiceKnowledgeNotFoundError:
        return _not_found(
            request,
            f"no service knowledge exists for application {application_id} "
            f"on server {server_id}",
        )
    except ScopeMismatchError:
        return _not_found(
            request,
            "the requested scope does not match the authorized server",
        )
    return JSONResponse(content=read.model_dump(mode="json"))


@router.get("/history")
async def read_fact_history(
    request: Request,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    fact_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> JSONResponse:
    """The paginated append-only fact revision history, newest change
    first, optional ``fact_id`` filter."""
    await _require_session(request)
    knowledge_read = _knowledge_read(request)
    refusal = await _ensure_scope(request, knowledge_read, server_id, application_id)
    if refusal is not None:
        return refusal
    page = await knowledge_read.list_fact_history(
        server_id=server_id,
        application_id=application_id,
        fact_id=fact_id,
        limit=limit,
        offset=offset,
    )
    return JSONResponse(content=page.model_dump(mode="json"))


@router.get("/operations")
async def read_operations(
    request: Request,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    status: OperationStatus | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> JSONResponse:
    """The paginated durable preservation-operation receipts of THIS
    application, newest acceptance first, optional ``status`` filter."""
    await _require_session(request)
    knowledge_read = _knowledge_read(request)
    # NOTE: no identity/map existence requirement here — a queued-only
    # application owns durable receipts; the (server, application)
    # predicates ARE the scope (unknown application → honest empty page).
    page = await knowledge_read.list_learning_operations(
        server_id=server_id,
        application_id=application_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return JSONResponse(content=page.model_dump(mode="json"))


@router.post("/facts/{fact_id}/retract")
async def retract_fact(
    request: Request,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    fact_id: uuid.UUID,
    payload: RetractBody,
) -> JSONResponse:
    """Operator retraction by exact compare-and-set under the C4 lease
    set (mutation order: session → allowed-origin → CSRF)."""
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    knowledge_read = _knowledge_read(request)
    refusal = await _ensure_scope(request, knowledge_read, server_id, application_id)
    if refusal is not None:
        return refusal
    import asyncio

    coordinator = _coordinator(request)
    write = _knowledge_write(request)

    held: tuple[Any, ...] = ()
    try:
        # The lease acquisition is a SYNC blocking call: offload it off
        # the event loop like every other blocking seam.
        held = await asyncio.to_thread(
            coordinator.lease_set, server_id, (application_id,)
        )
        result = await write.retract_fact(
            server_id=server_id,
            application_id=application_id,
            fact_id=fact_id,
            expected_revision=payload.expected_revision,
            expected_generation=payload.expected_generation,
            reason=payload.reason,
        )
    except MetadataMutationError as error:
        return _conflict(request, error.code.value, error.detail)
    except ScopeMismatchError as error:
        return _not_found(request, f"scope mismatch: {error}")
    except ValueError as error:
        return _bad_request(request, str(error))
    finally:
        if held:
            await asyncio.to_thread(coordinator._release_acquired, held)

    if result.status is OperationStatus.APPLIED:
        return JSONResponse(content=result.model_dump(mode="json"))
    # Refused by the seam (stale revision/generation): structured 409.
    code = result.error_code or "revision_conflict"
    message = result.error_detail or "the retraction was refused"
    return _conflict(request, code, message)


@router.post("/operations/{operation_id}/retry")
async def retry_operation(
    request: Request,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    operation_id: uuid.UUID,
) -> JSONResponse:
    """Explicit authorized retry of one unacknowledged (``queued``)
    preservation operation through its SAME deterministic workflow id.
    An unknown-effect outcome is refused (409); cancelled, superseded
    and otherwise non-queued rows are refused (409); an unknown or
    cross-scope identity answers 404."""
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    knowledge_read = _knowledge_read(request)
    operations = _learning_operations(request)
    enqueue = _learn_enqueue(request)

    # Strict scope binding: the operation must exist under the PATH's
    # (server, application) — a cross-server id answers 404, never a
    # substituted retry.
    scope_page = await knowledge_read.list_learning_operations(
        server_id=server_id,
        application_id=application_id,
        operation_id=operation_id,
        limit=1,
        offset=0,
    )
    if not scope_page.items:
        return _not_found(
            request,
            f"no preservation operation {operation_id} exists for "
            f"application {application_id} on server {server_id}",
        )

    try:
        receipt = await operations.retry_operation(
            operation_id, engine_enqueue=enqueue
        )
    except UnknownOutcomeRetryForbiddenError as error:
        return _conflict(request, "unknown_effect", str(error))
    except OperationAdmissionError as error:
        return _admission_error(request, error)
    except ScopeMismatchError as error:
        return _not_found(request, f"scope mismatch: {error}")
    return JSONResponse(content=receipt.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def register_service_knowledge(app: FastAPI) -> None:
    """Mount the scoped service-knowledge router on ``app``."""
    app.include_router(router)
