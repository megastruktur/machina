"""Resource locks: exclusive ``server_id`` + resource leases with fencing (C05 rule 6).

The execution-side serialization service for conflicting target mutations
(contracts.md **C05** rule 6, **C04** rule 7; card ``05-resource-locks``):

- **Lock keys and span** — a lock is the pair ``(server_id, resource)``:
  the C01 opaque server identity plus one caller-chosen resource
  identifier (e.g. a package manager, an application id, a port). Lock
  state lives entirely in Machina's Postgres, keyed by that pair, so a
  lock spans workers, parents and children: the owner is an opaque
  ``owner_token`` minted once per acquisition, not a process or a
  session. The run facade shares the token down its delegation tree
  (children execute under the root's lease); heartbeat/release are
  authorized by the token alone. The token authorizes ONLY lock-table
  operations on its own row and is itself stored in that row — it is not
  a credential and never substitutes for grants/approvals (C03).

- **Ownership-checked operations** — ``acquire`` either inserts a fresh
  lease or fails with a value-free rejection; ``heartbeat`` and
  ``release`` match BOTH the lock key AND the ``owner_token``. A
  heartbeat/release from any other identity is rejected; it can never
  extend, remove or retarget someone else's lease (acceptance 1).

- **Leases, not blind steals (acceptance 2)** — every lease carries a
  TTL renewed by heartbeats; all lease arithmetic happens in database
  time. When a lease lapses — a dead worker, a killed controller, a
  partition — the row is fenced (``state='fenced'``), never silently
  handed to a successor: the prior holder's remote effect may still be
  live (C04 rule 7: lease expiry never authorizes a duplicate mutation).
  ANY touch of a lapsed lease (successor acquire, late heartbeat, late
  release) performs the same fence transition, so the fail-safe state is
  reached no matter who observes the lapse first. A fence blocks every
  further acquire of the key until ``clear_fence`` is called with a
  reconciliation verdict from the frozen terminal classes
  (:class:`mai.execution.reconcile.ReconciliationClass`) — the host truth
  that the prior conflicting effect is over. A live or uncertain verdict
  keeps the fence. Fence-clearing audit lives with the reconciliation
  operation that produced the verdict (``04-reconcile`` records); this
  module only refuses to admit successors without one.

- **Controller boot identity** — each service instance carries a
  ``boot_id`` (opaque UUID, minted per construction): the identity of
  ONE controller boot. Leases record the holder's boot; fences record
  the fenced holder's boot. A restarted controller constructs a service
  with a NEW boot id and can never re-present the old owner tokens: a
  still-live lease rejects it (no double-hold across restart) and a
  lapsed one fences it. Restart alone therefore never re-admits
  conflicting work — reconciliation does. This is the controller-side
  boot identity; it is distinct from the TARGET ``boot_id`` inside C04
  receipts (``04-supervisor``/``04-reconcile`` vocabulary).

- **Deadlock-free multi-acquire** — ``acquire_all`` takes several locks
  in the canonical total order (sorted ``(server_id, resource)``) and
  releases what it already took when any acquisition fails: no
  hold-and-wait cycle over two lock sets can deadlock, and the observable
  end state is all-or-nothing.

**Storage.** The table :data:`LOCK_TABLE` is owned by this service and
created idempotently at first use (``ensure_schema``) — deliberately out
of the Alembic chain, exactly as migration ``0004_jobs.py`` records ("No
locks table: mutation locks are owned by ``05-resource-locks``") and in
the same spirit as the engine-owned ``dbos`` schema. The connection is
opened per operation: a lease op is one short transaction, survives
Postgres restarts without sticky-session state, and works unchanged from
any process role (worker, facade, control plane). The engine adapter
(``mai.jobs.engine``) stays the only durable-workflow path; this module
adds no queue, scheduler or second job-state authority (C05 rule 1).

Rejections are one value-free :class:`ResourceLockError` (code + fixed
reason, C01 rule 3): safe to log, store and return to a browser. An
unreachable database raises ``UNAVAILABLE`` (guard rule 10: an outage
pauses work instead of corrupting it).
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Final

import psycopg
from psycopg.rows import dict_row

from mai.execution.reconcile import ReconciliationClass

__all__ = [
    "CLEARABLE_VERDICTS",
    "DEFAULT_LEASE_TTL",
    "FENCE_REASON_LEASE_EXPIRED",
    "LOCK_TABLE",
    "MAX_LEASE_TTL",
    "MAX_NOTE_CHARS",
    "MAX_OWNER_REF_CHARS",
    "MAX_RESOURCE_CHARS",
    "STATE_FENCED",
    "STATE_HELD",
    "LockErrorCode",
    "LockLease",
    "LockRequest",
    "LockSnapshot",
    "ResourceLockError",
    "ResourceLockService",
    "fence_may_be_cleared",
]

# ---------------------------------------------------------------------------
# frozen vocabulary + bounds
# ---------------------------------------------------------------------------

#: The single table this service owns (created by :meth:`ensure_schema`).
LOCK_TABLE: Final[str] = "job_resource_locks"

#: Row states (closed set, pinned by the table CHECK).
STATE_HELD: Final[str] = "held"
STATE_FENCED: Final[str] = "fenced"

#: The one fence-entry reason: a lease lapsed while its holder's remote
#: effect may still be live. Closed vocabulary — a second entry path into
#: ``fenced`` would be a contract change, not a column value.
FENCE_REASON_LEASE_EXPIRED: Final[str] = "lease_expired"

#: Default lease TTL (seconds). Holders of long mutations renew by
#: heartbeat; the TTL bounds how long a dead holder blocks detection.
DEFAULT_LEASE_TTL: Final[float] = 30.0

#: Sanity ceiling for one lease (a longer claim is a design error: leases
#: are renewed by heartbeats, not assumed permanent).
MAX_LEASE_TTL: Final[float] = 7 * 24 * 3600.0

MAX_RESOURCE_CHARS: Final[int] = 200
MAX_OWNER_REF_CHARS: Final[int] = 200
MAX_NOTE_CHARS: Final[int] = 500

#: Reconciliation verdicts that prove the prior conflicting effect is
#: host-verified TERMINAL (``04-reconcile``): only these may clear a
#: fence (C04 rule 7). ``running`` (the process may live) and every
#: ``uncertain`` class (outcome unknowable) keep the fence up.
CLEARABLE_VERDICTS: Final[frozenset[ReconciliationClass]] = frozenset(
    {
        ReconciliationClass.FINAL_SUCCEEDED,
        ReconciliationClass.FINAL_FAILED,
        ReconciliationClass.FINAL_CANCELLED,
    }
)

#: A conflicting row can vanish between the failed INSERT and the locked
#: read (a concurrent release commits in that window); the acquire retries
#: the free-key insert a bounded number of times instead of asserting.
_ACQUIRE_ATTEMPTS: Final[int] = 4

_CONNECT_TIMEOUT_S: Final[int] = 5


def fence_may_be_cleared(verdict: ReconciliationClass) -> bool:
    """True iff the verdict proves the fenced effect host-verified terminal."""
    return verdict in CLEARABLE_VERDICTS


class LockErrorCode(StrEnum):
    """Value-free rejection codes (C01 envelope vocabulary)."""

    #: Malformed field set (identity, resource, ttl, note, verdict shape).
    INVALID_REQUEST = "invalid_request"
    #: A live lease of another owner holds the key (plain contention).
    HELD = "held"
    #: The key is fenced after lease loss; reconciliation must clear it.
    FENCED = "fenced"
    #: No live lease under this identity on this key (never held, already
    #: released, or taken by a different owner).
    NOT_OWNER = "not_owner"
    #: The caller's own lease has lapsed (fence recorded where applicable).
    LEASE_LOST = "lease_lost"
    #: ``clear_fence`` found nothing fenced to clear.
    NOT_FENCED = "not_fenced"
    #: The reconciliation verdict does not prove the prior effect terminal.
    FENCE_UNRESOLVED = "fence_unresolved"
    #: The database is unreachable (guard rule 10: pause, never guess).
    UNAVAILABLE = "unavailable"


class ResourceLockError(RuntimeError):
    """One value-free lock rejection: code + fixed reason.

    Messages carry codes and field names only — never key, token or
    caller-supplied values — so they are safe to log, store and surface
    (C01 rule 3, the sibling modules' envelope discipline).
    """

    def __init__(self, code: LockErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# validation (pure, value-free)
# ---------------------------------------------------------------------------


def _reject(code: LockErrorCode, detail: str) -> None:
    raise ResourceLockError(code, detail)


def _validate_uuid_field(value: object, field: str) -> str:
    """Require a canonical lowercase hyphenated UUID string (C01 rule 1)."""
    if not isinstance(value, str):
        _reject(LockErrorCode.INVALID_REQUEST, f"{field} must be a UUID string")
    try:
        parsed = uuid.UUID(value)  # type: ignore[arg-type]
    except ValueError:
        _reject(LockErrorCode.INVALID_REQUEST, f"{field} is not a UUID")
    canonical = str(parsed)
    if canonical != value:  # braces/urn/uppercase forms are not accepted
        _reject(
            LockErrorCode.INVALID_REQUEST,
            f"{field} must be in canonical lowercase hyphenated form",
        )
    return value


def _validate_resource(resource: object) -> str:
    """Require a bounded, control-character-free resource identifier."""
    if not isinstance(resource, str) or not resource:
        _reject(LockErrorCode.INVALID_REQUEST, "resource must be a non-empty string")
    if len(resource) > MAX_RESOURCE_CHARS:
        _reject(LockErrorCode.INVALID_REQUEST, "resource exceeds its length bound")
    if any(ch < " " or ch == "\x7f" for ch in resource):
        _reject(
            LockErrorCode.INVALID_REQUEST,
            "resource must not contain control characters",
        )
    return resource


def _validate_owner_ref(owner_ref: object) -> str:
    """Validate the optional opaque caller label (e.g. a run id reference)."""
    if not isinstance(owner_ref, str):
        _reject(LockErrorCode.INVALID_REQUEST, "owner_ref must be a string")
    if len(owner_ref) > MAX_OWNER_REF_CHARS:
        _reject(LockErrorCode.INVALID_REQUEST, "owner_ref exceeds its length bound")
    if any(ch < " " or ch == "\x7f" for ch in owner_ref):
        _reject(
            LockErrorCode.INVALID_REQUEST,
            "owner_ref must not contain control characters",
        )
    return owner_ref


def _validate_ttl(ttl: object) -> float:
    """Require a finite, positive lease TTL within the sanity ceiling."""
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
        _reject(LockErrorCode.INVALID_REQUEST, "ttl must be a number of seconds")
    value = float(ttl)
    if not math.isfinite(value) or value <= 0:
        _reject(LockErrorCode.INVALID_REQUEST, "ttl must be finite and positive")
    if value > MAX_LEASE_TTL:
        _reject(LockErrorCode.INVALID_REQUEST, "ttl exceeds the lease ceiling")
    return value


def _validate_note(note: object) -> str:
    """Require the deliberate non-empty reconciliation note for a fence clear."""
    if not isinstance(note, str) or not note.strip():
        _reject(LockErrorCode.INVALID_REQUEST, "note must be a non-empty string")
    if len(note) > MAX_NOTE_CHARS:
        _reject(LockErrorCode.INVALID_REQUEST, "note exceeds its length bound")
    return note


def _validate_lock_url(url: object) -> str:
    if not isinstance(url, str) or not url:
        _reject(
            LockErrorCode.INVALID_REQUEST, "database url must be a non-empty string"
        )
    return url  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# data vocabulary
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LockRequest:
    """One desired acquisition in an ``acquire_all`` batch."""

    server_id: str
    resource: str
    owner_ref: str = ""
    ttl: float | None = None


@dataclass(frozen=True, slots=True)
class LockLease:
    """One held lease: the capability token plus its database-recorded terms.

    The token is the ONLY release/heartbeat authority. The run facade
    shares the lease (or its token) across the delegation tree so children
    execute under the root's lock; possession is possession regardless of
    which process presents it.
    """

    server_id: str
    resource: str
    owner_token: str
    boot_id: str
    owner_ref: str
    lease_ttl: float
    expires_at: datetime
    acquired_at: datetime


@dataclass(frozen=True, slots=True)
class LockSnapshot:
    """Observational read of one lock row (``inspect``; never a mutation).

    ``observed_at`` is database time, so ``expired`` compares the lease
    against the same clock the service enforces with — never the caller's
    local clock.
    """

    server_id: str
    resource: str
    state: str
    owner_token: str
    boot_id: str
    owner_ref: str
    lease_ttl: float
    lease_expires_at: datetime
    acquired_at: datetime
    heartbeat_at: datetime
    fenced_at: datetime | None
    fence_reason: str | None
    fenced_boot_id: str | None
    observed_at: datetime

    @property
    def expired(self) -> bool:
        """True iff the lease has lapsed on the database clock."""
        return self.lease_expires_at <= self.observed_at


def _utc(dt: datetime) -> datetime:
    """Normalize a database timestamp to explicit UTC (C01 rule 5)."""
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

_LOCK_TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS {LOCK_TABLE} (
    server_id uuid NOT NULL,
    resource text NOT NULL CHECK (length(resource) <= {MAX_RESOURCE_CHARS}),
    state text NOT NULL CHECK (state IN ('{STATE_HELD}', '{STATE_FENCED}')),
    owner_token uuid NOT NULL,
    boot_id uuid NOT NULL,
    owner_ref text NOT NULL DEFAULT '' CHECK (length(owner_ref) <= {MAX_OWNER_REF_CHARS}),
    lease_ttl_seconds double precision NOT NULL CHECK (lease_ttl_seconds > 0),
    lease_expires_at timestamptz NOT NULL,
    acquired_at timestamptz NOT NULL,
    heartbeat_at timestamptz NOT NULL,
    fenced_at timestamptz,
    fence_reason text CHECK (fence_reason = '{FENCE_REASON_LEASE_EXPIRED}'),
    fenced_boot_id uuid,
    PRIMARY KEY (server_id, resource),
    CHECK ((state = '{STATE_HELD}') OR (fenced_at IS NOT NULL))
)
"""

_ROW_COLUMNS = """
    server_id::text AS server_id, resource, state, owner_token::text AS owner_token,
    boot_id::text AS boot_id, owner_ref, lease_ttl_seconds,
    lease_expires_at, acquired_at, heartbeat_at,
    fenced_at, fence_reason, fenced_boot_id::text AS fenced_boot_id
"""

#: Observation read (no row lock): the database's own view of expiry.
_SELECT_ROW = f"""
    SELECT {_ROW_COLUMNS}, (lease_expires_at <= now()) AS expired, now() AS observed_at
    FROM {LOCK_TABLE}
    WHERE server_id = %s AND resource = %s
"""

#: Same read under the row lock — the serialization point of every write.
_SELECT_ROW_FOR_UPDATE = f"""
    SELECT {_ROW_COLUMNS}, (lease_expires_at <= now()) AS expired, now() AS observed_at
    FROM {LOCK_TABLE}
    WHERE server_id = %s AND resource = %s
    FOR UPDATE
"""

_FENCE_UPDATE = f"""
    UPDATE {LOCK_TABLE}
    SET state = '{STATE_FENCED}', fenced_at = now(),
        fence_reason = '{FENCE_REASON_LEASE_EXPIRED}', fenced_boot_id = boot_id
    WHERE server_id = %s AND resource = %s AND state = '{STATE_HELD}'
      AND lease_expires_at <= now()
"""

_HEARTBEAT_UPDATE = f"""
    UPDATE {LOCK_TABLE}
    SET lease_expires_at = now() + (%s * interval '1 second'), heartbeat_at = now()
    WHERE server_id = %s AND resource = %s
"""


def _snapshot_of(row: dict[str, object]) -> LockSnapshot:
    return LockSnapshot(
        server_id=row["server_id"],  # type: ignore[arg-type]
        resource=row["resource"],  # type: ignore[arg-type]
        state=row["state"],  # type: ignore[arg-type]
        owner_token=row["owner_token"],  # type: ignore[arg-type]
        boot_id=row["boot_id"],  # type: ignore[arg-type]
        owner_ref=row["owner_ref"],  # type: ignore[arg-type]
        lease_ttl=row["lease_ttl_seconds"],  # type: ignore[arg-type]
        lease_expires_at=_utc(row["lease_expires_at"]),  # type: ignore[arg-type]
        acquired_at=_utc(row["acquired_at"]),  # type: ignore[arg-type]
        heartbeat_at=_utc(row["heartbeat_at"]),  # type: ignore[arg-type]
        fenced_at=_utc(row["fenced_at"]) if row["fenced_at"] is not None else None,
        fence_reason=row["fence_reason"],  # type: ignore[arg-type]
        fenced_boot_id=row["fenced_boot_id"],  # type: ignore[arg-type]
        observed_at=_utc(row["observed_at"]),  # type: ignore[arg-type]
    )


def _lease_of(row: dict[str, object]) -> LockLease:
    return LockLease(
        server_id=row["server_id"],  # type: ignore[arg-type]
        resource=row["resource"],  # type: ignore[arg-type]
        owner_token=row["owner_token"],  # type: ignore[arg-type]
        boot_id=row["boot_id"],  # type: ignore[arg-type]
        owner_ref=row["owner_ref"],  # type: ignore[arg-type]
        lease_ttl=row["lease_ttl_seconds"],  # type: ignore[arg-type]
        expires_at=_utc(row["lease_expires_at"]),  # type: ignore[arg-type]
        acquired_at=_utc(row["acquired_at"]),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------


class ResourceLockService:
    """Postgres-backed exclusive ``(server_id, resource)`` leases.

    One instance represents one controller boot (:attr:`boot_id`). All
    methods are synchronous, safe to call from any thread/process holding
    the same lease, and fail closed towards fencing: every uncertain
    outcome BLOCKS further conflicting work rather than admitting it.
    """

    def __init__(self, url: str, *, boot_id: str | None = None) -> None:
        self._url = _validate_lock_url(url)
        self._boot_id = (
            _validate_uuid_field(boot_id, "boot_id")
            if boot_id is not None
            else str(uuid.uuid4())
        )
        self._schema_ready = False

    @property
    def boot_id(self) -> str:
        """This controller boot's opaque identity (recorded on leases)."""
        return self._boot_id

    # -- schema ------------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create :data:`LOCK_TABLE` if absent (idempotent, race-safe).

        The table is deliberately out of the Alembic chain (migration
        ``0004_jobs.py`` defers locks to this card); creation happens at
        first use in every process, exactly like the engine-owned
        ``dbos`` schema. Two processes racing the first creation are both
        fine: the loser observes the duplicate-table race and verifies
        the winner's table instead of failing.
        """
        if self._schema_ready:
            return
        try:
            with self._connect() as conn:
                conn.execute(_LOCK_TABLE_DDL)
        except psycopg.errors.DuplicateTable:
            # Lost the concurrent-creation race; the winner's table must exist.
            with self._connect() as conn:
                present = conn.execute(
                    "SELECT to_regclass(%s) IS NOT NULL AS present",
                    (f"public.{LOCK_TABLE}",),
                ).fetchone()
            if not present or not present["present"]:
                _reject(
                    LockErrorCode.UNAVAILABLE,
                    "lock table creation raced and the table is absent",
                )
        self._schema_ready = True

    # -- acquire -----------------------------------------------------------

    def acquire(
        self,
        server_id: str,
        resource: str,
        *,
        owner_ref: str = "",
        ttl: float | None = None,
    ) -> LockLease:
        """Take the exclusive lease or reject it value-free (never wait).

        Grants only against an absent row: a live competing lease raises
        ``HELD``; a lapsed lease is fenced first and raises ``FENCED``; a
        fenced key stays rejected until an explicit reconciliation clears
        it (acceptance 2 — no blind steal).
        """
        server_id = _validate_uuid_field(server_id, "server_id")
        resource = _validate_resource(resource)
        owner_ref = _validate_owner_ref(owner_ref)
        lease_ttl = _validate_ttl(DEFAULT_LEASE_TTL if ttl is None else ttl)
        self.ensure_schema()
        token = str(uuid.uuid4())
        with self._connect() as conn:
            outcome = self._acquire_row(
                conn, server_id, resource, owner_ref, lease_ttl, token
            )
        # The transaction is committed (clean context exit) — including any
        # lazy fence transition observed on the way — before rejections rise.
        if isinstance(outcome, LockLease):
            return outcome
        code, detail = outcome
        raise ResourceLockError(code, detail)

    def _acquire_row(
        self,
        conn: psycopg.Connection,
        server_id: str,
        resource: str,
        owner_ref: str,
        lease_ttl: float,
        token: str,
    ) -> "LockLease | tuple[LockErrorCode, str]":
        for _attempt in range(_ACQUIRE_ATTEMPTS):
            inserted = conn.execute(
                f"""
                INSERT INTO {LOCK_TABLE}
                    (server_id, resource, state, owner_token, boot_id, owner_ref,
                     lease_ttl_seconds, lease_expires_at, acquired_at, heartbeat_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s,
                        now() + (%s * interval '1 second'), now(), now())
                ON CONFLICT (server_id, resource) DO NOTHING
                """,
                (
                    uuid.UUID(server_id),
                    resource,
                    STATE_HELD,
                    uuid.UUID(token),
                    uuid.UUID(self._boot_id),
                    owner_ref,
                    lease_ttl,
                    lease_ttl,
                ),
            ).rowcount
            if inserted:
                row = conn.execute(
                    _SELECT_ROW,
                    (uuid.UUID(server_id), resource),
                ).fetchone()
                assert row is not None  # the row this transaction just inserted
                return _lease_of(row)
            locked = conn.execute(
                _SELECT_ROW_FOR_UPDATE, (uuid.UUID(server_id), resource)
            ).fetchone()
            if locked is None:
                # The conflicting lease was released between the failed
                # insert and the locked read: the key is free again.
                continue
            if locked["state"] == STATE_FENCED:
                return (
                    LockErrorCode.FENCED,
                    "lock is fenced; a reconciliation verdict must clear it first",
                )
            # state == held here (the table CHECK leaves exactly two states).
            if not locked["expired"]:
                return (LockErrorCode.HELD, "lock is held by a live lease")
            conn.execute(_FENCE_UPDATE, (uuid.UUID(server_id), resource))
            return (
                LockErrorCode.FENCED,
                "lease lapsed; the key is fenced until reconciliation clears it",
            )
        return (
            LockErrorCode.HELD,
            "lock contention did not settle within the acquire retry bound",
        )

    def acquire_all(self, requests: Sequence[LockRequest]) -> tuple[LockLease, ...]:
        """Take several leases together, deadlock-free, all-or-nothing.

        Acquisition follows the canonical total order over
        ``(server_id, resource)``; if any acquisition is rejected, the
        leases taken so far are released (reverse order) and the first
        rejection rises. Two callers acquiring overlapping sets in any
        order therefore cannot hold-and-wait each other into a cycle, and
        no partial set survives a failure. Observers may transiently see
        the partial set while the batch is in flight — the order is what
        makes that safe.
        """
        batch = tuple(requests)
        if not batch:
            return ()
        for request in batch:
            if not isinstance(request, LockRequest):
                _reject(
                    LockErrorCode.INVALID_REQUEST, "requests must be LockRequest values"
                )
        validated = tuple(
            LockRequest(
                server_id=_validate_uuid_field(request.server_id, "server_id"),
                resource=_validate_resource(request.resource),
                owner_ref=_validate_owner_ref(request.owner_ref),
                ttl=None if request.ttl is None else _validate_ttl(request.ttl),
            )
            for request in batch
        )
        ordered = sorted(
            validated, key=lambda request: (request.server_id, request.resource)
        )
        for left, right in zip(ordered, ordered[1:]):
            if (left.server_id, left.resource) == (right.server_id, right.resource):
                _reject(
                    LockErrorCode.INVALID_REQUEST,
                    "batch contains the same lock key twice",
                )
        acquired: list[LockLease] = []
        try:
            for request in ordered:
                acquired.append(
                    self.acquire(
                        request.server_id,
                        request.resource,
                        owner_ref=request.owner_ref,
                        ttl=request.ttl,
                    )
                )
        except BaseException:
            for lease in reversed(acquired):
                try:
                    self.release(lease)
                except ResourceLockError:
                    pass  # the first rejection is the outcome; rollback is best-effort
            raise
        return tuple(acquired)

    # -- ownership-checked lease maintenance -------------------------------

    def heartbeat(self, lease: LockLease) -> bool:
        """Renew the lease; ``False`` means it is lost (and fenced if lapsed).

        A lost heartbeat is the caller's signal to stop issuing further
        conflicting work immediately (C04 rule 7): a lapsed lease is
        fenced by this very touch, so the successor path goes through
        reconciliation, never a blind takeover.
        """
        server_id, resource, token = self._lease_identity(lease)
        self.ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                _SELECT_ROW_FOR_UPDATE, (uuid.UUID(server_id), resource)
            ).fetchone()
            if (
                row is None
                or row["owner_token"] != token
                or row["state"] == STATE_FENCED
            ):
                return False
            if row["expired"]:
                conn.execute(_FENCE_UPDATE, (uuid.UUID(server_id), resource))
                return False
            conn.execute(
                _HEARTBEAT_UPDATE, (lease.lease_ttl, uuid.UUID(server_id), resource)
            )
            return True

    def release(self, lease: LockLease) -> None:
        """Release the lease; the owner AND the resource must both match.

        A release after the lease lapsed raises ``LEASE_LOST`` (and
        fences the row — the holder's effect may still be live); a
        release under any other identity raises ``NOT_OWNER`` and leaves
        the live lease untouched.
        """
        server_id, resource, token = self._lease_identity(lease)
        self.ensure_schema()
        with self._connect() as conn:
            outcome = self._release_row(conn, server_id, resource, token)
        if outcome is not None:
            code, detail = outcome
            raise ResourceLockError(code, detail)

    def _release_row(
        self,
        conn: psycopg.Connection,
        server_id: str,
        resource: str,
        token: str,
    ) -> "tuple[LockErrorCode, str] | None":
        row = conn.execute(
            _SELECT_ROW_FOR_UPDATE, (uuid.UUID(server_id), resource)
        ).fetchone()
        if row is None or row["owner_token"] != token:
            return (
                LockErrorCode.NOT_OWNER,
                "no live lease under this identity holds this key",
            )
        if row["state"] == STATE_FENCED:
            return (LockErrorCode.LEASE_LOST, "lease is fenced; it is no longer held")
        if row["expired"]:
            conn.execute(_FENCE_UPDATE, (uuid.UUID(server_id), resource))
            return (LockErrorCode.LEASE_LOST, "lease lapsed before release")
        conn.execute(
            f"DELETE FROM {LOCK_TABLE} WHERE server_id = %s AND resource = %s",
            (uuid.UUID(server_id), resource),
        )
        return None

    # -- observation -------------------------------------------------------

    def inspect(self, server_id: str, resource: str) -> LockSnapshot | None:
        """Read one lock row without touching it (``None``: nothing held)."""
        server_id = _validate_uuid_field(server_id, "server_id")
        resource = _validate_resource(resource)
        self.ensure_schema()
        with self._connect() as conn:
            row = conn.execute(_SELECT_ROW, (uuid.UUID(server_id), resource)).fetchone()
        return _snapshot_of(row) if row is not None else None

    # -- reconciliation gate (acceptance 2) ---------------------------------

    def clear_fence(
        self,
        server_id: str,
        resource: str,
        *,
        verdict: ReconciliationClass,
        note: str,
    ) -> LockSnapshot:
        """Release a fence under a host-verified terminal reconciliation.

        Only the frozen terminal verdicts
        (:data:`CLEARABLE_VERDICTS` — the prior effect provably ended)
        admit a successor; ``running``/``uncertain`` verdicts raise
        ``FENCE_UNRESOLVED`` and leave the fence up (C04 rule 7). The
        returned snapshot is the fenced row as it stood (evidence); the
        key becomes free for a fresh acquisition afterwards. The note is
        required and validated here but is NOT persisted by this module:
        the audit trail belongs to the reconciliation operation that
        produced the verdict (``04-reconcile`` records).
        """
        server_id = _validate_uuid_field(server_id, "server_id")
        resource = _validate_resource(resource)
        _validate_note(note)
        if not isinstance(verdict, ReconciliationClass):
            _reject(
                LockErrorCode.INVALID_REQUEST,
                "verdict must be a ReconciliationClass value",
            )
        if not fence_may_be_cleared(verdict):
            _reject(
                LockErrorCode.FENCE_UNRESOLVED,
                "verdict does not prove the prior conflicting effect is terminal",
            )
        self.ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                _SELECT_ROW_FOR_UPDATE, (uuid.UUID(server_id), resource)
            ).fetchone()
            if row is None or row["state"] != STATE_FENCED:
                outcome = (
                    LockErrorCode.NOT_FENCED,
                    "no fenced row exists under this key",
                )
            else:
                snapshot = _snapshot_of(row)
                conn.execute(
                    f"DELETE FROM {LOCK_TABLE} WHERE server_id = %s AND resource = %s",
                    (uuid.UUID(server_id), resource),
                )
                outcome = snapshot
        if isinstance(outcome, tuple):
            code, detail = outcome
            raise ResourceLockError(code, detail)
        return outcome

    # -- internals ----------------------------------------------------------

    def _connect(self) -> psycopg.Connection:
        try:
            return psycopg.connect(
                self._url, connect_timeout=_CONNECT_TIMEOUT_S, row_factory=dict_row
            )
        except psycopg.OperationalError as exc:
            raise ResourceLockError(
                LockErrorCode.UNAVAILABLE, "database is unreachable"
            ) from exc

    def _lease_identity(self, lease: object) -> tuple[str, str, str]:
        if not isinstance(lease, LockLease):
            _reject(LockErrorCode.INVALID_REQUEST, "lease must be a LockLease value")
        return (
            _validate_uuid_field(lease.server_id, "server_id"),  # type: ignore[arg-type]
            _validate_resource(lease.resource),  # type: ignore[arg-type]
            _validate_uuid_field(lease.owner_token, "owner_token"),  # type: ignore[arg-type]
        )
