"""Admin credential verification and revocable sessions (C08 rule 1).

Ownership (auth ADR ``3e79449`` §10): this module owns the administrator
credential hash (§3) and the server-side session record semantics (§4).
The HTTP boundary (cookies, CSRF/origin, throttling, G1 routing) belongs to
``02-auth-api``; this module exposes no HTTP concepts.

Frozen behavior implemented here:

- Argon2id credential hashing (§3.1): minimum accepted parameters 64 MiB
  memory, 3 iterations, parallelism 1, 16-byte random salt — this module
  pins exactly those parameters (hash length 32 exceeds nothing required).
  Verification is constant-time inside the argon2 binding.
- Bootstrap is one-shot (§3): the first credential record is stored; any
  further bootstrap attempt is rejected (``credential_present``). With no
  record, verification reports ``credential_missing`` (API setup state, G1).
- Sessions (§4): opaque 256-bit CSPRNG token; **only** ``SHA-256(token)`` is
  persisted (a database leak yields no usable cookies); absolute expiry
  created + 30 days; sliding idle expiry last_seen + 24 h refreshed on
  every authenticated use; revocation effective on the next request with no
  grace window; rows are retained after revocation and never reactivated;
  at most 16 live sessions, the oldest by ``last_seen_at`` evicted (as a
  revocation, so the audit row survives); a fresh token is minted at every
  successful login.

Secret hygiene (acceptance 1): the plaintext password and the opaque token
exist only as call arguments / return values. This module performs no
logging, never interpolates them into exceptions (all rejection details are
constant strings), and stores only the Argon2id PHC string and the SHA-256
token digest. Nothing here reads the environment; bootstrap-source
resolution (``MAI_ADMIN_PASSWORD[_FILE]``) is the caller's concern.

Schema owned by ``migrations/versions/0002_sessions.py`` (chain position
after ``0001``).

"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "AdminSession",
    "AuthError",
    "AuthErrorCode",
    "IssuedSession",
    "MAX_LIVE_SESSIONS",
    "SESSION_ABSOLUTE_TTL",
    "SESSION_IDLE_TTL",
    "SessionStore",
    "hash_password",
    "hash_token",
    "new_token",
    "verify_password",
]

#: Frozen session timeouts (auth ADR §4): sliding idle window and absolute
#: lifetime, both server-enforced.
SESSION_IDLE_TTL: Final[timedelta] = timedelta(hours=24)
SESSION_ABSOLUTE_TTL: Final[timedelta] = timedelta(days=30)

#: Frozen live-session cap (ADR §4): admin desktop + laptop + phone headroom.
MAX_LIVE_SESSIONS: Final[int] = 16

#: 256 bits of CSPRNG entropy per token (ADR §4).
_TOKEN_BYTES: Final[int] = 32

#: Argon2id parameters (ADR §3.1 minimum accepted, pinned exactly).
_ARGON2_TIME_COST: Final[int] = 3
_ARGON2_MEMORY_COST: Final[int] = 64 * 1024  # KiB → 64 MiB
_ARGON2_PARALLELISM: Final[int] = 1
_ARGON2_SALT_BYTES: Final[int] = 16
_ARGON2_HASH_BYTES: Final[int] = 32

#: Input bounds (DoS bound, not a password policy — no complexity rules are
#: invented here). Passwords/token types are rejected before any SQL effect.
_MAX_PASSWORD_CHARS: Final[int] = 1024

#: Transaction-scoped advisory lock serializing login so the ≤16 live cap
#: holds under concurrent logins (insert + evict is linearized). The value
#: is an arbitrary constant; only its stability matters.
_SESSION_CAP_LOCK_KEY: Final[int] = 0x6D41_495F_4155_5448


class AuthErrorCode(StrEnum):
    """Stable machine-readable rejection codes (value-free, C01 vocabulary)."""

    INVALID_PASSWORD = "invalid_password"
    INVALID_TOKEN = "invalid_token"
    CREDENTIAL_MISSING = "credential_missing"
    CREDENTIAL_PRESENT = "credential_present"
    CREDENTIAL_CORRUPTED = "credential_corrupted"
    SESSION_MISSING = "session_missing"
    SESSION_EXPIRED = "session_expired"
    SESSION_REVOKED = "session_revoked"
    TOKEN_CONFLICT = "token_conflict"


class AuthError(ValueError):
    """One structured, secret-free auth rejection.

    ``detail`` is always a constant string: passwords and tokens are never
    interpolated (acceptance 1).
    """

    def __init__(self, code: AuthErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


_HASHER: Final[PasswordHasher] = PasswordHasher(
    time_cost=_ARGON2_TIME_COST,
    memory_cost=_ARGON2_MEMORY_COST,
    parallelism=_ARGON2_PARALLELISM,
    salt_len=_ARGON2_SALT_BYTES,
    hash_len=_ARGON2_HASH_BYTES,
)


# ---------------------------------------------------------------------------
# Input validation (value-free; no SQL effect before success)
# ---------------------------------------------------------------------------


def _validated_password(password: object) -> str:
    if not isinstance(password, str) or not 1 <= len(password) <= _MAX_PASSWORD_CHARS:
        raise AuthError(
            AuthErrorCode.INVALID_PASSWORD, "password rejected before hashing"
        )
    return password


def _validated_token(token: object) -> str:
    if not isinstance(token, str) or not token:
        raise AuthError(AuthErrorCode.INVALID_TOKEN, "session token rejected")
    return token


def _validated_password_hash(password_hash: object) -> str:
    if not isinstance(password_hash, str) or not password_hash:
        raise AuthError(AuthErrorCode.CREDENTIAL_CORRUPTED, "stored hash unreadable")
    return password_hash


# ---------------------------------------------------------------------------
# Credential hashing (ADR §3.1) and token primitives (ADR §4)
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Return the Argon2id PHC string for ``password`` (never the reverse)."""
    return _HASHER.hash(_validated_password(password))


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time Argon2id verification; ``False`` on mismatch."""
    checked_hash = _validated_password_hash(password_hash)
    _validated_password(password)
    try:
        _HASHER.verify(checked_hash, password)
    except VerifyMismatchError:
        return False
    except (VerificationError, InvalidHashError) as orig:
        # Corrupt/foreign hash formats fail closed (observed: argon2-cffi 25
        # surfaces undecodable hashes as VerificationError "Decoding failed").
        raise AuthError(
            AuthErrorCode.CREDENTIAL_CORRUPTED, "stored hash is not argon2id"
        ) from orig
    return True


def new_token() -> str:
    """Mint one opaque 256-bit URL-safe session token (ADR §4)."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(token: str) -> str:
    """Return ``SHA-256(token)`` hex — the only persisted token form."""
    return hashlib.sha256(_validated_token(token).encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Dataclasses (frozen shapes)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdminSession:
    """One server-side session record (ADR §4 frozen columns)."""

    token_hash: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    idle_expires_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """Login outcome: the plaintext token (return value only) + its record."""

    token: str
    session: AdminSession


# ---------------------------------------------------------------------------
# SQL statements (schema owned by migrations/versions/0002_sessions.py)
# ---------------------------------------------------------------------------

_INSERT_CREDENTIAL = text(
    "INSERT INTO admin_credential (id, password_hash) VALUES (1, :password_hash) "
    "ON CONFLICT (id) DO NOTHING RETURNING id"
)

_SELECT_CREDENTIAL_HASH = text(
    "SELECT password_hash FROM admin_credential WHERE id = 1"
)

_CREDENTIAL_EXISTS = text("SELECT 1 FROM admin_credential WHERE id = 1")

_INSERT_SESSION = text(
    "INSERT INTO admin_sessions "
    "(token_hash, created_at, last_seen_at, expires_at, idle_expires_at) "
    "VALUES (:token_hash, :created_at, :last_seen_at, :expires_at, :idle_expires_at)"
)

#: ADR §4 core: every authenticated use re-validates the record and slides
#: the idle window in ONE statement — validation and refresh share a
#: transaction boundary; a concurrent revocation either lands before (row
#: rejected) or is visible to the next request (no grace window).
_TOUCH_LIVE_SESSION = text(
    "UPDATE admin_sessions SET last_seen_at = :now, idle_expires_at = :idle "
    "WHERE token_hash = :token_hash AND revoked_at IS NULL "
    "AND expires_at > :now AND idle_expires_at > :now "
    "RETURNING token_hash, created_at, last_seen_at, expires_at, "
    "idle_expires_at, revoked_at"
)

_SELECT_SESSION_STATE = text(
    "SELECT revoked_at, expires_at, idle_expires_at FROM admin_sessions "
    "WHERE token_hash = :token_hash"
)

_REVOKE_SESSION = text(
    "UPDATE admin_sessions SET revoked_at = :now "
    "WHERE token_hash = :token_hash AND revoked_at IS NULL"
)

_REVOKE_ALL_SESSIONS = text(
    "UPDATE admin_sessions SET revoked_at = :now WHERE revoked_at IS NULL"
)

#: Evict (revoke) live sessions beyond the frozen cap: order newest-first
#: and revoke everything after the newest ``keep`` rows — the oldest
#: ``last_seen_at`` goes first; the just-inserted row participates, so the
#: post-condition is ≤ MAX_LIVE_SESSIONS live rows per login.
_EVICT_BEYOND_CAP = text(
    "UPDATE admin_sessions SET revoked_at = :now WHERE revoked_at IS NULL "
    "AND token_hash IN (SELECT token_hash FROM admin_sessions "
    "WHERE revoked_at IS NULL AND expires_at > :now AND idle_expires_at > :now "
    "ORDER BY last_seen_at DESC, created_at DESC OFFSET :keep)"
)

_ADVISORY_LOCK = text("SELECT pg_advisory_xact_lock(:key)")

_SESSION_COLUMNS: Final[tuple[str, ...]] = (
    "token_hash",
    "created_at",
    "last_seen_at",
    "expires_at",
    "idle_expires_at",
    "revoked_at",
)


def _session_of(row: Sequence[object] | object) -> AdminSession:
    """Materialize one ``admin_sessions`` row into the frozen dataclass."""
    mapping = row._mapping  # noqa: SLF001 - core Row mapping access
    return AdminSession(
        **{column: mapping[column] for column in _SESSION_COLUMNS}
    )


# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------


class SessionStore:
    """Credential + session semantics over one application sessionmaker."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    # -- credential (ADR §3) ------------------------------------------------

    async def credential_exists(self) -> bool:
        """Whether the one-shot bootstrap already happened."""
        async with self._sessions() as session:
            row = await session.execute(_CREDENTIAL_EXISTS)
            return row.first() is not None

    async def bootstrap_credential(self, password: str) -> None:
        """Store the Argon2id hash once; later attempts are rejected (§3).

        The API maps :attr:`AuthErrorCode.CREDENTIAL_PRESENT` to a permanent
        ``409``; this module only refuses to touch the existing record.
        """
        password_hash = hash_password(password)
        async with self._sessions() as session, session.begin():
            inserted = await session.execute(
                _INSERT_CREDENTIAL, {"password_hash": password_hash}
            )
            if inserted.first() is None:
                raise AuthError(
                    AuthErrorCode.CREDENTIAL_PRESENT,
                    "administrator credential record already exists",
                )

    async def verify_credential(self, password: str) -> bool:
        """Constant-time verification against the stored hash (§3).

        Raises :class:`AuthError` with ``credential_missing`` in setup state
        (no record yet); a wrong password is ``False``, not an exception.
        """
        _validated_password(password)
        async with self._sessions() as session:
            row = (await session.execute(_SELECT_CREDENTIAL_HASH)).first()
        if row is None:
            raise AuthError(
                AuthErrorCode.CREDENTIAL_MISSING,
                "no administrator credential record exists",
            )
        return verify_password(row[0], password)

    # -- session lifecycle (ADR §4) -----------------------------------------

    async def login(self, password: str) -> IssuedSession | None:
        """Verify the credential and mint a fresh session (§4).

        Returns ``None`` for a wrong password (no row is created; the API
        owns throttling and response-latency parity). A fresh 256-bit token
        is minted per successful login and only its SHA-256 is stored.
        """
        _validated_password(password)
        now = _utcnow()
        async with self._sessions() as session, session.begin():
            await session.execute(_ADVISORY_LOCK, {"key": _SESSION_CAP_LOCK_KEY})
            stored_hash = (await session.execute(_SELECT_CREDENTIAL_HASH)).first()
            if stored_hash is None:
                raise AuthError(
                    AuthErrorCode.CREDENTIAL_MISSING,
                    "no administrator credential record exists",
                )
            if not verify_password(stored_hash[0], password):
                return None
            token = new_token()
            token_hash = hash_token(token)
            try:
                await session.execute(
                    _INSERT_SESSION,
                    {
                        "token_hash": token_hash,
                        "created_at": now,
                        "last_seen_at": now,
                        "expires_at": now + SESSION_ABSOLUTE_TTL,
                        "idle_expires_at": now + SESSION_IDLE_TTL,
                    },
                )
            except IntegrityError as orig:
                # 256-bit CSPRNG collision: not reachable in practice, but a
                # PK violation must never surface as a raw driver error.
                raise AuthError(
                    AuthErrorCode.TOKEN_CONFLICT, "session token collision"
                ) from orig
            await session.execute(
                _EVICT_BEYOND_CAP, {"now": now, "keep": MAX_LIVE_SESSIONS}
            )
        return IssuedSession(
            token=token,
            session=AdminSession(
                token_hash=token_hash,
                created_at=now,
                last_seen_at=now,
                expires_at=now + SESSION_ABSOLUTE_TTL,
                idle_expires_at=now + SESSION_IDLE_TTL,
                revoked_at=None,
            ),
        )

    async def validate(self, token: str) -> AdminSession:
        """Validate ``token`` and slide its idle window (§4).

        One atomic statement re-checks revocation and both expiries and
        refreshes ``last_seen_at``/``idle_expires_at``; expired/revoked
        tokens are rejected with a value-free code.
        """
        token_hash = hash_token(token)
        now = _utcnow()
        async with self._sessions() as session, session.begin():
            # The touch is a WRITE: it must commit, or the sliding idle
            # window would silently stay at login time (rollback on close).
            live = (
                await session.execute(
                    _TOUCH_LIVE_SESSION,
                    {
                        "token_hash": token_hash,
                        "now": now,
                        "idle": now + SESSION_IDLE_TTL,
                    },
                )
            ).first()
            if live is not None:
                return _session_of(live)
        async with self._sessions() as session:
            state = (
                await session.execute(
                    _SELECT_SESSION_STATE, {"token_hash": token_hash}
                )
            ).first()
        if state is None:
            raise AuthError(
                AuthErrorCode.SESSION_MISSING, "no session record for token"
            )
        if state[0] is not None:
            raise AuthError(AuthErrorCode.SESSION_REVOKED, "session is revoked")
        raise AuthError(AuthErrorCode.SESSION_EXPIRED, "session is expired")

    async def revoke(self, token: str) -> bool:
        """Revoke exactly the presenting session (logout, §4).

        Idempotent: ``False`` when the session was already revoked or does
        not exist. Effective on the next request; the audit row stays.
        """
        token_hash = hash_token(token)
        now = _utcnow()
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                _REVOKE_SESSION, {"token_hash": token_hash, "now": now}
            )
            return result.rowcount > 0

    async def revoke_all(self) -> int:
        """Revoke every live session (admin action, §4); returns the count."""
        now = _utcnow()
        async with self._sessions() as session, session.begin():
            result = await session.execute(_REVOKE_ALL_SESSIONS, {"now": now})
            return result.rowcount
