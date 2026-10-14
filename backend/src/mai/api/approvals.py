"""Approval decision HTTP API: safe preview + one-use admin decision.

Ownership map (contracts.md **C03 rule 4** human gate over the reviewed
``03-approval-store`` producer; **C01** envelope/identity; **C08 rule 2**
boundary; SERVICES ``mai-api`` row):

- ``GET /api/v1/approvals/{approval_id}`` — the safe preview an admin needs
  before deciding: what the decision would authorize (``capability_id`` x
  ``server_id`` target), the exact binding it is frozen to (canonical
  payload SHA-256 + policy revision), who requested it, its expiry and its
  current one-use state. Safe method: the session layer only, mirroring the
  other safe reads under the shared boundary. Payload bytes cannot leak
  through this view: the store layer never accepts them — only the digest
  exists anywhere.
- ``POST /api/v1/approvals/{approval_id}/decision`` — the human decision.
  The admin echoes the previewed binding field-for-field and the store
  spends the decision in one conditional UPDATE (C03 rule 4): exactly one
  consumption ever succeeds, and every other presentation is a stable-code
  rejection that leaves the row untouched. The full boundary layer order
  (session → origin → CSRF, ADR §5) runs before any state is touched, so a
  session/CSRF/origin failure can never consume a decision — a rejected
  call is never an implicit approval.
- The consuming principal is the validated session's server-side identity
  (:func:`mai.auth.sessions.hash_token` of the presenting cookie — the
  exact value ADR §4 persists in ``admin_sessions``): the principal comes
  from the session only (C01 rule 2), never from any header, query or body
  field, and the audit row answers *which session* spent the decision
  while the token preimage never leaves the HttpOnly cookie.
- Every error renders the C01 envelope; denial codes are the approval
  store's stable vocabulary (``not_found``, ``already_consumed``,
  ``expired``, ``capability_mismatch``, ``target_mismatch``,
  ``payload_mismatch``, ``revision_mismatch``) so this surface and the
  dispatcher report one denial language end-to-end. An unmapped producer
  code must surface as the generic ``internal_error`` (fail-closed), never
  as a guessed denial.

Route/doc listeners stay off (G1 minimal unauthenticated surface); the
schema is produced in-process via ``app.openapi()`` for the central
exporter owned by the ``openapi-client`` resource.
"""

from __future__ import annotations

import uuid
from typing import Final

from fastapi import APIRouter, FastAPI, Request
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
)
from mai.auth.sessions import hash_token
from mai.policy.approvals import (
    Approval,
    ApprovalError,
    ApprovalErrorCode,
    ApprovalStore,
)

__all__ = ["register_approvals"]


# ---------------------------------------------------------------------------
# Wire shapes (transport only; decision truth is the approval store)
# ---------------------------------------------------------------------------


class ApprovalView(BaseModel):
    """One persisted decision: the safe preview and the audit receipt.

    ``status`` is persisted-state only (``pending``/``consumed``) — the
    store derives it without reading a clock, so the view is replayable
    audit material. ``payload_hash`` is the binding digest, never payload
    bytes.
    """

    approval_id: uuid.UUID
    capability_id: str
    server_id: uuid.UUID
    payload_hash: str
    policy_revision: int
    status: str
    created_by: str
    created_at: str
    expires_at: str
    consumed_by: str | None
    consumed_at: str | None


class DecisionBody(BaseModel):
    """The binding the admin confirms, field-for-field (C03 rule 4).

    Transport bounds mirror the store's validators; the store re-validates
    authoritatively. The body carries no decision semantics of its own — a
    session/CSRF failure rejects the whole request before any store call.
    """

    capability_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    server_id: uuid.UUID
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_revision: int = Field(ge=1)


#: Stable denial mapping, store code → (HTTP status, envelope code,
#: value-free message). Exhaustive over :class:`ApprovalErrorCode`: a
#: producer code missing here must crash the mapping into the app's
#: generic ``internal_error`` path, never masquerade as a denial.
_DECISION_REJECTIONS: Final[dict[ApprovalErrorCode, tuple[int, str, str]]] = {
    ApprovalErrorCode.NOT_FOUND: (
        HTTP_404_NOT_FOUND,
        "not_found",
        "no approval record for this id",
    ),
    ApprovalErrorCode.ALREADY_CONSUMED: (
        HTTP_409_CONFLICT,
        "already_consumed",
        "decision already consumed (one-use)",
    ),
    ApprovalErrorCode.EXPIRED: (
        HTTP_409_CONFLICT,
        "expired",
        "approval expired before the decision",
    ),
    ApprovalErrorCode.CAPABILITY_MISMATCH: (
        HTTP_409_CONFLICT,
        "capability_mismatch",
        "presented capability does not match the approval binding",
    ),
    ApprovalErrorCode.TARGET_MISMATCH: (
        HTTP_409_CONFLICT,
        "target_mismatch",
        "presented target does not match the approval binding",
    ),
    ApprovalErrorCode.PAYLOAD_MISMATCH: (
        HTTP_409_CONFLICT,
        "payload_mismatch",
        "presented payload hash does not match the approval binding "
        "(an altered operation requires a new approval)",
    ),
    ApprovalErrorCode.REVISION_MISMATCH: (
        HTTP_409_CONFLICT,
        "revision_mismatch",
        "presented policy revision does not match the approval binding "
        "(grant or policy changed since the decision was requested)",
    ),
    ApprovalErrorCode.INVALID_APPROVAL: (
        HTTP_422_UNPROCESSABLE_CONTENT,
        "validation_failed",
        "decision body failed validation",
    ),
}


def _view(record: Approval) -> ApprovalView:
    """Materialize the wire view from the store's frozen dataclass.

    Timestamps cross the wire as explicit UTC ISO-8601 strings (the store's
    ``to_json`` convention) so the audit shape is identical on every
    consumer.
    """
    return ApprovalView(
        approval_id=record.approval_id,
        capability_id=record.capability_id,
        server_id=record.server_id,
        payload_hash=record.payload_hash,
        policy_revision=record.policy_revision,
        status=record.status,
        created_by=record.created_by,
        created_at=record.created_at.isoformat(),
        expires_at=record.expires_at.isoformat(),
        consumed_by=record.consumed_by,
        consumed_at=None
        if record.consumed_at is None
        else record.consumed_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Guards and error mapping (C01 envelope; boundary owned by mai.api.auth)
# ---------------------------------------------------------------------------


def _store(request: Request) -> ApprovalStore:
    """The lifespan-built store; without it fail closed, touch nothing."""
    store = getattr(request.app.state, "approvals", None)
    if store is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "approval service is unavailable",
        )
    return store


# ---------------------------------------------------------------------------
# Routes (C01 prefix/envelope; C03 rule 4 decision; C08 boundary)
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])


@router.get(
    "/{approval_id}",
    status_code=HTTP_200_OK,
    response_model=ApprovalView,
    summary="Inspect one approval decision (safe preview)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No approval record for this id",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Approval service unavailable",
        },
    },
)
async def read_approval(request: Request, approval_id: uuid.UUID) -> ApprovalView:
    """Return the persisted audit view of one decision.

    Safe method: layer 1 only (session), mirroring the other safe reads.
    The view is what the admin confirms on the decision call — target,
    authorized capability, payload digest, policy revision, expiry and the
    current one-use state.
    """
    await _require_session(request)  # layer 1
    store = _store(request)
    record = await store.get(approval_id)
    if record is None:
        raise BoundaryError(
            HTTP_404_NOT_FOUND, "not_found", "no approval record for this id"
        )
    return _view(record)


@router.post(
    "/{approval_id}/decision",
    status_code=HTTP_200_OK,
    response_model=ApprovalView,
    summary="Record the admin decision on a pending approval (one-use)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowed or CSRF token rejected",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No approval record for this id",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Decision already consumed, expired, or the "
            "presented binding mismatches the approval",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Decision body failed validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Approval service unavailable",
        },
    },
)
async def decide_approval(
    request: Request, approval_id: uuid.UUID, payload: DecisionBody
) -> ApprovalView:
    """Spend the one-use decision atomically; return the audit receipt.

    Boundary first (ADR §5 order): session → origin → CSRF before any
    state is touched, so an authentication failure can never consume a
    decision. The store's conditional UPDATE is the authoritative gate —
    duplicate, expired and mismatched presentations are stable-code
    rejections that leave the row exactly as it was.
    """
    _, token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    store = _store(request)
    try:
        record = await store.consume(
            approval_id,
            capability_id=payload.capability_id,
            server_id=payload.server_id,
            payload_hash=payload.payload_hash,
            policy_revision=payload.policy_revision,
            consumed_by=hash_token(token),
        )
    except ApprovalError as exc:
        status, code, message = _DECISION_REJECTIONS[exc.code]
        raise BoundaryError(status, code, message) from None
    return _view(record)


def register_approvals(app: FastAPI) -> None:
    """Register the approval surface under the shared boundary (api-wiring)."""
    app.include_router(router)
