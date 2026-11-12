"""Session-first transactional store: bounded queries, one sequence writer.

Campaign ``machina-session-first-chat`` D02; schema ``0014`` (sfc_PLAN.md
D02, sfc_RESEARCH §8, D01 contracts in :mod:`mai.contracts.sessions`).
This is the single shared store used by the HTTP admission surface (D06)
and the worker (D04) — the only writer of the session-first tables.

Governing rules:

- **One aggregate writer** — every append takes the session row lock
  (``SELECT ... FOR UPDATE``) and allocates the next per-session ``seq``
  (``COALESCE(MAX(seq), 0) + 1``) inside the SAME transaction, so
  concurrent writers serialize per session, sequences stay unique and
  contiguous, and an aborted transaction releases its allocated number: no
  gap is ever claimed as committed history.
- **Idempotent durable sources** — a repeated ``(run_id, durable_source_id,
  kind)`` returns the committed entry (schema UNIQUE ``0014``); the
  pre-check runs under the same row lock, so replays never race. The
  schema-level key applies to run-bound entries (PostgreSQL treats NULLs
  as distinct in unique indexes; see the migration docstring).
- **Scope blindness** — every function takes the explicit ``server_id`` and
  fails closed with the D01 ``session_not_found`` code for BOTH a missing
  session and a cross-server session (C01 rule 7; never leak existence).
- **Bounded reads** — the initial tail is at most
  :data:`INITIAL_TAIL_MAX_ENTRIES` entries, keyset pages at most
  :data:`HISTORY_PAGE_MAX_ENTRIES`, and every returned page is serialized
  and byte-checked against :data:`MAX_PAGE_BYTES` (drop oldest items +
  honest ``truncated_by_bytes``). ``total`` always comes from a ``COUNT(*)``
  under the same predicate; continuation flags come from ``EXISTS`` probes —
  never from materializing the history.
- **Value-free typed errors** — failures raise :class:`SessionContractError`
  with stable :class:`SessionErrorCode` codes; exception texts carry field
  paths and fixed reasons only.

The store is async (SQLAlchemy async, psycopg3) and pure with respect to
process environment/network beyond the supplied sessionmaker: no env, no
clock reads (timestamps come from the database ``now()``).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Final

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.contracts.session_channel import CHANNEL_NOTIFY_CHANNEL
from mai.contracts.sessions import (
    HISTORY_PAGE_MAX_ENTRIES,
    INITIAL_TAIL_MAX_ENTRIES,
    MAX_ENTRY_TEXT_CHARS,
    MAX_PAGE_BYTES,
    SESSION_LIST_DEFAULT_LIMIT,
    SESSION_LIST_MAX_LIMIT,
    CompactionEntry,
    EntryKind,
    RunStateEntry,
    SessionContractError,
    SessionEntryView,
    SessionErrorCode,
    SessionFieldError,
    SessionHistoryPage,
    SessionRunState,
    SessionSourceKind,
    SessionStatus,
    assert_session_not_run,
    assert_session_scope,
)
from mai.tag_store import (
    EntityTagPreview,
    RESERVED_UNCLASSIFIED_TAG_ID,
    SYSTEM_ASSIGNMENT_SOURCE,
    TagErrorCode,
    TagStoreError,
    aclassify_integrity_error,
)

__all__ = [
    "ARTIFACT_KINDS",
    "ASSIGNMENT_SOURCES",
    "SESSION_TERMINAL_RUN_STATES",
    "SessionAdmissionRow",
    "SessionArtifactRow",
    "SessionEntryRow",
    "SessionRunRow",
    "SessionRow",
    "SessionTagRow",
    "aappend_entry",
    "aattach_session_tag",
    "anotify_entry_updated",
    "acreate_root_run",
    "acreate_run",
    "acreate_session",
    "alist_compactions",
    "alist_provider_artifacts",
    "alist_session_tags",
    "arename_session",
    "aremove_session_tag",
    "apreview_session_tags",
    "alist_sessions",
    "aread_session_history",
    "arecord_admission",
    "arecord_compaction",
    "arecord_provider_artifact",
    "arecord_run_state",
    "asession_tags_page",
    "aget_session",
]

# --- frozen vocabularies (schema CHECKs own the enforcement; mirrored) ---------

#: Provider artifact classes (D01 ``ProviderReasoningArtifact`` literal).
ARTIFACT_KINDS: Final[frozenset[str]] = frozenset({"summary", "signature", "opaque_ref"})

#: Session-tag writer vocabulary (schema CHECK; matches the 0011 catalog).
ASSIGNMENT_SOURCES: Final[frozenset[str]] = frozenset({"agent", "user", "system"})
#: The SessionStatus vocabulary (same set the migration's CHECK enforces).
_SESSION_STATUSES: Final[frozenset[str]] = frozenset(
    member.value for member in SessionStatus
)

#: Run states that settle a run (``completed_at`` is stamped for these;
#: the waiting_*/running/queued states stay resumable).
SESSION_TERMINAL_RUN_STATES: Final[frozenset[SessionRunState]] = frozenset(
    {
        SessionRunState.SUCCEEDED,
        SessionRunState.FAILED,
        SessionRunState.CANCELLED,
        SessionRunState.OUTCOME_UNKNOWN,
    }
)

_TITLE_MAX_CHARS: Final[int] = 240
_SOURCE_ID_MAX_CHARS: Final[int] = 128
_ARTIFACT_CONTENT_MAX_CHARS: Final[int] = 20_000
_PROVIDER_MAX_CHARS: Final[int] = 128
_ASSIGNED_BY_MAX_CHARS: Final[int] = 200

# Run-state entries deduplicate through a stable per-state source id, so a
# repeated transition to the SAME state never duplicates the entry (D02
# acceptance 6). Different states carry different ids and are recorded.
_RUN_STATE_SOURCE_PREFIX: Final[str] = "run_state:"


# --- row dataclasses -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionRow:
    """One session header row (the stable conversation root)."""

    session_id: uuid.UUID
    server_id: uuid.UUID
    service_id: uuid.UUID | None
    source: str
    status: str
    title: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SessionEntryRow:
    """One committed domain entry row."""

    entry_id: uuid.UUID
    session_id: uuid.UUID
    run_id: uuid.UUID | None
    seq: int
    kind: str
    payload: dict[str, object]
    durable_source_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SessionRunRow:
    """One subordinate child-run row."""

    run_id: uuid.UUID
    session_id: uuid.UUID
    server_id: uuid.UUID
    parent_run_id: uuid.UUID | None
    state: str
    model_profile: str
    client_request_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class SessionAdmissionRow:
    """One admission-ledger row (idempotency: one per client request id)."""

    admission_id: uuid.UUID
    session_id: uuid.UUID
    run_id: uuid.UUID | None
    client_request_id: uuid.UUID
    source: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SessionArtifactRow:
    """One provider reasoning artifact (model-history layer only)."""

    artifact_id: uuid.UUID
    session_id: uuid.UUID
    run_id: uuid.UUID | None
    provider: str
    model_profile: str
    artifact_kind: str
    content: str
    redacted: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SessionTagRow:
    """One session-tag association row (shared generic catalog)."""

    session_id: uuid.UUID
    tag_id: uuid.UUID
    assigned_by: str
    assignment_source: str
    created_at: datetime


# --- validation helpers (value-free, fail closed) -------------------------------


def _value_of(value: object) -> object:
    return value.value if isinstance(value, Enum) else value


_SOURCES: Final[frozenset[str]] = frozenset(
    member.value for member in SessionSourceKind
)
_ENTRY_KIND_VALUES: Final[frozenset[str]] = frozenset(
    member.value for member in EntryKind
)
_RUN_STATE_VALUES: Final[frozenset[str]] = frozenset(
    member.value for member in SessionRunState
)


def _validated_source(source: object) -> str:
    value = _value_of(source)
    if not isinstance(value, str) or value not in _SOURCES:
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    "source",
                    "source must be one of: interactive, runbook, "
                    "document_attach, api",
                )
            ]
        )
    return value


def _validated_kind(kind: object) -> str:
    value = _value_of(kind)
    if not isinstance(value, str) or value not in _ENTRY_KIND_VALUES:
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    "kind",
                    "entry kind must be one of the frozen D01 EntryKind values",
                )
            ]
        )
    return value


def _validated_state(state: object) -> SessionRunState:
    value = _value_of(state)
    if not isinstance(value, str) or value not in _RUN_STATE_VALUES:
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    "state",
                    "run state must be one of the frozen D01 "
                    "SessionRunState values",
                )
            ]
        )
    return SessionRunState(value)


def _validated_durable_source_id(durable_source_id: object) -> str:
    value = _value_of(durable_source_id)
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= _SOURCE_ID_MAX_CHARS
    ):
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    "durable_source_id",
                    f"durable_source_id must be 1..{_SOURCE_ID_MAX_CHARS} "
                    "characters",
                )
            ]
        )
    return value


def _validated_payload(payload: object) -> str:
    if not isinstance(payload, Mapping):
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    "payload",
                    "entry payload must be a JSON object document",
                )
            ]
        )
    # The budget is CHARACTERS (the pydantic ``max_length`` unit), never
    # escaped ASCII: ``ensure_ascii=True`` would inflate every Cyrillic
    # char to a 6-char ``\uXXXX`` escape and reject a long Russian final
    # answer AFTER the model already succeeded (2026-11-12 incident).
    serialized = json.dumps(dict(payload), ensure_ascii=False)
    if len(serialized) > MAX_ENTRY_TEXT_CHARS:
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    "payload",
                    "entry payload exceeds the bounded entry budget",
                )
            ]
        )
    return serialized


def _validated_title(title: str | None) -> str | None:
    if title is None:
        return None
    if not isinstance(title, str) or not 1 <= len(title) <= _TITLE_MAX_CHARS:
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    "title",
                    f"title must be 1..{_TITLE_MAX_CHARS} characters when set",
                )
            ]
        )
    return title


def _not_found(field: str) -> SessionContractError:
    """The scope-blind not-found rejection (missing == cross-server)."""
    return SessionContractError(
        [
            SessionFieldError(
                SessionErrorCode.SESSION_NOT_FOUND,
                field,
                "no session with this identity in the requesting server scope",
            )
        ]
    )


# --- session header --------------------------------------------------------------


def _session_row(row: Mapping[str, object]) -> SessionRow:
    return SessionRow(
        session_id=row["session_id"],  # type: ignore[arg-type]
        server_id=row["server_id"],  # type: ignore[arg-type]
        service_id=row.get("service_id"),  # type: ignore[arg-type]
        source=row["source"],  # type: ignore[arg-type]
        status=row["status"],  # type: ignore[arg-type]
        title=row["title"],  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
        updated_at=row["updated_at"],  # type: ignore[arg-type]
    )


async def acreate_session(
    sessions: async_sessionmaker[AsyncSession],
    *,
    server_id: uuid.UUID,
    source: SessionSourceKind | str,
    title: str | None = None,
    session_id: uuid.UUID | None = None,
    service_id: uuid.UUID | None = None,
) -> SessionRow:
    """Create one session row; ``source`` validated against D01 vocabulary.

    ``session_id`` is a trusted-producer key (the admission surface owns
    identity allocation); a duplicate id is a caller contract fault and
    surfaces as the schema's unique violation. ``service_id`` is the
    optional adopted-service scope (SCOPE-1): already validated against the
    SAME-server Service Map by the admission surface, stored in the SAME
    transaction (never a follow-up write).
    """
    validated_source = _validated_source(source)
    validated_title = _validated_title(title)
    new_id = session_id if session_id is not None else uuid.uuid4()
    async with sessions() as session:
        async with session.begin():
            row = (
                await session.execute(
                    text(
                        """
                        INSERT INTO sessions
                            (session_id, server_id, service_id, source,
                             status, title)
                        VALUES
                            (:sid, :server_id, :service_id, :source, 'idle',
                             :title)
                        RETURNING session_id, server_id, service_id, source,
                                  status, title, created_at, updated_at
                        """
                    ),
                    {
                        "sid": new_id,
                        "server_id": server_id,
                        "service_id": service_id,
                        "source": validated_source,
                        "title": validated_title,
                    },
                )
            ).mappings().one()
    return _session_row(row)


async def aget_session(
    sessions: async_sessionmaker[AsyncSession],
    session_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
) -> SessionRow:
    """Read one session header; scope-blind not-found (C01 rule 7)."""
    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT session_id, server_id, service_id, source, status,
                           title, created_at, updated_at
                    FROM sessions
                    WHERE session_id = :sid
                    """
                ),
                {"sid": session_id},
            )
        ).mappings().first()
    if row is None:
        raise _not_found("session_id")
    # Fail closed on cross-server reads: the SAME not-found code a missing
    # session produces, so cross-server existence never leaks.
    assert_session_scope(server_id, row["server_id"])  # type: ignore[arg-type]
    return _session_row(row)


async def alist_sessions(
    sessions: async_sessionmaker[AsyncSession],
    *,
    server_id: uuid.UUID | None = None,
    status: str | None = None,
    tag_id: uuid.UUID | None = None,
    limit: int = SESSION_LIST_DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[SessionRow], int]:
    """Bounded deterministic session page (newest-change first) + total.

    D07-fix1 F2: ``server_id`` is OPTIONAL (``None`` lists the recent page
    across all enrolled servers + General Consult, mirroring the reviewed
    conversation list), and the page accepts the bounded ``status`` and
    ``tag_id`` filters (the tag filter joins ``session_tags`` under the
    identical predicate, so ``total`` stays honest)."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= SESSION_LIST_MAX_LIMIT:
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.INVALID_PAGINATION,
                    "limit",
                    f"limit must be an integer in [1, {SESSION_LIST_MAX_LIMIT}]",
                )
            ]
        )
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.INVALID_PAGINATION,
                    "offset",
                    "offset must be an integer >= 0",
                )
            ]
        )
    if status is not None and status not in _SESSION_STATUSES:
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    "status",
                    "status must be one of the SessionStatus vocabulary values",
                )
            ]
        )
    predicate = "WHERE TRUE"
    params: dict[str, object] = {"l": limit, "o": offset}
    if server_id is not None:
        predicate += " AND sessions.server_id = :server_id"
        params["server_id"] = server_id
    if status is not None:
        predicate += " AND sessions.status = :status"
        params["status"] = status
    if tag_id is not None:
        predicate += (
            " AND sessions.session_id IN "
            "(SELECT session_id FROM session_tags WHERE tag_id = :tag_id)"
        )
        params["tag_id"] = tag_id
    async with sessions() as session:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT session_id, server_id, service_id, source, status,
                           title, created_at, updated_at
                    FROM sessions
                    {predicate}
                    ORDER BY updated_at DESC, session_id
                    LIMIT :l OFFSET :o
                    """
                ),
                params,
            )
        ).mappings().all()
        total = (
            await session.execute(
                text(f"SELECT COUNT(*) FROM sessions {predicate}"),
                {key: value for key, value in params.items() if key not in ("l", "o")},
            )
        ).scalar_one()
    return [_session_row(row) for row in rows], int(total)


# --- transactional append core ---------------------------------------------------


async def _read_entry_row(
    tx: AsyncSession, entry_id: uuid.UUID
) -> SessionEntryRow | None:
    row = (
        await tx.execute(
            text(
                """
                SELECT entry_id, session_id, run_id, seq, kind, payload,
                       durable_source_id, created_at
                FROM session_entries
                WHERE entry_id = :eid
                """
            ),
            {"eid": entry_id},
        )
    ).mappings().first()
    return None if row is None else _entry_row(row)


async def _find_committed_entry(
    tx: AsyncSession,
    *,
    session_id: uuid.UUID,
    run_id: uuid.UUID | None,
    durable_source_id: str,
    kind: str,
) -> SessionEntryRow | None:
    """Read back the entry committed for this durable source, if any.

    Called AFTER the session row lock is held, so concurrent appends to one
    session are serialized and this pre-check is race-free.
    """
    row = (
        await tx.execute(
            text(
                """
                SELECT entry_id, session_id, run_id, seq, kind, payload,
                       durable_source_id, created_at
                FROM session_entries
                WHERE session_id = :sid
                  AND run_id IS NOT DISTINCT FROM :rid
                  AND durable_source_id = :src
                  AND kind = :kind
                """
            ),
            {
                "sid": session_id,
                "rid": run_id,
                "src": durable_source_id,
                "kind": kind,
            },
        )
    ).mappings().first()
    return None if row is None else _entry_row(row)


def _entry_row(row: Mapping[str, object]) -> SessionEntryRow:
    payload = row["payload"]
    return SessionEntryRow(
        entry_id=row["entry_id"],  # type: ignore[arg-type]
        session_id=row["session_id"],  # type: ignore[arg-type]
        run_id=row["run_id"],  # type: ignore[arg-type]
        seq=row["seq"],  # type: ignore[arg-type]
        kind=row["kind"],  # type: ignore[arg-type]
        payload=payload if isinstance(payload, dict) else json.loads(str(payload)),  # type: ignore[arg-type]
        durable_source_id=row["durable_source_id"],  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
    )


async def _lock_session(
    tx: AsyncSession, *, session_id: uuid.UUID, server_id: uuid.UUID
) -> None:
    """Lock the session row and enforce the explicit server scope.

    The lock serializes per-session writers (unique contiguous sequences);
    the scope check runs on the locked row so a concurrent re-scope is
    impossible. A missing row and a cross-server row fail with the SAME
    value-free not-found code.
    """
    row = (
        await tx.execute(
            text("SELECT server_id FROM sessions WHERE session_id = :sid FOR UPDATE"),
            {"sid": session_id},
        )
    ).mappings().first()
    if row is None:
        raise _not_found("session_id")
    assert_session_scope(server_id, row["server_id"])  # type: ignore[arg-type]


async def _append_entry_tx(
    tx: AsyncSession,
    *,
    session_id: uuid.UUID,
    run_id: uuid.UUID | None,
    kind: str,
    payload: Mapping[str, object],
    durable_source_id: str,
) -> SessionEntryRow:
    """Append inside one open transaction that holds the session row lock.

    Allocation and insert share the transaction: a rollback releases the
    allocated sequence number, so no gap is claimed as committed history.
    """
    validated_kind = _validated_kind(kind)
    validated_source = _validated_durable_source_id(durable_source_id)
    if run_id is not None:
        # The D01 identity conflict is a store invariant too.
        assert_session_not_run(session_id, run_id)
    committed = await _find_committed_entry(
        tx,
        session_id=session_id,
        run_id=run_id,
        durable_source_id=validated_source,
        kind=validated_kind,
    )
    if committed is not None:
        return committed
    payload_json = _validated_payload(payload)
    seq = (
        await tx.execute(
            text(
                """
                SELECT COALESCE(MAX(seq), 0) + 1
                FROM session_entries
                WHERE session_id = :sid
                """
            ),
            {"sid": session_id},
        )
    ).scalar_one()
    inserted = (
        await tx.execute(
            text(
                """
                INSERT INTO session_entries
                    (entry_id, session_id, run_id, seq, kind, payload,
                     durable_source_id)
                VALUES
                    (:eid, :sid, :rid, :seq, :kind, CAST(:payload AS jsonb),
                     :src)
                RETURNING entry_id, session_id, run_id, seq, kind, payload,
                          durable_source_id, created_at
                """
            ),
            {
                "eid": uuid.uuid4(),
                "sid": session_id,
                "rid": run_id,
                "seq": seq,
                "kind": validated_kind,
                "payload": payload_json,
                "src": validated_source,
            },
        )
    ).mappings().one()
    await tx.execute(
        text("UPDATE sessions SET updated_at = now() WHERE session_id = :sid"),
        {"sid": session_id},
    )
    # Transactional NOTIFY (S1): PostgreSQL holds it until COMMIT and
    # drops it on ROLLBACK, so every committed entry — from the HTTP API
    # and the DBOS worker alike — wakes the SessionChannelHub exactly
    # when the entry becomes durable. Identity-only payload.
    await tx.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {
            "channel": CHANNEL_NOTIFY_CHANNEL,
            "payload": json.dumps(
                {"session_id": str(session_id), "latest_seq": int(seq)}
            ),
        },
    )
    return _entry_row(inserted)


async def anotify_entry_updated(
    tx: AsyncSession,
    *,
    session_id: uuid.UUID,
    seq: int,
) -> None:
    """Transactional in-place-update wakeup for the session channel.

    Callers run their ``UPDATE session_entries ... RETURNING seq`` inside
    an open transaction and invoke this with the returned ``seq``: the
    NOTIFY is held until COMMIT (dropped on ROLLBACK), so the
    SessionChannelHub re-reads the row only after it is durably updated.
    The identity-only payload carries ``updated_seq`` so at-head
    connections refresh the entry they already delivered.
    """
    await tx.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {
            "channel": CHANNEL_NOTIFY_CHANNEL,
            "payload": json.dumps(
                {
                    "session_id": str(session_id),
                    "latest_seq": int(seq),
                    "updated_seq": int(seq),
                }
            ),
        },
    )


async def aappend_entry(
    sessions: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    kind: EntryKind | str,
    payload: Mapping[str, object],
    run_id: uuid.UUID | None = None,
    durable_source_id: str,
) -> SessionEntryRow:
    """Append one domain entry: lock, allocate ``seq``, insert, commit.

    Idempotent for a repeated ``(run_id, durable_source_id, kind)``: the
    committed entry is returned unchanged (no duplicate row, no second
    sequence number). Cross-server sessions fail scope-blind; a ``run_id``
    equal to the ``session_id`` fails with the D01 identity-conflict code.
    """
    async with sessions() as session:
        async with session.begin():
            await _lock_session(
                session, session_id=session_id, server_id=server_id
            )
            return await _append_entry_tx(
                session,
                session_id=session_id,
                run_id=run_id,
                kind=kind,
                payload=payload,
                durable_source_id=durable_source_id,
            )


# --- bounded history reads ---------------------------------------------------------


def _wire_bytes(items: Sequence[SessionEntryView]) -> int:
    """Serialized size of the page's items (compact canonical JSON)."""
    return len(
        json.dumps(
            [item.model_dump(mode="json") for item in items],
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _truncate_by_bytes(
    items: list[SessionEntryView],
) -> tuple[list[SessionEntryView], bool]:
    """Drop the OLDEST items until the page fits the byte budget.

    Chronological order is preserved; the newest tail wins (older items are
    re-fetchable through the keyset cursor). Returns the truncated list and
    the honest ``truncated_by_bytes`` flag.
    """
    truncated_by_bytes = False
    while items and _wire_bytes(items) > MAX_PAGE_BYTES:
        items.pop(0)
        truncated_by_bytes = True
    return items, truncated_by_bytes


def _assemble_history_page(
    *,
    session_id: uuid.UUID,
    items: list[SessionEntryView],
    limit: int,
    total: int,
    has_more: bool,
    has_older: bool,
    truncated_by_bytes: bool,
) -> SessionHistoryPage:
    """Assemble the D01 page from the ALREADY-truncated item list.

    Continuation flags MUST be probed against the truncated list's bounds
    (an honest page advertises the older entries it dropped).
    """
    if not items:
        has_more = False
        has_older = False
    oldest_seq = items[0].seq if items else None
    latest_seq = items[-1].seq if items else None
    return SessionHistoryPage(
        session_id=session_id,
        items=items,
        limit=limit,
        total=total,
        has_more=has_more,
        has_older=has_older,
        oldest_seq=oldest_seq,
        latest_seq=latest_seq,
        next_before_seq=oldest_seq if has_older else None,
        byte_size=_wire_bytes(items),
        truncated_by_bytes=truncated_by_bytes,
    )


def _entry_view(row: SessionEntryRow) -> SessionEntryView:
    """Project one committed row onto the D01 entry contract.

    A corrupted stored payload fails the union validation and surfaces as a
    pydantic ``ValidationError`` — an integrity fault of the trusted store,
    deliberately NOT masked as a value-free contract rejection.
    """
    return SessionEntryView(
        entry_id=row.entry_id,
        session_id=row.session_id,
        seq=row.seq,
        run_id=row.run_id,
        durable_source_id=row.durable_source_id,
        created_at=row.created_at,
        payload=row.payload,
    )


async def arecord_compaction(
    sessions: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    compaction: CompactionEntry,
) -> tuple[SessionEntryRow, bool]:
    """Append ONE compaction marker (D05; the sole compaction writer).

    Idempotent per covered RANGE (durable id ``compaction:{from}:{to}``)
    under the session-row fence: a re-run over the same range returns the
    EXISTING row with ``created=False`` — never a second summary, never
    double coverage. The originals are never touched; the marker is just
    another appended entry (``session_id`` never rotates).
    """
    durable_source_id = (
        f"compaction:{compaction.from_seq}:{compaction.to_seq}"
    )
    async with sessions() as session:
        async with session.begin():
            await _lock_session(session, session_id=session_id, server_id=server_id)
            committed = await _find_committed_entry(
                session,
                session_id=session_id,
                run_id=None,
                durable_source_id=durable_source_id,
                kind=EntryKind.COMPACTION.value,
            )
            if committed is not None:
                return committed, False
            row = await _append_entry_tx(
                session,
                session_id=session_id,
                run_id=None,
                kind=EntryKind.COMPACTION.value,
                payload=compaction.model_dump(mode="json"),
                durable_source_id=durable_source_id,
            )
            return row, True


async def alist_compactions(
    sessions: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    limit: int = 16,
) -> list[SessionEntryRow]:
    """The session's compaction markers, newest first (bounded page).

    Bounded per the pagination invariant: ``limit`` is enforced (clamped to
    the history page maximum); no unbounded scan.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        limit = 1
    limit = min(limit, HISTORY_PAGE_MAX_ENTRIES)
    async with sessions() as session:
        async with session.begin():
            await _lock_session(session, session_id=session_id, server_id=server_id)
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT entry_id, session_id, run_id, seq, kind, payload,
                               durable_source_id, created_at
                        FROM session_entries
                        WHERE session_id = :sid AND kind = 'compaction'
                        ORDER BY seq DESC
                        LIMIT :limit
                        """
                    ),
                    {"sid": session_id, "limit": limit},
                )
            ).mappings().all()
            return [_entry_row(row) for row in rows]


async def aread_session_history(
    sessions: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    limit: int = INITIAL_TAIL_MAX_ENTRIES,
    before_seq: int | None = None,
    before_entry_id: uuid.UUID | None = None,
    after_seq: int | None = None,
) -> SessionHistoryPage:
    """One bounded, byte-checked history page (sfc_RESEARCH §8 keyset model).

    - No cursor: the newest ≤ ``limit`` rows (chronological display order).
    - ``before_seq`` (± ``before_entry_id``): strict keyset
      ``(seq, entry_id) < (cursor, id)``, newest-first, reversed.
    - ``after_seq``: catch-up page ``seq > cursor`` ascending.

    ``total`` is the per-session entry count; ``has_more``/``has_older`` come
    from ``EXISTS`` probes. The SQL never materializes more than ``limit``
    rows.
    """
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= HISTORY_PAGE_MAX_ENTRIES
    ):
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.INVALID_PAGINATION,
                    "limit",
                    f"limit must be an integer in [1, {HISTORY_PAGE_MAX_ENTRIES}]",
                )
            ]
        )
    if before_seq is not None and after_seq is not None:
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.INVALID_CURSOR,
                    "cursor",
                    "before_seq and after_seq are mutually exclusive",
                )
            ]
        )
    async with sessions() as session:
        async with session.begin():
            # Scope-blind gate (shared with every other read).
            await _lock_session(
                session, session_id=session_id, server_id=server_id
            )
            total = (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM session_entries "
                        "WHERE session_id = :sid"
                    ),
                    {"sid": session_id},
                )
            ).scalar_one()
            if after_seq is not None:
                rows = (
                    await session.execute(
                        text(
                            """
                            SELECT entry_id, session_id, run_id, seq, kind,
                                   payload, durable_source_id, created_at
                            FROM session_entries
                            WHERE session_id = :sid AND seq > :after_seq
                            ORDER BY seq
                            LIMIT :l
                            """
                        ),
                        {"sid": session_id, "after_seq": after_seq, "l": limit},
                    )
                ).mappings().all()
            elif before_seq is not None:
                if before_entry_id is not None:
                    cursor_predicate = (
                        "(seq, entry_id) < (:before_seq, :before_entry_id)"
                    )
                    params: dict[str, object] = {
                        "sid": session_id,
                        "before_seq": before_seq,
                        "before_entry_id": before_entry_id,
                        "l": limit,
                    }
                else:
                    cursor_predicate = "seq < :before_seq"
                    params = {
                        "sid": session_id,
                        "before_seq": before_seq,
                        "l": limit,
                    }
                rows = (
                    await session.execute(
                        text(
                            f"""
                            SELECT entry_id, session_id, run_id, seq, kind,
                                   payload, durable_source_id, created_at
                            FROM session_entries
                            WHERE session_id = :sid AND {cursor_predicate}
                            ORDER BY seq DESC
                            LIMIT :l
                            """
                        ),
                        params,
                    )
                ).mappings().all()
                rows = list(reversed(rows))
            else:
                rows = (
                    await session.execute(
                        text(
                            """
                            SELECT entry_id, session_id, run_id, seq, kind,
                                   payload, durable_source_id, created_at
                            FROM session_entries
                            WHERE session_id = :sid
                            ORDER BY seq DESC
                            LIMIT :l
                            """
                        ),
                        {"sid": session_id, "l": limit},
                    )
                ).mappings().all()
                rows = list(reversed(rows))
            items = [_entry_view(_entry_row(row)) for row in rows]
            # Byte budget FIRST: the continuation flags must be probed
            # against the truncated list's bounds, so a page that dropped
            # its oldest items advertises them via has_older/cursor.
            items, truncated_by_bytes = _truncate_by_bytes(items)
            if not items:
                has_more = False
                has_older = False
            else:
                oldest_seq = items[0].seq
                latest_seq = items[-1].seq
                has_more = bool(
                    (
                        await session.execute(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM session_entries "
                                "WHERE session_id = :sid AND seq > :s)"
                            ),
                            {"sid": session_id, "s": latest_seq},
                        )
                    ).scalar_one()
                )
                has_older = bool(
                    (
                        await session.execute(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM session_entries "
                                "WHERE session_id = :sid AND seq < :s)"
                            ),
                            {"sid": session_id, "s": oldest_seq},
                        )
                    ).scalar_one()
                )
    return _assemble_history_page(
        session_id=session_id,
        items=items,
        limit=limit,
        total=int(total),
        has_more=has_more,
        has_older=has_older,
        truncated_by_bytes=truncated_by_bytes,
    )


# --- runs and run-state authority ---------------------------------------------------


def _run_row(row: Mapping[str, object]) -> SessionRunRow:
    return SessionRunRow(
        run_id=row["run_id"],  # type: ignore[arg-type]
        session_id=row["session_id"],  # type: ignore[arg-type]
        server_id=row["server_id"],  # type: ignore[arg-type]
        parent_run_id=row["parent_run_id"],  # type: ignore[arg-type]
        state=row["state"],  # type: ignore[arg-type]
        model_profile=row["model_profile"],  # type: ignore[arg-type]
        client_request_id=row["client_request_id"],  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
        updated_at=row["updated_at"],  # type: ignore[arg-type]
        completed_at=row["completed_at"],  # type: ignore[arg-type]
    )


async def acreate_run(
    sessions: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    run_id: uuid.UUID,
    model_profile: str,
    client_request_id: uuid.UUID,
    parent_run_id: uuid.UUID | None = None,
) -> SessionRunRow:
    """Create one subordinate child-run row (``queued``), idempotently.

    The run id IS the idempotency key (D05 fix1, R05 F1): a replayed plan
    step after a crash between the child-row commit and the DBOS step
    record converges on the EXISTING row (read-back) instead of raising a
    UniqueViolation. A run id reused under a DIFFERENT session identity is
    a hard contract conflict. The run/session identity conflict is enforced
    by the schema CHECK and the D01 validators; ``parent_run_id`` must
    reference a run of the same session (schema FK ties parents into
    ``session_runs``).
    """
    if not isinstance(model_profile, str) or not 1 <= len(model_profile) <= _PROVIDER_MAX_CHARS:
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    "model_profile",
                    "model_profile must be 1..128 characters",
                )
            ]
        )
    async with sessions() as session:
        async with session.begin():
            await _lock_session(
                session, session_id=session_id, server_id=server_id
            )
            assert_session_not_run(session_id, run_id)
            row = (
                await session.execute(
                    text(
                        """
                        INSERT INTO session_runs
                            (run_id, session_id, server_id, parent_run_id,
                             state, model_profile, client_request_id)
                        VALUES
                            (:rid, :sid, :server_id, :parent, 'queued',
                             :profile, :creq)
                        ON CONFLICT (run_id) DO NOTHING
                        RETURNING run_id, session_id, server_id, parent_run_id,
                                  state, model_profile, client_request_id,
                                  created_at, updated_at, completed_at
                        """
                    ),
                    {
                        "rid": run_id,
                        "sid": session_id,
                        "server_id": server_id,
                        "parent": parent_run_id,
                        "profile": model_profile,
                        "creq": client_request_id,
                    },
                )
            ).mappings().first()
            if row is None:
                # D05 fix1 (R05 F1): the run id already exists. A replayed
                # plan step converges on the SAME child row (the crash
                # between the child commit and the DBOS step record); a run
                # id reused across SESSIONS is a hard identity conflict.
                row = (
                    await session.execute(
                        text(
                            """
                            SELECT run_id, session_id, server_id, parent_run_id,
                                   state, model_profile, client_request_id,
                                   created_at, updated_at, completed_at
                            FROM session_runs
                            WHERE run_id = :rid AND session_id = :sid
                            """
                        ),
                        {"rid": run_id, "sid": session_id},
                    )
                ).mappings().first()
                if row is None:
                    raise SessionContractError(
                        [
                            SessionFieldError(
                                SessionErrorCode.SESSION_RUN_IDENTITY_CONFLICT,
                                "run_id",
                                "the run id already exists under a different "
                                "session identity",
                            )
                        ]
                    )
    return _run_row(row)

async def acreate_root_run(
    sessions: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    run_id: uuid.UUID,
    model_profile: str,
    client_request_id: uuid.UUID,
    profile_revision: int,
    prompt_revision: int,
    requested_grants: Sequence[str],
) -> SessionRunRow:
    """Admit one ROOT execution run: ``session_runs`` + the retained
    execution facade (``job_runs`` depth 0) + its mandatory ``job_tags``
    classification in ONE transaction (queued-chat hotfix S1).

    The retained jobs machinery (steps/lifecycle) FK-references
    ``job_runs`` and the 0011 mandatory-tag invariant binds the
    classification into the SAME transaction, so a root admission that
    committed ``session_runs`` without the facade would strand the run
    queued forever (diagnostic: session_runs row present, worker startup
    ``run_not_found``). This seam is the single atomic root writer; child
    runs keep :func:`acreate_run` + the delegation facade unchanged.

    ``profile_revision``/``prompt_revision``/``requested_grants`` have NO
    defaults: the caller supplies the real bound revisions and grant
    references (never fabricated values, never widened authority). A
    matching replay is idempotent — the run id is the key; a replayed
    admission converges on the committed rows and back-fills a facade
    missing from pre-hotfix root rows. A run id already committed under a
    different session OR a facade owned by a different server/depth is a
    hard identity conflict (fail closed; the whole transaction rolls
    back).
    """
    if (
        not isinstance(model_profile, str)
        or not 1 <= len(model_profile) <= _PROVIDER_MAX_CHARS
    ):
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    "model_profile",
                    "model_profile must be 1..128 characters",
                )
            ]
        )
    for field_name, revision in (
        ("profile_revision", profile_revision),
        ("prompt_revision", prompt_revision),
    ):
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise SessionContractError(
                [
                    SessionFieldError(
                        SessionErrorCode.FORBIDDEN_FIELD,
                        field_name,
                        "revision must be an integer >= 1 supplied by the "
                        "caller; fabricated revisions are refused",
                    )
                ]
            )
    if (
        isinstance(requested_grants, (str, bytes))
        or not isinstance(requested_grants, Sequence)
        or any(
            not isinstance(grant, str) or not grant for grant in requested_grants
        )
    ):
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    "requested_grants",
                    "requested_grants must be a sequence of non-empty grant "
                    "references supplied by the caller",
                )
            ]
        )
    grants_document = json.dumps(sorted(requested_grants))
    async with sessions() as session:
        async with session.begin():
            await _lock_session(
                session, session_id=session_id, server_id=server_id
            )
            assert_session_not_run(session_id, run_id)
            row = (
                await session.execute(
                    text(
                        """
                        INSERT INTO session_runs
                            (run_id, session_id, server_id, parent_run_id,
                             state, model_profile, client_request_id)
                        VALUES
                            (:rid, :sid, :server_id, NULL, 'queued',
                             :profile, :creq)
                        ON CONFLICT (run_id) DO NOTHING
                        RETURNING run_id, session_id, server_id, parent_run_id,
                                  state, model_profile, client_request_id,
                                  created_at, updated_at, completed_at
                        """
                    ),
                    {
                        "rid": run_id,
                        "sid": session_id,
                        "server_id": server_id,
                        "profile": model_profile,
                        "creq": client_request_id,
                    },
                )
            ).mappings().first()
            if row is None:
                # The run id already exists: same-identity replay converges
                # on the committed row; a run id committed under a
                # DIFFERENT session is a hard identity conflict.
                row = (
                    await session.execute(
                        text(
                            """
                            SELECT run_id, session_id, server_id, parent_run_id,
                                   state, model_profile, client_request_id,
                                   created_at, updated_at, completed_at
                            FROM session_runs
                            WHERE run_id = :rid AND session_id = :sid
                            """
                        ),
                        {"rid": run_id, "sid": session_id},
                    )
                ).mappings().first()
                if row is None:
                    raise SessionContractError(
                        [
                            SessionFieldError(
                                SessionErrorCode.SESSION_RUN_IDENTITY_CONFLICT,
                                "run_id",
                                "the run id already exists under a different "
                                "session identity",
                            )
                        ]
                    )
            facade = (
                await session.execute(
                    text(
                        """
                        INSERT INTO job_runs
                            (run_id, server_id, depth, profile_revision,
                             prompt_revision, model_profile, requested_grants,
                             submitted_at, updated_at)
                        VALUES
                            (:rid, :server_id, 0, :profile_revision,
                             :prompt_revision, :profile, :grants, now(), now())
                        ON CONFLICT (run_id) DO NOTHING
                        RETURNING run_id
                        """
                    ),
                    {
                        "rid": run_id,
                        "server_id": server_id,
                        "profile_revision": profile_revision,
                        "prompt_revision": prompt_revision,
                        "profile": model_profile,
                        "grants": grants_document,
                    },
                )
            ).scalar()
            if facade is None:
                # The facade row already exists (a replay through this seam,
                # or a pre-hotfix/foreign-surface run id): converge only
                # when it is THIS server's root facade.
                existing = (
                    await session.execute(
                        text(
                            """
                            SELECT server_id, depth FROM job_runs
                            WHERE run_id = :rid
                            """
                        ),
                        {"rid": run_id},
                    )
                ).mappings().first()
                if (
                    existing is None
                    or existing["server_id"] != server_id
                    or existing["depth"] != 0
                ):
                    raise SessionContractError(
                        [
                            SessionFieldError(
                                SessionErrorCode.SESSION_RUN_IDENTITY_CONFLICT,
                                "run_id",
                                "the run id already exists under a different "
                                "execution identity",
                            )
                        ]
                    )
            # 0011 mandatory-tag invariant: the classification commits in
            # the SAME transaction as the facade row it belongs to.
            await session.execute(
                text(
                    """
                    INSERT INTO job_tags (job_id, tag_id, assignment_source)
                    VALUES (:rid, :tag, :source)
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "rid": run_id,
                    "tag": uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID),
                    "source": SYSTEM_ASSIGNMENT_SOURCE,
                },
            )
    return _run_row(row)


async def arecord_run_state(
    sessions: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    run_id: uuid.UUID,
    state: SessionRunState | str,
    reason_code: str | None = None,
    detail: str | None = None,
    source_suffix: str | None = None,
    source_prefix: str = "",
) -> None:
    """Update the run state AND append its ``run_state`` entry atomically.

    One run-state authority: the browser never derives run state from a
    connection. The appended entry deduplicates through the stable
    ``[<source_prefix>]run_state:<state>[:<source_suffix>]`` source id, so a
    repeated call with the same state does not duplicate the entry, while a
    per-transition ``source_suffix`` (e.g. the delegation call id) lets the
    SAME state recur chronologically instead of deduplicating against an
    earlier same-state entry. ``source_prefix`` namespaces child-run
    entries (``child:``) so they can never collide with a parent run's
    entry. An unknown ``run_id`` inside the scoped session fails closed
    with the value-free not-found code (field ``run_id``).
    """
    validated_state = _validated_state(state)
    if reason_code is not None and (
        not isinstance(reason_code, str) or not 1 <= len(reason_code) <= 128
    ):
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    "reason_code",
                    "reason_code must be 1..128 characters when set",
                )
            ]
        )
    durable_source_id = (
        f"{source_prefix}{_RUN_STATE_SOURCE_PREFIX}{validated_state.value}"
    )
    if source_suffix is not None:
        durable_source_id = f"{durable_source_id}:{source_suffix}"
    async with sessions() as session:
        async with session.begin():
            await _lock_session(
                session, session_id=session_id, server_id=server_id
            )
            updated = (
                await session.execute(
                    text(
                        """
                        UPDATE session_runs
                        SET state = CAST(:state AS varchar),
                            updated_at = now(),
                            completed_at = COALESCE(
                                completed_at,
                                CASE WHEN :state IN ('succeeded', 'failed',
                                                     'cancelled',
                                                     'outcome_unknown')
                                     THEN now() END
                            )
                        WHERE run_id = :rid AND session_id = :sid
                        """
                    ),
                    {"rid": run_id, "sid": session_id, "state": validated_state.value},
                )
            ).rowcount
            if updated == 0:
                raise _not_found("run_id")
            await _append_entry_tx(
                session,
                session_id=session_id,
                run_id=run_id,
                kind=EntryKind.RUN_STATE,
                payload=RunStateEntry(
                    kind=EntryKind.RUN_STATE,
                    state=validated_state,
                    reason_code=reason_code,
                    detail=detail,
                ).model_dump(mode="json"),
                durable_source_id=durable_source_id,
            )


# --- admission ledger ----------------------------------------------------------------


def _admission_row(row: Mapping[str, object]) -> SessionAdmissionRow:
    return SessionAdmissionRow(
        admission_id=row["admission_id"],  # type: ignore[arg-type]
        session_id=row["session_id"],  # type: ignore[arg-type]
        run_id=row["run_id"],  # type: ignore[arg-type]
        client_request_id=row["client_request_id"],  # type: ignore[arg-type]
        source=row["source"],  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
    )


async def arecord_admission(
    sessions: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    client_request_id: uuid.UUID,
    source: SessionSourceKind | str,
    run_id: uuid.UUID | None = None,
) -> SessionAdmissionRow:
    """Record one admission; a repeated ``client_request_id`` is a no-op.

    The ledger key is GLOBAL (schema UNIQUE): one admission per client
    request id, whatever replays it. If the committed admission belongs to a
    session outside the requester's scope, the reply fails closed with the
    scope-blind not-found code (never leak the other session's identity).
    """
    validated_source = _validated_source(source)
    async with sessions() as session:
        async with session.begin():
            await _lock_session(
                session, session_id=session_id, server_id=server_id
            )
            inserted = (
                await session.execute(
                    text(
                        """
                        INSERT INTO session_admissions
                            (admission_id, session_id, run_id,
                             client_request_id, source)
                        VALUES
                            (:aid, :sid, :rid, :creq, :source)
                        ON CONFLICT (client_request_id) DO NOTHING
                        RETURNING admission_id, session_id, run_id,
                                  client_request_id, source, created_at
                        """
                    ),
                    {
                        "aid": uuid.uuid4(),
                        "sid": session_id,
                        "rid": run_id,
                        "creq": client_request_id,
                        "source": validated_source,
                    },
                )
            ).mappings().first()
            if inserted is None:
                inserted = (
                    await session.execute(
                        text(
                            """
                            SELECT admission_id, session_id, run_id,
                                   client_request_id, source, created_at
                            FROM session_admissions
                            WHERE client_request_id = :creq
                            """
                        ),
                        {"creq": client_request_id},
                    )
                ).mappings().one()
    row = _admission_row(inserted)
    if row.session_id != session_id:
        # A replayed key bound to another session: scope-blind refusal when
        # that session is not visible to the requester's server scope.
        existing = await aget_session(
            sessions, row.session_id, server_id=server_id
        )
        assert_session_scope(server_id, existing.server_id)
    return row


# --- provider artifacts (model-history layer only) -----------------------------------


def _artifact_row(row: Mapping[str, object]) -> SessionArtifactRow:
    return SessionArtifactRow(
        artifact_id=row["artifact_id"],  # type: ignore[arg-type]
        session_id=row["session_id"],  # type: ignore[arg-type]
        run_id=row["run_id"],  # type: ignore[arg-type]
        provider=row["provider"],  # type: ignore[arg-type]
        model_profile=row["model_profile"],  # type: ignore[arg-type]
        artifact_kind=row["artifact_kind"],  # type: ignore[arg-type]
        content=row["content"],  # type: ignore[arg-type]
        redacted=row["redacted"],  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
    )


async def arecord_provider_artifact(
    sessions: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    provider: str,
    model_profile: str,
    artifact_kind: str,
    content: str,
    run_id: uuid.UUID | None = None,
    redacted: bool = False,
) -> SessionArtifactRow:
    """Persist one provider reasoning artifact (bounded, compatibility-tagged).

    Artifacts are model-history input only: the store never projects them
    into any UI/public surface, and the ``redacted`` flag is honored by
    every reader.
    """
    artifact_kind_value = _value_of(artifact_kind)
    if (
        not isinstance(artifact_kind_value, str)
        or artifact_kind_value not in ARTIFACT_KINDS
    ):
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    "artifact_kind",
                    "artifact_kind must be one of: summary, signature, opaque_ref",
                )
            ]
        )
    for field, value, bound in (
        ("provider", provider, _PROVIDER_MAX_CHARS),
        ("model_profile", model_profile, _PROVIDER_MAX_CHARS),
        ("content", content, _ARTIFACT_CONTENT_MAX_CHARS),
    ):
        if not isinstance(value, str) or not 1 <= len(value) <= bound:
            raise SessionContractError(
                [
                    SessionFieldError(
                        SessionErrorCode.FORBIDDEN_FIELD,
                        field,  # type: ignore[arg-type]
                        f"{field} must be 1..{bound} characters",
                    )
                ]
            )
    async with sessions() as session:
        async with session.begin():
            await _lock_session(
                session, session_id=session_id, server_id=server_id
            )
            row = (
                await session.execute(
                    text(
                        """
                        INSERT INTO session_provider_artifacts
                            (artifact_id, session_id, run_id, provider,
                             model_profile, artifact_kind, content, redacted)
                        VALUES
                            (:aid, :sid, :rid, :provider, :profile, :kind,
                             :content, :redacted)
                        RETURNING artifact_id, session_id, run_id, provider,
                                  model_profile, artifact_kind, content,
                                  redacted, created_at
                        """
                    ),
                    {
                        "aid": uuid.uuid4(),
                        "sid": session_id,
                        "rid": run_id,
                        "provider": provider,
                        "profile": model_profile,
                        "kind": artifact_kind_value,
                        "content": content,
                        "redacted": bool(redacted),
                    },
                )
            ).mappings().one()
    return _artifact_row(row)


async def alist_provider_artifacts(
    sessions: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    limit: int = SESSION_LIST_DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[SessionArtifactRow], int]:
    """Bounded artifact page (newest first) + total; model-history reader."""
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= SESSION_LIST_MAX_LIMIT
    ):
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.INVALID_PAGINATION,
                    "limit",
                    f"limit must be an integer in [1, {SESSION_LIST_MAX_LIMIT}]",
                )
            ]
        )
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.INVALID_PAGINATION,
                    "offset",
                    "offset must be an integer >= 0",
                )
            ]
        )
    async with sessions() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT a.artifact_id, a.session_id, a.run_id, a.provider,
                           a.model_profile, a.artifact_kind, a.content,
                           a.redacted, a.created_at
                    FROM session_provider_artifacts a
                    JOIN sessions s ON s.session_id = a.session_id
                    WHERE a.session_id = :sid AND s.server_id = :server_id
                    ORDER BY a.created_at DESC, a.artifact_id
                    LIMIT :l OFFSET :o
                    """
                ),
                {"sid": session_id, "server_id": server_id, "l": limit, "o": offset},
            )
        ).mappings().all()
        total = (
            await session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM session_provider_artifacts a
                    JOIN sessions s ON s.session_id = a.session_id
                    WHERE a.session_id = :sid AND s.server_id = :server_id
                    """
                ),
                {"sid": session_id, "server_id": server_id},
            )
        ).scalar_one()
    if not rows and int(total) == 0:
        # Scope-blind: a missing or cross-server session is the same failure.
        await aget_session(sessions, session_id, server_id=server_id)
    return [_artifact_row(row) for row in rows], int(total)


# --- session tags (shared generic catalog) ---------------------------------------------


def _tag_row(row: Mapping[str, object]) -> SessionTagRow:
    return SessionTagRow(
        session_id=row["session_id"],  # type: ignore[arg-type]
        tag_id=row["tag_id"],  # type: ignore[arg-type]
        assigned_by=row["assigned_by"],  # type: ignore[arg-type]
        assignment_source=row["assignment_source"],  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
    )


async def aattach_session_tag(
    sessions: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    tag_id: uuid.UUID,
    assigned_by: str = "",
    assignment_source: str = "system",
) -> SessionTagRow:
    """Associate one catalog tag with a session (append-only association).

    The tag must exist in the RETAINED generic catalog; unknown tags fail
    with the tag-store vocabulary (``tag_not_found``). Session scope is
    enforced scope-blind.
    """
    if assignment_source not in ASSIGNMENT_SOURCES:
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    "assignment_source",
                    "assignment_source must be one of: agent, user, system",
                )
            ]
        )
    if not isinstance(assigned_by, str) or len(assigned_by) > _ASSIGNED_BY_MAX_CHARS:
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    "assigned_by",
                    f"assigned_by must be at most {_ASSIGNED_BY_MAX_CHARS} "
                    "characters",
                )
            ]
        )
    async with sessions() as session:
        async with session.begin():
            await _lock_session(
                session, session_id=session_id, server_id=server_id
            )
            tag = (
                await session.execute(
                    text("SELECT tag_id FROM tags WHERE tag_id = :tid"),
                    {"tid": tag_id},
                )
            ).first()
            if tag is None:
                raise TagStoreError(
                    TagErrorCode.TAG_NOT_FOUND,
                    "no catalog tag with this identity",
                    "tag_id",
                )
            row = (
                await session.execute(
                    text(
                        """
                        INSERT INTO session_tags
                            (session_id, tag_id, assigned_by,
                             assignment_source)
                        VALUES (:sid, :tid, :assigned_by, :source)
                        ON CONFLICT (session_id, tag_id) DO NOTHING
                        RETURNING session_id, tag_id, assigned_by,
                                  assignment_source, created_at
                        """
                    ),
                    {
                        "sid": session_id,
                        "tid": tag_id,
                        "assigned_by": assigned_by,
                        "source": assignment_source,
                    },
                )
            ).mappings().first()
            if row is None:  # already associated: read back the committed row
                row = (
                    await session.execute(
                        text(
                            """
                            SELECT session_id, tag_id, assigned_by,
                                   assignment_source, created_at
                            FROM session_tags
                            WHERE session_id = :sid AND tag_id = :tid
                            """
                        ),
                        {"sid": session_id, "tid": tag_id},
                    )
                ).mappings().one()
    return _tag_row(row)


async def asession_tags_page(
    sessions: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    limit: int = SESSION_LIST_DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[SessionTagRow], int]:
    """Bounded per-session tag page + honest total; scope-blind refusal."""
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= SESSION_LIST_MAX_LIMIT
    ):
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.INVALID_PAGINATION,
                    "limit",
                    f"limit must be an integer in [1, {SESSION_LIST_MAX_LIMIT}]",
                )
            ]
        )
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.INVALID_PAGINATION,
                    "offset",
                    "offset must be an integer >= 0",
                )
            ]
        )
    async with sessions() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT t.session_id, t.tag_id, t.assigned_by,
                           t.assignment_source, t.created_at
                    FROM session_tags t
                    JOIN sessions s ON s.session_id = t.session_id
                    WHERE t.session_id = :sid AND s.server_id = :server_id
                    ORDER BY t.created_at DESC, t.tag_id
                    LIMIT :l OFFSET :o
                    """
                ),
                {"sid": session_id, "server_id": server_id, "l": limit, "o": offset},
            )
        ).mappings().all()
        total = (
            await session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM session_tags t
                    JOIN sessions s ON s.session_id = t.session_id
                    WHERE t.session_id = :sid AND s.server_id = :server_id
                    """
                ),
                {"sid": session_id, "server_id": server_id},
            )
        ).scalar_one()
    if not rows and int(total) == 0:
        # Distinguish an empty-but-visible page from a scope-blind refusal.
        await aget_session(sessions, session_id, server_id=server_id)
    return [_tag_row(row) for row in rows], int(total)


#: Read-oriented alias kept for call-site symmetry with the catalog helpers.
alist_session_tags = asession_tags_page


async def arename_session(
    sessions: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
    server_id: uuid.UUID | None,
    title: str,
) -> SessionRow:
    """Rename one session under its session-row lock (D07-fix1 F2).

    The trimmed title must stay in 1..120 characters (a tighter bound than
    the create path's 240: rename is an explicit user act, and the drawer's
    inline editor posts exactly this vocabulary). ``server_id=None``
    resolves the session's OWN stored scope (the drawer rename carries no
    scope pin); a supplied scope must match, else scope-blind not-found.
    The updated row is re-read under the SAME lock so the returned
    ``SessionRow`` is the committed truth.
    """
    if not isinstance(title, str):
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    "title",
                    "title must be a string",
                )
            ]
        )
    trimmed = title.strip()
    if not 1 <= len(trimmed) <= 120:
        raise SessionContractError(
            [
                SessionFieldError(
                    SessionErrorCode.FORBIDDEN_FIELD,
                    "title",
                    "title must be 1..120 characters when set",
                )
            ]
        )
    async with sessions() as session, session.begin():
        if server_id is None:
            stored = (
                await session.execute(
                    text("SELECT server_id FROM sessions WHERE session_id = :sid"),
                    {"sid": session_id},
                )
            ).scalar()
            if stored is None:
                raise _not_found("session_id")
            server_id = uuid.UUID(str(stored))
        await _lock_session(session, session_id=session_id, server_id=server_id)
        await session.execute(
            text(
                """
                UPDATE sessions
                SET title = :title, updated_at = now()
                WHERE session_id = :sid
                """
            ),
            {"title": trimmed, "sid": session_id},
        )
        row = (
            await session.execute(
                text(
                    """
                    SELECT session_id, server_id, service_id, source, status,
                           title, created_at, updated_at
                    FROM sessions
                    WHERE session_id = :sid
                    """
                ),
                {"sid": session_id},
            )
        ).mappings().one()
    return _session_row(row)


async def aremove_session_tag(
    sessions: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    tag_id: uuid.UUID,
    replacement_tag_id: uuid.UUID | None = None,
) -> None:
    """Remove one session-tag association (D07-fix1 F2); mirrors
    :func:`mai.tag_store.aremove_conversation_tag`:

    - scope-blind session refusal via :func:`_lock_session`;
    - an unbound tag is an idempotent no-op (204 either way);
    - the mandatory-at-least-one-tag invariant: removing the last tag
      without a replacement refuses ``last_tag`` (409); a replacement
      attaches in the SAME transaction (``assignment_source='system'``).
    """
    if replacement_tag_id is not None and replacement_tag_id == tag_id:
        raise TagStoreError(
            TagErrorCode.VALIDATION,
            "the replacement must differ from the removed tag",
            "replacement_tag_id",
        )
    try:
        async with sessions() as session, session.begin():
            await _lock_session(session, session_id=session_id, server_id=server_id)
            bound = (
                await session.execute(
                    text(
                        "SELECT 1 FROM session_tags "
                        "WHERE session_id = :sid AND tag_id = :tid"
                    ),
                    {"sid": session_id, "tid": tag_id},
                )
            ).scalar()
            if bound is None:
                return
            remaining = int(
                (
                    await session.execute(
                        text(
                            "SELECT COUNT(*) FROM session_tags "
                            "WHERE session_id = :sid"
                        ),
                        {"sid": session_id},
                    )
                ).scalar_one()
            )
            if remaining == 1 and replacement_tag_id is None:
                raise TagStoreError(
                    TagErrorCode.LAST_TAG,
                    "the last tag cannot be removed without a replacement",
                    "replacement_tag_id",
                )
            await session.execute(
                text(
                    "DELETE FROM session_tags "
                    "WHERE session_id = :sid AND tag_id = :tid"
                ),
                {"sid": session_id, "tid": tag_id},
            )
            if replacement_tag_id is not None:
                replacement = (
                    await session.execute(
                        text("SELECT tag_id FROM tags WHERE tag_id = :tid"),
                        {"tid": replacement_tag_id},
                    )
                ).first()
                if replacement is None:
                    raise TagStoreError(
                        TagErrorCode.TAG_NOT_FOUND,
                        "no catalog tag with this identity",
                        "replacement_tag_id",
                    )
                await session.execute(
                    text(
                        """
                        INSERT INTO session_tags (session_id, tag_id,
                                                  assignment_source)
                        VALUES (:sid, :tid, 'system')
                        ON CONFLICT (session_id, tag_id) DO NOTHING
                        """
                    ),
                    {"sid": session_id, "tid": replacement_tag_id},
                )
    except DBAPIError as exc:
        # Mirrors aremove_conversation_tag: classify commit-time deferred
        # trigger failures; raw DB errors never leak.
        raise aclassify_integrity_error(exc) from exc


async def apreview_session_tags(
    sessions: async_sessionmaker[AsyncSession],
    *,
    session_ids: Sequence[uuid.UUID],
    server_id: uuid.UUID | None = None,
) -> dict[uuid.UUID, EntityTagPreview]:
    """Batch tag previews for at most one list page of sessions (N5).

    Two bounded queries: per-session totals and the first 3 names
    (deterministic ``created_at, name`` order). ``server_id=None``
    previews the recent cross-server page (each id was already produced
    under its own scope). Sessions with zero tags are absent from the
    result — the caller renders honest absence, never a fabricated
    preview."""
    if not session_ids:
        return {}
    identifiers = [uuid.UUID(str(value)) for value in session_ids][:SESSION_LIST_MAX_LIMIT]
    scope_predicate = "" if server_id is None else "AND s.server_id = :server_id"
    params: dict[str, object] = {"ids": identifiers}
    if server_id is not None:
        params["server_id"] = server_id
    async with sessions() as session:
        totals = (
            await session.execute(
                text(
                    f"""
                    SELECT st.session_id, COUNT(*) AS total
                    FROM session_tags st
                    JOIN sessions s ON s.session_id = st.session_id
                    WHERE st.session_id = ANY(:ids)
                      {scope_predicate}
                    GROUP BY st.session_id
                    """
                ),
                params,
            )
        ).mappings().all()
        names = (
            await session.execute(
                text(
                    f"""
                    SELECT session_id, name
                    FROM (
                        SELECT st.session_id, t.name,
                               ROW_NUMBER() OVER (
                                   PARTITION BY st.session_id
                                   ORDER BY st.created_at, t.name
                               ) AS rn
                        FROM session_tags st
                        JOIN tags t ON t.tag_id = st.tag_id
                        JOIN sessions s ON s.session_id = st.session_id
                        WHERE st.session_id = ANY(:ids)
                          {scope_predicate}
                    ) ranked
                    WHERE rn <= 3
                    ORDER BY session_id, rn
                    """
                ),
                params,
            )
        ).mappings().all()
    totals_by_id = {uuid.UUID(str(row["session_id"])): int(row["total"]) for row in totals}
    names_by_id: dict[uuid.UUID, list[str]] = {}
    for row in names:
        names_by_id.setdefault(uuid.UUID(str(row["session_id"])), []).append(str(row["name"]))
    return {
        session_id: EntityTagPreview(
            preview=tuple(names_by_id.get(session_id, ())),
            total=total,
        )
        for session_id, total in totals_by_id.items()
    }
