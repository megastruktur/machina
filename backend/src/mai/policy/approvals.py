"""Payload-bound approval persistence with atomic one-use consumption.

Contracts: ``contracts.md`` **C03 rule 4** (approval binding), **C01
rules 1–2** (opaque UUID identity, principal from the session), and the
C05 ``waiting_approval`` lifecycle. This module is the durable decision
store the approval API (``03-approval-api``) serves and the dispatcher
(``03-dispatcher``) consumes immediately before a privileged effect; it
owns neither the human gate UX nor the authorization decision itself.

What it persists
----------------

One :class:`Approval` row per human decision, bound (C03 rule 4) to

- ``payload_hash`` — SHA-256 hex of the *canonical operation payload*.
  The store never sees payload bytes: canonical serialization and
  hashing belong to the dispatcher, so no second canonicalization can
  drift. Payload bytes never enter SQL.
- server/target (``server_id``) and scope — the exact capability
  application: ``capability_id`` x ``server_id``. There is no wildcard
  and no wider scope: one decision authorizes exactly one capability on
  exactly one server. Whether the *current* grant set still covers that
  application is re-checked by the dispatcher against live grants via
  :func:`mai.policy.evaluate.evaluate` immediately before the effect
  (C03 rule 3) — never from a scope snapshot stored here.
- ``policy_revision`` — the revision the decision was made under.
  ``03-policy-config`` ties the revision to the stored grant
  configuration, so *every* grant add/revoke or deny-list change is a
  new revision: a revoked grant therefore can never match a stale
  approval (consumption reports ``revision_mismatch``).
- expiry (``expires_at``) — exclusive: at ``expires_at`` exactly the
  approval is dead. There is no stored "expired" state; expiry is a
  property of the stored timestamp against the clock at use time, so
  the audit record stays clock-independent and replayable.

Decision ownership is explicit: ``created_by`` names the principal that
granted the decision (from the authenticated session only — never a
client-supplied flag), ``consumed_by`` names the principal that
consumed it.

One-use consumption
-------------------

:func:`ApprovalStore.consume` is ONE conditional
``UPDATE … WHERE … RETURNING``: the decision is spent (``consumed_at``/
``consumed_by`` set) if and only if the row is still pending, unexpired
and the presented binding matches field-for-field. Two processes
racing to consume the same decision are serialized by the row lock:
exactly one UPDATE wins, the loser observes ``already_consumed`` —
there is no check-then-act window and no second consumption. A denial
(expired, mismatched binding, replay) never mutates the row: the
approval stays exactly as it was and no effect is authorized.

Fail-closed
-----------

Every structural problem is a :class:`ApprovalError` with a stable,
value-free code before any SQL effect; every runtime denial has a
stable reason from a frozen precedence (first match wins):

``not_found`` → ``already_consumed`` → ``expired`` →
``capability_mismatch`` → ``target_mismatch`` → ``payload_mismatch`` →
``revision_mismatch``.

Rejection details quote field names and opaque ids — never payload
material (the store only ever handles hashes), never credential
material. The store performs no logging and reads no process
environment.

Layering note: the store is not an authorization decision point. That
an approval may be *created* (an allowed decision with
``requires_approval``) and that the *consuming* process still holds the
grant are the dispatcher's ``evaluate()`` obligations (C03 rule 3); the
``policy_revision`` binding here is the durable backstop that makes a
stale approval useless after any policy change. HTTP shapes belong to
``03-approval-api``.

PostgreSQL assumption: the row-lock serialization of concurrent
consumption is a PostgreSQL guarantee (``mai.database`` is
Postgres-only by design); Windows targets remain a frozen non-goal.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Final

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "MAX_PRINCIPAL_CHARS",
    "Approval",
    "ApprovalError",
    "ApprovalErrorCode",
    "ApprovalRequest",
    "ApprovalStore",
]

#: Capability identifier shape — the registry's slug charset, kept local
#: with the same note as ``mai.policy.evaluate`` (the shape owner is
#: outside this card's allowlist; a divergence is a review finding, not
#: a silent import of a private symbol).
_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

#: SHA-256 hex digest shape for the payload binding.
_PAYLOAD_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

#: Principal length bound (DoS bound, not an identity policy — principals
#: are authenticated session identities, C01 rule 2). Rejected before any
#: SQL effect; mirrors the storage CHECK in ``0003_approvals``.
MAX_PRINCIPAL_CHARS: Final[int] = 256


class ApprovalErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    INVALID_APPROVAL = "invalid_approval"
    NOT_FOUND = "not_found"
    ALREADY_CONSUMED = "already_consumed"
    EXPIRED = "expired"
    CAPABILITY_MISMATCH = "capability_mismatch"
    TARGET_MISMATCH = "target_mismatch"
    PAYLOAD_MISMATCH = "payload_mismatch"
    REVISION_MISMATCH = "revision_mismatch"


class ApprovalError(ValueError):
    """One structured, value-free approval rejection.

    ``invalid_approval`` is raised at construction (before any SQL
    effect); every other code is a runtime consumption denial that
    leaves the stored row untouched.
    """

    def __init__(self, code: ApprovalErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Input validation (value-free; no SQL effect before success)
# ---------------------------------------------------------------------------


def _validated_approval_id(approval_id: object) -> uuid.UUID:
    if not isinstance(approval_id, uuid.UUID):
        raise ApprovalError(
            ApprovalErrorCode.INVALID_APPROVAL,
            "approval_id must be a uuid.UUID (C01 rule 1)",
        )
    return approval_id


def _validated_capability_id(capability_id: object) -> str:
    if not isinstance(capability_id, str) or not _IDENTIFIER_RE.fullmatch(
        capability_id
    ):
        raise ApprovalError(
            ApprovalErrorCode.INVALID_APPROVAL,
            "capability_id must match "
            f"{_IDENTIFIER_RE.pattern} (C03 rule 1 slug shape)",
        )
    return capability_id


def _validated_server_id(server_id: object) -> uuid.UUID:
    if not isinstance(server_id, uuid.UUID):
        raise ApprovalError(
            ApprovalErrorCode.INVALID_APPROVAL,
            "server_id must be a uuid.UUID (C01 rule 1)",
        )
    return server_id


def _validated_payload_hash(payload_hash: object) -> str:
    if not isinstance(payload_hash, str) or not _PAYLOAD_HASH_RE.fullmatch(
        payload_hash
    ):
        raise ApprovalError(
            ApprovalErrorCode.INVALID_APPROVAL,
            "payload_hash must be a lowercase SHA-256 hex digest of the "
            "canonical operation payload (C03 rule 4)",
        )
    return payload_hash


def _validated_revision(policy_revision: object) -> int:
    if isinstance(policy_revision, bool) or not isinstance(policy_revision, int):
        raise ApprovalError(
            ApprovalErrorCode.INVALID_APPROVAL,
            "policy_revision must be an int (bool is not an int here)",
        )
    if policy_revision < 1:
        raise ApprovalError(
            ApprovalErrorCode.INVALID_APPROVAL,
            "policy_revision must be >= 1",
        )
    return policy_revision


def _validated_principal(principal: object, field: str) -> str:
    if not isinstance(principal, str) or not 1 <= len(principal) <= MAX_PRINCIPAL_CHARS:
        raise ApprovalError(
            ApprovalErrorCode.INVALID_APPROVAL,
            f"{field} must be a non-empty session principal "
            f"(at most {MAX_PRINCIPAL_CHARS} chars, C01 rule 2)",
        )
    return principal


def _validated_expiry(expires_at: object) -> datetime:
    if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
        raise ApprovalError(
            ApprovalErrorCode.INVALID_APPROVAL,
            "expires_at must be a timezone-aware datetime (C03 rule 4 "
            "expiry binding)",
        )
    return expires_at


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Dataclasses (frozen shapes)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """Validated creation input for one approval decision.

    ``payload_hash`` is the dispatcher's canonical SHA-256 hex of the
    operation payload; ``created_by`` is the granting principal; the
    store rejects an ``expires_at`` that is not still in the future at
    insert time (a dead approval is never persisted).
    """

    capability_id: str
    server_id: uuid.UUID
    payload_hash: str
    policy_revision: int
    created_by: str
    expires_at: datetime

    def __post_init__(self) -> None:
        _validated_capability_id(self.capability_id)
        _validated_server_id(self.server_id)
        _validated_payload_hash(self.payload_hash)
        _validated_revision(self.policy_revision)
        _validated_principal(self.created_by, "created_by")
        _validated_expiry(self.expires_at)


@dataclass(frozen=True, slots=True)
class Approval:
    """One persisted approval record (0003 migration frozen columns).

    ``consumed_at``/``consumed_by`` are ``None`` while the decision is
    pending and set together, atomically, exactly once. The derived
    :attr:`status` is persisted-state only — no clock reading — so the
    record is replayable audit material.
    """

    approval_id: uuid.UUID
    capability_id: str
    server_id: uuid.UUID
    payload_hash: str
    policy_revision: int
    created_by: str
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None
    consumed_by: str | None

    @property
    def status(self) -> str:
        """``pending`` or ``consumed`` — persisted state, clock-free."""
        return "consumed" if self.consumed_at is not None else "pending"

    def to_json(self) -> dict[str, object]:
        """Frozen-shape JSON document (stable key order; audit view)."""
        return {
            "approval_id": str(self.approval_id),
            "capability_id": self.capability_id,
            "server_id": str(self.server_id),
            "payload_hash": self.payload_hash,
            "policy_revision": self.policy_revision,
            "status": self.status,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "consumed_by": self.consumed_by,
            "consumed_at": None
            if self.consumed_at is None
            else self.consumed_at.isoformat(),
        }

    def __repr__(self) -> str:
        return (
            f"<Approval {self.approval_id} {self.capability_id}@{self.server_id} "
            f"rev={self.policy_revision} {self.status}>"
        )


_APPROVAL_COLUMNS: Final[tuple[str, ...]] = (
    "approval_id",
    "capability_id",
    "server_id",
    "payload_hash",
    "policy_revision",
    "created_by",
    "created_at",
    "expires_at",
    "consumed_at",
    "consumed_by",
)


def _approval_of(row: Sequence[object] | object) -> Approval:
    """Materialize one ``approvals`` row into the frozen dataclass."""
    mapping = row._mapping  # noqa: SLF001 - core Row mapping access
    return Approval(
        **{column: mapping[column] for column in _APPROVAL_COLUMNS}
    )


# ---------------------------------------------------------------------------
# SQL statements (schema owned by migrations/versions/0003_approvals.py)
# ---------------------------------------------------------------------------

_INSERT_APPROVAL = text(
    "INSERT INTO approvals "
    "(approval_id, capability_id, server_id, payload_hash, policy_revision, "
    "created_by, created_at, expires_at) "
    "VALUES (:approval_id, :capability_id, :server_id, :payload_hash, "
    ":policy_revision, :created_by, :created_at, :expires_at)"
)

_SELECT_APPROVAL = text(
    "SELECT approval_id, capability_id, server_id, payload_hash, "
    "policy_revision, created_by, created_at, expires_at, consumed_at, "
    "consumed_by FROM approvals WHERE approval_id = :approval_id"
)

#: C03 rule 4 core: the decision is spent in ONE conditional statement.
#: The WHERE clause re-checks pending state, expiry and the full binding
#: (capability, target, payload hash, policy revision) under the row
#: lock, so two concurrent consumers are serialized by PostgreSQL:
#: exactly one UPDATE matches, the loser matches zero rows — a replay
#: can never double-spend, and a mismatched presentation never touches
#: the row. The RETURNING row is the authoritative consumption receipt.
_CONSUME_APPROVAL = text(
    "UPDATE approvals SET consumed_at = :now, consumed_by = :consumed_by "
    "WHERE approval_id = :approval_id AND consumed_at IS NULL "
    "AND expires_at > :now "
    "AND capability_id = :capability_id AND server_id = :server_id "
    "AND payload_hash = :payload_hash AND policy_revision = :policy_revision "
    "RETURNING approval_id, capability_id, server_id, payload_hash, "
    "policy_revision, created_by, created_at, expires_at, consumed_at, "
    "consumed_by"
)


class ApprovalStore:
    """Approval persistence + one-use consumption over one sessionmaker."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(self, request: ApprovalRequest) -> Approval:
        """Persist one pending approval decision; return its record.

        ``approval_id`` is generated here (opaque UUID, C01 rule 1). The
        insert is rejected with ``invalid_approval`` when ``expires_at``
        is not still in the future at insert time — a dead approval is
        never persisted (the storage CHECK
        ``ck_approvals_expiry_after_creation`` is the boundary of last
        resort).
        """
        created_at = _utcnow()
        if request.expires_at <= created_at:
            raise ApprovalError(
                ApprovalErrorCode.INVALID_APPROVAL,
                "expires_at must be in the future at creation (C03 rule 4)",
            )
        approval_id = uuid.uuid4()
        async with self._sessions() as session, session.begin():
            try:
                await session.execute(
                    _INSERT_APPROVAL,
                    {
                        "approval_id": approval_id,
                        "capability_id": request.capability_id,
                        "server_id": request.server_id,
                        "payload_hash": request.payload_hash,
                        "policy_revision": request.policy_revision,
                        "created_by": request.created_by,
                        "created_at": created_at,
                        "expires_at": request.expires_at,
                    },
                )
            except IntegrityError as orig:
                # Defense in depth: the construction validation above and
                # the 0003 CHECK constraints make this unreachable for
                # validated input, but a constraint violation must never
                # surface as a raw driver error.
                raise ApprovalError(
                    ApprovalErrorCode.INVALID_APPROVAL,
                    "approval rejected by storage constraints",
                ) from orig
        return Approval(
            approval_id=approval_id,
            capability_id=request.capability_id,
            server_id=request.server_id,
            payload_hash=request.payload_hash,
            policy_revision=request.policy_revision,
            created_by=request.created_by,
            created_at=created_at,
            expires_at=request.expires_at,
            consumed_at=None,
            consumed_by=None,
        )

    async def get(self, approval_id: uuid.UUID) -> Approval | None:
        """Read one approval record (audit view); ``None`` if absent."""
        checked_id = _validated_approval_id(approval_id)
        async with self._sessions() as session:
            row = (
                await session.execute(_SELECT_APPROVAL, {"approval_id": checked_id})
            ).first()
        return None if row is None else _approval_of(row)

    async def consume(
        self,
        approval_id: uuid.UUID,
        *,
        capability_id: str,
        server_id: uuid.UUID,
        payload_hash: str,
        policy_revision: int,
        consumed_by: str,
    ) -> Approval:
        """Spend the one-use decision atomically; return the receipt.

        The presented binding (capability, target, payload hash, policy
        revision) must match the approval field-for-field, the decision
        must be pending and unexpired — one conditional UPDATE decides
        all of that under the row lock. Exactly one concurrent consumer
        wins; every other outcome is a stable-code denial that leaves
        the row untouched (denial precedence in the module docstring).
        """
        checked_id = _validated_approval_id(approval_id)
        params = {
            "approval_id": checked_id,
            "capability_id": _validated_capability_id(capability_id),
            "server_id": _validated_server_id(server_id),
            "payload_hash": _validated_payload_hash(payload_hash),
            "policy_revision": _validated_revision(policy_revision),
            "consumed_by": _validated_principal(consumed_by, "consumed_by"),
        }
        now = _utcnow()
        async with self._sessions() as session, session.begin():
            row = (
                await session.execute(
                    _CONSUME_APPROVAL, {"now": now, **params}
                )
            ).first()
        if row is not None:
            return _approval_of(row)
        # Zero rows updated: classify why (fresh read, fresh clock). The
        # UPDATE above was the authoritative gate; this classification
        # can only observe a state that was already true when it lost —
        # including a concurrent winner (reported as already_consumed).
        now = _utcnow()
        async with self._sessions() as session:
            state = (
                await session.execute(_SELECT_APPROVAL, {"approval_id": checked_id})
            ).first()
        if state is None:
            raise ApprovalError(
                ApprovalErrorCode.NOT_FOUND, "no approval record for id"
            )
        record = _approval_of(state)
        if record.consumed_at is not None:
            raise ApprovalError(
                ApprovalErrorCode.ALREADY_CONSUMED,
                "decision already consumed (one-use, C03 rule 4)",
            )
        if record.expires_at <= now:
            raise ApprovalError(
                ApprovalErrorCode.EXPIRED,
                "approval expired (C03 rule 4 expiry binding)",
            )
        if record.capability_id != params["capability_id"]:
            raise ApprovalError(
                ApprovalErrorCode.CAPABILITY_MISMATCH,
                "presented capability_id differs from the approval binding",
            )
        if record.server_id != params["server_id"]:
            raise ApprovalError(
                ApprovalErrorCode.TARGET_MISMATCH,
                "presented server_id differs from the approval binding",
            )
        if record.payload_hash != params["payload_hash"]:
            raise ApprovalError(
                ApprovalErrorCode.PAYLOAD_MISMATCH,
                "presented payload_hash differs from the approval binding "
                "(altered payload requires a new approval, C03 rule 4)",
            )
        raise ApprovalError(
            ApprovalErrorCode.REVISION_MISMATCH,
            "presented policy_revision differs from the approval binding "
            "(policy/grant changed since the decision, C03 rule 4)",
        )
