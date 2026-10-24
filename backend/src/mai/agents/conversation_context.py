"""Role-preserving conversation context assembly (06-context-continuity).

The ONE seam that turns canonical server records
(``mai.agents.conversation_store``) into the PydanticAI ``message_history``
seed for a continued conversation run — and the trusted producer boundary
that records the canonical assistant reply when the run finishes.

Native multi-turn history (hard architectural rule)
---------------------------------------------------

Prior turns are assembled as native alternating
``ModelRequest(parts=[UserPromptPart(...)])`` and
``ModelResponse(parts=[TextPart(...)])`` messages in natural chronological
order, preceded by the leading
``ModelRequest(parts=[SystemPromptPart(system_prompt)])`` and followed by
the new user turn as the final user request. Prior assistant text stays
strictly inside ``ModelResponse`` — it can never be elevated to
``UserPromptPart``/``SystemPromptPart`` authority, and prior user text is
never rewritten into a synthetic recap wrapper inside the prompt.

Budget honesty (C3)
--------------------

The serialized seed is bounded by :data:`SEED_BUDGET_BYTES`. Turns are
kept verbatim newest-first while they fit; older turns are compacted into
ONE bounded provenance-carrying block on the SYSTEM channel (the pinned
harness's ``SUMMARY_PREFIX`` summary form, so later in-run compaction
passes recognize and anchor on it). The block carries a bounded excerpt of
every omitted turn with its role, sequence and provenance — required user
constraints survive compaction readably, nothing is silently dropped, and
nothing is fabricated. The report counts every included, compacted and
skipped row. This module stays import-independent of ``mai.agents.run``
(that module imports THIS one); the invariant
``SEED_BUDGET_BYTES * 2 <= run.MAX_HISTORY_BYTES`` is pinned by test.

Legacy honesty (C7)
--------------------

The conversation's ``history_completeness`` marker and ``missing_range``
are carried from the store row into the assembly report verbatim; only
authoritative ``conversation_messages`` rows become model-visible turns.
When the marker is ``partial``/``unavailable``, authoritative
``job_events`` submission records are consulted ONLY to count provable
prior submissions whose content is not recoverable — the count is
reported, never converted into fabricated turns or placeholder text.

Producer boundary
-----------------

:func:`arecord_assistant_reply` is the trusted producer path for the
canonical assistant turn: idempotent per ``run_id`` (replays/restarts
never duplicate the row), provenance-marked via the safe ``content_ref``
reference (the run owns the sanitized tool/evidence receipts), content
projected through the store's C2 ``project_text`` boundary exactly like
every other row.

No prompt content crosses this module's return boundary except into the
checkpointed seed itself; every report field is value-free metadata.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)
from pydantic_ai_harness.compaction._summarizing_compaction import SUMMARY_PREFIX
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents.conversation_store import (
    MAX_PAGE_LIMIT,
    StoredMessage,
    aappend_producer_message,
    aget_conversation,
    alist_messages,
)
from mai.contracts.conversations import (
    HistoryCompleteness,
    MessageRole,
    MessageSourceKind,
)

__all__ = [
    "COMPACTED_BLOCK_MAX_CHARS",
    "ELIDED_EXCERPT_MARKER",
    "LEGACY_PROVEN_SUBMISSIONS_CAP",
    "PRODUCER_CONTENT_REF_PREFIX",
    "SEED_BUDGET_BYTES",
    "SEED_EXCERPT_MAX_CHARS",
    "SEED_MAX_PRIOR_TURNS",
    "ConversationSeed",
    "SeedReport",
    "abuild_conversation_seed",
    "aconversation_for_run",
    "arecord_assistant_reply",
    "compact_block_text",
    "native_turns",
]


# ---------------------------------------------------------------------------
# frozen bounds (06-context-continuity; AGENTS.md §1 bounded collections)
# ---------------------------------------------------------------------------

#: Serialized seed budget. Deliberately half of ``run.MAX_HISTORY_BYTES``
#: (the run-level checkpoint cap stays the last-resort honest failure
#: after in-run compaction): the seed always leaves TieredCompaction room
#: to work with in the first round.
SEED_BUDGET_BYTES: Final[int] = 1024 * 1024

#: Upper bound on prior turns read into one seed (the byte budget bites
#: first; this caps pathological many-tiny-message histories and bounds
#: the store read pattern).
SEED_MAX_PRIOR_TURNS: Final[int] = 200

#: Per-turn excerpt ceiling inside the compaction block (chars).
SEED_EXCERPT_MAX_CHARS: Final[int] = 600

#: Whole compaction block ceiling (chars). Oldest excerpts yield first;
#: the block always states how many excerpts were themselves elided.
COMPACTED_BLOCK_MAX_CHARS: Final[int] = 24_000

#: Explicit marker prefixed to the count of excerpts elided at the block
#: bound (value-free; never a fabricated turn).
ELIDED_EXCERPT_MARKER: Final[str] = "[...] "

#: Bounded cap on the legacy proven-submission count query (C7; the
#: count is metadata, never content).
LEGACY_PROVEN_SUBMISSIONS_CAP: Final[int] = 10_000

#: Safe ``content_ref`` prefix marking ONE canonical producer append per
#: run (the idempotency key of the assistant producer boundary).
PRODUCER_CONTENT_REF_PREFIX: Final[str] = "agent-run:"


# ---------------------------------------------------------------------------
# assembly result rows (plain frozen values; no ORM)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeedReport:
    """Value-free assembly report (the honest seed receipt).

    Every field is safe metadata: counts, bounds and completeness
    markers — never prompt or transcript content (C05 rule 4).
    """

    conversation_linked: bool
    conversation_id: str | None
    history_completeness: str
    missing_from_sequence: int | None
    missing_to_sequence: int | None
    prior_turns_total: int
    turns_included_verbatim: int
    turns_compacted: int
    excerpts_elided: int
    user_rows_included: int
    assistant_rows_included: int
    non_chat_rows_skipped: int
    current_turn_rows_excluded: int
    legacy_proven_submissions: int
    seed_messages: int
    seed_bytes: int
    budget_bytes: int


@dataclass(frozen=True, slots=True)
class ConversationSeed:
    """One assembled conversation seed: the checkpointed JSON + report."""

    history_json: str
    report: SeedReport


@dataclass(frozen=True, slots=True)
class _TurnView:
    """Internal: the role-preserving turns of one transcript snapshot."""

    rows: list[StoredMessage]  # chat rows only, chronological
    messages: list[ModelMessage]  # aligned 1:1 with ``rows``
    user_rows: int
    assistant_rows: int
    skipped: int
    current_excluded: int


# ---------------------------------------------------------------------------
# run-link probe (the worker's conversation discovery seam)
# ---------------------------------------------------------------------------


def _as_uuid(value: str | uuid.UUID) -> uuid.UUID:
    """Coerce one identity boundary value (either form) to UUID."""
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


async def aconversation_for_run(
    sessions: async_sessionmaker[AsyncSession],
    *,
    run_id: str,
) -> uuid.UUID | None:
    """The conversation a durable run is linked to, when one exists.

    The ``conversation_runs`` linkage is authoritative app-owned state
    attached under the submit transaction BEFORE the run is enqueued
    (05-turn-submit), so a started worker always observes it. Legacy
    runs predating the canonical seam have no link and return ``None``
    (the caller keeps the plain system+user seed — no fabricated
    history). Idempotent re-attach keeps this a single row.
    """
    rid = uuid.UUID(run_id)
    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    "SELECT conversation_id FROM conversation_runs "
                    "WHERE run_id = :rid "
                    "ORDER BY created_at DESC, conversation_id LIMIT 1"
                ),
                {"rid": rid},
            )
        ).first()
    return None if row is None else uuid.UUID(str(row[0]))


# ---------------------------------------------------------------------------
# native turn construction (zero synthetic wrappers)
# ---------------------------------------------------------------------------


def _native_message(row: StoredMessage) -> ModelMessage:
    """One role-preserving native message (no flattening, no elevation)."""
    if row.role is MessageRole.USER:
        return ModelRequest(parts=[UserPromptPart(content=row.content)])
    return ModelResponse(parts=[TextPart(content=row.content)])


def _turn_view(
    rows: list[StoredMessage],
    *,
    excluded_run_id: str | None,
    user_message: str,
) -> _TurnView:
    """Filter one transcript snapshot into role-preserving turns.

    ``user`` rows become ``ModelRequest(parts=[UserPromptPart])``,
    ``assistant`` rows become ``ModelResponse(parts=[TextPart])`` —
    chronological order, zero synthetic wrappers, no authority
    elevation. ``system``/``tool`` rows are SKIPPED (counted): a
    persisted tool row cannot carry the ``ToolCallPart`` pairing the
    provider protocol requires, and the assembled system prompt already
    is the seed's leading system message.

    The current turn's own row (``client_request_id == excluded_run_id``,
    persisted by the submit seam before this run started) is excluded so
    the final ``user_message`` request is not doubled; when the key does
    not match (defensive), a trailing user row equal to ``user_message``
    is excluded instead.
    """
    chat_rows: list[StoredMessage] = []
    messages: list[ModelMessage] = []
    user_rows = assistant_rows = skipped = current_excluded = 0
    excluded_uuid = uuid.UUID(excluded_run_id) if excluded_run_id else None
    for row in rows:
        if (
            excluded_uuid is not None
            and row.client_request_id is not None
            and row.client_request_id == excluded_uuid
        ):
            current_excluded += 1
            continue
        if row.role not in (MessageRole.USER, MessageRole.ASSISTANT):
            skipped += 1
            continue
        chat_rows.append(row)
        messages.append(_native_message(row))
        if row.role is MessageRole.USER:
            user_rows += 1
        else:
            assistant_rows += 1
    if current_excluded == 0 and chat_rows and chat_rows[-1].role is MessageRole.USER:
        if chat_rows[-1].content == user_message:
            chat_rows = chat_rows[:-1]
            messages = messages[:-1]
            current_excluded += 1
    return _TurnView(
        rows=chat_rows,
        messages=messages,
        user_rows=user_rows,
        assistant_rows=assistant_rows,
        skipped=skipped,
        current_excluded=current_excluded,
    )


def native_turns(
    rows: list[StoredMessage],
    *,
    excluded_run_id: str | None,
    user_message: str,
) -> tuple[list[ModelMessage], int, int, int, int]:
    """Role-preserving native ``ModelMessage`` turns (public shape).

    See :func:`_turn_view`. Returns ``(messages, user_rows,
    assistant_rows, skipped, current_excluded)``.
    """
    view = _turn_view(
        rows, excluded_run_id=excluded_run_id, user_message=user_message
    )
    return (
        view.messages,
        view.user_rows,
        view.assistant_rows,
        view.skipped,
        view.current_excluded,
    )


def compact_block_text(
    rows: list[StoredMessage],
    *,
    conversation_id: str,
) -> tuple[str, int]:
    """The bounded provenance-carrying compaction block for older turns.

    ``rows`` are the already role-filtered chat rows being compacted
    (oldest first). Rides the SYSTEM channel under the pinned harness's
    ``SUMMARY_PREFIX`` (never user authority, never assistant prose): a
    bounded excerpt of every omitted turn with its role, sequence and
    source provenance, oldest-first. Oldest excerpts yield first when the
    block bound bites; the block always closes with an explicit count of
    fully elided excerpts. Nothing is invented: excerpts are verbatim
    prefixes of authoritative rows. Returns ``(block_text,
    excerpts_elided)``.
    """
    header = (
        f"{SUMMARY_PREFIX}"
        f"{len(rows)} earlier conversation turn(s) of conversation "
        f"{conversation_id} are compacted below (bounded excerpts with "
        "role, sequence and provenance; recent turns follow verbatim).\n"
    )
    size = len(header)
    kept_lines: list[str] = []
    elided = 0
    for row in rows:  # oldest-first: the newest excerpts yield last
        excerpt = row.content.strip()
        if len(excerpt) > SEED_EXCERPT_MAX_CHARS:
            excerpt = excerpt[: SEED_EXCERPT_MAX_CHARS - 3] + "..."
        line = (
            f"[turn {row.sequence} | {row.role.value} | "
            f"{row.source_kind.value}"
            + (" | redacted" if row.redacted else "")
            + f"] {excerpt}"
        )
        if size + len(line) + 1 > COMPACTED_BLOCK_MAX_CHARS:
            elided += 1
            continue
        kept_lines.append(line)
        size += len(line) + 1
    lines = kept_lines + (
        [
            f"{ELIDED_EXCERPT_MARKER}{elided} older compacted turn "
            "excerpt(s) elided at the block bound."
        ]
        if elided
        else []
    )
    return header + "\n".join(lines), elided


# ---------------------------------------------------------------------------
# bounded transcript read (AGENTS.md §1: newest pages, deterministic order)
# ---------------------------------------------------------------------------

#: The storage owner's exact ``conversation_messages`` column set (the
#: public ``alist_messages`` page model deliberately omits
#: ``client_request_id``, which the current-turn exclusion needs).
_TRANSCRIPT_COLUMNS: Final[str] = (
    "conversation_id, message_id, seq, role, source_kind, content, "
    "content_fingerprint, redacted, content_ref, client_request_id, "
    "created_at"
)


async def _aall_messages(
    sessions: async_sessionmaker[AsyncSession],
    *,
    conversation_id: uuid.UUID,
    total: int,
) -> list[StoredMessage]:
    """Read the transcript NEWEST PAGE FIRST, bounded.

    Deterministic ``(seq, message_id)`` pages of ``MAX_PAGE_LIMIT`` over
    the ``conversation_messages`` table (the storage owner's exact
    column set; the public ``alist_messages`` page model deliberately
    carries no ``client_request_id``, which the current-turn exclusion
    needs). Reading stops at :data:`SEED_MAX_PRIOR_TURNS` rows — the
    newest rows are what a continuation seed needs, and the report's
    ``prior_turns_total`` exposes any older remainder honestly.
    """
    collected: list[StoredMessage] = []
    offset = max(0, ((total - 1) // MAX_PAGE_LIMIT) * MAX_PAGE_LIMIT)
    while offset >= 0 and len(collected) < SEED_MAX_PRIOR_TURNS:
        async with sessions() as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT {_TRANSCRIPT_COLUMNS} "
                        "FROM conversation_messages "
                        "WHERE conversation_id = :cid "
                        "ORDER BY seq, message_id LIMIT :l OFFSET :o"
                    ),
                    {"cid": conversation_id, "l": MAX_PAGE_LIMIT, "o": offset},
                )
            ).mappings().all()
        collected = [_stored_message_of(row) for row in rows] + collected
        if offset == 0:
            break
        offset = max(0, offset - MAX_PAGE_LIMIT)
    return collected


async def _alegacy_proven_submissions(
    session: AsyncSession,
    conversation_id: uuid.UUID,
) -> int:
    """Count authoritative submissions with no recoverable content (C7).

    ``chat.submitted`` job-event records prove WHICH user submissions
    happened for this conversation; their payloads carry digests and
    identities, never transcript content. The count feeds the explicit
    gap marker only — a proven-but-unrecoverable submission is never
    converted into a synthetic turn or placeholder text.
    """
    value = await session.execute(
        text(
            "SELECT COUNT(*) FROM job_events "
            "WHERE type = 'chat.submitted' "
            "AND data->>'conversation_id' = :cid"
        ),
        {"cid": str(conversation_id)},
    )
    return min(int(value.scalar_one()), LEGACY_PROVEN_SUBMISSIONS_CAP)


# ---------------------------------------------------------------------------
# seed assembly (the checkpointed context step's implementation)
# ---------------------------------------------------------------------------


def _json_size(message: ModelMessage) -> int:
    """Exact serialized size of one message inside a JSON array."""
    return len(ModelMessagesTypeAdapter.dump_json([message])) - 2


def _array_size(parts: list[int]) -> int:
    """Exact serialized size of a JSON array of messages."""
    return sum(parts) + max(0, len(parts) - 1) + 2


async def abuild_conversation_seed(
    sessions: async_sessionmaker[AsyncSession],
    *,
    conversation_id: str,
    server_id: str,
    system_prompt: str,
    user_message: str,
    run_id: str,
) -> ConversationSeed:
    """Assemble the bounded native multi-turn seed for one continued run.

    Scope-checked against ``server_id`` (C8 fails closed). Returns the
    serialized ``message_history`` JSON plus the value-free report. The
    seed shape is always::

        [SystemPromptPart(system_prompt)]
        [SystemPromptPart(compaction block)]  # only when older turns exist
        [UserPromptPart / TextPart turns ...]  # verbatim, chronological
        [UserPromptPart(user_message)]         # the new turn, LAST

    The newest verbatim tail that fits :data:`SEED_BUDGET_BYTES` is kept;
    at least ONE prior turn always rides verbatim (a single oversized
    turn is bounded by the store's ``MAX_MESSAGE_CHARS``, and the
    run-level ``MAX_HISTORY_BYTES`` checkpoint stays the honest last
    resort — budgets never silently drop the most recent turns).
    """
    cid = _as_uuid(conversation_id)
    sid = _as_uuid(server_id)
    conversation = await aget_conversation(sessions, cid, server_id=sid)
    probe = await alist_messages(sessions, cid, server_id=sid, limit=1, offset=0)
    total = probe.total
    rows: list[StoredMessage] = (
        []
        if total == 0
        else await _aall_messages(sessions, conversation_id=cid, total=total)
    )
    view = _turn_view(rows, excluded_run_id=run_id, user_message=user_message)

    leading = ModelRequest(parts=[SystemPromptPart(content=system_prompt)])
    final_request = ModelRequest(parts=[UserPromptPart(content=user_message)])
    leading_size = _json_size(leading)
    final_size = _json_size(final_request)

    # Keep the newest verbatim tail that fits the budget (exact per-
    # message accounting). ``keep_from`` is the index of the first
    # verbatim message; the older prefix compacts into the block. At
    # least ONE prior turn always rides verbatim — budgets never
    # silently drop the most recent turns.
    part_sizes = [_json_size(message) for message in view.messages]
    keep_from = 0
    block_text: str | None = None
    excerpts_elided = 0
    if view.messages:
        while True:
            block_text, excerpts_elided = (
                (None, 0)
                if keep_from == 0
                else compact_block_text(
                    view.rows[:keep_from], conversation_id=str(cid)
                )
            )
            block_size = (
                0
                if block_text is None
                else _json_size(
                    ModelRequest(parts=[SystemPromptPart(content=block_text)])
                )
            )
            total_size = (
                leading_size
                + (block_size + 1 if block_text is not None else 0)
                + _array_size(part_sizes[keep_from:])
                + final_size
            )
            if total_size <= SEED_BUDGET_BYTES:
                break
            if keep_from >= len(view.messages) - 1:
                break  # keep at least the most recent turn verbatim
            keep_from += 1
    kept_messages = view.messages[keep_from:]
    compacted_rows = view.rows[:keep_from]

    messages: list[ModelMessage] = []
    if block_text is not None:
        messages.append(ModelRequest(parts=[SystemPromptPart(content=block_text)]))
    messages.extend(kept_messages)

    legacy_proven = 0
    if conversation.history_completeness is not HistoryCompleteness.COMPLETE:
        async with sessions() as session:
            legacy_proven = await _alegacy_proven_submissions(session, cid)

    seed_json = ModelMessagesTypeAdapter.dump_json(
        [leading, *messages, final_request]
    ).decode("utf-8")
    missing = conversation.missing_range
    report = SeedReport(
        conversation_linked=True,
        conversation_id=str(cid),
        history_completeness=conversation.history_completeness.value,
        missing_from_sequence=None if missing is None else missing.from_sequence,
        missing_to_sequence=None if missing is None else missing.to_sequence,
        prior_turns_total=total,
        turns_included_verbatim=len(kept_messages),
        turns_compacted=len(compacted_rows),
        excerpts_elided=excerpts_elided,
        user_rows_included=view.user_rows,
        assistant_rows_included=view.assistant_rows,
        non_chat_rows_skipped=view.skipped,
        current_turn_rows_excluded=view.current_excluded,
        legacy_proven_submissions=legacy_proven,
        seed_messages=2 + len(messages),
        seed_bytes=len(seed_json.encode("utf-8")),
        budget_bytes=SEED_BUDGET_BYTES,
    )
    return ConversationSeed(history_json=seed_json, report=report)


# ---------------------------------------------------------------------------
# canonical assistant producer boundary (idempotent per run_id)
# ---------------------------------------------------------------------------


def _stored_message_of(row: object) -> StoredMessage:
    """Map one ``conversation_messages`` row to the store's row type.

    Mirrors the storage owner's private mapper (identical column set);
    kept local so the storage module stays untouched by this card.
    """
    mapping = row  # sqlalchemy Row supports mapping access positionally by name
    return StoredMessage(
        conversation_id=mapping["conversation_id"],  # type: ignore[index]
        message_id=mapping["message_id"],  # type: ignore[index]
        sequence=int(mapping["seq"]),  # type: ignore[index]
        role=MessageRole(str(mapping["role"])),  # type: ignore[index]
        source_kind=MessageSourceKind(str(mapping["source_kind"])),  # type: ignore[index]
        content=str(mapping["content"]),  # type: ignore[index]
        content_fingerprint=str(mapping["content_fingerprint"]),  # type: ignore[index]
        redacted=bool(mapping["redacted"]),  # type: ignore[index]
        content_ref=mapping["content_ref"],  # type: ignore[index]
        client_request_id=mapping["client_request_id"],  # type: ignore[index]
        created_at=mapping["created_at"],  # type: ignore[index]
    )


async def arecord_assistant_reply(
    sessions: async_sessionmaker[AsyncSession],
    *,
    conversation_id: str,
    server_id: str,
    run_id: str,
    content: str,
) -> StoredMessage | None:
    """Record ONE canonical assistant reply for this run (producer seam).

    Trusted producer boundary (§1): the worker is the only writer of
    ``assistant`` rows. Idempotent per ``run_id`` — the safe
    ``content_ref = "agent-run:<run_id>"`` marker is checked before the
    append, so a replayed/restarted workflow (or a crash between the
    append commit and the engine checkpoint) can never duplicate the
    row. Content passes the store's C2 projection; the sanitized
    tool/evidence receipts stay referenced through the run identity
    (the transcript never copies raw tool output).

    Provenance is ``MessageSourceKind.SYSTEM``: the frozen role/source
    matrix admits ``system`` for assistant rows as platform provenance
    (the run's own model is a platform producer; a browser/API source
    would misattribute the origin). Returns the stored row, or ``None``
    when the run produced no non-blank final text (nothing honest to
    record) or the append found no conversation scope.
    """
    cid = _as_uuid(conversation_id)
    sid = _as_uuid(server_id)
    text_body = content if isinstance(content, str) else ""
    if not text_body.strip():
        return None
    content_ref = f"{PRODUCER_CONTENT_REF_PREFIX}{run_id}"
    async with sessions() as session:
        existing = (
            await session.execute(
                text(
                    f"SELECT {_TRANSCRIPT_COLUMNS} FROM conversation_messages "
                    "WHERE conversation_id = :cid "
                    "AND role = 'assistant' "
                    "AND content_ref = :cref "
                    "ORDER BY seq, message_id LIMIT 1"
                ),
                {"cid": cid, "cref": content_ref},
            )
        ).mappings().first()
    if existing is not None:
        return _stored_message_of(existing)
    appended = await aappend_producer_message(
        sessions,
        conversation_id=cid,
        server_id=sid,
        role=MessageRole.ASSISTANT,
        source_kind=MessageSourceKind.SYSTEM,
        content=text_body,
        content_ref=content_ref,
    )
    return appended.message
