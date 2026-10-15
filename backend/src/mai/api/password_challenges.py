"""Password challenge submission HTTP API: the ephemeral secret transport.

Card ``04-password-api`` against contracts.md **C03 rule 4** (the human
decision and its bound one-use consumption), **C04 rule 2** (credentials
are never concatenated into shell or argv — the secret travels as one
``sudo -S`` stdin line through the reviewed ``04-password-channel``
transport), **C01** (envelope, opaque identity, principal from the
session only) and **C08 rule 2** (session → origin → CSRF on every
mutation); SERVICES ``mai-api`` row.

- ``POST /api/v1/password-challenges`` — submit the operator's sudo
  secret for ONE approved challenge. The presentation echoes the pinned
  challenge context field-for-field: the approval binding
  (``approval_id``/``capability_id``/``server_id``/``payload_hash``/
  ``policy_revision``), the bound operation (the exact transported
  command request — the channel recomputes its payload hash and refuses
  any alteration before the decision is spent), the enrolled transport
  identity and the secret itself. The body deliberately carries NO
  transport paths: both are resolved SERVER-SIDE only (see below), and
  unknown fields are rejected, never ignored — there is deliberately NO
  read endpoint either: a password challenge is submit-only; nothing
  here can retrieve secret material.
- Every denial is a stable-code rejection that leaves the approval row
  exactly as it was (unknown id, replay, expiry, capability/target/
  payload/revision mismatch — the store's frozen vocabulary, shared
  verbatim with the approval decision surface) or a channel refusal
  (``account_mismatch``/``payload_mismatch`` before consumption;
  ``password_rejected``/``account_locked`` after it — the decision is
  spent, a retry requires a fresh approval).
- **Target binding beyond the store** (acceptance 2 "wrong target"):
  before anything is consumed, the presented transport identity must
  equal the enrolled profile's ``ssh`` identity for the bound
  ``server_id`` (C04 rule 1: the target profile determines endpoint and
  user; the enrolled known-hosts file enforces the host key at transport
  time). A compromised presentation cannot redirect the SSH channel to
  another endpoint while the decision is spent.
- **Transport paths never travel the wire** (path-injection refusal;
  C03 rule 6 + C04 rule 1): a client-named ``key_path`` could aim
  ``ssh -i`` at an arbitrary controller file and a client-named
  ``known_hosts_path`` could point the host-key check at a file the
  client can plant — so the wire carries NO transport path at all and
  unknown body fields are rejected (``extra="forbid"``, value-free 422
  before anything is touched). Both paths resolve here, server-side
  only: the identity key from the profile's ``credential_ref`` through
  the one configured ``.env`` source (:func:`mai.execution.ssh.resolve_identity_key`
  over the lifespan-built :class:`mai.credentials.env.EnvResolver`,
  ``MAI_ENV_FILE``), and the host-key store as the canonical enrolled
  file ``<data_root>/servers/<server_id>/known_hosts`` (written only by
  the explicit enrollment flow; an unenrolled/rotated endpoint refuses
  pre-consume in the channel). Resolution runs before the one-use gate,
  so every resolution failure consumes nothing.
- **Principal binding** (C01 rule 2): the consuming principal is the
  validated session's server-side identity
  (:func:`mai.auth.sessions.hash_token` of the presenting cookie). No
  header, query or body field can supply or alter it; the audit row
  answers which session spent the decision.
- **Secret hygiene** (acceptance 1): the secret exists only as the
  request body field and the one ``bytearray`` handed to the channel,
  which is zero-filled after the attempt whatever the outcome (the
  buffer is wiped here too, covering the channel's pre-consume
  rejections). Nothing is persisted, no outcome store exists on this
  surface, and the module performs NO logging; error messages are fixed
  strings and the C01 envelope never echoes submitted values. The JSON
  transport itself holds the secret as an immutable ``str`` until the
  request ends — the documented honesty of every HTTP secret path (the
  controller never writes it down).
- **Event-loop composition duty** (password channel limitation 7): the
  transport is a bounded BLOCKING lane inside an ``async`` function, so
  the whole call runs via ``asyncio.to_thread``/``asyncio.run`` off the
  request loop. Its approval gate therefore uses the dedicated no-pool
  store (:func:`lane_approval_store`, built once in the lifespan): a
  ``NullPool`` connection lives on exactly one event loop, so no pooled
  connection can ever cross loops.

Route/doc listeners stay off (G1 minimal unauthenticated surface); the
schema is produced in-process via ``app.openapi()`` and recorded as
contract evidence for the central exporter owned by the
``openapi-client`` resource (this card does not hand-edit generated
client types).
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Final

from fastapi import APIRouter, FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from starlette.status import (
    HTTP_200_OK,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_502_BAD_GATEWAY,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from mai.api.approvals import _DECISION_REJECTIONS as _APPROVAL_REJECTIONS
from mai.api.auth import (
    BoundaryError,
    ErrorEnvelope,
    _require_allowed_origin,
    _require_csrf,
    _require_session,
)
from mai.auth.sessions import hash_token
from mai.contracts.server import (
    ProfileValidationError,
    SshIdentity,
    parse_server_profile_text,
)
from mai.credentials.env import EnvFileError, EnvResolver
from mai.execution.commands import CommandRequest, CommandRequestError
from mai.execution.password_input import (
    MAX_SECRET_BYTES,
    PasswordChallenge,
    PasswordChannelError,
    PasswordErrorCode,
    execute_passworded_command,
)
from mai.execution.ssh import SshConnectError, SshErrorCode, resolve_identity_key
from mai.policy.approvals import ApprovalError, ApprovalStore
from mai.profiles.files import FileStoreErrorCode, ProfileFileError, ProfileFileStore

__all__ = ["lane_approval_store", "register_password_challenges"]

#: Canonical enrollment layout (C02 rules 2–3): ``servers/<server_id>/…``
#: under the explicitly configured data root. Kept local: ``mai.profiles.files``
#: is outside this card's allowlist (the same discipline as the local
#: credential-reference regex in :mod:`mai.credentials.env`).
_SERVERS_DIR: Final = "servers"

#: The enrolled known-hosts store (C04 rule 1): one pinned-host-key file per
#: target at ``<data_root>/servers/<server_id>/known_hosts``, written ONLY by
#: the explicit admin enrollment flow (``mai.execution.ssh.pin_host_key`` —
#: a rotated/unenrolled endpoint is never auto-trusted). This surface merely
#: points the transport at it; absence refuses pre-consume in the channel
#: (``invalid_configuration``). Kept local for the allowlist reason above.
_KNOWN_HOSTS_FILENAME: Final = "known_hosts"


# ---------------------------------------------------------------------------
# Wire shapes (transport only; decision truth is the approval store and the
# channel's own validation — every wire bound here is a DoS guard, the
# producers re-validate authoritatively)
# ---------------------------------------------------------------------------


class ChallengeOperationBody(BaseModel):
    """The bound command request, field-for-field (C04 rule 3 shape).

    The channel recomputes the transported payload from these fields and
    refuses any presentation whose hash differs from the approved
    binding (``payload_mismatch``) BEFORE the decision is consumed.
    """

    op_id: uuid.UUID
    server_id: uuid.UUID
    display: str = Field(min_length=1, max_length=200)
    timeout: float = Field(gt=0, le=3600.0)
    output_budget: int = Field(gt=0, le=8 * 1024 * 1024)
    argv: list[str] = Field(min_length=1, max_length=4096)
    shell: str | None = Field(default=None, max_length=128 * 1024)
    working_directory: str | None = Field(default=None, max_length=4096)


class ChallengeIdentityBody(BaseModel):
    """The enrolled transport identity, echoed from the target profile."""

    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    user: str = Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")
    host_key: str = Field(min_length=1, max_length=64)
    credential_ref: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")


class ChallengeBody(BaseModel):
    """One challenge presentation: binding + operation + identity + secret.

    ``server_id`` appears in the binding and the operation and must agree
    (checked here, value-free 422); the store re-checks the binding
    against the persisted approval under its row lock. ``account`` is the
    target account whose sudo credential this challenge authorizes; the
    channel refuses a mismatch with the transport identity's login
    account (``sudo -S`` authenticates the invoking account only).

    The body deliberately carries NO transport paths: both are resolved
    server-side only (:func:`_transport_paths`). Unknown fields — a
    client-supplied ``key_path``/``known_hosts_path`` included — are
    REJECTED, never ignored: a presentation is the pinned challenge
    context field-for-field, and a silently dropped field would let a
    stale or hostile client believe its transport naming was honored.
    """

    model_config = ConfigDict(extra="forbid")

    approval_id: uuid.UUID
    capability_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    server_id: uuid.UUID
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_revision: int = Field(ge=1)
    account: str = Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")
    operation: ChallengeOperationBody
    identity: ChallengeIdentityBody
    secret: str = Field(min_length=1, max_length=MAX_SECRET_BYTES)


class ChallengeOutcomeView(BaseModel):
    """The frozen ``CommandOutcome`` shape (C04 rule 4) — nothing else.

    ``stdout``/``stderr`` are the bounded payload streams (at most
    ``output_budget`` bytes each); no secret material is ever placed in
    them by the lane and no submission field is echoed here.
    """

    op_id: uuid.UUID
    server_id: uuid.UUID
    display: str
    status: str
    exit_code: int | None
    stdout: str
    stdout_bytes: int
    stdout_truncated: bool
    stderr: str
    stderr_bytes: int
    stderr_truncated: bool
    timed_out: bool
    cwd_change_failed: bool
    duration_seconds: float


#: Channel rejections, producer code → (HTTP status, envelope code,
#: value-free message). Exhaustive over :class:`PasswordErrorCode`:
#: an unmapped producer code must crash into the generic
#: ``internal_error`` path, never masquerade as a denial.
_CHALLENGE_REJECTIONS: Final[dict[PasswordErrorCode, tuple[int, str, str]]] = {
    PasswordErrorCode.INVALID_REQUEST: (
        HTTP_422_UNPROCESSABLE_CONTENT,
        "validation_failed",
        "challenge presentation failed validation",
    ),
    PasswordErrorCode.ACCOUNT_MISMATCH: (
        HTTP_409_CONFLICT,
        "account_mismatch",
        (
            "challenge account does not match the authenticated transport "
            "account (sudo -S authenticates the invoking account only)"
        ),
    ),
    PasswordErrorCode.PAYLOAD_BINDING_MISMATCH: (
        HTTP_409_CONFLICT,
        "payload_mismatch",
        (
            "presented payload hash does not match the recomputed transported "
            "payload (an altered operation requires a new approval); the "
            "decision was NOT consumed"
        ),
    ),
    PasswordErrorCode.PASSWORD_REJECTED: (
        HTTP_403_FORBIDDEN,
        "password_rejected",
        (
            "the target rejected the presented secret; the decision is spent "
            "— a fresh approval is required"
        ),
    ),
    PasswordErrorCode.ACCOUNT_LOCKED: (
        HTTP_403_FORBIDDEN,
        "account_locked",
        (
            "the target reports the account or password as expired/locked; "
            "the decision is spent — reconcile the account and require a "
            "fresh approval"
        ),
    ),
}


# ---------------------------------------------------------------------------
# Lifespan-built lane dependencies
# ---------------------------------------------------------------------------


def lane_approval_store(database_url: str) -> tuple[ApprovalStore, AsyncEngine]:
    """The off-loop approval gate for the passworded transport lane.

    The lane runs via ``asyncio.to_thread``/``asyncio.run`` (composition
    duty), so its connections must never join the request loop's pool:
    ``NullPool`` opens one fresh connection per use on the calling loop
    and closes it at session exit. The store is the same authoritative
    DB gate as the main approval store — only its transport differs.
    """
    engine = create_async_engine(database_url, poolclass=NullPool)
    return ApprovalStore(async_sessionmaker(engine, expire_on_commit=False)), engine


def _lane_store(request: Request) -> ApprovalStore:
    """The lifespan-built off-loop store; without it fail closed."""
    store = getattr(request.app.state, "password_approvals", None)
    if store is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "password challenge service is unavailable",
        )
    return store


def _profiles(request: Request) -> ProfileFileStore:
    """The lifespan-built canonical profile store (target enrollment)."""
    store = getattr(request.app.state, "profiles", None)
    if store is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "profile service is unavailable",
        )
    return store


def _credentials(request: Request) -> EnvResolver:
    """The lifespan-built ``.env`` resolver (C03 rule 6); fail closed.

    The configured credential source is loaded ONCE at composition
    (``MAI_ENV_FILE``, ``app.state.credentials``); a missing or unusable
    source means no transport identity key can ever resolve, so the
    surface answers 503 and touches no state.
    """
    resolver = getattr(request.app.state, "credentials", None)
    if resolver is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "password challenge service is unavailable",
        )
    return resolver


# ---------------------------------------------------------------------------
# Presentation assembly (pre-consume; every rejection is value-free)
# ---------------------------------------------------------------------------


def _enrolled_identity(
    request: Request, presented: ChallengeIdentityBody, server_id: uuid.UUID
) -> None:
    """Refuse any transport identity the enrolled profile does not name.

    C04 rule 1: the target profile determines SSH endpoint and user. The
    comparison runs BEFORE the approval gate, so a redirected or spoofed
    presentation never burns the one-use decision (and the pinned
    known-hosts file remains the transport-time host-key enforcement).
    """
    try:
        profile = parse_server_profile_text(_profiles(request).read(server_id).text)
    except ProfileFileError as exc:
        if exc.code is FileStoreErrorCode.NOT_FOUND:
            raise BoundaryError(
                HTTP_404_NOT_FOUND,
                "not_found",
                "no enrolled profile for this target",
            ) from None
        raise BoundaryError(
            HTTP_409_CONFLICT,
            "conflict",
            "the enrolled profile for this target is unreadable",
        ) from None
    except ProfileValidationError:
        raise BoundaryError(
            HTTP_409_CONFLICT,
            "conflict",
            "the enrolled profile for this target is unreadable",
        ) from None
    enrolled = profile.ssh
    if enrolled is None:
        raise BoundaryError(
            HTTP_409_CONFLICT,
            "target_mismatch",
            "the enrolled profile has no SSH target identity",
        )
    if (
        enrolled.host != presented.host
        or enrolled.port != presented.port
        or enrolled.user != presented.user
        or enrolled.credential_ref != presented.credential_ref
        or enrolled.host_key != presented.host_key
    ):
        raise BoundaryError(
            HTTP_409_CONFLICT,
            "target_mismatch",
            "presented transport identity differs from the enrolled profile",
        )


def _transport_paths(request: Request, payload: ChallengeBody) -> tuple[Path, Path]:
    """Resolve BOTH controller-local transport paths server-side only.

    The wire never names a transport path (a client-controlled
    ``known_hosts_path`` could point the host-key check at a file the
    client can plant, defeating the C04 rule 1 pin; a client-controlled
    ``key_path`` would aim ``ssh -i`` at an arbitrary controller file).
    Resolution happens BEFORE the one-use gate, so every failure here
    consumes nothing:

    - **identity key** (C03 rule 6): the enrolled profile's
      ``credential_ref`` resolves through the configured ``.env`` source
      (:func:`mai.execution.ssh.resolve_identity_key`); a reference that
      does not name an existing regular file refuses, as does any
      resolver-level rejection (missing reference, empty value, hostile
      source) — the reviewed helper raises them already structured and
      value-free, and both refuse here as the same value-free
      ``invalid_configuration`` 422;
    - **known-hosts store** (C04 rule 1): the canonical enrolled file for
      the bound ``server_id`` under the profile data root; an unenrolled
      target refuses pre-consume in the channel
      (``invalid_configuration``).
    """
    try:
        key_path = resolve_identity_key(
            _credentials(request), payload.identity.credential_ref
        )
    except (EnvFileError, SshConnectError):
        raise BoundaryError(
            HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_configuration",
            "transport prerequisites are missing; nothing ran and no "
            "decision was consumed",
        ) from None
    known_hosts_path = (
        _profiles(request).root
        / _SERVERS_DIR
        / str(payload.server_id)
        / _KNOWN_HOSTS_FILENAME
    )
    return key_path, known_hosts_path


def _presentation(
    request: Request, payload: ChallengeBody, principal: str
) -> tuple[PasswordChallenge, SshIdentity, CommandRequest, Path, Path]:
    """Assemble the typed challenge presentation (pre-consume rejections).

    Structural validation is the producers' (``CommandRequest`` /
    ``PasswordChallenge``); a rejected presentation is a value-free 422
    that consumed nothing. ``operation.server_id`` must agree with the
    binding's ``server_id`` — the channel's payload hash intentionally
    excludes correlation IDs, so this seam closes the gap. The transport
    paths resolve here too (server-side only, :func:`_transport_paths`),
    before anything is consumed.
    """
    if payload.operation.server_id != payload.server_id:
        raise BoundaryError(
            HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_failed",
            "operation target does not match the approval binding",
        )
    _enrolled_identity(request, payload.identity, payload.server_id)
    key_path, known_hosts_path = _transport_paths(request, payload)
    try:
        operation = CommandRequest(
            op_id=payload.operation.op_id,
            server_id=payload.operation.server_id,
            display=payload.operation.display,
            timeout=payload.operation.timeout,
            output_budget=payload.operation.output_budget,
            argv=tuple(payload.operation.argv),
            shell=payload.operation.shell,
            working_directory=payload.operation.working_directory,
        )
        identity = SshIdentity(
            host=payload.identity.host,
            port=payload.identity.port,
            user=payload.identity.user,
            host_key=payload.identity.host_key,
            credential_ref=payload.identity.credential_ref,
        )
        challenge = PasswordChallenge(
            approval_id=payload.approval_id,
            capability_id=payload.capability_id,
            server_id=payload.server_id,
            payload_hash=payload.payload_hash,
            policy_revision=payload.policy_revision,
            account=payload.account,
            consuming_principal=principal,
        )
    except (CommandRequestError, PasswordChannelError):
        raise BoundaryError(
            HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_failed",
            "challenge presentation failed validation",
        ) from None
    return challenge, identity, operation, key_path, known_hosts_path


# ---------------------------------------------------------------------------
# Route (C01 prefix/envelope; C03 rule 4 + C04 rule 2 transport; C08 boundary)
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/password-challenges", tags=["password-challenges"])


@router.post(
    "",
    status_code=HTTP_200_OK,
    response_model=ChallengeOutcomeView,
    summary="Submit the sudo secret for one approved password challenge",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowed, CSRF token rejected, or the "
            "target refused the presented secret (decision spent)",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No approval record or no enrolled profile for "
            "this target",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Decision already consumed, expired, or a "
            "binding/target/identity mismatch",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Challenge presentation failed validation",
        },
        HTTP_502_BAD_GATEWAY: {
            "model": ErrorEnvelope,
            "description": "SSH transport failed; the decision is spent",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Password challenge service unavailable",
        },
    },
)
async def submit_challenge(
    request: Request, payload: ChallengeBody
) -> ChallengeOutcomeView:
    """Spend the one-use decision and transport the secret to ``sudo -S``.

    Boundary first (ADR §5 order): session → origin → CSRF, then the
    enrolled-identity binding, before any state is touched. The lane
    itself is fail-closed at every seam (structural → account → payload
    hash → known-hosts → ONE-USE consume → transport) and wipes the
    secret buffer whatever the outcome; this surface wipes it again
    around the whole call so even a pre-consume channel rejection leaves
    nothing behind. Exactly ONE authentication attempt is ever made.
    """
    _, token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    store = _lane_store(request)
    challenge, identity, operation, key_path, known_hosts_path = _presentation(
        request, payload, hash_token(token)
    )
    secret = bytearray(payload.secret.encode("utf-8"))
    try:
        try:
            # Composition duty: the bounded blocking ssh lane must never
            # stall the request loop (asyncio.run owns a fresh loop in
            # the worker thread; the store is the dedicated NullPool one).
            outcome = await asyncio.to_thread(
                asyncio.run,
                execute_passworded_command(
                    store,
                    challenge,
                    identity,
                    operation,
                    secret,
                    key_path=key_path,
                    known_hosts_path=known_hosts_path,
                ),
            )
        finally:
            secret[:] = b"\x00" * len(secret)  # wiped whatever happened
    except ApprovalError as exc:
        status, code, message = _APPROVAL_REJECTIONS[exc.code]
        raise BoundaryError(status, code, message) from None
    except PasswordChannelError as exc:
        status, code, message = _CHALLENGE_REJECTIONS[exc.code]
        raise BoundaryError(status, code, message) from None
    except CommandRequestError:
        raise BoundaryError(
            HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_failed",
            "bound operation failed validation",
        ) from None
    except SshConnectError as exc:
        if exc.code is SshErrorCode.INVALID_CONFIGURATION:
            # Pre-consume (enrollment/known-hosts): nothing ran, nothing
            # spent — the channel checks this before the gate.
            raise BoundaryError(
                HTTP_422_UNPROCESSABLE_CONTENT,
                exc.code.value,
                "transport prerequisites are missing; nothing ran and no "
                "decision was consumed",
            ) from None
        # Post-gate transport failure: the decision is spent (the lane
        # consumes before any transport attempt), the secret never
        # reached a target that did not run the payload.
        raise BoundaryError(
            HTTP_502_BAD_GATEWAY,
            exc.code.value,
            "ssh transport failed after the decision was spent; a fresh "
            "approval is required",
        ) from None
    return ChallengeOutcomeView(
        op_id=outcome.op_id,
        server_id=outcome.server_id,
        display=outcome.display,
        status=outcome.status.value,
        exit_code=outcome.exit_code,
        stdout=outcome.stdout,
        stdout_bytes=outcome.stdout_bytes,
        stdout_truncated=outcome.stdout_truncated,
        stderr=outcome.stderr,
        stderr_bytes=outcome.stderr_bytes,
        stderr_truncated=outcome.stderr_truncated,
        timed_out=outcome.timed_out,
        cwd_change_failed=outcome.cwd_change_failed,
        duration_seconds=outcome.duration_seconds,
    )


def register_password_challenges(app: FastAPI) -> None:
    """Register the challenge surface under the shared boundary."""
    app.include_router(router)
