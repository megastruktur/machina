"""Knowledge/memory and local-model HTTP surfaces (card 11-knowledge-api).

Ownership map (contracts.md **C07** memory, **C03** credentials/grants,
**C09** planned lifecycle, **C08** UI/API and auth, **C01** envelope/
identity/scope; SERVICES ``mai-api`` row: router owners; producers
``11-memory-adapter`` (:mod:`mai.memory.hindsight`) and
``11-model-install`` (:mod:`mai.applications.local_models`); downstream
consumers ``11-web-memory-models`` and the ``11-verify`` gate scenario):

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
- **The local-model surface is the DURABLE planned lifecycle, never a
  raw URL shell (C09 rule 3, acceptance 1).** ``POST …/preview``
  rebuilds the deterministic policy-bound plan and MEASURES the real
  target prerequisite facts through the SAME bounded read-only command
  lane discovery uses (arch, CPU flags, RAM, Docker/compose readiness,
  real Docker data-root disk budget; GPU memory only when the recipe
  declares a ``hw.vram.*`` token) and evaluates the producer's frozen
  fail-closed hardware semantics — a preview measures and reports, it
  never installs anything. ``POST …/{application_id}/install`` and
  ``POST …/{application_id}/pull`` submit ONE capability step through
  the EXISTING jobs facade (:func:`mai.api.jobs.submit_run` — the
  reviewed idempotent durable submit, C05 rule 5): HTTP persists intent
  only; the executor's full gate chain (fail-closed hardware
  prerequisites, policy decision + fresh recheck, one-use approval
  bound to the payload hash, exclusive ``application:<id>`` lease,
  verified apply, real-endpoint completion gate) runs in the worker.
  The pull intent's completion IS the endpoint registration: the
  executor registers the C02 connection only after the serving daemon
  really lists the pulled model and the bounded tool-call probe has
  answered — there is deliberately no second, ungated registration
  route that could bypass that honesty gate.
- **Credential references only (C03 rule 6).** Recipe credential
  references travel as resolver names into the durable intent
  (``credential_references``); no value is ever read, resolved or
  returned here. Fail-closed everywhere: an unusable store or
  transport answers ``503``/``422`` with the producer's stable codes.

Route/doc listeners stay off (G1); the schema is produced in-process
via ``app.openapi()`` for the central exporter owned by the
``openapi-client`` resource; this surface's route types are regenerated
mechanically from that export (no handwritten type edits).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Final, Literal

from fastapi import APIRouter, FastAPI, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.status import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from mai.api.applications import LIFECYCLE_MODEL_PROFILE, PlanView, _plan_view
from mai.api.auth import (
    BoundaryError,
    ErrorEnvelope,
    _require_allowed_origin,
    _require_csrf,
    _require_session,
    render_error_envelope,
)
from mai.api.jobs import (
    RunCreated,
    RunSubmitBody,
    StepBody,
    _canonical_profile,
    submit_run,
)
from mai.applications.install import LOCK_RESOURCE_PREFIX
from mai.applications.local_models import (
    CAPABILITY_ID as MODEL_INSTALL_CAPABILITY,
    collect_target_facts,
    evaluate_hardware_checks,
)
from mai.applications.recipes import (
    RecipeError,
    TargetEnvironment,
    build_deployment_plan,
    canonical_plan_json,
    parse_recipe_text,
    render_recipe,
)
from mai.auth.sessions import hash_token
from mai.capabilities.dispatch import CapabilityCall
from mai.contracts.server import parse_server_profile_text
from mai.credentials.env import EnvFileError
from mai.execution.commands import CommandOutcome, CommandRequest, execute_command
from mai.execution.ssh import SshConnectError, SshErrorCode, resolve_identity_key
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

#: The lifecycle lane marker for local-model submits (same no-model lane the
#: P08 application lifecycle uses; the capability steps never touch a model).
MODEL_LIFECYCLE_MODEL_PROFILE: Final = LIFECYCLE_MODEL_PROFILE

#: Pull-intent payload marker (the executor vocabulary for the weight
#: acquisition + registration operation; the install intent is identified by
#: the ``model.install`` capability id and its plan document).
MODEL_PULL_KIND: Final = "model.pull"
MODEL_INSTALL_KIND: Final = "model.install"


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


def _applications(request: Request) -> Any:
    store = getattr(request.app.state, "applications", None)
    if store is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "applications canonical store is unavailable",
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
# Local-model wire shapes (strict; plan truth is the recipe producer)
# ---------------------------------------------------------------------------


class ModelLocalPreviewBody(BaseModel):
    """Prerequisite preview body (strict): identity + recipe + target."""

    model_config = ConfigDict(extra="forbid")

    application_id: str
    recipe: str = Field(min_length=1)
    target_os: str = Field(min_length=1, max_length=64)
    target_arch: str = Field(min_length=1, max_length=64)


class ModelLocalSubmitBody(BaseModel):
    """Install submit body (strict): the plan is rebuilt server-side."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=200)
    recipe: str = Field(min_length=1)
    target_os: str = Field(min_length=1, max_length=64)
    target_arch: str = Field(min_length=1, max_length=64)
    backup_confirmed: bool = False


class ModelLocalPullBody(BaseModel):
    """Pull submit body (strict): one bounded acquisition + registration.

    ``license`` is REQUIRED metadata naming the license under which the
    operator acquires the weights (the recipe pre-authorizes none); it
    travels value-free inside the digest-bound intent.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=200)
    recipe: str = Field(min_length=1)
    target_os: str = Field(min_length=1, max_length=64)
    target_arch: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    license: str = Field(min_length=1, max_length=256)
    base_url: str = Field(min_length=1, max_length=2048)


class ModelLocalPreviewView(BaseModel):
    """The C09 rule 3 preview + REAL measured prerequisite facts.

    ``hardware_unsatisfied`` is the producer's fail-closed verdict over
    the measured facts (an unknown/unmeasurable fact counts as NOT
    satisfied); ``verdict`` is ``satisfied`` only when every declared
    prerequisite token is measured as satisfied. The preview never
    installs anything; the executor re-measures fresh facts behind the
    full gate chain at apply time.
    """

    server_id: str
    application_id: str
    plan: PlanView
    facts: dict[str, Any]
    unknown: list[str]
    measurement_notes: list[str]
    hardware_unsatisfied: list[str]
    verdict: Literal["satisfied", "unsatisfied"]


# ---------------------------------------------------------------------------
# Local-model routes
# ---------------------------------------------------------------------------

model_local_router = APIRouter(
    prefix="/api/v1/servers/{server_id}/models/local",
    tags=["models-local"],
)


def _recipe_error(request: Request, error: RecipeError) -> Response:
    return render_error_envelope(
        request,
        HTTP_422_UNPROCESSABLE_CONTENT,
        "validation_failed",
        error.detail,
        fields={"recipe": error.code.value},
    )


def _parse_recipe_or_error(request: Request, text: str) -> tuple[Any, Response | None]:
    try:
        return parse_recipe_text(text), None
    except RecipeError as error:
        return None, _recipe_error(request, error)


def _build_plan_or_error(
    request: Request,
    recipe: Any,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    target_os: str,
    target_arch: str,
) -> tuple[Any, Response | None]:
    try:
        return (
            build_deployment_plan(
                recipe,
                server_id,
                application_id,
                TargetEnvironment(os=target_os, arch=target_arch),
            ),
            None,
        )
    except RecipeError as error:
        return None, _recipe_error(request, error)


def _application_uuid(
    request: Request, value: str
) -> tuple[uuid.UUID | None, Response | None]:
    try:
        return uuid.UUID(value), None
    except ValueError:
        return None, render_error_envelope(
            request,
            HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_failed",
            "application_id must be a canonical UUID string",
            fields={"application_id": "invalid"},
        )


def _cross_server_guard(
    request: Request, store: Any, server_id: uuid.UUID, application_id: uuid.UUID
) -> Response | None:
    """C09 rule 1 identity guard (mirrors the P08 submit boundary): the
    application id must never belong to another server's canonical tree."""
    if any(other != server_id for other in store.servers_storing(application_id)):
        return render_error_envelope(
            request,
            HTTP_409_CONFLICT,
            "conflict",
            "this application id already exists under a different server; "
            "cross-server relocation is refused",
        )
    return None


@model_local_router.post(
    "/preview",
    status_code=HTTP_200_OK,
    response_model=ModelLocalPreviewView,
    summary="Preview the plan and MEASURE real hardware prerequisites",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowlisted or CSRF token mismatch",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No canonical profile stored for the target server id",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Pinned host key does not match the target",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Recipe, target or transport prerequisites failed "
            "validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Credential source or SSH transport unavailable",
        },
    },
)
async def preview_model_install(
    request: Request, server_id: uuid.UUID, payload: ModelLocalPreviewBody
) -> Response:
    """Plan + fail-closed prerequisite verdict from REAL target facts.

    POST carries the frozen mutating-request boundary, but the route is
    effect-free on both ends: no store write, no deployment, no weight
    download — only the bounded read-only measurement commands
    (``uname``/``/proc/cpuinfo``/``/proc/meminfo``/docker
    version+info/``df``; ``nvidia-smi`` only when a ``hw.vram.*`` token
    is declared). Unsatisfied or unmeasurable prerequisites are reported
    honestly; the executor re-verifies fresh facts behind the full gate
    chain before any effect.
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    store = _applications(request)
    stored = _canonical_profile(request, server_id)
    application_id, error = _application_uuid(request, payload.application_id)
    if error is not None:
        return error
    recipe, error = _parse_recipe_or_error(request, payload.recipe)
    if error is not None:
        return error
    plan, error = _build_plan_or_error(
        request,
        recipe,
        server_id,
        application_id,
        payload.target_os,
        payload.target_arch,
    )
    if error is not None:
        return error
    resolver = getattr(request.app.state, "credentials", None)
    if resolver is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "credential source is not configured",
        )
    profile = parse_server_profile_text(stored.text)
    known_hosts_path = store.root / "servers" / str(server_id) / "known_hosts"
    try:
        key_path = resolve_identity_key(resolver, profile.ssh.credential_ref)
    except EnvFileError, SshConnectError:
        raise BoundaryError(
            HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_configuration",
            "transport prerequisites are missing; nothing ran",
        ) from None

    def transport(command: CommandRequest) -> CommandOutcome:
        return execute_command(
            profile.ssh,
            command,
            key_path=key_path,
            known_hosts_path=known_hosts_path,
        )

    need_vram = any(str(token).startswith("hw.vram.") for token in plan.prerequisites)
    try:
        facts, unknown, notes = await asyncio.to_thread(
            collect_target_facts, transport, server_id, need_vram=need_vram
        )
    except SshConnectError as error:
        if error.code is SshErrorCode.HOST_KEY_MISMATCH:
            return render_error_envelope(
                request,
                HTTP_409_CONFLICT,
                "conflict",
                "pinned host key does not match the target; re-enrollment "
                "is an explicit admin action",
            )
        if error.code is SshErrorCode.INVALID_CONFIGURATION:
            return render_error_envelope(
                request,
                HTTP_422_UNPROCESSABLE_CONTENT,
                "invalid_configuration",
                error.detail,
            )
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "prerequisite measurement transport is unavailable",
        ) from None
    except OSError as error:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "prerequisite measurement transport is unavailable",
        ) from error
    unsatisfied = sorted(evaluate_hardware_checks(set(plan.prerequisites), facts))
    plan_view = _local_plan_view(plan)
    return ModelLocalPreviewView(
        server_id=str(server_id),
        application_id=str(application_id),
        plan=plan_view,
        facts=dict(facts),
        unknown=list(unknown),
        measurement_notes=list(notes),
        hardware_unsatisfied=unsatisfied,
        verdict="satisfied" if not unsatisfied else "unsatisfied",
    )


def _local_plan_view(plan: Any) -> PlanView:
    """Project the deterministic plan into the shared wire view."""
    return _plan_view(plan)


async def _model_lifecycle_submit(
    request: Request,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    body: ModelLocalSubmitBody | ModelLocalPullBody,
    *,
    kind: str,
    extra_payload: dict[str, Any],
) -> Response:
    """Shared durable submit: validate → rebuild plan → ONE gated step.

    Mutation order: session → origin → CSRF (the jobs facade re-checks
    its own boundary on submit). The intent names exactly the frozen
    ``model.install`` capability id the worker's G2 gate must allow; the
    payload is digest-bound into the approval at effect time, so the
    executor's one-use approval and this submit's idempotency digest
    always agree.
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    store = _applications(request)
    _canonical_profile(request, server_id)
    recipe, error = _parse_recipe_or_error(request, body.recipe)
    if error is not None:
        return error
    guard = _cross_server_guard(request, store, server_id, application_id)
    if guard is not None:
        return guard
    plan, error = _build_plan_or_error(
        request, recipe, server_id, application_id, body.target_os, body.target_arch
    )
    if error is not None:
        return error
    payload: dict[str, Any] = {
        "kind": kind,
        **json.loads(canonical_plan_json(plan)),
        "recipe": render_recipe(recipe),
        **extra_payload,
    }
    step = StepBody(
        capability_id=MODEL_INSTALL_CAPABILITY,
        payload=payload,
        resource=f"{LOCK_RESOURCE_PREFIX}{application_id}",
    )
    facade_body = RunSubmitBody(
        run_id=body.run_id,
        server_id=str(server_id),
        model_profile=MODEL_LIFECYCLE_MODEL_PROFILE,
        credential_references=list(recipe.credentials),
        steps=[step],
    )
    return await submit_run(request, facade_body)


@model_local_router.post(
    "/{application_id}/install",
    status_code=HTTP_201_CREATED,
    response_model=RunCreated,
    summary="Submit a bounded local-model service install intent (durable)",
    responses={
        HTTP_200_OK: {
            "model": RunCreated,
            "description": "Duplicate submit attached to the existing run",
        },
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowlisted or CSRF token mismatch",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No canonical profile stored for the target server id",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Run id reused by a different intent, or the "
            "application id belongs to another server",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Jobs domain unavailable",
        },
    },
)
async def install_model_service(
    request: Request,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    payload: ModelLocalSubmitBody,
) -> Response:
    """Persist the install intent (``model.install`` step, C09 rule 3).

    No download, no compose effect and no approval consumption ever
    happens in this route: the worker's executor re-builds the SAME
    deterministic plan (identical plan hash), refuses unsatisfied
    hardware BEFORE the one-use approval is spent, and converges the
    canonical manifest only on verified success. An ungranted target
    leaves the disk untouched by construction — the durable intent is
    refused at the worker's G2 gate.
    """
    return await _model_lifecycle_submit(
        request,
        server_id,
        application_id,
        payload,
        kind=MODEL_INSTALL_KIND,
        extra_payload={"backup_confirmed": payload.backup_confirmed},
    )


@model_local_router.post(
    "/{application_id}/pull",
    status_code=HTTP_201_CREATED,
    response_model=RunCreated,
    summary="Submit one bounded weight pull + endpoint registration intent",
    responses={
        HTTP_200_OK: {
            "model": RunCreated,
            "description": "Duplicate submit attached to the existing run",
        },
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowlisted or CSRF token mismatch",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No canonical profile stored for the target server id",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Run id reused by a different intent, or the "
            "application id belongs to another server",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Jobs domain unavailable",
        },
    },
)
async def pull_model_weights(
    request: Request,
    server_id: uuid.UUID,
    application_id: uuid.UUID,
    payload: ModelLocalPullBody,
) -> Response:
    """Persist one explicit acquisition + registration intent.

    The pull IS the registration path: the executor's approval is bound
    to THIS payload (model + license + recipe revision + identities —
    one decision approves exactly one acquisition), the bounded lane's
    cut is the cancellation (``outcome_unknown`` + lease fence), and the
    C02 connection record is written only after the daemon really lists
    the pulled model and the bounded tool-call probe has answered —
    capabilities are reported honestly, never inferred from the model
    name. There is no separate ungated registration route by design.
    """
    return await _model_lifecycle_submit(
        request,
        server_id,
        application_id,
        payload,
        kind=MODEL_PULL_KIND,
        extra_payload={
            "application_id": str(application_id),
            "model": payload.model,
            "license": payload.license,
            "base_url": payload.base_url,
        },
    )


# ---------------------------------------------------------------------------
# Registration (api-wiring lock; single boundary, no bypass routes)
# ---------------------------------------------------------------------------


def register_knowledge_models(app: FastAPI) -> None:
    """Register both surfaces under the shared boundary (C01/C08)."""
    app.include_router(memory_router)
    app.include_router(model_local_router)
