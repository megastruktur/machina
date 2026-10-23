"""Memory HTTP surface (card 11-knowledge-api).

Ownership map (contracts.md **C07** memory, **C03** credentials/grants,
**C08** UI/API and auth, **C01** envelope/identity/scope; SERVICES
``mai-api`` row: router owners; producer ``11-memory-adapter``
(:mod:`mai.memory.hindsight`); downstream consumer
``11-web-memory-models`` and the ``11-verify`` gate scenario):

- **Memory operations are server-scoped through the session-derived
  context and the grant configuration (C07 rule 6, C03 rule 3).** The
  bank is NEVER a client-supplied name: ``scope`` may be ``"server"``
  (the path server's own bank, derived from the authenticated request's
  explicit target) or ``"shared:<name>"`` (only through the configured
  allowlist wired from ``MAI_MEMORY_SHARED_BANKS``). Every content
  operation re-reads the current grant configuration and runs the frozen
  policy evaluator (:func:`mai.policy.evaluate.evaluate`) against the
  memory capability descriptors BEFORE any provider call: an ungranted
  capability, a server outside the grant scope, a policy-denied or an
  unenrolled target is a structured ``403``, never a silent no-op. The
  frozen capability vocabulary is exactly the producer's:
  ``memory.recall``/``memory.reflect`` (read class) and
  ``memory.retain`` (mutate class). Delete and export are bank-scope
  operations of the same two classes — delete is a bank WRITE (the
  ``memory.retain`` grant), export is a bank READ (the ``memory.recall``
  grant); no new capability id is invented here. Recall/retain/reflect
  execute through the producer's own capability executors — the same
  gated code path the agent bridge uses — so outbound secret screening,
  payload bounds and honest provider-failure classification cannot
  diverge between surfaces.
- **Failures are honest and value-free (C07 rule 6, KB-H2).** A
  configured-but-unreachable Hindsight answers ``200`` with the
  producer's own ``unavailable`` document (status/code/error_type —
  the adapter's degradation contract); a payload/scope rejection is a
  ``422`` carrying the producer's stable code; an unconfigured memory
  service fails closed with ``503 unavailable``. A retain failure is
  never reported as success, and no response ever carries credential
  material: the surface names ``MAI_HINDSIGHT_URL`` wiring only.

- **No credential material (C03 rule 6).** The surface never reads,
  resolves or returns credential values: requests authenticate through
  the session-derived context only, fail closed on every gate, and
  responses stay value-free.

Route/doc listeners stay off (G1); the schema is produced in-process
via ``app.openapi()`` for the central exporter owned by the
``openapi-client`` resource; this surface's route types are regenerated
mechanically from that export (no handwritten type edits).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Final, Literal

from fastapi import APIRouter, FastAPI, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.status import (
    HTTP_200_OK,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from mai.api.auth import (
    BoundaryError,
    ErrorEnvelope,
    _require_allowed_origin,
    _require_csrf,
    _require_session,
    render_error_envelope,
)
from mai.auth.sessions import hash_token
from mai.capabilities.dispatch import CapabilityCall
from mai.contracts.server import parse_server_profile_text
from mai.memory.hindsight import (
    RECALL_CAPABILITY_ID,
    REFLECT_CAPABILITY_ID,
    RETAIN_CAPABILITY_ID,
    DeleteOutcome,
    HealthOutcome,
    MemoryAdapterError,
    MemoryErrorCode,
    MemoryProvider,
    MemoryRecallCapability,
    MemoryReflectCapability,
    MemoryRetainCapability,
    MemoryStatus,
    VALID_FACT_TYPES,
    bank_for_scope,
    delete_document,
    health_document,
    parse_delete_document,
    recall_descriptor,
    reflect_descriptor,
    retain_descriptor,
)
from mai.policy.config import GrantConfigStore
from mai.policy.evaluate import (
    ExecutionContext,
    PolicyMode,
    evaluate,
)

__all__ = [
    "MEMORY_BASE_URL_VAR",
    "MEMORY_SHARED_BANKS_VAR",
    "MemoryServices",
    "register_knowledge_models",
]

# ---------------------------------------------------------------------------
# Wiring vocabulary (lifespan-owned configuration; documents-surface pattern)
# ---------------------------------------------------------------------------

#: Bank-scoped Hindsight API root (compose-internal per knowledge-packaging
#: ADR §1/§3; C07 rule 7). There is NO discovered default: an absent value
#: leaves the memory surface absent and every route fails closed with
#: ``503 unavailable`` — the embeddings-endpoint precedent.
MEMORY_BASE_URL_VAR: Final = "MAI_HINDSIGHT_URL"

#: Optional comma-separated explicit shared-bank allowlist (C07 rule 6:
#: bank-per-server plus explicitly named shared banks). Absent/empty means
#: the deployment exposes no shared banks (every ``shared:…`` scope is then
#: the producer's ``bank_forbidden``).
MEMORY_SHARED_BANKS_VAR: Final = "MAI_MEMORY_SHARED_BANKS"


class MemoryServices:
    """The memory-surface bundle built once in the app lifespan.

    ``provider`` is the bank-scoped Hindsight HTTP seam or ``None`` — the
    producer's own degradation contract turns a ``None`` provider into an
    honest ``unavailable`` answer for recall/retain/reflect, while the
    surface keeps ``503`` for an entirely unconfigured service bundle.
    ``shared_banks`` is the validated explicit allowlist (possibly empty).
    """

    def __init__(
        self,
        *,
        provider: MemoryProvider | None,
        shared_banks: tuple[str, ...] = (),
    ) -> None:
        self.provider = provider
        self.shared_banks = shared_banks


# ---------------------------------------------------------------------------
# Fail-closed accessors (documents/jobs surface pattern)
# ---------------------------------------------------------------------------


def _memory(request: Request) -> MemoryServices:
    services = getattr(request.app.state, "memory", None)
    if services is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "the memory service is not configured",
        )
    return services


def _grants(request: Request) -> GrantConfigStore:
    store = getattr(request.app.state, "grants", None)
    if store is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "capability grant configuration is unavailable",
        )
    return store


def _profiles(request: Request) -> Any:
    store = getattr(request.app.state, "profiles", None)
    if store is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "canonical profile service is unavailable",
        )
    return store


# ---------------------------------------------------------------------------
# Session-derived context + grant gate (C01 rule 2, C03 rule 3)
# ---------------------------------------------------------------------------


async def _server_context(
    request: Request,
    server_id: uuid.UUID,
    capability_id: str,
    descriptor: Any,
    *,
    mode: PolicyMode,
    mutating: bool = True,
) -> tuple[ExecutionContext, str]:
    """Session → enrolled target → current grants/policy → decision.

    Returns the session-derived :class:`ExecutionContext` (the principal
    is the session token hash — never a client-supplied flag, C01 rule 2)
    and the raw token. Raises the C01 envelope errors for every gate:
    unknown target ``404``, ungranted/denied ``403`` with the producer's
    stable decision reason (value-free; nothing about OTHER servers'
    grants is revealed). ``mutating=False`` (safe methods) applies the
    session layer only — origin/CSRF are mutating-request layers (ADR §5).
    """
    _store, token = await _require_session(request)  # layer 1
    if mutating:
        _require_allowed_origin(request)  # layer 2
        _require_csrf(request)  # layer 3
    profiles = _profiles(request)
    try:
        stored = profiles.read(server_id)
    except Exception:
        raise BoundaryError(
            HTTP_404_NOT_FOUND,
            "not_found",
            "no enrolled profile stored for this server id",
        ) from None
    profile = parse_server_profile_text(stored.text)
    config = _grants(request).read()
    context = ExecutionContext(
        principal=hash_token(token),
        server_id=server_id,
        run_id=uuid.uuid4(),
        parent_scope=None,
        profile_revision=str(stored.revision),
        prompt_revision=str(profile.prompt_revision),
        credential_references=(),
        allowed_capabilities=(capability_id,),
        mode=mode,
    )
    # The enrolled canonical profile IS the adoption probe (G2 step 4
    # evidence parity with the documents research surface): a readable
    # profile is an enrolled target; anything else is refused above.
    decision = evaluate(
        context=context,
        capability_id=capability_id,
        descriptor=descriptor,
        grants=config.grants,
        policy=config.policy,
        adopted=True,
    )
    if not decision.allowed:
        raise BoundaryError(
            HTTP_403_FORBIDDEN,
            "request_rejected",
            f"capability is not available for this server ({decision.reason.value})",
        )
    return context, token


def _payload_error(request: Request, exc: MemoryAdapterError) -> Response:
    """Producer payload/scope rejection → value-free ``422`` envelope."""
    return render_error_envelope(
        request,
        HTTP_422_UNPROCESSABLE_CONTENT,
        "validation_failed",
        exc.detail,
        fields={"payload": exc.code.value},
    )


# ---------------------------------------------------------------------------
# Memory wire shapes (strict; payload truth is the memory adapter)
# ---------------------------------------------------------------------------


class MemoryRetainItemBody(BaseModel):
    """One retain item (strict; the adapter validates bounds/content)."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1)
    context: str | None = None
    document_id: str | None = None


class MemoryRecallBody(BaseModel):
    """Recall body (strict): scope + bounded query vocabulary."""

    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1, max_length=80)
    query: str = Field(min_length=1, max_length=4096)
    types: list[str] | None = None
    budget: str | None = None
    max_tokens: int | None = None


class MemoryRetainBody(BaseModel):
    """Retain body (strict): scope + synchronous items (real receipt)."""

    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1, max_length=80)
    items: list[MemoryRetainItemBody] = Field(min_length=1, max_length=64)


class MemoryReflectBody(BaseModel):
    """Reflect body (strict): scope + bounded generated answer request."""

    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1, max_length=80)
    query: str = Field(min_length=1, max_length=4096)
    max_tokens: int | None = None


class MemoryDeleteBody(BaseModel):
    """Delete body (strict): explicit target, never an inferred flag."""

    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1, max_length=80)
    target: Literal["memories", "bank"]
    fact_type: str | None = None


# ---------------------------------------------------------------------------
# Memory routes (frozen design targets; mutation order: session → origin → CSRF)
# ---------------------------------------------------------------------------

memory_router = APIRouter(
    prefix="/api/v1/servers/{server_id}/memory",
    tags=["memory"],
)


@memory_router.get(
    "/status",
    status_code=HTTP_200_OK,
    summary="Hindsight availability/version probe (KB-H1 adapter side)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Memory service is not configured",
        },
    },
)
async def memory_status(request: Request, server_id: uuid.UUID) -> Response:
    """One honest availability probe; no bank content is touched.

    Safe method: session layer only. The enrolled-target check still
    applies (the probe names an explicit server scope), but no grant is
    consumed: availability discloses no memory content.
    """
    await _require_session(request)
    profiles = _profiles(request)
    try:
        profiles.read(server_id)
    except Exception:
        raise BoundaryError(
            HTTP_404_NOT_FOUND,
            "not_found",
            "no enrolled profile stored for this server id",
        ) from None
    services = _memory(request)
    provider = services.provider
    if provider is None:
        return _memory_document(
            health_document(
                HealthOutcome(
                    status=MemoryStatus.UNAVAILABLE,
                    code=MemoryErrorCode.MEMORY_DISABLED,
                    api_version=None,
                    error_type=None,
                )
            )
        )
    try:
        document = await provider.version()
    except MemoryAdapterError as exc:
        return _payload_error(request, exc)
    except Exception as exc:  # noqa: BLE001 - classified, value-free
        return _memory_document(
            health_document(
                HealthOutcome(
                    status=MemoryStatus.UNAVAILABLE,
                    code=_transport_code(exc),
                    api_version=None,
                    error_type=type(exc).__name__,
                )
            )
        )
    return _memory_document(
        health_document(
            HealthOutcome(
                status=MemoryStatus.OK,
                code=None,
                api_version=document.get("api_version")
                if isinstance(document.get("api_version"), str)
                else None,
                error_type=None,
            )
        )
    )


@memory_router.post(
    "/recall",
    status_code=HTTP_200_OK,
    summary="Recall cited historical context from the scoped bank",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin/CSRF rejected, or the memory.recall "
            "grant does not cover this server",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No enrolled profile stored for this server id",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Payload or scope rejected by the memory adapter",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Memory service is not configured",
        },
    },
)
async def memory_recall(
    request: Request, server_id: uuid.UUID, payload: MemoryRecallBody
) -> Response:
    """One gated query through the producer's own recall executor.

    The grant check runs the frozen evaluator for ``memory.recall`` on
    the session-derived context BEFORE any provider I/O; the payload is
    validated by the adapter (unknown fields, secret-shaped queries and
    unlisted shared banks are refused before anything leaves the
    process).
    """
    return await _memory_operation(
        request,
        server_id,
        capability_id=RECALL_CAPABILITY_ID,
        descriptor=recall_descriptor(),
        payload=_recall_payload(payload),
        build_call=lambda descriptor, payload, context, op_id: MemoryRecallCapability(
            _memory(request).provider,
            shared_banks=_memory(request).shared_banks,
        )(
            CapabilityCall(
                descriptor=descriptor,
                payload=payload,
                context=context,
                credentials={},
                op_id=op_id,
            )
        ),
    )


@memory_router.post(
    "/retain",
    status_code=HTTP_200_OK,
    summary="Retain items synchronously into the scoped bank",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin/CSRF rejected, or the memory.retain "
            "grant does not cover this server",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No enrolled profile stored for this server id",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Payload or scope rejected by the memory adapter",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Memory service is not configured",
        },
    },
)
async def memory_retain(
    request: Request, server_id: uuid.UUID, payload: MemoryRetainBody
) -> Response:
    """Synchronous write with the adapter's real receipt (KB-H2).

    The authenticated POST is itself the operator decision, so the
    context runs in ``delegated`` mode (C03 rule 5: prompting behavior
    within grants — no additional approval is promptable inside a
    request). A failed retain answers the producer's ``unavailable``
    document — never a fabricated success.
    """
    return await _memory_operation(
        request,
        server_id,
        capability_id=RETAIN_CAPABILITY_ID,
        descriptor=retain_descriptor(),
        payload=_retain_payload(payload),
        build_call=lambda descriptor, payload, context, op_id: MemoryRetainCapability(
            _memory(request).provider,
            shared_banks=_memory(request).shared_banks,
        )(
            CapabilityCall(
                descriptor=descriptor,
                payload=payload,
                context=context,
                credentials={},
                op_id=op_id,
            )
        ),
    )


@memory_router.post(
    "/reflect",
    status_code=HTTP_200_OK,
    summary="Bounded generated answer grounded in the scoped bank",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin/CSRF rejected, or the memory.reflect "
            "grant does not cover this server",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No enrolled profile stored for this server id",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Payload or scope rejected by the memory adapter",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Memory service is not configured",
        },
    },
)
async def memory_reflect(
    request: Request, server_id: uuid.UUID, payload: MemoryReflectBody
) -> Response:
    """One gated reflect through the producer's reflect executor."""
    return await _memory_operation(
        request,
        server_id,
        capability_id=REFLECT_CAPABILITY_ID,
        descriptor=reflect_descriptor(),
        payload=_reflect_payload(payload),
        build_call=lambda descriptor, payload, context, op_id: MemoryReflectCapability(
            _memory(request).provider,
            shared_banks=_memory(request).shared_banks,
        )(
            CapabilityCall(
                descriptor=descriptor,
                payload=payload,
                context=context,
                credentials={},
                op_id=op_id,
            )
        ),
    )


@memory_router.get(
    "/export",
    status_code=HTTP_200_OK,
    summary="Export the scoped bank manifest (backup-grade read)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "The memory.recall grant does not cover this server",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No enrolled profile stored for this server id",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Scope rejected by the memory adapter",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Memory service is not configured",
        },
    },
)
async def memory_export(
    request: Request,
    server_id: uuid.UUID,
    scope: str = Query(min_length=1, max_length=80),
) -> Response:
    """Bank READ (the ``memory.recall`` grant class): the manifest IS the
    data; it passes through unmodified and can be large (ADR backup rule:
    banks are backed up, never silently rebuilt)."""
    services = _memory(request)
    provider = services.provider
    context, _token = await _server_context(
        request,
        server_id,
        RECALL_CAPABILITY_ID,
        recall_descriptor(),
        mode=PolicyMode.DELEGATED,
        mutating=False,
    )
    if provider is None:
        return _memory_document(_export_unavailable(MemoryErrorCode.MEMORY_DISABLED))
    try:
        bank = bank_for_scope(scope, context, shared_banks=services.shared_banks)
        document = await provider.export_bank(bank_id=bank)
    except MemoryAdapterError as exc:
        return _payload_error(request, exc)
    except Exception as exc:  # noqa: BLE001 - classified, value-free
        return _memory_document(
            {
                "status": MemoryStatus.UNAVAILABLE.value,
                "code": _transport_code(exc).value,
                "provider": "hindsight",
                "operation": "export",
                "error_type": type(exc).__name__,
            }
        )
    return _memory_document(
        {"status": MemoryStatus.OK.value, "provider": "hindsight", "manifest": document}
    )


@memory_router.post(
    "/delete",
    status_code=HTTP_200_OK,
    summary="Delete memories or a whole bank (explicit, scoped, receipted)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin/CSRF rejected, or the memory.retain "
            "grant does not cover this server",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No enrolled profile stored for this server id",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Payload or scope rejected by the memory adapter",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Memory service is not configured",
        },
    },
)
async def memory_delete(
    request: Request, server_id: uuid.UUID, payload: MemoryDeleteBody
) -> Response:
    """Bank WRITE (the ``memory.retain`` grant class): the explicit
    ``target`` decides memories-vs-bank; a wrong ``fact_type`` is a 422,
    never an inferred deletion."""
    services = _memory(request)
    provider = services.provider
    context, _token = await _server_context(
        request,
        server_id,
        RETAIN_CAPABILITY_ID,
        retain_descriptor(),
        mode=PolicyMode.DELEGATED,
    )
    kind = "delete-bank" if payload.target == "bank" else "delete-memories"
    if payload.fact_type is not None and payload.fact_type not in VALID_FACT_TYPES:
        return render_error_envelope(
            request,
            HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_failed",
            "type must be one of: " + ", ".join(VALID_FACT_TYPES),
            fields={"payload": MemoryErrorCode.INVALID_PAYLOAD.value},
        )
    if provider is None:
        return _memory_document(
            delete_document(
                DeleteOutcome(
                    status=MemoryStatus.UNAVAILABLE,
                    code=MemoryErrorCode.MEMORY_DISABLED,
                    kind=kind,
                    scope=payload.scope,
                    bank="",
                    deleted_count=None,
                    error_type=None,
                )
            )
        )
    try:
        bank = bank_for_scope(
            payload.scope, context, shared_banks=services.shared_banks
        )
        if payload.target == "bank":
            document = await provider.delete_bank(bank_id=bank)
        else:
            document = await provider.delete_memories(
                bank_id=bank, fact_type=payload.fact_type
            )
    except MemoryAdapterError as exc:
        return _payload_error(request, exc)
    except Exception as exc:  # noqa: BLE001 - classified, value-free
        return _memory_document(
            delete_document(
                DeleteOutcome(
                    status=MemoryStatus.UNAVAILABLE,
                    code=_transport_code(exc),
                    kind=kind,
                    scope=payload.scope,
                    bank="",
                    error_type=type(exc).__name__,
                )
            )
        )
    return _memory_document(
        delete_document(
            parse_delete_document(document, kind=kind, scope=payload.scope, bank=bank)
        )
    )


# ---------------------------------------------------------------------------
# Memory operation plumbing
# ---------------------------------------------------------------------------


def _memory_document(document: dict[str, Any]) -> Response:
    return Response(
        content=json.dumps(document, sort_keys=True),
        media_type="application/json",
    )


def _transport_code(exc: BaseException) -> MemoryErrorCode:
    """Local classification for the no-capability operations only.

    The recall/retain/reflect executors classify through the producer's
    own seam; delete/export/status have no capability wrapper, so this
    narrow local classifier keeps the same honest vocabulary without
    importing a private producer symbol.
    """
    if isinstance(exc, TimeoutError):
        return MemoryErrorCode.PROVIDER_TIMEOUT
    return MemoryErrorCode.PROVIDER_ERROR


def _recall_payload(body: MemoryRecallBody) -> dict[str, object]:
    payload: dict[str, object] = {"scope": body.scope, "query": body.query}
    if body.types is not None:
        payload["types"] = body.types
    if body.budget is not None:
        payload["budget"] = body.budget
    if body.max_tokens is not None:
        payload["max_tokens"] = body.max_tokens
    return payload


def _retain_payload(body: MemoryRetainBody) -> dict[str, object]:
    items: list[dict[str, object]] = []
    for item in body.items:
        entry: dict[str, object] = {"content": item.content}
        if item.context is not None:
            entry["context"] = item.context
        if item.document_id is not None:
            entry["document_id"] = item.document_id
        items.append(entry)
    return {"scope": body.scope, "items": items}


def _reflect_payload(body: MemoryReflectBody) -> dict[str, object]:
    payload: dict[str, object] = {"scope": body.scope, "query": body.query}
    if body.max_tokens is not None:
        payload["max_tokens"] = body.max_tokens
    return payload


def _export_unavailable(code: MemoryErrorCode) -> dict[str, object]:
    return {
        "status": MemoryStatus.UNAVAILABLE.value,
        "code": code.value,
        "provider": "hindsight",
        "operation": "export",
        "error_type": None,
    }


async def _memory_operation(
    request: Request,
    server_id: uuid.UUID,
    *,
    capability_id: str,
    descriptor: Any,
    payload: dict[str, object],
    build_call: Any,
) -> Response:
    """Shared gated execution: grant gate → producer executor → document."""
    context, _token = await _server_context(
        request, server_id, capability_id, descriptor, mode=PolicyMode.DELEGATED
    )
    try:
        # Payload validation happens INSIDE the producer executor path
        # (request_from_payload + outbound screening) — the exact gated
        # code the agent bridge dispatches; no second validation exists.
        result = await build_call(descriptor, payload, context, uuid.uuid4())
    except MemoryAdapterError as exc:
        return _payload_error(request, exc)
    document = json.loads(result.output)
    return _memory_document(document)


# ---------------------------------------------------------------------------
# Registration (api-wiring lock; single boundary, no bypass routes)
# ---------------------------------------------------------------------------


def register_knowledge_models(app: FastAPI) -> None:
    """Register the memory surface under the shared boundary (C01/C08)."""
    app.include_router(memory_router)
