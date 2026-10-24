"""Conversation-domain persistence over the 0008 schema (card 04).

App-owned storage for conversation identity, safe ordered transcript
content, run linkage/lease and the durable steering queue (act_CONTRACTS
§1–§3, ratified). ONE storage seam for the chat API producers (05/07/08)
and the runtime workers — no sibling redesigns (act_CONTRACTS §11).

Frozen-contract reuse (no second vocabulary lives here):

- scope: :func:`mai.contracts.conversations.assert_conversation_scope`
  (scenario C8 — a cross-server read fails CLOSED with
  ``conversation_not_found``, never leaking the other scope's existence);
- idempotency: :func:`content_fingerprint` +
  :func:`classify_retransmission` (scenario C4 — same
  ``client_request_id`` + same projected content deduplicates onto the
  existing row; changed content is the stable ``client_request_conflict``);
- pagination: :class:`ConversationTranscriptPage` /
  :func:`compute_has_more` with ``COUNT(*)`` under the identical filter
  predicate and deterministic ``ORDER BY seq, message_id LIMIT :l OFFSET
  :o`` (AGENTS.md §1 mandatory pagination);
- privacy: every persisted text passes
  :func:`mai.agents.output_safety.project_text` BEFORE the INSERT (C2:
  zero secret persistence); fingerprints are computed over the PROJECTED
  content, so idempotency is defined over stored truth.

Serialization: message ``seq`` allocation and client-request dedup run
inside one transaction holding ``SELECT ... FOR UPDATE`` on the
conversation row (scenario C6 — concurrent submissions serialize under
the conversation row lock; the UNIQUE ``(conversation_id, seq)`` and
``(conversation_id, client_request_id)`` constraints are the belt behind
the lock).

Producer honesty: roles cannot be forged. The public submit path admits
only ``role=user`` rows with browser/api provenance; assistant/tool/system
rows go through :func:`aappend_producer_message`, which enforces the
frozen role/provenance matrix (browser-authored assistant rows and
system-authored user rows are rejected — legacy mixed-origin material is
preserved as ``legacy_import`` provenance, act_CONTRACTS §4). The
``client_request_id`` idempotency key is a UI-authored concept and is
accepted on ``user`` rows only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents.output_safety import REDACTED, project_text
from mai.contracts.conversations import (
    DEFAULT_PAGE_LIMIT,
    MAX_MESSAGE_CHARS,
    MAX_PAGE_LIMIT,
    ConversationContractError,
    ConversationErrorCode,
    ConversationFieldError,
    ConversationTranscriptPage,
    HistoryCompleteness,
    MessageRange,
    MessageRole,
    MessageSourceKind,
    SteerAction,
    TranscriptMessage,
    TurnDisposition,
    assert_conversation_scope,
    classify_retransmission,
    compute_has_more,
    content_fingerprint,
)
from mai.contracts.tasks import TITLE_MAX_CHARS

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "ConversationRow",
    "MessagePageRow",
    "ProducerMessageRow",
    "RunLinkRow",
    "SteeringRow",
    "StoredMessage",
    "TurnAppendResult",
    "ROLE_SOURCE_MATRIX",
    "aappend_producer_message",
    "aattach_run",
    "acreate_conversation",
    "adelete_conversation",
    "aenqueue_steering",
    "aget_conversation",
    "alist_conversations",
    "alist_messages",
    "aactive_run",
    "aapply_steering",
    "arelease_run",
    "asubmit_turn",
]


# ---------------------------------------------------------------------------
# rows (plain frozen storage rows; the HTTP models live in the contracts)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConversationRow:
    """One ``conversations`` row (storage truth, not an HTTP view)."""

    conversation_id: uuid.UUID
    server_id: uuid.UUID
    title: str | None
    history_completeness: HistoryCompleteness
    missing_range: MessageRange | None
    active_task_id: uuid.UUID | None
    active_run_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StoredMessage:
    """One persisted ``conversation_messages`` row."""

    conversation_id: uuid.UUID
    message_id: uuid.UUID
    sequence: int
    role: MessageRole
    source_kind: MessageSourceKind
    content: str
    content_fingerprint: str
    redacted: bool
    content_ref: str | None
    client_request_id: uuid.UUID | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TurnAppendResult:
    """Outcome of one turn submit: the row plus its disposition."""

    message: StoredMessage
    disposition: TurnDisposition


@dataclass(frozen=True, slots=True)
class MessagePageRow:
    """One list-page summary row (safe metadata; no transcript content)."""

    conversation_id: uuid.UUID
    server_id: uuid.UUID
    title: str | None
    created_at: datetime
    updated_at: datetime
    message_count: int
    active_task_id: uuid.UUID | None
    active_run_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class RunLinkRow:
    """One ``conversation_runs`` linkage row (NULL ``released_at`` = lease)."""

    conversation_id: uuid.UUID
    run_id: uuid.UUID
    task_id: uuid.UUID | None
    created_at: datetime
    released_at: datetime | None


@dataclass(frozen=True, slots=True)
class SteeringRow:
    """One durable steering/control queue row."""

    steering_id: uuid.UUID
    conversation_id: uuid.UUID
    run_id: uuid.UUID | None
    client_request_id: uuid.UUID
    action: SteerAction
    content: str | None
    delivery_status: str
    created_at: datetime
    applied_at: datetime | None


@dataclass(frozen=True, slots=True)
class ProducerMessageRow:
    """Outcome of one trusted-producer append."""

    message: StoredMessage


# ---------------------------------------------------------------------------
# frozen role/provenance admissibility matrix (§1/§4)
# ---------------------------------------------------------------------------

#: Which provenance may back which role. A ``user`` row is human evidence:
#: it may arrive over the browser or API, or be recovered as
#: ``legacy_import`` — never ``system`` (a system-authored "user" row would
#: forge user authority). ``assistant``/``tool`` rows are model/executor
#: output: trusted producer paths only, never ``browser`` (browser
#: reconstructed material stays legacy mixed-origin, §4). ``system`` rows
#: carry platform provenance or legacy recovery.
ROLE_SOURCE_MATRIX: Final[Mapping[MessageRole, frozenset[MessageSourceKind]]] = {
    MessageRole.USER: frozenset(
        {
            MessageSourceKind.BROWSER,
            MessageSourceKind.API,
            MessageSourceKind.LEGACY_IMPORT,
        }
    ),
    MessageRole.ASSISTANT: frozenset(
        {
            MessageSourceKind.API,
            MessageSourceKind.SYSTEM,
            MessageSourceKind.LEGACY_IMPORT,
        }
    ),
    MessageRole.TOOL: frozenset(
        {
            MessageSourceKind.API,
            MessageSourceKind.SYSTEM,
            MessageSourceKind.LEGACY_IMPORT,
        }
    ),
    MessageRole.SYSTEM: frozenset(
        {MessageSourceKind.SYSTEM, MessageSourceKind.LEGACY_IMPORT}
    ),
}

#: Provenance admitted on the public submit path (real-time user turns).
_SUBMIT_SOURCES: Final[frozenset[MessageSourceKind]] = frozenset(
    {MessageSourceKind.BROWSER, MessageSourceKind.API}
)

_CONVERSATION_COLUMNS: Final[str] = (
    "conversation_id, server_id, title, history_completeness, "
    "missing_from_seq, missing_to_seq, active_task_id, active_run_id, "
    "created_at, updated_at"
)


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------


def _not_found() -> ConversationContractError:
    """The single closed error for a missing/out-of-scope conversation."""
    return ConversationContractError(
        [
            ConversationFieldError(
                ConversationErrorCode.CONVERSATION_NOT_FOUND,
                "conversation_id",
                "no conversation in the requesting server scope",
            )
        ]
    )


def _check_page_bounds(limit: int, offset: int) -> None:
    """Bounded-page validation (AGENTS.md §1): fail closed, never clamp."""
    limit_ok = isinstance(limit, int) and not isinstance(limit, bool)
    if not limit_ok or not 1 <= limit <= MAX_PAGE_LIMIT:
        raise ValueError(f"limit must be an integer in [1, {MAX_PAGE_LIMIT}]")
    offset_ok = isinstance(offset, int) and not isinstance(offset, bool)
    if not offset_ok or offset < 0:
        raise ValueError("offset must be an integer >= 0")


def _row_to_conversation(row: Mapping[str, object]) -> ConversationRow:
    from_seq = row["missing_from_seq"]
    to_seq = row["missing_to_seq"]
    missing = (
        None
        if from_seq is None
        else MessageRange(
            from_sequence=int(from_seq),  # type: ignore[arg-type]
            to_sequence=int(to_seq),  # type: ignore[arg-type]
        )
    )
    return ConversationRow(
        conversation_id=row["conversation_id"],  # type: ignore[arg-type]
        server_id=row["server_id"],  # type: ignore[arg-type]
        title=row["title"],  # type: ignore[arg-type]
        history_completeness=HistoryCompleteness(str(row["history_completeness"])),
        missing_range=missing,
        active_task_id=row["active_task_id"],  # type: ignore[arg-type]
        active_run_id=row["active_run_id"],  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
        updated_at=row["updated_at"],  # type: ignore[arg-type]
    )


def _row_to_message(row: Mapping[str, object]) -> StoredMessage:
    return StoredMessage(
        conversation_id=row["conversation_id"],  # type: ignore[arg-type]
        message_id=row["message_id"],  # type: ignore[arg-type]
        sequence=int(row["seq"]),  # type: ignore[arg-type]
        role=MessageRole(str(row["role"])),
        source_kind=MessageSourceKind(str(row["source_kind"])),
        content=str(row["content"]),
        content_fingerprint=str(row["content_fingerprint"]),
        redacted=bool(row["redacted"]),
        content_ref=row["content_ref"],  # type: ignore[arg-type]
        client_request_id=row["client_request_id"],  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
    )


async def _alocked_conversation(
    session: AsyncSession, conversation_id: uuid.UUID
) -> Mapping[str, object]:
    """Fetch one conversation row under ``FOR UPDATE`` (C6 serialization).

    Raises the closed ``conversation_not_found`` error when the row does
    not exist; scope is checked by the caller against the request's
    ``server_id``.
    """
    row = (
        await session.execute(
            text(
                f"SELECT {_CONVERSATION_COLUMNS} FROM conversations "
                "WHERE conversation_id = :cid FOR UPDATE"
            ),
            {"cid": conversation_id},
        )
    ).mappings().first()
    if row is None:
        raise _not_found()
    return row


async def _achecked_conversation(
    sessions: async_sessionmaker[AsyncSession],
    conversation_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
) -> ConversationRow:
    """Scope-checked read of one conversation (C8; no lock needed)."""
    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    f"SELECT {_CONVERSATION_COLUMNS} FROM conversations "
                    "WHERE conversation_id = :cid"
                ),
                {"cid": conversation_id},
            )
        ).mappings().first()
    if row is None:
        raise _not_found()
    conversation = _row_to_conversation(row)
    assert_conversation_scope(server_id, conversation.server_id)
    return conversation


def _project_content(content: str) -> tuple[str, bool]:
    """Project one raw text to its persistable form (C2 ordering).

    Redaction happens BEFORE storage; the bound is applied by
    :func:`project_text` AFTER redaction, so a cut can never split a
    secret. Returns the safe content and whether the redaction marker is
    present (the honest ``redacted`` flag — no textual-fidelity claim).
    """
    safe = project_text(content, max_chars=MAX_MESSAGE_CHARS)
    return safe, REDACTED in safe


def _validate_submit_text(content: str) -> None:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content must be a non-blank string")


# ---------------------------------------------------------------------------
# conversation identity
# ---------------------------------------------------------------------------


async def acreate_conversation(
    sessions: async_sessionmaker[AsyncSession],
    *,
    server_id: uuid.UUID,
    title: str | None = None,
    history_completeness: HistoryCompleteness = HistoryCompleteness.COMPLETE,
    missing_range: MessageRange | None = None,
    active_task_id: uuid.UUID | None = None,
    active_run_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
) -> ConversationRow:
    """Create one conversation pinned to its server scope.

    ``history_completeness``/``missing_range`` exist for legacy-recovery
    creators (§4): a recovered conversation honestly declares what is
    missing instead of fabricating complete history.
    """
    if title is not None:
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must be a non-blank string when set")
        if len(title) > TITLE_MAX_CHARS:
            raise ValueError(f"title must be at most {TITLE_MAX_CHARS} characters")
    if not isinstance(history_completeness, HistoryCompleteness):
        raise ValueError("history_completeness must be a HistoryCompleteness value")
    complete = history_completeness is HistoryCompleteness.COMPLETE
    if missing_range is not None and complete:
        raise ValueError("a complete history cannot declare a missing range")
    cid = conversation_id or uuid.uuid4()
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                """
                INSERT INTO conversations
                    (conversation_id, server_id, title, history_completeness,
                     missing_from_seq, missing_to_seq, active_task_id,
                     active_run_id)
                VALUES
                    (:cid, :sid, :title, :completeness, :mfrom, :mto,
                     :task, :run)
                """
            ),
            {
                "cid": cid,
                "sid": server_id,
                "title": title,
                "completeness": history_completeness.value,
                "mfrom": missing_range.from_sequence if missing_range else None,
                "mto": missing_range.to_sequence if missing_range else None,
                "task": active_task_id,
                "run": active_run_id,
            },
        )
    return await _achecked_conversation(sessions, cid, server_id=server_id)


async def aget_conversation(
    sessions: async_sessionmaker[AsyncSession],
    conversation_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
) -> ConversationRow:
    """Scope-checked singleton read (C8 fails closed on any scope mismatch)."""
    return await _achecked_conversation(
        sessions, conversation_id, server_id=server_id
    )


async def alist_conversations(
    sessions: async_sessionmaker[AsyncSession],
    *,
    server_id: uuid.UUID,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
) -> tuple[list[MessagePageRow], int]:
    """Server-scoped bounded conversation list page (§3 ``GET`` list).

    Deterministic ``ORDER BY updated_at DESC, conversation_id`` with the
    ``COUNT(*)`` computed under the identical ``server_id`` predicate.
    Returns ``(items, total)``.
    """
    _check_page_bounds(limit, offset)
    async with sessions() as session:
        total = int(
            (
                await session.execute(
                    text("SELECT COUNT(*) FROM conversations WHERE server_id = :sid"),
                    {"sid": server_id},
                )
            ).scalar_one()
        )
        rows = (
            await session.execute(
                text(
                    """
                    SELECT c.conversation_id, c.server_id, c.title,
                           c.created_at, c.updated_at,
                           (SELECT COUNT(*) FROM conversation_messages m
                            WHERE m.conversation_id = c.conversation_id)
                               AS message_count,
                           c.active_task_id, c.active_run_id
                    FROM conversations c
                    WHERE c.server_id = :sid
                    ORDER BY c.updated_at DESC, c.conversation_id
                    LIMIT :l OFFSET :o
                    """
                ),
                {"sid": server_id, "l": limit, "o": offset},
            )
        ).mappings().all()
    items = [
        MessagePageRow(
            conversation_id=row["conversation_id"],  # type: ignore[arg-type]
            server_id=row["server_id"],  # type: ignore[arg-type]
            title=row["title"],  # type: ignore[arg-type]
            created_at=row["created_at"],  # type: ignore[arg-type]
            updated_at=row["updated_at"],  # type: ignore[arg-type]
            message_count=int(row["message_count"]),
            active_task_id=row["active_task_id"],  # type: ignore[arg-type]
            active_run_id=row["active_run_id"],  # type: ignore[arg-type]
        )
        for row in rows
    ]
    return items, total


async def adelete_conversation(
    sessions: async_sessionmaker[AsyncSession],
    conversation_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
) -> None:
    """Scope-checked delete; transcript/runs/steering cascade (§2 retention)."""
    async with sessions() as session, session.begin():
        row = await _alocked_conversation(session, conversation_id)
        assert_conversation_scope(server_id, row["server_id"])  # type: ignore[arg-type]
        await session.execute(
            text("DELETE FROM conversations WHERE conversation_id = :cid"),
            {"cid": conversation_id},
        )


# ---------------------------------------------------------------------------
# transcript writes (C4 idempotency, C6 serialization)
# ---------------------------------------------------------------------------


async def asubmit_turn(
    sessions: async_sessionmaker[AsyncSession],
    *,
    conversation_id: uuid.UUID,
    server_id: uuid.UUID,
    client_request_id: uuid.UUID,
    message: str,
    source_kind: MessageSourceKind = MessageSourceKind.API,
) -> TurnAppendResult:
    """Persist one user turn under its idempotency key (§1/§3; C4/C6).

    One transaction: lock the conversation row, check scope, project the
    content through ``project_text``, dedup on
    ``(conversation_id, client_request_id)`` by comparing PROJECTED
    fingerprints, then allocate the next ``seq`` and insert.

    Same key + same projected content → ``DEDUPLICATED`` carrying the
    ORIGINAL row. Same key + different projected content → the stable
    ``client_request_conflict``. The returned disposition is the store
    truth the HTTP layer maps onto 201/200 (act_CONTRACTS §3).
    """
    _validate_submit_text(message)
    if source_kind not in _SUBMIT_SOURCES:
        raise ValueError(
            "the submit path admits browser/api provenance only; "
            "legacy/system material has its own trusted producer path"
        )
    safe_content, redacted = _project_content(message)
    fingerprint = content_fingerprint(safe_content)
    async with sessions() as session, session.begin():
        row = await _alocked_conversation(session, conversation_id)
        assert_conversation_scope(server_id, row["server_id"])  # type: ignore[arg-type]
        existing = (
            await session.execute(
                text(
                    "SELECT * FROM conversation_messages "
                    "WHERE conversation_id = :cid "
                    "AND client_request_id = :crid"
                ),
                {"cid": conversation_id, "crid": client_request_id},
            )
        ).mappings().first()
        if existing is not None:
            # Same key: the fingerprint pair decides dedup vs conflict.
            disposition = classify_retransmission(
                fingerprint, str(existing["content_fingerprint"])
            )
            return TurnAppendResult(
                message=_row_to_message(existing), disposition=disposition
            )
        seq = int(
            (
                await session.execute(
                    text(
                        "SELECT COALESCE(MAX(seq), 0) + 1 "
                        "FROM conversation_messages WHERE conversation_id = :cid"
                    ),
                    {"cid": conversation_id},
                )
            ).scalar_one()
        )
        message_id = uuid.uuid4()
        inserted = (
            await session.execute(
                text(
                    """
                    INSERT INTO conversation_messages
                        (message_id, conversation_id, seq, role, source_kind,
                         content, content_fingerprint, redacted, content_ref,
                         client_request_id)
                    VALUES
                        (:mid, :cid, :seq, :role, :source, :content,
                         :fingerprint, :redacted, NULL, :crid)
                    RETURNING *
                    """
                ),
                {
                    "mid": message_id,
                    "cid": conversation_id,
                    "seq": seq,
                    "role": MessageRole.USER.value,
                    "source": source_kind.value,
                    "content": safe_content,
                    "fingerprint": fingerprint,
                    "redacted": redacted,
                    "crid": client_request_id,
                },
            )
        ).mappings().one()
        await session.execute(
            text(
                "UPDATE conversations SET updated_at = now() "
                "WHERE conversation_id = :cid"
            ),
            {"cid": conversation_id},
        )
        return TurnAppendResult(
            message=_row_to_message(inserted), disposition=TurnDisposition.CREATED
        )


async def aappend_producer_message(
    sessions: async_sessionmaker[AsyncSession],
    *,
    conversation_id: uuid.UUID,
    server_id: uuid.UUID,
    role: MessageRole,
    source_kind: MessageSourceKind,
    content: str,
    client_request_id: uuid.UUID | None = None,
    content_ref: str | None = None,
) -> ProducerMessageRow:
    """Trusted-producer append (assistant/tool/system/legacy rows, §1/§4).

    Enforces the frozen :data:`ROLE_SOURCE_MATRIX` (forged combos are
    rejected) and confines the ``client_request_id`` idempotency key to
    user rows. Content is projected through ``project_text`` exactly like
    the submit path (C2: zero secret persistence).
    """
    _validate_submit_text(content)
    if not isinstance(role, MessageRole):
        raise ValueError("role must be a MessageRole value")
    if not isinstance(source_kind, MessageSourceKind):
        raise ValueError("source_kind must be a MessageSourceKind value")
    if source_kind not in ROLE_SOURCE_MATRIX[role]:
        raise ValueError(
            f"provenance {source_kind.value} is not admissible for role "
            f"{role.value} (frozen role/provenance matrix)"
        )
    if client_request_id is not None and role is not MessageRole.USER:
        raise ValueError(
            "client_request_id is a UI-authored idempotency key and is "
            "accepted on user rows only"
        )
    if content_ref is not None and (
        not isinstance(content_ref, str)
        or not content_ref.strip()
        or len(content_ref) > 512
    ):
        raise ValueError("content_ref must be a non-blank string of at most 512 chars")
    safe_content, redacted = _project_content(content)
    fingerprint = content_fingerprint(safe_content)
    async with sessions() as session, session.begin():
        row = await _alocked_conversation(session, conversation_id)
        assert_conversation_scope(server_id, row["server_id"])  # type: ignore[arg-type]
        if client_request_id is not None:
            existing = (
                await session.execute(
                    text(
                        "SELECT * FROM conversation_messages "
                        "WHERE conversation_id = :cid "
                        "AND client_request_id = :crid"
                    ),
                    {"cid": conversation_id, "crid": client_request_id},
                )
            ).mappings().first()
            if existing is not None:
                # Same key: classify (raises client_request_conflict on
                # mismatched content), otherwise dedup onto the original.
                classify_retransmission(
                    fingerprint, str(existing["content_fingerprint"])
                )
                return ProducerMessageRow(message=_row_to_message(existing))
        seq = int(
            (
                await session.execute(
                    text(
                        "SELECT COALESCE(MAX(seq), 0) + 1 "
                        "FROM conversation_messages WHERE conversation_id = :cid"
                    ),
                    {"cid": conversation_id},
                )
            ).scalar_one()
        )
        inserted = (
            await session.execute(
                text(
                    """
                    INSERT INTO conversation_messages
                        (message_id, conversation_id, seq, role, source_kind,
                         content, content_fingerprint, redacted, content_ref,
                         client_request_id)
                    VALUES
                        (:mid, :cid, :seq, :role, :source, :content,
                         :fingerprint, :redacted, :cref, :crid)
                    RETURNING *
                    """
                ),
                {
                    "mid": uuid.uuid4(),
                    "cid": conversation_id,
                    "seq": seq,
                    "role": role.value,
                    "source": source_kind.value,
                    "content": safe_content,
                    "fingerprint": fingerprint,
                    "redacted": redacted,
                    "cref": content_ref,
                    "crid": client_request_id,
                },
            )
        ).mappings().one()
        await session.execute(
            text(
                "UPDATE conversations SET updated_at = now() "
                "WHERE conversation_id = :cid"
            ),
            {"cid": conversation_id},
        )
        return ProducerMessageRow(message=_row_to_message(inserted))


async def alist_messages(
    sessions: async_sessionmaker[AsyncSession],
    conversation_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    role: MessageRole | None = None,
) -> ConversationTranscriptPage:
    """Deterministic sanitized transcript page (§3 ``GET`` messages).

    Ordered ``(seq, message_id)``; ``total`` is the ``COUNT(*)`` under the
    IDENTICAL filter predicate (conversation scope + optional role); the
    page carries the conversation's honest history-completeness marker.
    """
    _check_page_bounds(limit, offset)
    if role is not None and not isinstance(role, MessageRole):
        raise ValueError("role filter must be a MessageRole value or None")
    conversation = await _achecked_conversation(
        sessions, conversation_id, server_id=server_id
    )
    predicate = "WHERE conversation_id = :cid"
    params: dict[str, object] = {"cid": conversation_id, "l": limit, "o": offset}
    if role is not None:
        predicate += " AND role = :role"
        params["role"] = role.value
    async with sessions() as session:
        total = int(
            (
                await session.execute(
                    text(f"SELECT COUNT(*) FROM conversation_messages {predicate}"),
                    params,
                )
            ).scalar_one()
        )
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT message_id, conversation_id, seq, role, source_kind,
                           created_at, content, redacted, content_ref
                    FROM conversation_messages {predicate}
                    ORDER BY seq, message_id
                    LIMIT :l OFFSET :o
                    """
                ),
                params,
            )
        ).mappings().all()
    items = [
        TranscriptMessage(
            conversation_id=row["conversation_id"],  # type: ignore[arg-type]
            message_id=row["message_id"],  # type: ignore[arg-type]
            sequence=int(row["seq"]),  # type: ignore[arg-type]
            role=MessageRole(str(row["role"])),
            source_kind=MessageSourceKind(str(row["source_kind"])),
            created_at=row["created_at"],  # type: ignore[arg-type]
            content=str(row["content"]),
            redacted=bool(row["redacted"]),
            content_ref=row["content_ref"],  # type: ignore[arg-type]
        )
        for row in rows
    ]
    return ConversationTranscriptPage(
        conversation_id=conversation.conversation_id,
        history_completeness=conversation.history_completeness,
        limit=limit,
        offset=offset,
        total=total,
        has_more=compute_has_more(offset, len(items), total),
        items=items,
    )


# ---------------------------------------------------------------------------
# run linkage / writer lease (§1)
# ---------------------------------------------------------------------------


async def aattach_run(
    sessions: async_sessionmaker[AsyncSession],
    *,
    conversation_id: uuid.UUID,
    server_id: uuid.UUID,
    run_id: uuid.UUID,
    task_id: uuid.UUID | None = None,
) -> RunLinkRow:
    """Attach one durable run to the conversation (idempotent attach).

    Sets the conversation's ``active_run_id``; the new link row starts
    lease-holding (``released_at IS NULL``).
    """
    async with sessions() as session, session.begin():
        row = await _alocked_conversation(session, conversation_id)
        assert_conversation_scope(server_id, row["server_id"])  # type: ignore[arg-type]
        await session.execute(
            text(
                """
                INSERT INTO conversation_runs
                    (conversation_id, run_id, task_id)
                VALUES (:cid, :rid, :task)
                ON CONFLICT (conversation_id, run_id) DO NOTHING
                """
            ),
            {"cid": conversation_id, "rid": run_id, "task": task_id},
        )
        await session.execute(
            text(
                "UPDATE conversations SET active_run_id = :rid, "
                "updated_at = now() WHERE conversation_id = :cid"
            ),
            {"cid": conversation_id, "rid": run_id},
        )
        link = (
            await session.execute(
                text(
                    "SELECT conversation_id, run_id, task_id, created_at, "
                    "released_at FROM conversation_runs "
                    "WHERE conversation_id = :cid AND run_id = :rid"
                ),
                {"cid": conversation_id, "rid": run_id},
            )
        ).mappings().one()
    return RunLinkRow(
        conversation_id=link["conversation_id"],  # type: ignore[arg-type]
        run_id=link["run_id"],  # type: ignore[arg-type]
        task_id=link["task_id"],  # type: ignore[arg-type]
        created_at=link["created_at"],  # type: ignore[arg-type]
        released_at=link["released_at"],  # type: ignore[arg-type]
    )


async def arelease_run(
    sessions: async_sessionmaker[AsyncSession],
    *,
    conversation_id: uuid.UUID,
    server_id: uuid.UUID,
    run_id: uuid.UUID,
) -> RunLinkRow:
    """Release the writer lease held by ``run_id`` (idempotent release)."""
    async with sessions() as session, session.begin():
        row = await _alocked_conversation(session, conversation_id)
        assert_conversation_scope(server_id, row["server_id"])  # type: ignore[arg-type]
        released = (
            await session.execute(
                text(
                    "UPDATE conversation_runs SET released_at = now() "
                    "WHERE conversation_id = :cid AND run_id = :rid "
                    "AND released_at IS NULL "
                    "RETURNING conversation_id, run_id, task_id, created_at, "
                    "released_at"
                ),
                {"cid": conversation_id, "rid": run_id},
            )
        ).mappings().first()
        if released is None:
            link = (
                await session.execute(
                    text(
                        "SELECT conversation_id, run_id, task_id, created_at, "
                        "released_at FROM conversation_runs "
                        "WHERE conversation_id = :cid AND run_id = :rid"
                    ),
                    {"cid": conversation_id, "rid": run_id},
                )
            ).mappings().first()
            if link is None:
                raise ValueError("no conversation_runs link for that run_id")
            released = link
        await session.execute(
            text(
                "UPDATE conversations SET active_run_id = NULL "
                "WHERE conversation_id = :cid AND active_run_id = :rid"
            ),
            {"cid": conversation_id, "rid": run_id},
        )
    return RunLinkRow(
        conversation_id=released["conversation_id"],  # type: ignore[arg-type]
        run_id=released["run_id"],  # type: ignore[arg-type]
        task_id=released["task_id"],  # type: ignore[arg-type]
        created_at=released["created_at"],  # type: ignore[arg-type]
        released_at=released["released_at"],  # type: ignore[arg-type]
    )


async def aactive_run(
    sessions: async_sessionmaker[AsyncSession],
    conversation_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
) -> RunLinkRow | None:
    """The conversation's current lease-holding run, when one exists."""
    conversation = await _achecked_conversation(
        sessions, conversation_id, server_id=server_id
    )
    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    "SELECT conversation_id, run_id, task_id, created_at, "
                    "released_at FROM conversation_runs "
                    "WHERE conversation_id = :cid AND released_at IS NULL "
                    "ORDER BY created_at DESC, run_id LIMIT 1"
                ),
                {"cid": conversation.conversation_id},
            )
        ).mappings().first()
    if row is None:
        return None
    return RunLinkRow(
        conversation_id=row["conversation_id"],  # type: ignore[arg-type]
        run_id=row["run_id"],  # type: ignore[arg-type]
        task_id=row["task_id"],  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
        released_at=row["released_at"],  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# durable steering queue (§2/§3)
# ---------------------------------------------------------------------------


async def aenqueue_steering(
    sessions: async_sessionmaker[AsyncSession],
    *,
    conversation_id: uuid.UUID,
    server_id: uuid.UUID,
    action: SteerAction,
    client_request_id: uuid.UUID,
    content: str | None = None,
    run_id: uuid.UUID | None = None,
) -> tuple[SteeringRow, TurnDisposition]:
    """Durably enqueue one control request BEFORE queue notification (§3).

    Atomic enqueue recovery: the UNIQUE
    ``(conversation_id, client_request_id)`` row is the durable delivery
    identity — a crash between enqueue and delivery replays onto the SAME
    row (``DEDUPLICATED``), never a second intake. ``steer`` requires
    non-blank content; ``pause``/``cancel`` are deterministic and carry
    none (mirrors :class:`mai.contracts.conversations.ConversationSteerRequest`).
    Content is projected through ``project_text`` (C2).
    """
    if not isinstance(action, SteerAction):
        raise ValueError("action must be a SteerAction value")
    if action is SteerAction.STEER:
        _validate_submit_text(content)  # type: ignore[arg-type]
    elif content is not None:
        raise ValueError("pause/cancel are deterministic controls and carry no message")
    projected: str | None = None
    if content is not None:
        projected, _ = _project_content(content)
    async with sessions() as session, session.begin():
        row = await _alocked_conversation(session, conversation_id)
        assert_conversation_scope(server_id, row["server_id"])  # type: ignore[arg-type]
        existing = (
            await session.execute(
                text(
                    "SELECT * FROM conversation_steering "
                    "WHERE conversation_id = :cid "
                    "AND client_request_id = :crid"
                ),
                {"cid": conversation_id, "crid": client_request_id},
            )
        ).mappings().first()
        if existing is not None:
            return _row_to_steering(existing), TurnDisposition.DEDUPLICATED
        inserted = (
            await session.execute(
                text(
                    """
                    INSERT INTO conversation_steering
                        (steering_id, conversation_id, run_id,
                         client_request_id, action, content)
                    VALUES (:stid, :cid, :rid, :crid, :action, :content)
                    RETURNING *
                    """
                ),
                {
                    "stid": uuid.uuid4(),
                    "cid": conversation_id,
                    "rid": run_id,
                    "crid": client_request_id,
                    "action": action.value,
                    "content": projected,
                },
            )
        ).mappings().one()
        return _row_to_steering(inserted), TurnDisposition.CREATED


async def aapply_steering(
    sessions: async_sessionmaker[AsyncSession],
    *,
    conversation_id: uuid.UUID,
    server_id: uuid.UUID,
    steering_id: uuid.UUID,
) -> SteeringRow:
    """Mark one pending control applied at its safe boundary (§3).

    Idempotent: an already-applied/cancelled row is returned unchanged
    with its durable state; the UI displays pending/applied distinctly.
    """
    async with sessions() as session, session.begin():
        row = await _alocked_conversation(session, conversation_id)
        server = row["server_id"]
        assert_conversation_scope(server_id, server)  # type: ignore[arg-type]
        updated = (
            await session.execute(
                text(
                    "UPDATE conversation_steering "
                    "SET delivery_status = 'applied', applied_at = now() "
                    "WHERE steering_id = :stid AND delivery_status = 'pending' "
                    "RETURNING *"
                ),
                {"stid": steering_id},
            )
        ).mappings().first()
        if updated is None:
            updated = (
                await session.execute(
                    text(
                        "SELECT * FROM conversation_steering "
                        "WHERE steering_id = :stid"
                    ),
                    {"stid": steering_id},
                )
            ).mappings().first()
            if updated is None or updated["conversation_id"] != conversation_id:
                raise ValueError(
                    "no steering row for that steering_id in this conversation"
                )
    return _row_to_steering(updated)


def _row_to_steering(row: Mapping[str, object]) -> SteeringRow:
    return SteeringRow(
        steering_id=row["steering_id"],  # type: ignore[arg-type]
        conversation_id=row["conversation_id"],  # type: ignore[arg-type]
        run_id=row["run_id"],  # type: ignore[arg-type]
        client_request_id=row["client_request_id"],  # type: ignore[arg-type]
        action=SteerAction(str(row["action"])),
        content=row["content"],  # type: ignore[arg-type]
        delivery_status=str(row["delivery_status"]),
        created_at=row["created_at"],  # type: ignore[arg-type]
        applied_at=row["applied_at"],  # type: ignore[arg-type]
    )
