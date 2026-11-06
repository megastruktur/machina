"""Bounded tag-search pages and the job-run content reader (history-tags W3A).

The read-only search layer on top of the 0011 mandatory-tag schema and the
W2 tag catalog/association store: :func:`asearch_jobs_by_tags` returns a
deterministic, bounded page of job-run references carrying a requested set
of catalog tags, and :func:`aread_job_content` reads one server-scoped
job run's content row. The conversation search/read half was removed with
the D09 coordinated deletion (migration ``0015`` drops the tables);
session content is read through the bounded agent-session history surface.

- **Scope is closed**: only job runs whose parent row lives in the
  requesting ``server_id`` participate — as the filter for the items AND
  for the ``total`` count (a cross-server match never leaks, not even its
  count).
- **EXISTS semantics**: each run appears at most once, regardless of
  how many of the requested tags it carries (semi-join predicate, never a
  counted join).
- **Match modes**: ``match="any"`` (default) matches runs carrying
  ANY requested tag; ``match="all"`` matches only runs carrying
  EVERY distinct requested tag.
- **Tag ids**: deduplicated and validated fail-closed — the set must be
  non-empty and every member a ``uuid.UUID``. Unknown catalog ids are not
  a refusal: they simply match nothing (existence never leaks through
  search, and there is no unfiltered/unbounded search in this slice).
- **Determinism**: runs order by ``submitted_at DESC, run_id`` — the same
  newest-first + stable id tiebreak the list pages use. ``COUNT(*)`` is
  computed under the identical predicate (AGENTS.md pagination invariant;
  bounds fail closed, never clamp).
- **Previews are N+1-free**: the bounded page's per-run tag preview and
  honest count reuse the W2 batch preview helper (one grouped query per
  page, never one query per row).

This module adds no status/cursor filters and no schema. Search projects
references only; :func:`aread_job_content` projects exactly one
server-scoped job run's content row (parentage, frozen label, replayed
lifecycle state, error/evidence references) — no raw events are exposed.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.contracts.tags import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, EntityTagPreview
from mai.jobs.lifecycle import RunLifecycle
from mai.tag_store import (
    TagErrorCode,
    TagStoreError,
    _check_page_bounds,
    apreview_job_tags,
)

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "JobContent",
    "JobSearchRow",
    "asearch_jobs_by_tags",
    "aread_job_content",
]


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
class JobContent:
    """One server-scoped job run's content row.

    ``label`` is the frozen-at-submit run label, stored verbatim (empty
    string when absent). ``state`` is the lifecycle state replayed from
    the run's persisted event stream (``None`` before the submit edge).
    """

    run_id: uuid.UUID
    parent_run_id: uuid.UUID | None
    label: str | None
    state: str | None
    submitted_at: datetime
    updated_at: datetime
    error_ref: str | None
    evidence_ref: str | None


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
    """``True`` for ``"all"``, ``False`` for ``"any"`` (fail-closed)."""
    if match == "all":
        return True
    if match == "any":
        return False
    raise TagStoreError(TagErrorCode.VALIDATION, "match must be any|all", "match")


# ---------------------------------------------------------------------------
# search (semi-join page machinery)
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


# ---------------------------------------------------------------------------
# job content reader
# ---------------------------------------------------------------------------


async def aread_job_content(
    sessions: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
) -> JobContent:
    """Read one job run's content row (fail-closed scope check).

    The run must belong to ``server_id``; otherwise a ``ValueError`` is
    raised — a missing and another server's run are indistinguishable.
    The lifecycle state is replayed from the persisted event stream via
    ``RunLifecycle(sessions).current_state``; no raw events are exposed.
    """
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

    lifecycle_state = await RunLifecycle(sessions).current_state(str(run_id))
    return JobContent(
        run_id=run_id,
        parent_run_id=run_row.parent_run_id,
        label=run_row.label,
        state=None if lifecycle_state is None else lifecycle_state.value,
        submitted_at=run_row.submitted_at,
        updated_at=run_row.updated_at,
        error_ref=run_row.error_ref,
        evidence_ref=run_row.evidence_ref,
    )
