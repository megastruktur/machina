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

This module adds no content readers, no status/cursor filters, and no
schema: it searches and projects references only.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    "asearch_conversations_by_tags",
    "asearch_jobs_by_tags",
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
