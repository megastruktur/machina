"""Bounded tag catalog + job-run association store (history-tags W2; D09).

The persistence layer for the 0011 mandatory-tag schema: a global,
server-agnostic tag catalog plus per-entity associations for ``job_runs``.
The conversation association half (``conversation_tags``) was removed with
the D09 coordinated deletion (migration ``0015`` drops the table); session
tagging lives in :mod:`mai.session_store` on the ``session_tags`` table. The catalog is case/whitespace-normalized and
canonically unique (``ux_tags_name_canonical`` on ``lower(name)``); the
schema enforces at-least-one-tag per entity with deferred constraint
triggers, and this store is the disciplined writer on top:

- **Create is idempotent on the canonical name.** A name is normalized
  (whitespace runs collapsed, trimmed) before the lookup; an existing
  canonical match is returned AS-IS — the stored description is never
  overwritten by a later same-name create. The unique index is the race
  backstop: a lost insert race re-reads and returns the winner's row.
- **Every collection view is a bounded page**: ``(items, total)`` with
  deterministic ordering and ``COUNT(*)`` under the identical predicate
  (AGENTS.md pagination invariant; ``compute_has_more`` composes the wire
  envelope). Bounds fail closed, never clamp.
- **Entity access is scope-checked and closed**: a job run is
  resolved by (id, server_id) — a missing entity and a cross-server entity
  are the SAME single ``entity_not_found`` refusal (existence never leaks
  across scopes).
- **Assignment/removal is idempotent**; removing an entity's FINAL
  ordinary tag is refused without an atomic replacement
  (``replacement_tag_id`` swaps both in one transaction, so the schema's
  deferred triggers see >= 1 tag at commit).
- **Deletion policy**: an unused tag deletes directly; a used tag refuses
  without ``force=True``; a forced delete proceeds only when EVERY
  affected entity keeps another tag or receives ``replacement_tag_id``
  within the same transaction — otherwise it refuses. The reserved
  ``unclassified`` catalog row can never be deleted.
- **Concurrency protocol**: every association write and every deletion
  takes ``FOR UPDATE`` on the catalog row first, so writers serialize per
  tag; foreign keys and the schema's deferred constraint triggers are the
  backstop, and EVERY ``DBAPIError`` the write paths raise — including
  commit-time P0001 from the deferred must-keep-one-tag triggers, which
  surfaces as ``ProgrammingError`` — is translated into typed
  :class:`TagStoreError` codes (never raw DB exceptions).
- **Batch previews are N+1-free**: one grouped query per batch returns the
  bounded name preview and total count per entity (listing projections).

Reserved identities mirror ``mai.tags`` (the runtime single source of
truth, seeded by migration 0011). This module never imports application
code outside ``mai.tags`` and the pagination contracts.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# D01 extraction: the neutral tag/pagination vocabulary now lives in
# :mod:`mai.contracts.tags`; this module re-exports it so every existing
# ``mai.tag_store`` caller keeps working unchanged. The re-exports are the
# same objects, never copies.
from mai.contracts.tags import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    EntityTagPreview,
    compute_has_more,
)
from mai.tags import RESERVED_UNCLASSIFIED_TAG_ID, SYSTEM_ASSIGNMENT_SOURCE

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "RESERVED_UNCLASSIFIED_TAG_ID",
    "SYSTEM_ASSIGNMENT_SOURCE",
    "EntityTagPreview",
    "EntityTagRow",
    "TagErrorCode",
    "TagRow",
    "TagStoreError",
    "TerminalClassification",
    "aclassify_integrity_error",
    "aclassify_terminal_job",
    "acreate_tag",
    "adelete_tag",
    "aassign_job_tag",
    "aremove_job_tag",
    "aresolve_job_server",
    "atags_for_job",
    "alist_tags",
    "apreview_job_tags",
    "anormalize_tag_name",
    "compute_has_more",
]


# ---------------------------------------------------------------------------
# typed errors (compact store-domain vocabulary; W3 maps these to wire codes)
# ---------------------------------------------------------------------------


class TagErrorCode(StrEnum):
    """Classified, value-free tag-store failure codes."""

    VALIDATION = "validation"
    TAG_NOT_FOUND = "tag_not_found"
    ENTITY_NOT_FOUND = "entity_not_found"
    TAG_IN_USE = "tag_in_use"
    RESERVED_TAG = "reserved_tag"
    LAST_TAG = "last_tag"
    INVARIANT_VIOLATION = "invariant_violation"


class TagStoreError(ValueError):
    """One classified failure: a code, a value-free message, an optional field."""

    def __init__(self, code: TagErrorCode, message: str, field: str | None = None):
        super().__init__(message)
        self.code = code
        self.field = field


#: The schema's deferred triggers raise exactly these messages when the
#: mandatory-tag invariant would be violated at commit (migration 0011).
_TRIGGER_MARKERS: Final[Mapping[str, TagErrorCode]] = {
    "must keep at least one tag": TagErrorCode.LAST_TAG,
    "reserved unclassified tag cannot be deleted": TagErrorCode.RESERVED_TAG,
}

_NAME_MAX: Final[int] = 64
_DESCRIPTION_MAX: Final[int] = 500
_ASSIGNED_BY_MAX: Final[int] = 200
#: The frozen writer vocabulary mirrored from the association CHECKs.
_ASSIGNMENT_SOURCES: Final[frozenset[str]] = frozenset(
    {"agent", "user", "system"}
)
#: Bounded per-entity preview length for batch listing projections.
_PREVIEW_MAX: Final[int] = 10

_CONTROL_CHARS: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f\x80-\x9f]")


def anormalize_tag_name(name: str) -> str:
    """Canonical form: whitespace runs collapsed, trimmed, bounds-checked.

    Pure validation helper — the caller persists exactly this value, so the
    stored display name is always identical to its normalized form (the
    ``ck_tags_name_normalized`` CHECK).
    """
    if not isinstance(name, str):
        raise TagStoreError(TagErrorCode.VALIDATION, "name must be a string", "name")
    normalized = re.sub(r"\s+", " ", name).strip()
    if not normalized:
        raise TagStoreError(
            TagErrorCode.VALIDATION,
            "name must contain at least one non-whitespace character",
            "name",
        )
    if _CONTROL_CHARS.search(normalized):
        raise TagStoreError(
            TagErrorCode.VALIDATION,
            "name must be single-line printable characters",
            "name",
        )
    if len(normalized) > _NAME_MAX:
        raise TagStoreError(
            TagErrorCode.VALIDATION,
            f"name must be at most {_NAME_MAX} characters",
            "name",
        )
    return normalized


@dataclass(frozen=True, slots=True)
class TerminalClassification:
    classified: bool
    tag_id: uuid.UUID
    reason: str

async def _aclassify_terminal_job_locked(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    terminal_tag_name: str,
) -> TerminalClassification:
    """Whole terminal classification inside a caller-held transaction.

    Exact body of the public helper minus the session open/begin: the
    caller owns the session and the transaction, so this can compose with
    other writers (e.g. the run-lifecycle terminal event) atomically.
    """
    name = anormalize_tag_name(terminal_tag_name)
    if name.lower() == "unclassified":
        raise TagStoreError(TagErrorCode.RESERVED_TAG, "terminal tag must not be unclassified", "terminal_tag_name")
    reserved = uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID)
    try:
        await aresolve_job_server(session, job_id, server_id=server_id)
        terminal_id = (await session.execute(text("SELECT tag_id FROM tags WHERE lower(name) = lower(:n) FOR UPDATE"), {"n": name})).scalar()
        if terminal_id is None:
            raise TagStoreError(TagErrorCode.TAG_NOT_FOUND, "no such terminal tag", "terminal_tag_name")
        rows = (await session.execute(text("SELECT tag_id, assignment_source FROM job_tags WHERE job_id = :j FOR UPDATE"), {"j": job_id})).all()
        has_terminal = any(row[0] == terminal_id for row in rows)
        if not has_terminal and any(row[0] != reserved for row in rows):
            return TerminalClassification(False, terminal_id, "meaningful_tags_present")
        if not has_terminal:
            await session.execute(text("INSERT INTO job_tags (job_id, tag_id, assigned_by, assignment_source) VALUES (:j, :t, :by, :src) ON CONFLICT DO NOTHING"), {"j": job_id, "t": terminal_id, "by": "", "src": SYSTEM_ASSIGNMENT_SOURCE})
        deleted = await session.execute(text("DELETE FROM job_tags WHERE job_id = :j AND tag_id = :t AND assignment_source = :src"), {"j": job_id, "t": reserved, "src": SYSTEM_ASSIGNMENT_SOURCE})
        removed = deleted.rowcount > 0
        return TerminalClassification(not has_terminal or removed, terminal_id, "classified" if not has_terminal else ("removed_system_unclassified" if removed else "already_classified"))
    except DBAPIError as exc:
        raise aclassify_integrity_error(exc) from exc


async def aclassify_terminal_job(
    sessions: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    terminal_tag_name: str,
) -> TerminalClassification:
    async with sessions() as session, session.begin():
        return await _aclassify_terminal_job_locked(
            session, job_id, server_id=server_id, terminal_tag_name=terminal_tag_name
        )


def _validated_description(description: str | None) -> str | None:
    if description is None:
        return None
    if not isinstance(description, str):
        raise TagStoreError(
            TagErrorCode.VALIDATION, "description must be a string", "description"
        )
    if len(description) > _DESCRIPTION_MAX:
        raise TagStoreError(
            TagErrorCode.VALIDATION,
            f"description must be at most {_DESCRIPTION_MAX} characters",
            "description",
        )
    return description


def _validated_assigned_by(assigned_by: str) -> str:
    if not isinstance(assigned_by, str):
        raise TagStoreError(
            TagErrorCode.VALIDATION, "assigned_by must be a string", "assigned_by"
        )
    if len(assigned_by) > _ASSIGNED_BY_MAX:
        raise TagStoreError(
            TagErrorCode.VALIDATION,
            f"assigned_by must be at most {_ASSIGNED_BY_MAX} characters",
            "assigned_by",
        )
    return assigned_by


def _validated_source(assignment_source: str) -> str:
    if assignment_source not in _ASSIGNMENT_SOURCES:
        raise TagStoreError(
            TagErrorCode.VALIDATION,
            "assignment_source must be one of: agent, user, system",
            "assignment_source",
        )
    return assignment_source


def _check_page_bounds(limit: int, offset: int) -> None:
    """Bounded-page validation (AGENTS.md §1): fail closed, never clamp."""
    limit_ok = isinstance(limit, int) and not isinstance(limit, bool)
    if not limit_ok or not 1 <= limit <= MAX_PAGE_LIMIT:
        raise ValueError(f"limit must be an integer in [1, {MAX_PAGE_LIMIT}]")
    offset_ok = isinstance(offset, int) and not isinstance(offset, bool)
    if not offset_ok or offset < 0:
        raise ValueError("offset must be an integer >= 0")


def _like_pattern(value: str) -> str:
    """Escape LIKE metacharacters for a case-insensitive substring filter."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def aclassify_integrity_error(exc: DBAPIError) -> TagStoreError:
    """Translate a schema-level integrity failure into a typed store error.

    The deferred constraint triggers (migration 0011) are the invariant
    backstop against out-of-band writers and race windows; their messages
    are matched value-free. They fire at COMMIT (SQLSTATE P0001, wrapped as
    ``ProgrammingError``); FK/RESTRICT and unique failures arrive as
    ``IntegrityError`` — both are ``DBAPIError`` subclasses, and only
    ``exc.orig`` (sqlstate + message) is inspected here.
    """
    orig = exc.orig
    sqlstate = getattr(orig, "sqlstate", None) or ""
    message = str(orig)
    for marker, code in _TRIGGER_MARKERS.items():
        if marker in message:
            return TagStoreError(code, message)
    if sqlstate in {"23001", "23503"}:
        return TagStoreError(
            TagErrorCode.TAG_NOT_FOUND, "tag no longer exists", "tag_id"
        )
    return TagStoreError(
        TagErrorCode.INVARIANT_VIOLATION,
        "the mandatory-tag schema refused the write",
    )


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TagRow:
    """One catalog row: the normalized display name IS the stored name."""

    tag_id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class EntityTagRow:
    """One association row as seen from its owning entity."""

    tag_id: uuid.UUID
    name: str
    assigned_by: str
    assignment_source: str
    created_at: datetime


# NOTE: EntityTagPreview is re-exported from :mod:`mai.contracts.tags`
# (D01 neutral extraction) — see the import block above.


def _row_to_tag(row: Mapping[str, object]) -> TagRow:
    return TagRow(
        tag_id=row["tag_id"],  # type: ignore[arg-type]
        name=row["name"],  # type: ignore[arg-type]
        description=row["description"],  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
        updated_at=row["updated_at"],  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------


async def acreate_tag(
    sessions: async_sessionmaker[AsyncSession],
    *,
    name: str,
    description: str | None = None,
    tag_id: uuid.UUID | None = None,
) -> TagRow:
    """Create (or canonically return) one catalog tag.

    The name is normalized first; an existing canonical match is returned
    untouched — an idempotent re-create never overwrites the stored
    description. A lost create race (unique ``lower(name)`` index) re-reads
    and returns the winner's row.
    """
    normalized = anormalize_tag_name(name)
    checked = _validated_description(description)
    try:
        async with sessions() as session, session.begin():
            existing = (
                await session.execute(
                    text(
                        "SELECT tag_id, name, description, created_at, updated_at "
                        "FROM tags WHERE lower(name) = lower(:name)"
                    ),
                    {"name": normalized},
                )
            ).mappings().first()
            if existing is not None:
                return _row_to_tag(existing)
            row = (
                await session.execute(
                    text(
                        """
                        INSERT INTO tags (tag_id, name, description)
                        VALUES (:tag_id, :name, :description)
                        RETURNING tag_id, name, description, created_at, updated_at
                        """
                    ),
                    {
                        "tag_id": tag_id or uuid.uuid4(),
                        "name": normalized,
                        "description": checked,
                    },
                )
            ).mappings().one()
    except IntegrityError as exc:
        # Lost canonical create race (unique lower(name) index): the
        # winner's row is the answer. The re-read needs a FRESH
        # transaction — the losing one aborted with the constraint.
        async with sessions() as session:
            existing = (
                await session.execute(
                    text(
                        "SELECT tag_id, name, description, created_at, updated_at "
                        "FROM tags WHERE lower(name) = lower(:name)"
                    ),
                    {"name": normalized},
                )
            ).mappings().first()
        if existing is not None:
            return _row_to_tag(existing)
        raise aclassify_integrity_error(exc) from exc
    return _row_to_tag(row)


async def alist_tags(
    sessions: async_sessionmaker[AsyncSession],
    *,
    name_filter: str | None = None,
    description_filter: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
) -> tuple[list[TagRow], int]:
    """Deterministic bounded catalog page with case-insensitive filters.

    ``ORDER BY lower(name)`` is total (canonical names are unique), so the
    page order is stable; ``COUNT(*)`` runs under the identical predicate.
    """
    _check_page_bounds(limit, offset)
    predicate = "WHERE TRUE"
    params: dict[str, object] = {"l": limit, "o": offset}
    if name_filter:
        predicate += " AND name ILIKE :nfilter"
        params["nfilter"] = _like_pattern(name_filter)
    if description_filter:
        predicate += " AND description ILIKE :dfilter"
        params["dfilter"] = _like_pattern(description_filter)
    async with sessions() as session:
        total = int(
            (
                await session.execute(
                    text(f"SELECT COUNT(*) FROM tags {predicate}"), params
                )
            ).scalar_one()
        )
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT tag_id, name, description, created_at, updated_at
                    FROM tags {predicate}
                    ORDER BY lower(name), tag_id
                    LIMIT :l OFFSET :o
                    """
                ),
                params,
            )
        ).mappings().all()
    return [_row_to_tag(row) for row in rows], total


async def aget_tag(
    sessions: async_sessionmaker[AsyncSession], tag_id: uuid.UUID
) -> TagRow:
    """One catalog row by id; ``tag_not_found`` when absent."""
    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    "SELECT tag_id, name, description, created_at, updated_at "
                    "FROM tags WHERE tag_id = :t"
                ),
                {"t": tag_id},
            )
        ).mappings().first()
    if row is None:
        raise TagStoreError(TagErrorCode.TAG_NOT_FOUND, "no such tag", "tag_id")
    return _row_to_tag(row)


async def adelete_tag(
    sessions: async_sessionmaker[AsyncSession],
    tag_id: uuid.UUID,
    *,
    force: bool = False,
    replacement_tag_id: uuid.UUID | None = None,
) -> int:
    """Delete one catalog tag under the used/unused policy.

    Returns the number of removed association rows. An unused tag deletes
    directly; a used tag refuses without ``force=True``; a forced delete
    proceeds only when every affected entity keeps another tag or receives
    ``replacement_tag_id`` inside this same transaction — otherwise
    ``tag_in_use`` is raised and nothing changes. The reserved
    ``unclassified`` row can never be deleted. The catalog-row lock
    serializes this against concurrent association writers, and the FULL
    transaction (commit included) is failure-translated: a commit-time
    deferred-trigger refusal (a racing remover emptied an entity) surfaces
    as the typed invariant code, never a raw DB exception (see module
    docstring).
    """
    try:
        async with sessions() as session, session.begin():
            return await _adelete_tag_locked(
                session, tag_id, force=force, replacement_tag_id=replacement_tag_id
            )
    except DBAPIError as exc:
        raise aclassify_integrity_error(exc) from exc


async def _adelete_tag_locked(
    session: AsyncSession,
    tag_id: uuid.UUID,
    *,
    force: bool,
    replacement_tag_id: uuid.UUID | None,
) -> int:
    locked = (
        await session.execute(
            text("SELECT tag_id FROM tags WHERE tag_id = :t FOR UPDATE"),
            {"t": tag_id},
        )
    ).scalar()
    if locked is None:
        raise TagStoreError(TagErrorCode.TAG_NOT_FOUND, "no such tag", "tag_id")
    if tag_id == uuid.UUID(RESERVED_UNCLASSIFIED_TAG_ID):
        raise TagStoreError(
            TagErrorCode.RESERVED_TAG,
            "the reserved unclassified tag cannot be deleted",
            "tag_id",
        )
    total_refs = int(
        (
            await session.execute(
                text("SELECT count(*) FROM job_tags WHERE tag_id = :t"),
                {"t": tag_id},
            )
        ).scalar_one()
    )
    if total_refs == 0:
        await session.execute(text("DELETE FROM tags WHERE tag_id = :t"), {"t": tag_id})
        return 0
    if not force:
        raise TagStoreError(
            TagErrorCode.TAG_IN_USE,
            "the tag is still assigned; delete with force after review",
            "tag_id",
        )
    # Entities whose ONLY tag is the doomed one need the replacement
    # (or the refusal below). The replacement itself is locked too, so
    # a concurrent replacement deletion cannot strand the entity.
    if replacement_tag_id is not None:
        if replacement_tag_id == tag_id:
            raise TagStoreError(
                TagErrorCode.VALIDATION,
                "the replacement must differ from the deleted tag",
                "replacement_tag_id",
            )
        replaced = (
            await session.execute(
                text(
                    "SELECT tag_id FROM tags WHERE tag_id = :r FOR UPDATE"
                ),
                {"r": replacement_tag_id},
            )
        ).scalar()
        if replaced is None:
            raise TagStoreError(
                TagErrorCode.TAG_NOT_FOUND,
                "no such replacement tag",
                "replacement_tag_id",
            )
    needy_jobs = (
        await session.execute(
            text(
                """
                SELECT jt.job_id FROM job_tags jt
                WHERE jt.tag_id = :t
                  AND NOT EXISTS (
                      SELECT 1 FROM job_tags other
                      WHERE other.job_id = jt.job_id AND other.tag_id <> :t
                  )
                """
            ),
            {"t": tag_id},
        )
    ).scalars().all()
    if needy_jobs and replacement_tag_id is None:
        raise TagStoreError(
            TagErrorCode.TAG_IN_USE,
            "some entities keep only this tag; pass replacement_tag_id "
            "to re-classify them in the same transaction",
            "replacement_tag_id",
        )
    if needy_jobs:
        await session.execute(
            text(
                """
                INSERT INTO job_tags (job_id, tag_id, assignment_source)
                SELECT u.job_id, :r, :src
                FROM unnest(CAST(:jobs AS uuid[])) AS u(job_id)
                ON CONFLICT DO NOTHING
                """
            ),
            {
                "r": replacement_tag_id,
                "src": SYSTEM_ASSIGNMENT_SOURCE,
                "jobs": list(needy_jobs),
            },
        )
    await session.execute(
        text("DELETE FROM job_tags WHERE tag_id = :t"), {"t": tag_id}
    )
    await session.execute(text("DELETE FROM tags WHERE tag_id = :t"), {"t": tag_id})
    return total_refs


# ---------------------------------------------------------------------------
# entity resolution (same-server, closed refusal)
# ---------------------------------------------------------------------------


async def aresolve_job_server(
    session: AsyncSession, job_id: uuid.UUID, *, server_id: uuid.UUID
) -> None:
    """Resolve a job run inside the requesting server scope, else refuse.

    A missing row and a cross-server row are the same closed refusal —
    existence never leaks across scopes.
    """
    row = (
        await session.execute(
            text("SELECT server_id FROM job_runs WHERE run_id = :j"),
            {"j": job_id},
        )
    ).scalar()
    if row is None or row != server_id:
        raise TagStoreError(
            TagErrorCode.ENTITY_NOT_FOUND,
            "no job run in the requesting server scope",
            "job_id",
        )


# ---------------------------------------------------------------------------
# associations (idempotent; scoped; locked)
# ---------------------------------------------------------------------------


async def _alocked_tag_id(session: AsyncSession, tag_id: uuid.UUID) -> None:
    """Lock the catalog row (the per-tag writer serialization point)."""
    locked = (
        await session.execute(
            text("SELECT tag_id FROM tags WHERE tag_id = :t FOR UPDATE"),
            {"t": tag_id},
        )
    ).scalar()
    if locked is None:
        raise TagStoreError(TagErrorCode.TAG_NOT_FOUND, "no such tag", "tag_id")


async def aassign_job_tag(
    sessions: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    tag_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    assigned_by: str = "",
    assignment_source: str = SYSTEM_ASSIGNMENT_SOURCE,
) -> None:
    """Assign one tag to a job run; re-assignment is a no-op."""
    source = _validated_source(assignment_source)
    actor = _validated_assigned_by(assigned_by)
    try:
        async with sessions() as session, session.begin():
            await _alocked_tag_id(session, tag_id)
            await aresolve_job_server(session, job_id, server_id=server_id)
            await session.execute(
                text(
                    """
                    INSERT INTO job_tags (job_id, tag_id, assigned_by,
                                          assignment_source)
                    VALUES (:j, :t, :by, :src)
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"j": job_id, "t": tag_id, "by": actor, "src": source},
            )
    except IntegrityError as exc:
        raise aclassify_integrity_error(exc) from exc


async def aremove_job_tag(
    sessions: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    tag_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    replacement_tag_id: uuid.UUID | None = None,
) -> None:
    """Remove one job-tag association; idempotent, final-tag protected.

    Removing the entity's last ordinary tag requires ``replacement_tag_id``:
    the removal and the replacement commit together, so the entity never
    drops below one tag. Removing an absent association is a no-op.
    """
    if replacement_tag_id == tag_id and replacement_tag_id is not None:
        raise TagStoreError(
            TagErrorCode.VALIDATION,
            "the replacement must differ from the removed tag",
            "replacement_tag_id",
        )
    try:
        async with sessions() as session, session.begin():
            await _alocked_tag_id(session, tag_id)
            await aresolve_job_server(session, job_id, server_id=server_id)
            bound = (
                await session.execute(
                    text(
                        "SELECT 1 FROM job_tags WHERE job_id = :j AND tag_id = :t"
                    ),
                    {"j": job_id, "t": tag_id},
                )
            ).scalar()
            if bound is None:
                return
            remaining = int(
                (
                    await session.execute(
                        text("SELECT count(*) FROM job_tags WHERE job_id = :j"),
                        {"j": job_id},
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
                text("DELETE FROM job_tags WHERE job_id = :j AND tag_id = :t"),
                {"j": job_id, "t": tag_id},
            )
            if replacement_tag_id is not None:
                await _alocked_tag_id(session, replacement_tag_id)
                await session.execute(
                    text(
                        """
                        INSERT INTO job_tags (job_id, tag_id, assignment_source)
                        VALUES (:j, :r, :src)
                        ON CONFLICT DO NOTHING
                        """
                    ),
                    {
                        "j": job_id,
                        "r": replacement_tag_id,
                        "src": SYSTEM_ASSIGNMENT_SOURCE,
                    },
                )
    except DBAPIError as exc:
        # The in-transaction refusal above is typed directly; anything from
        # the DB — including the commit-time deferred-trigger P0001
        # (ProgrammingError) when a racing remover emptied the entity — is
        # classified here, after the transaction's commit/rollback ran.
        raise aclassify_integrity_error(exc) from exc


# ---------------------------------------------------------------------------
# per-entity pages + batch previews
# ---------------------------------------------------------------------------


async def atags_for_job(
    sessions: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
) -> tuple[list[EntityTagRow], int]:
    """Deterministic bounded tag page for one job run (scope-checked)."""
    return await _atags_for_entity(
        sessions,
        job_id,
        server_id=server_id,
        limit=limit,
        offset=offset,
        table="job_tags",
        entity_column="job_id",
        resolver=aresolve_job_server,
    )


async def _atags_for_entity(
    sessions: async_sessionmaker[AsyncSession],
    entity_id: uuid.UUID,
    *,
    server_id: uuid.UUID,
    limit: int,
    offset: int,
    table: str,
    entity_column: str,
    resolver,
) -> tuple[list[EntityTagRow], int]:
    _check_page_bounds(limit, offset)
    async with sessions() as session:
        await resolver(session, entity_id, server_id=server_id)
        total = int(
            (
                await session.execute(
                    text(f"SELECT count(*) FROM {table} WHERE {entity_column} = :e"),
                    {"e": entity_id},
                )
            ).scalar_one()
        )
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT t.tag_id, t.name, a.assigned_by,
                           a.assignment_source, a.created_at
                    FROM {table} a JOIN tags t ON t.tag_id = a.tag_id
                    WHERE a.{entity_column} = :e
                    ORDER BY lower(t.name), t.tag_id
                    LIMIT :l OFFSET :o
                    """
                ),
                {"e": entity_id, "l": limit, "o": offset},
            )
        ).mappings().all()
    items = [
        EntityTagRow(
            tag_id=row["tag_id"],  # type: ignore[arg-type]
            name=row["name"],  # type: ignore[arg-type]
            assigned_by=row["assigned_by"],  # type: ignore[arg-type]
            assignment_source=row["assignment_source"],  # type: ignore[arg-type]
            created_at=row["created_at"],  # type: ignore[arg-type]
        )
        for row in rows
    ]
    return items, total


async def apreview_job_tags(
    sessions: async_sessionmaker[AsyncSession],
    job_ids: Sequence[uuid.UUID],
    *,
    server_id: uuid.UUID,
    preview_limit: int = 3,
) -> dict[uuid.UUID, EntityTagPreview]:
    """Bounded per-job tag preview + count in ONE grouped query (no N+1).

    Only job runs inside the requesting server scope appear in the result;
    unknown or cross-server ids are silently absent (the caller renders an
    honest empty preview).
    """
    return await _apreview_entity_tags(
        sessions,
        job_ids,
        server_id=server_id,
        preview_limit=preview_limit,
        association="job_tags",
        entity_column="job_id",
        parent="job_runs",
        parent_key="run_id",
    )


async def _apreview_entity_tags(
    sessions: async_sessionmaker[AsyncSession],
    entity_ids: Sequence[uuid.UUID],
    *,
    server_id: uuid.UUID,
    preview_limit: int,
    association: str,
    entity_column: str,
    parent: str,
    parent_key: str,
) -> dict[uuid.UUID, EntityTagPreview]:
    if not isinstance(preview_limit, int) or isinstance(preview_limit, bool):
        raise ValueError("preview_limit must be an integer")
    if not 1 <= preview_limit <= _PREVIEW_MAX:
        raise ValueError(f"preview_limit must be an integer in [1, {_PREVIEW_MAX}]")
    ids = sorted({*entity_ids})
    if not ids:
        return {}
    async with sessions() as session:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT e.entity_id,
                           count(*) AS total,
                           (array_agg(t.name ORDER BY t.name))[1 : :pl] AS preview
                    FROM (SELECT DISTINCT unnest(CAST(:ids AS uuid[])) AS entity_id) e
                    JOIN {parent} p ON p.{parent_key} = e.entity_id
                        AND p.server_id = :sid
                    JOIN {association} a ON a.{entity_column} = e.entity_id
                    JOIN tags t ON t.tag_id = a.tag_id
                    GROUP BY e.entity_id
                    """
                ),
                {"ids": ids, "sid": server_id, "pl": preview_limit},
            )
        ).mappings().all()
    return {
        row["entity_id"]: EntityTagPreview(  # type: ignore[arg-type]
            preview=tuple(row["preview"] or ()),  # type: ignore[arg-type]
            total=int(row["total"]),
        )
        for row in rows
    }
