"""Model configuration/selection/probe HTTP API over the confined
atomic model store (C01, C03, C06, C08).

Ownership map (contracts.md C01/C03/C06/C08; cards 06-model-config,
06-model-store, 06-chat-api; SERVICES ``mai-api`` row):

- **Configuration** (acceptance 1): connections and model profiles are
  stored through the reviewed confined store
  (:class:`mai.agents.model_store.ModelConfigStore`) — validated by the ONE
  frozen producer (:mod:`mai.agents.models`) before any filesystem effect,
  revision-checked (C01 rule 6: the caller's ``expected_revision`` is
  compared before every mutation; a stale caller gets a structured
  ``409 conflict``), and atomically persisted. HTTP transports typed JSON
  bodies and re-renders the canonical document; field truth stays in the
  producer, never duplicated here.
- **Credential-reference-only** (C03 rule 6, acceptance 2): the wire
  carries the credential *reference* only. No route on this surface
  resolves, accepts or returns credential material: the store never echoes
  document content, the typed views expose ``credential_ref`` (a name
  matching ``[A-Z][A-Z0-9_]{0,127}``), and resolution happens exactly
  once, inside the producer's :func:`mai.agents.models.build_model` at
  probe time through the lifespan-built
  :class:`mai.credentials.env.EnvResolver`. Inline credential fields are
  rejected by the producer (``inline_credential_forbidden`` → 422) — the
  same classification the store's own write path enforces.
- **Selection** (C06 rule 1): the selected (connection, profile) pair is
  an explicit versioned record validated at write time with
  :func:`mai.agents.models.validate_selection` (pairing + capability
  gating); deleting a record the selection names is refused
  (``selection_conflict`` → 409) so the store never keeps a dangling
  selection.
- **Probe** (acceptance 1): ``POST /api/v1/models/test`` runs one bounded
  model round (a fixed minimal prompt, an output-token cap, a total
  deadline) against the selected or explicitly named pair and reports a
  single classified outcome. The classes are the reviewed producer
  vocabulary (:class:`mai.agents.models.ProviderFailureKind`) —
  ``reachability``, ``auth``, ``rejected_request`` (the endpoint/tool
  payload-rejection class: a ``tools`` probe against an endpoint that
  refuses tool calls lands here), ``provider``, ``unknown`` — plus the
  local ``credential`` class (the reference could not be resolved from the
  configured ``.env`` source; the endpoint never saw anything) and
  ``ok``. The probe is a diagnostic: a completed probe reports its
  classified outcome with ``200``, not a synthetic error status. Outcome
  details are value-free (exception class names and status codes only —
  provider response bodies are never echoed, C01 rule 3).
- **SSRF stance** (acceptance 2, consistent with the privileged admin
  grant): MAI has exactly one principal — the authenticated single
  administrator (C08 rule 1) — and pointing the controller at local
  gateways (Ollama, LM Studio, vLLM on the controller host/LAN) is a core
  product scenario, so an internal-host blocklist would break the product.
  The enforced policy is therefore producer-owned URL hygiene (absolute
  ``http(s)`` URL with a host, no userinfo credentials, no query/fragment,
  bounded length; ``file://``/``ftp://`` and userinfo forms are 422) plus
  transport hygiene: requests go through the OpenAI SDK's httpx client
  with redirects NOT followed, and the probe is deadline-bounded so a
  hanging endpoint cannot pin the request.
- **Boundary** (C08 rule 2): the surface joins the shared request
  boundary owned by :mod:`mai.api.auth` — every mutation runs session →
  exact-origin → CSRF before any effect; safe reads apply the session
  layer only. The store is built once in the lifespan over the explicitly
  configured data root; if it cannot be constructed the routes fail
  closed with ``503 unavailable`` and never touch state.
- **Registry** (C08 rule 8): this router is registered by
  :func:`mai.api.app.create_app` under the api-wiring lock; the exported
  OpenAPI contract is regenerated mechanically by the central exporter
  (no handwritten client types).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Annotated, Final, Literal

from fastapi import APIRouter, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings
from starlette.status import (
    HTTP_200_OK,
    HTTP_204_NO_CONTENT,
    HTTP_401_UNAUTHORIZED,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from mai.agents.model_store import (
    ModelConfigStore,
    ModelStoreError,
    ModelStoreErrorCode,
    StoredDocument,
)
from mai.agents.models import (
    ModelConfigError,
    ModelConnection,
    ModelProfile,
    ProviderFailureKind,
    build_model,
    classify_provider_error,
    parse_connection,
    parse_profile,
)
from mai.api.auth import (
    BoundaryError,
    ErrorEnvelope,
    _require_allowed_origin,
    _require_csrf,
    _require_session,
    render_error_envelope,
)
from mai.api.servers import resolve_profiles_root
from mai.credentials.env import EnvFileError

__all__ = ["PROBE_DEADLINE_SECONDS", "PROBE_MAX_OUTPUT_TOKENS", "register_models"]

# ---------------------------------------------------------------------------
# Module-owned probe bounds (transport policy; all explicitly bounded)
# ---------------------------------------------------------------------------

#: Total wall-clock deadline for one probe round. A connection may declare
#: its own ``timeout_seconds`` (producer-bounded ≤ 600 s); the probe never
#: runs longer than this even then — a hung endpoint must classify as
#: ``reachability``, not pin the request.
PROBE_DEADLINE_SECONDS: Final[float] = 20.0

#: Output-token cap forwarded on the probe request (it overrides any larger
#: profile setting for this one diagnostic round; a smaller profile cap
#: still wins). The probe is a bounded liveness check, not a generation run.
PROBE_MAX_OUTPUT_TOKENS: Final[int] = 64

#: The probe's fixed minimal task (value-free; never user content).
_PROBE_PROMPT: Final[str] = "Reply with exactly: ok"

_PROBE_INSTRUCTIONS: Final[str] = (
    "You are a connectivity probe for the MAI controller. "
    "Answer with exactly the requested marker text."
)


def _probe_ping() -> str:
    """The probe's declared tool: a local, effect-free round-trip marker.

    Declared only when the caller asks for the tool smoke (``tools: true``
    on a connection that declares ``capabilities.tool_calls``); a plain
    chat endpoint that cannot accept tool definitions classifies as
    ``rejected_request``.
    """
    return "pong"


# ---------------------------------------------------------------------------
# Wire shapes (transport only; field truth is the schema producer)
# ---------------------------------------------------------------------------


class ModelCapabilitiesBody(BaseModel):
    """Optional endpoint capabilities the connection declares (C06 rule 1).

    Everything defaults to false: an endpoint is assumed plain chat-only
    until the administrator declares more.
    """

    model_config = ConfigDict(extra="forbid")

    tool_calls: bool = False
    reasoning: bool = False


class ModelConnectionBody(BaseModel):
    """Connection upsert payload (strict: unknown fields are rejected).

    ``credential_ref`` is a *reference* (the ``.env`` name); there is
    deliberately no credential-value field anywhere on this surface. The
    record name is the path identity, never a body field.
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(min_length=1)
    credential_ref: str | None = None
    timeout_seconds: float | None = None
    headers: dict[str, str] | None = None
    capabilities: ModelCapabilitiesBody | None = None
    expected_revision: int = Field(ge=0)


class ModelProfileBody(BaseModel):
    """Model-profile upsert payload (strict; allowlisted settings only).

    Settings outside the MAI allowlist are producer rejections
    (``unknown_field``/``invalid_field`` → 422); endpoint-capability gates
    are enforced at selection time (C06 rule 1).
    """

    model_config = ConfigDict(extra="forbid")

    connection: str
    model: str = Field(min_length=1)
    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    context_limit: int | None = None
    output_limit: int | None = None
    expected_revision: int = Field(ge=0)


class ModelSelectionBody(BaseModel):
    """Selection payload: one explicit (connection, profile) pair."""

    model_config = ConfigDict(extra="forbid")

    connection: str
    profile: str
    expected_revision: int = Field(ge=0)


class ModelProbeBody(BaseModel):
    """Bounded probe request.

    Without explicit names the stored selection picks the pair (a missing
    selection is a 404, never an ambient default). ``tools`` additionally
    declares one local round-trip tool for the smoke round and requires
    the connection to declare ``capabilities.tool_calls``.
    """

    model_config = ConfigDict(extra="forbid")

    connection: str | None = None
    profile: str | None = None
    tools: bool = False


class ModelCapabilitiesView(BaseModel):
    """The endpoint's declared optional capabilities."""

    tool_calls: bool
    reasoning: bool


class ModelHeadersView(BaseModel):
    """One declared custom header (literal transport metadata only)."""

    name: str
    value: str


class ModelConnectionView(BaseModel):
    """Stored connection document view (credential *reference* only)."""

    schema_version: int
    name: str
    base_url: str
    credential_ref: str | None
    timeout_seconds: float | None
    headers: list[ModelHeadersView]
    capabilities: ModelCapabilitiesView
    revision: int
    sha256: str


class ModelProfileView(BaseModel):
    """Stored model-profile document view."""

    schema_version: int
    name: str
    connection: str
    model: str
    temperature: float | None
    top_p: float | None
    seed: int | None
    max_tokens: int | None
    reasoning_effort: str | None
    context_limit: int | None
    output_limit: int | None
    revision: int
    sha256: str


class ModelRecordRef(BaseModel):
    """Value-free record reference (identity + version triple, C02 rule 4)."""

    name: str
    revision: int
    schema_revision: int
    sha256: str


class ModelConnectionList(BaseModel):
    """One bounded page of stored connection references."""

    connections: list[ModelRecordRef]


class ModelProfileList(BaseModel):
    """One bounded page of stored profile references."""

    profiles: list[ModelRecordRef]


class ModelMutationResult(BaseModel):
    """Post-commit version triple of one successful record write."""

    name: str
    revision: int
    schema_revision: int
    sha256: str


class ModelSelectionView(BaseModel):
    """The stored selection (it names the pair, it pins no revisions)."""

    connection: str
    profile: str
    revision: int
    schema_revision: int
    sha256: str


class ModelProbeOutcome(BaseModel):
    """One classified probe outcome (C06 rule 1 vocabulary, value-free).

    ``ok`` — the bounded round (and the declared tool round-trip, when
    requested) completed against the endpoint. ``reachability`` —
    transport failure or probe deadline exceeded. ``auth`` — the endpoint
    rejected the credential (401/403). ``rejected_request`` — the endpoint
    refused the request payload (the unsupported-tool/payload class).
    ``provider``/``unknown`` — other structured/unclassified API
    failures. ``credential`` — the reference could not be resolved from
    the configured ``.env`` source (the endpoint saw no request).
    """

    connection: str
    profile: str
    model: str
    connection_revision: int
    profile_revision: int
    tools: bool
    outcome: Literal[
        "ok",
        "reachability",
        "auth",
        "rejected_request",
        "provider",
        "unknown",
        "credential",
    ]
    latency_ms: int
    detail: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Canonical document rendering (single producer re-validates on write)
# ---------------------------------------------------------------------------


def _canonical_connection_document(name: str, payload: ModelConnectionBody) -> str:
    """Render the canonical connection JSON body in frozen field order."""
    document: dict[str, object] = {
        "schema_version": 1,
        "name": name,
        "base_url": payload.base_url,
    }
    if payload.credential_ref is not None:
        document["credential_ref"] = payload.credential_ref
    if payload.timeout_seconds is not None:
        document["timeout_seconds"] = payload.timeout_seconds
    if payload.headers:
        document["headers"] = dict(payload.headers)
    if payload.capabilities is not None:
        declared = payload.capabilities.model_dump(exclude_none=True)
        if declared:
            document["capabilities"] = declared
    return json.dumps(document, sort_keys=True) + "\n"


def _canonical_profile_document(name: str, payload: ModelProfileBody) -> str:
    """Render the canonical model-profile JSON body in frozen field order."""
    document: dict[str, object] = {
        "schema_version": 1,
        "name": name,
        "connection": payload.connection,
        "model": payload.model,
    }
    for field in (
        "temperature",
        "top_p",
        "seed",
        "max_tokens",
        "reasoning_effort",
        "context_limit",
        "output_limit",
    ):
        value = getattr(payload, field)
        if value is not None:
            document[field] = value
    return json.dumps(document, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# Guards and error mapping (C01 envelope; boundary owned by mai.api.auth)
# ---------------------------------------------------------------------------


def _model_service(request: Request) -> ModelConfigStore:
    """The lifespan-built confined store; without it fail closed."""
    store = getattr(request.app.state, "models", None)
    if store is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "model configuration service is unavailable",
        )
    return store


def _resolver_of(request: Request) -> object | None:
    """The lifespan-built ``.env`` resolver (may be absent)."""
    return getattr(request.app.state, "credentials", None)


#: Store rejections that are document validation, not state (422).
_DOCUMENT_REJECTIONS: Final[frozenset[ModelStoreErrorCode]] = frozenset(
    {
        ModelStoreErrorCode.INVALID_NAME,
        ModelStoreErrorCode.INVALID_REVISION,
        ModelStoreErrorCode.NAME_MISMATCH,
        ModelStoreErrorCode.DOCUMENT_TOO_LARGE,
        ModelStoreErrorCode.DOCUMENT_TOO_DEEP,
        ModelStoreErrorCode.UNPARSABLE_DOCUMENT,
    }
)

#: Store rejections meaning canonical state needs recovery (409): symlinked
#: components, damaged/missing metadata, hash drift — surfaced, never
#: silently repaired (C02 crash windows).
_STATE_REJECTIONS: Final[frozenset[ModelStoreErrorCode]] = frozenset(
    {
        ModelStoreErrorCode.PATH_ESCAPE,
        ModelStoreErrorCode.METADATA_MISSING,
        ModelStoreErrorCode.METADATA_INVALID,
        ModelStoreErrorCode.ORPHAN_METADATA,
        ModelStoreErrorCode.HASH_MISMATCH,
    }
)


def _store_error(request: Request, exc: ModelStoreError) -> JSONResponse:
    """Map store rejections onto the C01 envelope (value-free, always)."""
    if exc.code in _DOCUMENT_REJECTIONS:
        return render_error_envelope(
            request,
            HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_failed",
            "model configuration document failed validation",
            fields={"document": exc.code.value},
        )
    if exc.code is ModelStoreErrorCode.NOT_FOUND:
        return render_error_envelope(
            request, HTTP_404_NOT_FOUND, "not_found", "no such model record"
        )
    if exc.code is ModelStoreErrorCode.STALE_REVISION:
        return render_error_envelope(
            request,
            HTTP_409_CONFLICT,
            "conflict",
            "the record changed since the last observed revision",
        )
    if exc.code is ModelStoreErrorCode.SELECTION_CONFLICT:
        return render_error_envelope(
            request,
            HTTP_409_CONFLICT,
            "conflict",
            "the stored selection names this record; re-point it first",
        )
    if exc.code in _STATE_REJECTIONS:
        return render_error_envelope(
            request,
            HTTP_409_CONFLICT,
            "conflict",
            "stored model configuration requires recovery",
        )
    raise exc  # unmapped failures take the app's generic internal_error path


def _config_error(request: Request, exc: ModelConfigError) -> JSONResponse:
    """Map producer rejections onto the C01 envelope with field paths."""
    return render_error_envelope(
        request,
        HTTP_422_UNPROCESSABLE_CONTENT,
        "validation_failed",
        "model configuration document failed validation",
        fields=exc.fields,
    )


def _connection_view(
    stored: StoredDocument, parsed: ModelConnection
) -> ModelConnectionView:
    return ModelConnectionView(
        schema_version=parsed.schema_version,
        name=parsed.name,
        base_url=parsed.base_url,
        credential_ref=parsed.credential_ref,
        timeout_seconds=parsed.timeout_seconds,
        headers=[
            ModelHeadersView(name=name, value=value)
            for name, value in parsed.headers
        ],
        capabilities=ModelCapabilitiesView(
            tool_calls=parsed.capabilities.tool_calls,
            reasoning=parsed.capabilities.reasoning,
        ),
        revision=stored.revision,
        sha256=stored.sha256,
    )


def _profile_view(
    stored: StoredDocument, parsed: ModelProfile
) -> ModelProfileView:
    return ModelProfileView(
        schema_version=parsed.schema_version,
        name=parsed.name,
        connection=parsed.connection,
        model=parsed.model,
        temperature=parsed.temperature,
        top_p=parsed.top_p,
        seed=parsed.seed,
        max_tokens=parsed.max_tokens,
        reasoning_effort=parsed.reasoning_effort,
        context_limit=parsed.context_limit,
        output_limit=parsed.output_limit,
        revision=stored.revision,
        sha256=stored.sha256,
    )


# ---------------------------------------------------------------------------
# Probe execution (bounded; producer-owned classification)
# ---------------------------------------------------------------------------


def _probe_detail(error: BaseException) -> dict[str, str]:
    """Value-free classification aids only: class names and status codes.

    Provider response bodies, credential material and document content are
    never echoed (C01 rule 3).
    """
    detail: dict[str, str] = {"error_type": type(error).__name__}
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        detail["status_code"] = str(status)
    return detail


async def _run_probe(
    store: ModelConfigStore,
    resolver: object | None,
    *,
    connection: str | None,
    profile: str | None,
    tools: bool,
) -> ModelProbeOutcome:
    """One bounded, classified probe round (acceptance 1).

    The binding is validated first (pairing + capability gating, C06
    rule 1), credentials resolve at call time through the configured
    ``.env`` source (C03 rule 6), and the round runs under a hard
    deadline. Every failure classifies; nothing raises past this point.
    """
    binding = store.bind(connection, profile)
    assert binding.connection.name and binding.profile.name
    try:
        resolved = build_model(
            binding.selection(),
            resolver=resolver,  # type: ignore[arg-type]
        )
    except ModelConfigError as error:
        # missing_resolver: a credential reference without a configured
        # source — the endpoint never saw anything (local config class).
        code = error.errors[0].code.value if error.errors else "invalid"
        return ModelProbeOutcome(
            connection=binding.connection.name,
            profile=binding.profile.name,
            model=binding.profile.model,
            connection_revision=binding.connection_revision,
            profile_revision=binding.profile_revision,
            tools=tools,
            outcome="credential",
            latency_ms=0,
            detail={"code": code},
        )
    except EnvFileError as error:
        # The configured source rejected the resolution (missing key,
        # unusable file): local, value-free, never credential material.
        return ModelProbeOutcome(
            connection=binding.connection.name,
            profile=binding.profile.name,
            model=binding.profile.model,
            connection_revision=binding.connection_revision,
            profile_revision=binding.profile_revision,
            tools=tools,
            outcome="credential",
            latency_ms=0,
            detail={"code": error.code.value},
        )

    settings: ModelSettings = {"max_tokens": PROBE_MAX_OUTPUT_TOKENS}
    agent: Agent[None, str] = Agent(
        resolved.model,
        instructions=_PROBE_INSTRUCTIONS,
        tools=[_probe_ping] if tools else [],
        output_type=str,
    )
    started = time.perf_counter()
    try:
        async with asyncio.timeout(PROBE_DEADLINE_SECONDS):
            await agent.run(_PROBE_PROMPT, model_settings=settings)
    except TimeoutError:
        return ModelProbeOutcome(
            connection=binding.connection.name,
            profile=binding.profile.name,
            model=binding.profile.model,
            connection_revision=binding.connection_revision,
            profile_revision=binding.profile_revision,
            tools=tools,
            outcome="reachability",
            latency_ms=int((time.perf_counter() - started) * 1000),
            detail={"reason": "probe_deadline_exceeded"},
        )
    except Exception as error:  # noqa: BLE001 - classified below, never raised
        kind = classify_provider_error(error)
        return ModelProbeOutcome(
            connection=binding.connection.name,
            profile=binding.profile.name,
            model=binding.profile.model,
            connection_revision=binding.connection_revision,
            profile_revision=binding.profile_revision,
            tools=tools,
            outcome=kind.value,  # type: ignore[assignment]
            latency_ms=int((time.perf_counter() - started) * 1000),
            detail=_probe_detail(error),
        )
    return ModelProbeOutcome(
        connection=binding.connection.name,
        profile=binding.profile.name,
        model=binding.profile.model,
        connection_revision=binding.connection_revision,
        profile_revision=binding.profile_revision,
        tools=tools,
        outcome="ok",
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


# ---------------------------------------------------------------------------
# Routes (C01 prefix/envelope, C08 boundary, C02 revision discipline)
# ---------------------------------------------------------------------------

#: Record-name grammar is producer-frozen; the store classifies violations
#: (``invalid_name`` → 422). Revision query bounds: non-negative int (C02).
RevisionQuery = Annotated[int, Query(ge=0)]

router = APIRouter(prefix="/api/v1/models", tags=["models"])


# -- connections -----------------------------------------------------------


@router.get(
    "/connections",
    status_code=HTTP_200_OK,
    response_model=ModelConnectionList,
    summary="List stored model connections (value-free references)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
    },
)
async def list_connections(request: Request) -> Response:
    """Stored connection names with their version triples; no content."""
    await _require_session(request)
    store = _model_service(request)
    refs = [
        ModelRecordRef(
            name=name,
            revision=stored.revision,
            schema_revision=stored.schema_revision,
            sha256=stored.sha256,
        )
        for name in store.list_connections()
        if (stored := _read_or_none(store.read_connection, name)) is not None
    ]
    return ModelConnectionList(connections=refs)


@router.put(
    "/connections/{name}",
    status_code=HTTP_200_OK,
    response_model=ModelMutationResult,
    summary="Create or replace a connection (optimistic revision)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Stale expected_revision",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Connection document failed producer validation",
        },
    },
)
async def put_connection(
    request: Request, name: str, payload: ModelConnectionBody
) -> Response:
    """Validate through the producer, then atomically store (C02 rule 5).

    ``expected_revision: 0`` creates; any other value must match the
    caller's last observed revision (C01 rule 6).
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    store = _model_service(request)
    try:
        stored = store.write_connection(
            name,
            _canonical_connection_document(name, payload),
            expected_revision=payload.expected_revision,
        )
    except ModelConfigError as exc:  # producer validation, pre-filesystem
        return _config_error(request, exc)
    except ModelStoreError as exc:
        return _store_error(request, exc)
    return ModelMutationResult(
        name=stored.name,
        revision=stored.revision,
        schema_revision=stored.schema_revision,
        sha256=stored.sha256,
    )


@router.get(
    "/connections/{name}",
    status_code=HTTP_200_OK,
    response_model=ModelConnectionView,
    summary="Read one stored connection (credential reference only)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No connection stored under this name",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Stored document requires recovery",
        },
    },
)
async def get_connection(request: Request, name: str) -> Response:
    """The stored document view; ``credential_ref`` is a name, never a value."""
    await _require_session(request)
    store = _model_service(request)
    try:
        stored = store.read_connection(name)
        parsed = parse_connection(json.loads(stored.text))
    except ModelStoreError as exc:
        return _store_error(request, exc)
    except ModelConfigError as exc:
        # Only reachable through out-of-band file edits (writes are
        # producer-validated): stored state needs recovery, fail closed.
        return _config_error(request, exc)
    return _connection_view(stored, parsed)


@router.delete(
    "/connections/{name}",
    status_code=HTTP_204_NO_CONTENT,
    summary="Delete a connection (revision-checked)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No connection stored under this name",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Stale revision or the selection names this record",
        },
    },
)
async def delete_connection(
    request: Request, name: str, expected_revision: RevisionQuery
) -> Response:
    """Revision-checked removal; a selection-named record is refused."""
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    store = _model_service(request)
    try:
        store.delete_connection(name, expected_revision=expected_revision)
    except ModelStoreError as exc:
        return _store_error(request, exc)
    return Response(status_code=HTTP_204_NO_CONTENT)


# -- profiles --------------------------------------------------------------


@router.get(
    "/profiles",
    status_code=HTTP_200_OK,
    response_model=ModelProfileList,
    summary="List stored model profiles (value-free references)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
    },
)
async def list_profiles(request: Request) -> Response:
    """Stored profile names with their version triples; no content."""
    await _require_session(request)
    store = _model_service(request)
    refs = [
        ModelRecordRef(
            name=name,
            revision=stored.revision,
            schema_revision=stored.schema_revision,
            sha256=stored.sha256,
        )
        for name in store.list_profiles()
        if (stored := _read_or_none(store.read_profile, name)) is not None
    ]
    return ModelProfileList(profiles=refs)


@router.put(
    "/profiles/{name}",
    status_code=HTTP_200_OK,
    response_model=ModelMutationResult,
    summary="Create or replace a model profile (optimistic revision)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Stale expected_revision",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Profile document failed producer validation",
        },
    },
)
async def put_profile(
    request: Request, name: str, payload: ModelProfileBody
) -> Response:
    """Validate through the producer, then atomically store (C02 rule 5)."""
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    store = _model_service(request)
    try:
        stored = store.write_profile(
            name,
            _canonical_profile_document(name, payload),
            expected_revision=payload.expected_revision,
        )
    except ModelConfigError as exc:  # producer validation, pre-filesystem
        return _config_error(request, exc)
    except ModelStoreError as exc:
        return _store_error(request, exc)
    return ModelMutationResult(
        name=stored.name,
        revision=stored.revision,
        schema_revision=stored.schema_revision,
        sha256=stored.sha256,
    )


@router.get(
    "/profiles/{name}",
    status_code=HTTP_200_OK,
    response_model=ModelProfileView,
    summary="Read one stored model profile",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No profile stored under this name",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Stored document requires recovery",
        },
    },
)
async def get_profile(request: Request, name: str) -> Response:
    """The stored document view (allowlisted settings only, by construction)."""
    await _require_session(request)
    store = _model_service(request)
    try:
        stored = store.read_profile(name)
        parsed = parse_profile(json.loads(stored.text))
    except ModelStoreError as exc:
        return _store_error(request, exc)
    except ModelConfigError as exc:  # out-of-band edits only; fail closed
        return _config_error(request, exc)
    return _profile_view(stored, parsed)


@router.delete(
    "/profiles/{name}",
    status_code=HTTP_204_NO_CONTENT,
    summary="Delete a model profile (revision-checked)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No profile stored under this name",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Stale revision or the selection names this record",
        },
    },
)
async def delete_profile(
    request: Request, name: str, expected_revision: RevisionQuery
) -> Response:
    """Revision-checked removal; a selection-named record is refused."""
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    store = _model_service(request)
    try:
        store.delete_profile(name, expected_revision=expected_revision)
    except ModelStoreError as exc:
        return _store_error(request, exc)
    return Response(status_code=HTTP_204_NO_CONTENT)


# -- selection ---------------------------------------------------------------


@router.get(
    "/selection",
    status_code=HTTP_200_OK,
    response_model=ModelSelectionView,
    summary="Read the stored (connection, profile) selection",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No selection stored",
        },
    },
)
async def get_selection(request: Request) -> Response:
    """The selected pair; it names the records, it pins no revisions."""
    await _require_session(request)
    store = _model_service(request)
    try:
        selected = store.get_selection()
    except ModelStoreError as exc:
        return _store_error(request, exc)
    return ModelSelectionView(
        connection=selected.connection,
        profile=selected.profile,
        revision=selected.revision,
        schema_revision=selected.schema_revision,
        sha256=selected.sha256,
    )


@router.put(
    "/selection",
    status_code=HTTP_200_OK,
    response_model=ModelSelectionView,
    summary="Select one (connection, profile) pair (validated, optimistic)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "A named record is not stored",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Stale expected_revision",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "The pair fails producer validation (pairing or "
            "endpoint capability gating)",
        },
    },
)
async def put_selection(request: Request, payload: ModelSelectionBody) -> Response:
    """Both records must be stored and pair-valid (C06 rule 1).

    Capability gating is deliberate: e.g. ``reasoning_effort`` on a
    connection that does not declare ``capabilities.reasoning`` is a
    classified 422 (``unsupported_option``), never a silent drop.
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    store = _model_service(request)
    try:
        selected = store.set_selection(
            payload.connection,
            payload.profile,
            expected_revision=payload.expected_revision,
        )
    except ModelStoreError as exc:
        return _store_error(request, exc)
    except ModelConfigError as exc:
        return _config_error(request, exc)
    return ModelSelectionView(
        connection=selected.connection,
        profile=selected.profile,
        revision=selected.revision,
        schema_revision=selected.schema_revision,
        sha256=selected.sha256,
    )


@router.delete(
    "/selection",
    status_code=HTTP_204_NO_CONTENT,
    summary="Clear the stored selection (revision-checked)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No selection stored",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Stale expected_revision",
        },
    },
)
async def delete_selection(
    request: Request, expected_revision: RevisionQuery
) -> Response:
    """Revision-checked removal of the selection record."""
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    store = _model_service(request)
    try:
        store.clear_selection(expected_revision=expected_revision)
    except ModelStoreError as exc:
        return _store_error(request, exc)
    return Response(status_code=HTTP_204_NO_CONTENT)


# -- bounded probe -----------------------------------------------------------


@router.post(
    "/test",
    status_code=HTTP_200_OK,
    response_model=ModelProbeOutcome,
    summary="Run one bounded classified probe round",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "The named records are not stored / no selection",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "The pair fails producer validation, or tools "
            "were requested from a connection that does not declare "
            "tool_calls",
        },
    },
)
async def test_models(request: Request, payload: ModelProbeBody) -> Response:
    """One bounded model round with a classified, value-free outcome.

    Distinct outcomes (acceptance 1): ``reachability`` (transport/deadline),
    ``auth`` (credential rejected by the endpoint), ``rejected_request``
    (the endpoint refused the payload — the tool-smoke rejection class),
    plus ``provider``/``unknown`` (other API failures), ``credential``
    (local ``.env`` resolution failed; the endpoint saw nothing) and
    ``ok``. Credential material is resolved at call time (C03 rule 6) and
    never appears in any response.
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    store = _model_service(request)
    if payload.tools:
        # Deliberate capability gate before any network effect (C06 rule 1):
        # the tool smoke requires the connection to declare tool_calls, the
        # same unsupported_option class the producer uses for settings.
        try:
            binding = store.bind(payload.connection, payload.profile)
        except ModelStoreError as exc:
            return _store_error(request, exc)
        except ModelConfigError as exc:
            return _config_error(request, exc)
        if not binding.connection.capabilities.tool_calls:
            return render_error_envelope(
                request,
                HTTP_422_UNPROCESSABLE_CONTENT,
                "validation_failed",
                "model configuration document failed validation",
                fields={"tools": "unsupported_option"},
            )
    try:
        return await _run_probe(
            store,
            _resolver_of(request),
            connection=payload.connection,
            profile=payload.profile,
            tools=payload.tools,
        )
    except ModelStoreError as exc:
        return _store_error(request, exc)
    except ModelConfigError as exc:
        return _config_error(request, exc)


# ---------------------------------------------------------------------------
# Registration (api-wiring exclusive resource)
# ---------------------------------------------------------------------------


def register_models(app: FastAPI) -> None:
    """Register the model surface under the shared boundary (api-wiring)."""
    app.include_router(router)


def _read_or_none(read, name: str) -> StoredDocument | None:
    """Tolerate concurrent deletions during list rendering (404-free list).

    A list is a bounded, eventually-consistent page: a record deleted
    between ``list_*`` and its read is simply absent from the response.
    """
    with contextlib.suppress(ModelStoreError):
        return read(name)
    return None
