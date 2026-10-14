"""Capability-grant configuration HTTP API: versioned reads and edits.

Card ``03-grants-api`` against contracts.md **C03 rules 3–5** (grant
semantics, approval binding, ask/delegated modes), **C01** (envelope,
opaque identity, revision conflicts), **C02** (canonical service over the
explicit data root, revision triple) and **C08 rule 2** (session → origin
→ CSRF on every mutation). The storage semantics underneath are owned by
:mod:`mai.policy.config` (``03-policy-config``); this surface joins them
to the authenticated API the way ``mai.api.servers`` joins the profile
store.

What the administrator edits here is exactly the frozen grant
configuration vocabulary: the policy deny list (disabled tools) and the
explicit capability → server-UUID grants (enabled tools, no wildcards).
The ask/delegated prompting modes (C03 rule 5) are deliberately **not**
part of this document: a mode is a property of each
:class:`~mai.policy.evaluate.ExecutionContext` (C03 rule 2), chosen by
the host per run, and the frozen configuration vocabulary cannot store
one. The safety property this surface must never break is enforced by
the reviewed producers, not asserted here: a mode never widens any check
(:func:`mai.policy.evaluate.evaluate` runs the grant/scope/policy/adoption
checks identically in both modes), and the store keeps no cache, so every
consumer — a dispatcher gate or the immediate recheck before a later
effect — re-reads the configuration this route committed and a revocation
takes effect on the next decision.

Routes (C01 prefix/envelope; C02 rule-4 revision triple; C08 boundary):

- ``GET /api/v1/grants`` — the current committed configuration, or
  ``404 not_found`` before the first commit (shown, never faked as an
  empty default, C08 rule 6). Safe method: session layer only.
- ``PUT /api/v1/grants`` — one versioned edit: the body carries the
  caller's last observed ``expected_revision`` plus the new deny list and
  grants; the route derives ``policy.revision = expected_revision + 1``
  (one counter — the file revision IS the policy revision, C03 rule 4)
  and calls the canonical service. A client can never nominate a
  revision: stale callers get ``409 conflict`` before any filesystem
  effect, and unknown body fields are never passengers into the
  canonical document.

Write serialization: the store's write path is synchronous (no await
between the revision check and the atomic replace), so concurrent
requests in one process serialize on the event loop and the
``expected_revision`` guard rejects every superseded caller; the
cross-process single-writer-per-data-root invariant remains the store
card's caller contract (C05 rule 6), recorded as an inherited limitation
of the single-uvicorn-process deployment.

Value discipline: error bodies carry stable value-free codes only; the
view reports opaque server UUIDs and capability slugs — never prompt
text, credential material or free-form annotations (C02 rule 6: the only
route into the grants namespace is the frozen schema).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.status import (
    HTTP_200_OK,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
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
from mai.policy.config import (
    GrantConfigError,
    GrantConfigErrorCode,
    GrantConfigStore,
    StoredGrantConfig,
)

__all__ = ["register_grants"]

#: Capability identifier shape — the registry/policy slug charset (C03
#: rule 1), enforced at transport exactly as :mod:`mai.api.approvals`
#: does; the store re-validates authoritatively on every write.
_SLUG_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9._-]{0,127}$"

CapabilitySlug = Annotated[str, Field(pattern=_SLUG_PATTERN)]


# ---------------------------------------------------------------------------
# Wire shapes (transport only; document truth is the canonical service)
# ---------------------------------------------------------------------------


class PolicyState(BaseModel):
    """The committed policy half: one revision counter plus the deny list."""

    revision: int
    denied_capabilities: list[str]


class GrantEntry(BaseModel):
    """One explicit grant: capability slug → server UUIDs (no wildcards)."""

    capability_id: str
    server_ids: list[uuid.UUID]


class GrantsConfigView(BaseModel):
    """The committed configuration with its C02 rule-4 version triple."""

    revision: int
    schema_revision: int
    sha256: str
    policy: PolicyState
    grants: list[GrantEntry]


class GrantInput(BaseModel):
    """One grant in an edit body; scope members are opaque UUIDs (C01)."""

    capability_id: CapabilitySlug
    server_ids: list[uuid.UUID]


class GrantsEditBody(BaseModel):
    """One versioned edit: caller's revision + the new policy and grants.

    Deliberately carries **no** revision field for the document itself:
    the next revision is ``expected_revision + 1``, derived by the route
    (C03 rule 4 — approvals bind the policy revision, so callers never
    choose it).
    """

    expected_revision: int = Field(ge=0)
    denied_capabilities: list[CapabilitySlug] = Field(default_factory=list)
    grants: list[GrantInput] = Field(default_factory=list)


class GrantsMutationResult(BaseModel):
    """Post-commit version triple of one successful write (C02 rule 4)."""

    revision: int
    schema_revision: int
    sha256: str


#: Document-validation rejections (the producer refused the content —
#: the transport bounds already passed, so these fire only on semantic
#: schema violations such as duplicate grant entries or oversized
#: documents). Identity/revision misuse cannot occur through the typed
#: paths but is classified anyway (servers-router pattern).
_DOCUMENT_REJECTIONS: Final[frozenset[GrantConfigErrorCode]] = frozenset(
    {
        GrantConfigErrorCode.POLICY_INVALID,
        GrantConfigErrorCode.GRANTS_INVALID,
        GrantConfigErrorCode.DOCUMENT_SHAPE_INVALID,
        GrantConfigErrorCode.DOCUMENT_TOO_LARGE,
        GrantConfigErrorCode.DOCUMENT_TOO_DEEP,
        GrantConfigErrorCode.DOCUMENT_NOT_JSON,
        GrantConfigErrorCode.INVALID_REVISION,
    }
)

#: Canonical-state rejections needing recovery — surfaced, never silently
#: repaired (C02 rule 5; the store's hash/revision pairing guards them).
_STATE_REJECTIONS: Final[frozenset[GrantConfigErrorCode]] = frozenset(
    {
        GrantConfigErrorCode.REVISION_CONFLICT,
        GrantConfigErrorCode.PATH_ESCAPE,
        GrantConfigErrorCode.ORPHAN_METADATA,
        GrantConfigErrorCode.METADATA_MISSING,
        GrantConfigErrorCode.METADATA_INVALID,
        GrantConfigErrorCode.HASH_MISMATCH,
    }
)


# ---------------------------------------------------------------------------
# Canonical document assembly (single producer re-validates on write)
# ---------------------------------------------------------------------------


def _canonical_document(payload: GrantsEditBody) -> dict[str, object]:
    """Assemble the frozen-shape document with the derived next revision.

    Only the typed fields travel: unknown body fields are structurally
    excluded (pydantic model surface), so prose or annotation fields can
    never enter the canonical namespace (C02 rule 6 / C07 rule 5).
    """
    return {
        "policy": {
            "revision": payload.expected_revision + 1,
            "denied_capabilities": list(payload.denied_capabilities),
        },
        "grants": [
            {
                "capability_id": grant.capability_id,
                "server_ids": [str(server_id) for server_id in grant.server_ids],
            }
            for grant in payload.grants
        ],
    }


def _view(stored: StoredGrantConfig) -> GrantsConfigView:
    """Materialize the wire view from the store's frozen dataclass."""
    return GrantsConfigView(
        revision=stored.revision,
        schema_revision=stored.schema_revision,
        sha256=stored.sha256,
        policy=PolicyState(
            revision=stored.policy.revision,
            denied_capabilities=sorted(stored.policy.denied_capabilities),
        ),
        grants=[
            GrantEntry(
                capability_id=capability_id,
                server_ids=sorted(stored.grants[capability_id], key=str),
            )
            for capability_id in sorted(stored.grants)
        ],
    )


# ---------------------------------------------------------------------------
# Guards and error mapping (C01 envelope; boundary owned by mai.api.auth)
# ---------------------------------------------------------------------------


def _policy_service(request: Request) -> GrantConfigStore:
    """The lifespan-built store; without it fail closed, touch nothing."""
    store = getattr(request.app.state, "grants", None)
    if store is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "grant configuration service is unavailable",
        )
    return store


def _grants_error(request: Request, exc: GrantConfigError) -> JSONResponse:
    """Map producer/store rejections onto the C01 envelope.

    Exhaustive over :class:`GrantConfigErrorCode`: validation rejections
    carry the producer's stable code in ``fields``; state conflicts are
    value-free ``409`` bodies; anything unmapped would take the app's
    generic ``internal_error`` path rather than masquerade as a denial.
    """
    if exc.code is GrantConfigErrorCode.NOT_FOUND:
        return render_error_envelope(
            request,
            HTTP_404_NOT_FOUND,
            "not_found",
            "no grant configuration stored below this data root",
        )
    if exc.code is GrantConfigErrorCode.STALE_REVISION:
        return render_error_envelope(
            request,
            HTTP_409_CONFLICT,
            "conflict",
            "the grant configuration changed since the last observed revision",
        )
    if exc.code in _DOCUMENT_REJECTIONS:
        return render_error_envelope(
            request,
            HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_failed",
            "grant configuration failed validation",
            fields={"document": exc.code.value},
        )
    if exc.code in _STATE_REJECTIONS:
        return render_error_envelope(
            request,
            HTTP_409_CONFLICT,
            "conflict",
            "canonical policy state requires recovery",
        )
    raise exc  # pragma: no cover - the mapping above is exhaustive


# ---------------------------------------------------------------------------
# Routes (C01 prefix/envelope/identity; C02/C03 canonical service; C08)
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/grants", tags=["grants"])


@router.get(
    "",
    status_code=HTTP_200_OK,
    response_model=GrantsConfigView,
    summary="Read the committed capability-grant configuration",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No configuration committed yet",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Grant configuration service unavailable",
        },
    },
)
async def read_grants(request: Request) -> Response:
    """Return the current configuration: policy deny list plus grants.

    Safe method: layer 1 only. The response is the committed truth —
    the revision the next edit must name as ``expected_revision``.
    """
    await _require_session(request)
    store = _policy_service(request)
    try:
        stored = store.read()
    except GrantConfigError as exc:
        return _grants_error(request, exc)
    return _view(stored)


@router.put(
    "",
    status_code=HTTP_200_OK,
    response_model=GrantsMutationResult,
    summary="Commit one versioned grant-configuration edit",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowed or CSRF token rejected",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Stale expected revision or canonical state "
            "requires recovery",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Configuration document failed producer validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Grant configuration service unavailable",
        },
    },
)
async def edit_grants(request: Request, payload: GrantsEditBody) -> Response:
    """Validate and atomically commit one configuration revision.

    Full boundary order (session → origin → CSRF) runs before any state
    is touched. The route derives the next revision from the caller's
    ``expected_revision`` (C03 rule 4), so a stale caller is rejected
    with ``409 conflict`` before any filesystem effect and can never
    clobber a concurrent edit.
    """
    await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    store = _policy_service(request)
    try:
        stored = store.write(
            _canonical_document(payload),
            expected_revision=payload.expected_revision,
        )
    except GrantConfigError as exc:
        return _grants_error(request, exc)
    return GrantsMutationResult(
        revision=stored.revision,
        schema_revision=stored.schema_revision,
        sha256=stored.sha256,
    )


def register_grants(app: FastAPI) -> None:
    """Register the grant-configuration surface under the shared boundary
    (api-wiring)."""
    app.include_router(router)
