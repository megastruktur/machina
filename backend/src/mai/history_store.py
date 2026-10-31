"""Bounded tag-search pages over job/conversation history (history-tags W3A).

The read-only search layer on top of the 0011 mandatory-tag schema and the
W2 tag catalog/association store: :func:`asearch_jobs_by_tags` and
:func:`asearch_conversations_by_tags` return deterministic, bounded pages
of entity references carrying a requested set of catalog tags.

- **Scope is closed**: only entities whose parent row lives in the
  requesting ``server_id`` participate — as the filter for the items AND
  for the ``total`` count (a cross-server match never leaks, not even its
  count).
- **EXISTS semantics**: each entity appears at most once, regardless of
  how many of the requested tags it carries (semi-join predicate, never a
  counted join).
- **Match modes**: ``match="any"`` (default) matches entities carrying
  ANY requested tag; ``match="all"`` matches only entities carrying
  EVERY distinct requested tag.
- **Tag ids**: deduplicated and validated fail-closed — the set must be
  non-empty and every member a ``uuid.UUID``. Unknown catalog ids are not
  a refusal: they simply match nothing (existence never leaks through
  search, and there is no unfiltered/unbounded search in this slice).
- **Determinism**: jobs order by ``submitted_at DESC, run_id``;
  conversations by ``updated_at DESC, conversation_id`` — the same
  newest-first + stable id tiebreak the list pages use. ``COUNT(*)`` is
  computed under the identical predicate (AGENTS.md pagination invariant;
  bounds fail closed, never clamp).
- **Previews are N+1-free**: the bounded page's per-entity tag preview and
  honest count reuse the W2 batch preview helpers (one grouped query per
  page, never one query per row).

This module adds no status/cursor filters and no schema. W3A search
projects references only; W3B adds exactly one content reader,
:func:`aread_conversation_content`: bounded ``(seq, message_id)``-ordered
transcript pages over a single server-scoped conversation, continuable by
a message-index + character cursor — and W3B-JOB adds the job reader,
:func:`aread_job_content`: one server-scoped job run's content row
(parentage, frozen label, replayed lifecycle state, error/evidence
references), the newest non-null task binding across all of its
conversation links, and one bounded page of those same-server links.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.agents.conversation_store import StoredMessage
from mai.contracts.conversations import MessageRole, MessageSourceKind
from mai.jobs.lifecycle import RunLifecycle
from mai.tag_store import (
    DEFAULT_PAGE_LIMIT,
    EntityTagPreview,
    TagErrorCode,
    TagStoreError,
    _check_page_bounds,
    apreview_conversation_tags,
    apreview_job_tags,
)

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "ConversationSearchRow",
    "JobSearchRow",
    "MAX_CONTENT_CHARS",
    "ConversationContentPage",
    "ConversationContentWindow",
    "JobContent",
    "JobConversationLink",
    "LinkPage",
    "asearch_conversations_by_tags",
    "asearch_jobs_by_tags",
    "aread_conversation_content",
    "aread_job_content",
]

# Re-exported for callers composing the wire envelope next to the rows.
from mai.tag_store import MAX_PAGE_LIMIT  # noqa: E402


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobSearchRow:
    """One matched job run: identity, newest-first sort key, tag preview."""

    run_id: uuid.UUID
    submitted_at: datetime
    tag_preview: EntityTagPreview


@dataclass(frozen=True, slots=True)
class ConversationSearchRow:
    """One matched conversation: identity, sort key, tag preview."""

    conversation_id: uuid.UUID
    updated_at: datetime
    tag_preview: EntityTagPreview


# ---------------------------------------------------------------------------
# content-reader rows (W3B-CONV)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConversationContentWindow:
    """One message's content slice inside a char-paginated page.

    ``content`` is the stored (already redacted) text; ``char_cursor`` is
    the zero-based position inside this message where the next page should
    resume.  ``redacted`` and ``content_ref`` are preserved verbatim from
    the stored row.
    """

    message_id: uuid.UUID
    seq: int
    role: str
    source_kind: str
    content: str
    redacted: bool
    content_ref: str | None
    char_cursor: int


@dataclass(frozen=True, slots=True)
class ConversationContentPage:
    """One bounded, char-cursor-continuable transcript slice.

    ``items`` are ordered by ``(seq, message_id)`` ascending; ``total`` is
    the pre-slice message count under the identical scope predicate.
    ``next_offset`` is the message index for the next page; ``next_char_cursor``
    is the character position inside that message.  When ``has_more`` is
    ``False`` both are ``None``.
    """

    conversation_id: uuid.UUID
    items: list[ConversationContentWindow]
    total: int
    has_more: bool
    next_offset: int | None
    next_char_cursor: int | None


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def _validated_search_tag_ids(tag_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    """Deduplicated, sorted, non-empty uuid set (fail-closed validation)."""
    ids = sorted({*tag_ids})
    for tag_id in ids:
        if not isinstance(tag_id, uuid.UUID):
            raise TagStoreError(
                TagErrorCode.VALIDATION,
                "every tag id must be a uuid.UUID",
                "tag_ids",
            )
    if not ids:
        raise TagStoreError(
            TagErrorCode.VALIDATION,
            "at least one tag id is required",
            "tag_ids",
        )
    return ids


def _validated_match_mode(match: str) -> bool:
    """``match="any"|"all"`` validated fail-closed; returns ``match_all``."""
    match_all = {"any": False, "all": True}.get(match)
    if match_all is None:
        raise ValueError(f"match must be 'any' or 'all', got {match!r}")
    return match_all


# ---------------------------------------------------------------------------
# search (shared semi-join page machinery)
# ---------------------------------------------------------------------------

#: ANY-match: the entity carries at least one requested tag.
_SEMI_ANY_PREDICATE: str = """
    EXISTS (
        SELECT 1
        FROM unnest(CAST(:tags AS uuid[])) AS u(t)
        JOIN {association} a ON a.{entity_column} = p.{parent_key}
                             AND a.tag_id = u.t
    )
"""

#: ALL-match: no requested tag is missing from the entity's associations.
_SEMI_ALL_PREDICATE: str = """
    NOT EXISTS (
        SELECT 1
        FROM unnest(CAST(:tags AS uuid[])) AS u(t)
        WHERE NOT EXISTS (
            SELECT 1 FROM {association} a
            WHERE a.{entity_column} = p.{parent_key} AND a.tag_id = u.t
        )
    )
"""


async def _asearch_entities_by_tags(
    sessions: async_sessionmaker[AsyncSession],
    tag_ids: Sequence[uuid.UUID],
    *,
    server_id: uuid.UUID,
    match_all: bool,
    limit: int,
    offset: int,
    parent: str,
    parent_key: str,
    order_first: str,
    association: str,
    entity_column: str,
) -> tuple[list[tuple[uuid.UUID, datetime]], int]:
    """Shared bounded page: (entity_id, sort timestamp) + honest total.

    ``p`` is the scope-checked parent row; the ANY/ALL predicates are
    semi-joins, so a multi-tag match cannot duplicate the entity.
    """
    _check_page_bounds(limit, offset)
    ids = _validated_search_tag_ids(tag_ids)
    predicate = (
        _SEMI_ALL_PREDICATE if match_all else _SEMI_ANY_PREDICATE
    ).format(association=association, entity_column=entity_column, parent_key=parent_key)
    async with sessions() as session:
        total = int(
            (
                await session.execute(
                    text(
                        f"SELECT count(*) FROM {parent} p "
                        f"WHERE p.server_id = :sid AND {predicate}"
                    ),
                    {"sid": server_id, "tags": ids},
                )
            ).scalar_one()
        )
        if total == 0 or offset >= total:
            return [], total
        rows = (
            await session.execute(
                text(
                    f"SELECT p.{parent_key} AS entity_id, "
                    f"p.{order_first} AS sort_at "
                    f"FROM {parent} p "
                    f"WHERE p.server_id = :sid AND {predicate} "
                    f"ORDER BY p.{order_first} DESC, p.{parent_key} "
                    "LIMIT :l OFFSET :o"
                ),
                {"sid": server_id, "tags": ids, "l": limit, "o": offset},
            )
        ).all()
    return [(row[0], row[1]) for row in rows], total


# ---------------------------------------------------------------------------
# public search
# ---------------------------------------------------------------------------


async def asearch_jobs_by_tags(
    sessions: async_sessionmaker[AsyncSession],
    tag_ids: Sequence[uuid.UUID],
    *,
    server_id: uuid.UUID,
    match: str = "any",
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
) -> tuple[list[JobSearchRow], int]:
    """Bounded newest-first page of job runs carrying the requested tags.

    Only runs inside ``server_id`` count (items and total). ``match="all"``
    requires every distinct requested tag; the default ``"any"`` matches
    any. The per-row preview reuses the W2 batch helper: one grouped query
    for the whole page, never per-row queries.
    """
    match_all = _validated_match_mode(match)
    matches, total = await _asearch_entities_by_tags(
        sessions,
        tag_ids,
        server_id=server_id,
        match_all=match_all,
        limit=limit,
        offset=offset,
        parent="job_runs",
        parent_key="run_id",
        order_first="submitted_at",
        association="job_tags",
        entity_column="job_id",
    )
    previews = await apreview_job_tags(
        sessions, [entity_id for entity_id, _ in matches], server_id=server_id
    )
    empty = EntityTagPreview(preview=(), total=0)
    return [
        JobSearchRow(
            run_id=entity_id,
            submitted_at=sort_at,
            tag_preview=previews.get(entity_id, empty),
        )
        for entity_id, sort_at in matches
    ], total


async def asearch_conversations_by_tags(
    sessions: async_sessionmaker[AsyncSession],
    tag_ids: Sequence[uuid.UUID],
    *,
    server_id: uuid.UUID,
    match: str = "any",
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
) -> tuple[list[ConversationSearchRow], int]:
    """Bounded newest-first page of conversations carrying the tags.

    Mirrors :func:`asearch_jobs_by_tags` with the conversation ordering
    contract (``updated_at DESC, conversation_id``).
    """
    match_all = _validated_match_mode(match)
    matches, total = await _asearch_entities_by_tags(
        sessions,
        tag_ids,
        server_id=server_id,
        match_all=match_all,
        limit=limit,
        offset=offset,
        parent="conversations",
        parent_key="conversation_id",
        order_first="updated_at",
        association="conversation_tags",
        entity_column="conversation_id",
    )
    previews = await apreview_conversation_tags(
        sessions, [entity_id for entity_id, _ in matches], server_id=server_id
    )
    empty = EntityTagPreview(preview=(), total=0)
    return [
        ConversationSearchRow(
            conversation_id=entity_id,
            updated_at=sort_at,
            tag_preview=previews.get(entity_id, empty),
        )
        for entity_id, sort_at in matches
    ], total


# ---------------------------------------------------------------------------
# conversation content reader (W3B-CONV)
# ---------------------------------------------------------------------------

#: Hard upper bound for one page's accumulated character budget
#: (fail closed, never clamp; one stored message is at most 20k chars).
MAX_CONTENT_CHARS: Final[int] = 20_000


def _check_content_bounds(max_chars: int, char_cursor: int) -> None:
    """Fail-closed validation for the char-paginated reader's own bounds."""
    if (
        not isinstance(max_chars, int)
        or isinstance(max_chars, bool)
        or not 1 <= max_chars <= MAX_CONTENT_CHARS
    ):
        raise ValueError(
            f"max_chars must be an integer in [1, {MAX_CONTENT_CHARS}]"
        )
    if (
        not isinstance(char_cursor, int)
        or isinstance(char_cursor, bool)
        or char_cursor < 0
    ):
        raise ValueError("char_cursor must be an integer >= 0")


async def aread_conversation_content(
    sessions: async_sessionmaker[AsyncSession],
    conversation_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    offset: int = 0,
    char_cursor: int = 0,
    limit: int = DEFAULT_PAGE_LIMIT,
    max_chars: int = 4096,
) -> ConversationContentPage:
    """Read a bounded, char-paginated slice of conversation content.

    Messages are ordered by ``(seq, message_id)`` ascending.  The page
    starts at message index ``offset`` and character position ``char_cursor``
    inside that message.  At most ``limit`` messages (validated by
    ``_check_page_bounds``) and ``max_chars`` characters (at most
    ``MAX_CONTENT_CHARS``) of content are returned across the page.

    The conversation must belong to ``server_id``; otherwise a
    ``ValueError`` is raised (fail-closed scope check).

    Exact reconstruction: concatenating ``content`` across successive
    pages (using ``next_offset``/``next_char_cursor``) yields the exact
    concatenation of stored message contents with no skipped or repeated
    characters.  Empty messages and ``seq`` gaps are handled naturally.
    """
    _check_page_bounds(limit, offset)
    _check_content_bounds(max_chars, char_cursor)

    async with sessions() as session:
        # Scope check: conversation must exist and belong to server_id.
        conv = (
            await session.execute(
                text(
                    "SELECT conversation_id FROM conversations "
                    "WHERE conversation_id = :cid AND server_id = :sid"
                ),
                {"cid": conversation_id, "sid": server_id},
            )
        ).first()
        if conv is None:
            raise ValueError(
                f"conversation {conversation_id} not found in server {server_id}"
            )

        # Total message count under the identical predicate.
        total = int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM conversation_messages "
                        "WHERE conversation_id = :cid"
                    ),
                    {"cid": conversation_id},
                )
            ).scalar_one()
        )

        if total == 0 or offset >= total:
            return ConversationContentPage(
                conversation_id=conversation_id,
                items=[],
                total=total,
                has_more=False,
                next_offset=None,
                next_char_cursor=None,
            )

        # Fetch the page of messages starting at offset.
        rows = (
            await session.execute(
                text(
                    "SELECT message_id, seq, role, source_kind, content, "
                    "redacted, content_ref "
                    "FROM conversation_messages "
                    "WHERE conversation_id = :cid "
                    "ORDER BY seq, message_id "
                    "LIMIT :l OFFSET :o"
                ),
                {"cid": conversation_id, "l": limit, "o": offset},
            )
        ).mappings().all()

    # Build char-bounded windows.
    items: list[ConversationContentWindow] = []
    chars_used = 0
    next_offset: int | None = None
    next_char_cursor: int | None = None

    for idx, row in enumerate(rows):
        content: str = row["content"] or ""
        msg_offset = offset + idx

        # Determine start position inside this message.
        start = char_cursor if idx == 0 else 0
        if start > len(content):
            raise ValueError(
                f"char_cursor {start} beyond stored message length {len(content)} "
                f"at offset {msg_offset}"
            )

        remaining = max_chars - chars_used
        if remaining <= 0:
            next_offset = msg_offset
            next_char_cursor = 0
            break

        slice_end = min(start + remaining, len(content))
        slice_content = content[start:slice_end]
        chars_used += len(slice_content)

        items.append(
            ConversationContentWindow(
                message_id=row["message_id"],
                seq=row["seq"],
                role=row["role"],
                source_kind=row["source_kind"],
                content=slice_content,
                redacted=bool(row["redacted"]),
                content_ref=row["content_ref"],
                char_cursor=slice_end,
            )
        )

        if slice_end < len(content):
            # Truncated inside this message.
            next_offset = msg_offset
            next_char_cursor = slice_end
            break
    else:
        # Consumed all fetched rows without hitting max_chars limit.
        consumed = offset + len(rows)
        if consumed < total:
            next_offset = consumed
            next_char_cursor = 0

    has_more = next_offset is not None
    return ConversationContentPage(
        conversation_id=conversation_id,
        items=items,
        total=total,
        has_more=has_more,
        next_offset=next_offset,
        next_char_cursor=next_char_cursor,
    )


# ---------------------------------------------------------------------------
# job content reader (W3B-JOB)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobConversationLink:
    """One same-server conversation↔run binding, verbatim from the row.

    ``released_at IS NULL`` means this link currently holds the
    conversation's one writer/executor lease; a set ``released_at`` is a
    historical (released) binding.
    """

    conversation_id: uuid.UUID
    run_id: uuid.UUID
    task_id: uuid.UUID | None
    created_at: datetime
    released_at: datetime | None


@dataclass(frozen=True, slots=True)
class LinkPage:
    """One bounded page of same-server conversation links.

    ``items`` are ordered by ``created_at DESC, conversation_id``;
    ``total`` is the pre-slice count under the identical scope predicate.
    """

    items: list[JobConversationLink]
    limit: int
    offset: int
    total: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class JobContent:
    """One server-scoped job run's content row plus its link page.

    ``task_id`` is the newest non-null binding across ALL of the run's
    same-server links (released links included), not just the returned
    page. ``label`` is the frozen-at-submit run label, stored verbatim
    (empty string when absent). ``state`` is the lifecycle state replayed from the
    run's persisted event stream (``None`` before the submit edge).
    """

    run_id: uuid.UUID
    parent_run_id: uuid.UUID | None
    task_id: uuid.UUID | None
    label: str | None
    state: str | None
    submitted_at: datetime
    updated_at: datetime
    error_ref: str | None
    evidence_ref: str | None
    links: LinkPage


async def aread_job_content(
    sessions: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
) -> JobContent:
    """Read one job run's content plus a bounded page of its links.

    The run must belong to ``server_id``; otherwise a ``ValueError`` is
    raised (fail-closed scope check, mirroring
    :func:`aread_conversation_content`). Links join ``conversations`` on
    the same server — a cross-server conversation never leaks, not even
    into ``total`` or the ``task_id`` binding. The lifecycle state is
    replayed from the persisted event stream via
    ``RunLifecycle(sessions).current_state``; no raw events are exposed.
    """
    _check_page_bounds(limit, offset)

    async with sessions() as session:
        run_row = (
            await session.execute(
                text(
                    "SELECT parent_run_id, label, error_ref, evidence_ref, "
                    "submitted_at, updated_at FROM job_runs "
                    "WHERE run_id = :rid AND server_id = :sid"
                ),
                {"rid": run_id, "sid": server_id},
            )
        ).first()
        if run_row is None:
            raise ValueError(f"job run {run_id} not found in server {server_id}")

        # Same-server links only: the JOIN is the scope predicate for the
        # items AND the honest total (and the task binding below).
        link_scope = (
            "FROM conversation_runs cr "
            "JOIN conversations c ON c.conversation_id = cr.conversation_id "
            "WHERE cr.run_id = :rid AND c.server_id = :sid"
        )
        link_params: dict[str, object] = {"rid": run_id, "sid": server_id}

        total = int(
            (
                await session.execute(
                    text(f"SELECT count(*) {link_scope}"), link_params
                )
            ).scalar_one()
        )

        # Newest non-null task binding across ALL links (released ones
        # included); deterministic tiebreak on conversation_id.
        task_id = (
            await session.execute(
                text(
                    f"SELECT cr.task_id {link_scope} "
                    "AND cr.task_id IS NOT NULL "
                    "ORDER BY cr.created_at DESC, cr.conversation_id "
                    "LIMIT 1"
                ),
                link_params,
            )
        ).scalar()

        items: list[JobConversationLink] = []
        if total > 0 and offset < total:
            link_rows = (
                await session.execute(
                    text(
                        "SELECT cr.conversation_id, cr.run_id, cr.task_id, "
                        "cr.created_at, cr.released_at "
                        f"{link_scope} "
                        "ORDER BY cr.created_at DESC, cr.conversation_id "
                        "LIMIT :l OFFSET :o"
                    ),
                    {**link_params, "l": limit, "o": offset},
                )
            ).all()
            items = [
                JobConversationLink(
                    conversation_id=row[0],
                    run_id=row[1],
                    task_id=row[2],
                    created_at=row[3],
                    released_at=row[4],
                )
                for row in link_rows
            ]

    lifecycle_state = await RunLifecycle(sessions).current_state(str(run_id))
    return JobContent(
        run_id=run_id,
        parent_run_id=run_row.parent_run_id,
        task_id=task_id,
        label=run_row.label,
        state=None if lifecycle_state is None else lifecycle_state.value,
        submitted_at=run_row.submitted_at,
        updated_at=run_row.updated_at,
        error_ref=run_row.error_ref,
        evidence_ref=run_row.evidence_ref,
        links=LinkPage(
            items=items,
            limit=limit,
            offset=offset,
            total=total,
            has_more=offset + len(items) < total,
        ),
    )
