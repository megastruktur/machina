"""Machine ingest-principal credential store (auth ADR §13 addendum).

Sibling of ``auth/sessions.py``: this module owns the machine-credential
lifecycle for the notification-inbox ingest lane (mint / validate /
revoke / list + the durable per-key rate window). It never touches admin
sessions, cookies, CSRF/origin, or the G1/G2 gates — the HTTP boundary
(Bearer header parsing, the 401/413/422/429 collapse, G1 setup state)
belongs to the ingest API task; this module exposes no HTTP concepts.

Frozen behavior implemented here (auth-and-origin.md §13, notification-
inbox.md §2/§6):

- Tokens reuse the audited admin-session primitives: ``new_token``
  (256-bit URL-safe CSPRNG) and ``hash_token`` (SHA-256 hex) are
  imported from ``mai.auth.sessions``, never reimplemented.
- Mint returns the plaintext key EXACTLY ONCE; only ``key_hash``
  (SHA-256 hex, CHAR(64) UNIQUE) is persisted.
- ``validate`` (structured aliases: ``validate_and_consume``,
  ``consume_rate_window``) re-checks hash match, ``revoked_at IS NULL``
  and ``expires_at`` and — in ONE atomic statement — stamps
  ``last_used_at`` and consumes the durable fixed rate window. The
  window is DB-backed (``rate_window_start`` / ``rate_window_count``),
  so process restart cannot reset the count (restart-safe).
- The fixed window spans ``INGEST_RATE_WINDOW_SPAN`` (60 s); the budget
  per span is ``INGEST_RATE_WINDOW`` = 60 requests/minute (imported from
  ``mai.contracts.observations``). Every counted request increments the
  open window — created, deduped, 413 and 422 alike at the route layer —
  and the request that exceeds the budget is itself counted, then
  refused with ``rate_window_exhausted``. The first request after the
  span opens a fresh window (count restarts at 1).
- ``consume_rate_window`` names the route-level counting entry for the
  §13 rule-3 shape: an over-budget request answers ``413
  body_too_large`` regardless of key validity, writes no row, and STILL
  counts against a valid key's window — the route calls this after the
  budget pre-check, before answering.
- ``revoke`` is one conditional UPDATE (``revoked_at = now`` where not
  yet revoked); effective on the next validate. ``list_keys`` is a
  bounded page (limit/offset + total + has_more, deterministic
  ``created_at DESC, key_id DESC`` ordering) carrying metadata only —
  never key material.
- Value-free typed errors (``IngestKeyError``/``IngestKeyErrorCode``):
  every ``detail`` is a constant string; plaintext keys and key hashes
  are never interpolated into exceptions, reprs, or return payloads.
  The store MAY distinguish unknown/revoked/expired internally, but the
  HTTP route collapses them into one value-free ``401`` (no existence
  oracle at the API layer).

Schema owned by ``migrations/versions/0017_notification_inbox.py``
(exclusive migration-chain resource).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Final

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.auth.sessions import hash_token, new_token
from mai.contracts.observations import INGEST_RATE_WINDOW

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "IngestKey",
    "IngestKeyError",
    "IngestKeyErrorCode",
    "IngestKeyPage",
    "IngestKeyStore",
    "IssuedIngestKey",
    "LABEL_MAX_CHARS",
    "MAX_PAGE_LIMIT",
    "INGEST_RATE_WINDOW_SPAN",
]

#: Fixed rate-window span: the durable per-key window is 60 seconds long
#: (auth ADR §13 «fixed rate window»); the budget per span is the frozen
#: ``INGEST_RATE_WINDOW`` = 60 requests/minute from the contracts module.
INGEST_RATE_WINDOW_SPAN: Final[timedelta] = timedelta(seconds=60)

#: Label bound, mirroring ``ck_ingest_keys_label`` (0017): 1..64 chars
#: after whitespace trim (the DB CHECK evaluates ``btrim(label)``).
LABEL_MAX_CHARS: Final[int] = 64

#: Bounded page defaults (repo convention: api/jobs.py, api/monitoring.py).
DEFAULT_PAGE_LIMIT: Final[int] = 50
MAX_PAGE_LIMIT: Final[int] = 200


class IngestKeyErrorCode(StrEnum):
    """Stable machine-readable rejection codes (value-free, C01 vocabulary).

    The ingest route collapses ``key_unknown``/``key_revoked``/``key_expired``
    into one value-free ``401`` — these distinctions exist for the store and
    its tests, never for API responses (no existence oracle).
    """

    INVALID_KEY = "invalid_key"
    INVALID_LABEL = "invalid_label"
    INVALID_SERVER_SCOPE = "invalid_server_scope"
    INVALID_EXPIRY = "invalid_expiry"
    KEY_UNKNOWN = "key_unknown"
    KEY_REVOKED = "key_revoked"
    KEY_EXPIRED = "key_expired"
    KEY_CONFLICT = "key_conflict"
    RATE_WINDOW_EXHAUSTED = "rate_window_exhausted"


class IngestKeyError(ValueError):
    """One structured, secret-free ingest-key rejection.

    ``detail`` is always a constant string: the plaintext key and the key
    hash are never interpolated (secret hygiene, auth ADR §13 rule 4).
    """

    def __init__(self, code: IngestKeyErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Input validation (value-free; no SQL effect before success)
# ---------------------------------------------------------------------------


def _validated_key_material(key: object) -> str:
    if not isinstance(key, str) or not key:
        raise IngestKeyError(
            IngestKeyErrorCode.INVALID_KEY, "ingest credential rejected"
        )
    return key


def _validated_label(label: object) -> str:
    if not isinstance(label, str):
        raise IngestKeyError(
            IngestKeyErrorCode.INVALID_LABEL, "label rejected before insert"
        )
    trimmed = label.strip()
    if not 1 <= len(trimmed) <= LABEL_MAX_CHARS:
        raise IngestKeyError(
            IngestKeyErrorCode.INVALID_LABEL, "label rejected before insert"
        )
    return trimmed


def _validated_scope(server_id: object) -> uuid.UUID | None:
    if server_id is None:
        return None
    if isinstance(server_id, uuid.UUID):
        return server_id
    if isinstance(server_id, str):
        try:
            return uuid.UUID(server_id)
        except ValueError as orig:
            raise IngestKeyError(
                IngestKeyErrorCode.INVALID_SERVER_SCOPE, "server scope rejected"
            ) from orig
    raise IngestKeyError(
        IngestKeyErrorCode.INVALID_SERVER_SCOPE, "server scope rejected"
    )


def _validated_expiry(expires_at: object) -> datetime | None:
    if expires_at is None:
        return None
    if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
        raise IngestKeyError(
            IngestKeyErrorCode.INVALID_EXPIRY, "expiry must be timezone-aware"
        )
    return expires_at


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Dataclasses (frozen shapes; metadata only — key_hash is never carried)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IngestKey:
    """One ingest-key metadata record (0017 frozen columns, no material)."""

    key_id: uuid.UUID
    label: str
    server_id: uuid.UUID | None  # None = global-only scope
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    rate_window_start: datetime | None
    rate_window_count: int


@dataclass(frozen=True, slots=True)
class IssuedIngestKey:
    """Mint outcome: the plaintext key (returned exactly once) + record."""

    key: str
    record: IngestKey


@dataclass(frozen=True, slots=True)
class IngestKeyPage:
    """One bounded, deterministic list page (AGENTS pagination invariant)."""

    items: tuple[IngestKey, ...]
    limit: int
    offset: int
    total: int
    has_more: bool


# ---------------------------------------------------------------------------
# SQL statements (schema owned by migrations/versions/0017_notification_inbox.py)
# ---------------------------------------------------------------------------

_MINT_KEY = text(
    "INSERT INTO ingest_keys "
    "(key_id, key_hash, label, server_id, created_at, expires_at) "
    "VALUES (:key_id, :key_hash, :label, :server_id, :created_at, :expires_at) "
    "RETURNING key_id, label, server_id, created_at, last_used_at, expires_at, "
    "revoked_at, rate_window_start, rate_window_count"
)

#: Validation + touch + window consumption in ONE statement: the fixed
#: window reopens (count restarts at 1) when no window is open or the open
#: window's span has elapsed; otherwise the open window absorbs the
#: request. A concurrent revocation/expiry either lands before (row
#: rejected) or is visible to the next request — no grace window.
_CONSUME_WINDOW = text(
    "UPDATE ingest_keys SET "
    "last_used_at = :now, "
    "rate_window_start = CASE WHEN rate_window_start IS NULL "
    "  OR rate_window_start <= :window_floor THEN :now "
    "  ELSE rate_window_start END, "
    "rate_window_count = CASE WHEN rate_window_start IS NULL "
    "  OR rate_window_start <= :window_floor THEN 1 "
    "  ELSE rate_window_count + 1 END "
    "WHERE key_hash = :key_hash AND revoked_at IS NULL "
    "AND (expires_at IS NULL OR expires_at > :now) "
    "RETURNING key_id, label, server_id, created_at, last_used_at, "
    "expires_at, revoked_at, rate_window_start, rate_window_count"
)

_SELECT_KEY_STATE = text(
    "SELECT revoked_at, expires_at FROM ingest_keys WHERE key_hash = :key_hash"
)

_REVOKE_KEY = text(
    "UPDATE ingest_keys SET revoked_at = :now "
    "WHERE key_id = :key_id AND revoked_at IS NULL"
)

_LIST_PAGE = text(
    "SELECT key_id, label, server_id, created_at, last_used_at, expires_at, "
    "revoked_at, rate_window_start, rate_window_count "
    "FROM ingest_keys "
    "ORDER BY created_at DESC, key_id DESC "
    "LIMIT :limit OFFSET :offset"
)

_COUNT_KEYS = text("SELECT count(*) FROM ingest_keys")

_KEY_COLUMNS: Final[tuple[str, ...]] = (
    "key_id",
    "label",
    "server_id",
    "created_at",
    "last_used_at",
    "expires_at",
    "revoked_at",
    "rate_window_start",
    "rate_window_count",
)


def _ingest_key_of(row: Sequence[object] | object) -> IngestKey:
    """Materialize one ``ingest_keys`` row into the frozen dataclass."""
    mapping = row._mapping  # noqa: SLF001 - core Row mapping access
    return IngestKey(**{column: mapping[column] for column in _KEY_COLUMNS})


# ---------------------------------------------------------------------------
# Key store
# ---------------------------------------------------------------------------


class IngestKeyStore:
    """Machine ingest-principal semantics over one application sessionmaker."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def mint(
        self,
        label: str,
        server_id: uuid.UUID | None = None,
        expires_at: datetime | None = None,
    ) -> IssuedIngestKey:
        """Mint one key: plaintext returned exactly once, hash persisted.

        ``server_id=None`` mints a GLOBAL key (may only append server-less
        notices); a set value pins the key to that server. Inputs are
        validated before any SQL effect; a 256-bit hash collision surfaces
        as ``key_conflict`` instead of a raw driver error.
        """
        trimmed_label = _validated_label(label)
        scope = _validated_scope(server_id)
        expiry = _validated_expiry(expires_at)
        now = _utcnow()
        key = new_token()
        key_hash = hash_token(key)
        async with self._sessions() as session, session.begin():
            try:
                row = (
                    await session.execute(
                        _MINT_KEY,
                        {
                            "key_id": uuid.uuid4(),
                            "key_hash": key_hash,
                            "label": trimmed_label,
                            "server_id": scope,
                            "created_at": now,
                            "expires_at": expiry,
                        },
                    )
                ).one()
            except IntegrityError as orig:
                raise IngestKeyError(
                    IngestKeyErrorCode.KEY_CONFLICT, "ingest key hash collision"
                ) from orig
        return IssuedIngestKey(key=key, record=_ingest_key_of(row))

    async def validate(self, key: str) -> IngestKey:
        """Validate ``key`` and consume one window slot (§13 lifecycle).

        One atomic statement re-checks revocation and expiry, stamps
        ``last_used_at`` and counts the request against the durable fixed
        window; the refusing over-budget request is itself counted, then
        ``rate_window_exhausted`` is raised. Unknown/revoked/expired keys
        are rejected with value-free codes (the API route collapses them).
        """
        return await self._consume_one(key)

    async def consume_rate_window(self, key: str) -> IngestKey:
        """Count one request against a valid key's window (§13 rule 3).

        Identical atomic semantics to :meth:`validate`; the ingest route
        calls this on the over-budget path — after the ``413
        body_too_large`` pre-check, before answering — so the request is
        counted even though no payload row is written and the 413 outcome
        is decided independently of key validity.
        """
        return await self._consume_one(key)

    async def validate_and_consume(self, key: str) -> IngestKey:
        """Structured combined entry: validate + count in one statement.

        The canonical ingest-path call for the §13 rule-3 ordering (budget
        check → count → outcome) without a second round trip.
        """
        return await self._consume_one(key)

    async def _consume_one(self, key: str) -> IngestKey:
        key_hash = hash_token(_validated_key_material(key))
        now = _utcnow()
        async with self._sessions() as session, session.begin():
            record = (
                await session.execute(
                    _CONSUME_WINDOW,
                    {
                        "key_hash": key_hash,
                        "now": now,
                        "window_floor": now - INGEST_RATE_WINDOW_SPAN,
                    },
                )
            ).first()
        if record is not None:
            validated = _ingest_key_of(record)
            if validated.rate_window_count > INGEST_RATE_WINDOW:
                raise IngestKeyError(
                    IngestKeyErrorCode.RATE_WINDOW_EXHAUSTED,
                    "ingest rate window exhausted for this key",
                )
            return validated
        async with self._sessions() as session:
            state = (
                await session.execute(_SELECT_KEY_STATE, {"key_hash": key_hash})
            ).first()
        if state is None:
            raise IngestKeyError(
                IngestKeyErrorCode.KEY_UNKNOWN, "no ingest key record for credential"
            )
        if state[0] is not None:
            raise IngestKeyError(
                IngestKeyErrorCode.KEY_REVOKED, "ingest key is revoked"
            )
        if state[1] is not None and state[1] <= now:
            raise IngestKeyError(
                IngestKeyErrorCode.KEY_EXPIRED, "ingest key is expired"
            )
        # Fail closed: the consuming UPDATE missed a row that now reads as
        # valid (concurrent mutation between the two statements).
        raise IngestKeyError(
            IngestKeyErrorCode.KEY_UNKNOWN, "ingest credential state unsettled"
        )

    async def revoke(self, key_id: uuid.UUID) -> bool:
        """Revoke one key: one conditional UPDATE, effective next validate.

        Idempotent: ``False`` when the key was already revoked or does not
        exist. The audit row stays; reactivation never happens.
        """
        if not isinstance(key_id, uuid.UUID):
            raise ValueError("key_id must be a UUID")
        now = _utcnow()
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                _REVOKE_KEY, {"key_id": key_id, "now": now}
            )
        return bool(result.rowcount)

    async def list_keys(
        self, limit: int = DEFAULT_PAGE_LIMIT, offset: int = 0
    ) -> IngestKeyPage:
        """One bounded metadata page, ``created_at DESC, key_id DESC``.

        Carries label/scope/timestamps only — never ``key_hash`` or any
        plaintext material (secret hygiene, auth ADR §13 rule 4).
        """
        if not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_LIMIT:
            raise ValueError(
                f"page limit out of bounds (1..{MAX_PAGE_LIMIT})"
            )
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("page offset must be a non-negative integer")
        async with self._sessions() as session, session.begin():
            rows = (
                await session.execute(
                    _LIST_PAGE, {"limit": limit, "offset": offset}
                )
            ).all()
            total = int((await session.execute(_COUNT_KEYS)).scalar_one())
        items = tuple(_ingest_key_of(row) for row in rows)
        return IngestKeyPage(
            items=items,
            limit=limit,
            offset=offset,
            total=total,
            has_more=offset + len(items) < total,
        )
