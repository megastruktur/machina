"""History-tag capability surface for the agent (job-run vocabulary).

The current-target operations (``tags.assign``, ``tags.remove``,
``tags.list_for``) address the execution context's own job run and are
wired to the typed tag store — every classified store failure is
projected onto a value-free refused envelope, mutations stay idempotent
with atomic replacement support on remove, and ``list_for`` returns one
deterministic bounded page (limit/offset + honest total).

The catalog operations (``tags.create``, ``tags.list_catalog``,
``tags.delete``) manage the global tag vocabulary through the store's
reviewed catalog functions: canonical idempotent create, deterministic
bounded listing, and used/reserved-refusing delete whose forced path
still preserves every record's final tag. The ``tags.search_jobs`` read
pages this server's job runs by their tags through the read-only
history store, and ``tags.read_job`` pages one same-server job run's
content through the reviewed job content reader. The conversation
capabilities (``tags.search_conversations``/``tags.read_conversation``)
were removed with the D09 coordinated deletion. No registration or UI
belongs to this module.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from typing import Final, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.capabilities.dispatch import CapabilityCall
from mai.capabilities.internal_api import (
    InternalApiComposition,
    InternalApiError,
    InternalApiOperation,
    InternalApiSpec,
    build_descriptor,
    build_executor,
    internal_api_tool,
)
from mai.history_store import aread_job_content, asearch_jobs_by_tags
from mai.plugins.registry import CapabilityRegistry
from mai.tag_store import (
    TagErrorCode,
    TagStoreError,
    aassign_job_tag,
    acreate_tag,
    adelete_tag,
    alist_tags,
    aremove_job_tag,
    atags_for_job,
)

TAGS_ASSIGN_CAPABILITY: Final[str] = "tags.assign"
TAGS_REMOVE_CAPABILITY: Final[str] = "tags.remove"
TAGS_LIST_FOR_CAPABILITY: Final[str] = "tags.list_for"
TAGS_CREATE_CAPABILITY: Final[str] = "tags.create"
TAGS_LIST_CATALOG_CAPABILITY: Final[str] = "tags.list_catalog"
TAGS_DELETE_CAPABILITY: Final[str] = "tags.delete"
TAGS_SEARCH_JOBS_CAPABILITY: Final[str] = "tags.search_jobs"
TAGS_READ_JOB_CAPABILITY: Final[str] = "tags.read_job"

#: The public ``tags.*`` vocabulary: the current-target scaffold, the
#: catalog tools, and the job tag-search and content reads.
TAGS_CAPABILITY_IDS: Final[tuple[str, ...]] = (
    TAGS_ASSIGN_CAPABILITY,
    TAGS_REMOVE_CAPABILITY,
    TAGS_LIST_FOR_CAPABILITY,
    TAGS_CREATE_CAPABILITY,
    TAGS_LIST_CATALOG_CAPABILITY,
    TAGS_DELETE_CAPABILITY,
    TAGS_SEARCH_JOBS_CAPABILITY,
    TAGS_READ_JOB_CAPABILITY,
)

#: Bounded pagination vocabulary for ``tags.list_for`` (AGENTS.md §1).
TAGS_LIST_DEFAULT_LIMIT: Final[int] = 10
TAGS_LIST_MAX_LIMIT: Final[int] = 100

#: Bounded search vocabulary for ``tags.search_jobs`` (AGENTS.md §1).
TAGS_SEARCH_DEFAULT_LIMIT: Final[int] = 50
TAGS_SEARCH_MAX_LIMIT: Final[int] = 50
TAGS_SEARCH_MAX_TAG_IDS: Final[int] = 50

#: Bounded content-read vocabulary for ``tags.read_job`` (AGENTS.md §1).
TAGS_READ_DEFAULT_LIMIT: Final[int] = 50
TAGS_READ_MAX_LIMIT: Final[int] = 50

#: Bounded catalog vocabulary (AGENTS.md §1), mirroring the tag store's
#: own canonical-name and description validation.
TAGS_NAME_MAX_LENGTH: Final[int] = 64
TAGS_DESCRIPTION_MAX_LENGTH: Final[int] = 500
TAGS_QUERY_MAX_LENGTH: Final[int] = 64

#: The tag store's canonical control-character class for names.
_NAME_CONTROL_CHARS: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f\x80-\x9f]")


# ---------------------------------------------------------------------------
# Payload contracts (fail-closed at declaration; documented for LLM matching)
# ---------------------------------------------------------------------------


class TagsAssignPayload(BaseModel):
    """Attach an existing history tag to the current job run."""

    tag_id: uuid.UUID = Field(
        description=(
            "Canonical UUID of the existing history tag to attach; the "
            "tag must already exist in the tag store."
        ),
    )


class TagsRemovePayload(BaseModel):
    """Detach a history tag from the current job run."""

    tag_id: uuid.UUID = Field(
        description=(
            "Canonical UUID of the history tag to detach from the "
            "current job run."
        ),
    )
    replacement_tag_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Optional tag UUID attached in place of the removed one; "
            "omit to remove without replacement."
        ),
    )


class TagsListForPayload(BaseModel):
    """List the history tags attached to the current job run."""

    limit: int = Field(
        default=TAGS_LIST_DEFAULT_LIMIT,
        ge=1,
        le=TAGS_LIST_MAX_LIMIT,
        description=(
            "Maximum number of tag records returned in this bounded "
            "page (1-100)."
        ),
    )
    offset: int = Field(
        default=0,
        ge=0,
        description=(
            "Number of matching tag records to skip before this page "
            "starts."
        ),
    )


class TagsSearchJobsPayload(BaseModel):
    """Search this server's job runs by their history tags."""

    tag_ids: list[uuid.UUID] = Field(
        min_length=1,
        max_length=TAGS_SEARCH_MAX_TAG_IDS,
        description=(
            "1-50 canonical UUIDs of the existing history tags the "
            "matched job runs must carry; the match mode decides "
            "whether any one of them or every one of them must be "
            "attached to a run for it to match."
        ),
    )
    match: Literal["any", "all"] = Field(
        default="any",
        description=(
            "Whether a job run matches when it carries at least one "
            "of the requested tags ('any', the default) or every "
            "requested tag ('all')."
        ),
    )
    limit: int = Field(
        default=TAGS_SEARCH_DEFAULT_LIMIT,
        ge=1,
        le=TAGS_SEARCH_MAX_LIMIT,
        description=(
            "Maximum number of matched job runs returned in this "
            "bounded page (1-50)."
        ),
    )
    offset: int = Field(
        default=0,
        ge=0,
        description=(
            "Number of matching job runs to skip before this page "
            "starts."
        ),
    )


class TagsReadJobPayload(BaseModel):
    """Read one job run's stored content row."""

    run_id: uuid.UUID = Field(
        description=(
            "Canonical UUID of the job run to read; only runs stored "
            "on this server are visible and another server's run is "
            "indistinguishable from a missing one."
        ),
    )


class TagsCreatePayload(BaseModel):
    """Create (or canonically return) one history tag in the catalog."""

    name: str = Field(
        min_length=1,
        description=(
            "Display name of the tag; surrounding and repeated "
            "whitespace is trimmed, and after trimming it must be "
            "1-64 printable single-line characters. Matching against "
            "existing tags is case-insensitive: an existing tag with "
            "the same canonical name is returned unchanged."
        ),
    )
    description: str | None = Field(
        default=None,
        max_length=TAGS_DESCRIPTION_MAX_LENGTH,
        description=(
            "Optional human-readable purpose of the tag, at most 500 "
            "characters; omit to leave the description unset."
        ),
    )

    @field_validator("name")
    @classmethod
    def _canonical_name(cls, value: str) -> str:
        """Mirror the store's canonical form so refusal happens up front."""
        if _NAME_CONTROL_CHARS.search(value):
            raise ValueError("name must be single-line printable characters")
        normalized = re.sub(r"\s+", " ", value).strip()
        if not normalized:
            raise ValueError(
                "name must contain at least one non-whitespace character"
            )
        if len(normalized) > TAGS_NAME_MAX_LENGTH:
            raise ValueError(
                f"name must be at most {TAGS_NAME_MAX_LENGTH} characters"
            )
        return normalized


class TagsListCatalogPayload(BaseModel):
    """Read one deterministic bounded page of the global tag catalog."""

    q: str | None = Field(
        default=None,
        min_length=1,
        max_length=TAGS_QUERY_MAX_LENGTH,
        description=(
            "Optional case-insensitive substring filter on the tag "
            "name, 1-64 characters; omit to list every catalog tag."
        ),
    )
    limit: int = Field(
        default=TAGS_LIST_DEFAULT_LIMIT,
        ge=1,
        le=TAGS_LIST_MAX_LIMIT,
        description=(
            "Maximum number of catalog tag records returned in this "
            "bounded page (1-100)."
        ),
    )
    offset: int = Field(
        default=0,
        ge=0,
        description=(
            "Number of matching catalog tag records to skip before "
            "this page starts."
        ),
    )


class TagsDeletePayload(BaseModel):
    """Delete one history tag from the global catalog."""

    tag_id: uuid.UUID = Field(
        description=(
            "Canonical UUID of the catalog tag to delete; the tag must "
            "exist in the tag store."
        ),
    )
    force: bool = Field(
        default=False,
        description=(
            "Whether a tag that is still attached to history records "
            "may be force-deleted. With the default False a used tag "
            "is refused; set True to force the delete. The reserved "
            "'unclassified' tag is always refused, with or without "
            "force. A forced delete proceeds only while every "
            "affected record keeps another tag, so the final tag of "
            "a record is always preserved."
        ),
    )


# ---------------------------------------------------------------------------
# Typed store failures → value-free wire refusals
# ---------------------------------------------------------------------------


#: Frozen mapping: every classified store failure has exactly one wire code.
_TAG_STORE_WIRE_CODES: Final[Mapping[TagErrorCode, str]] = {
    TagErrorCode.VALIDATION: "invalid_request",
    TagErrorCode.TAG_NOT_FOUND: "not_found",
    TagErrorCode.ENTITY_NOT_FOUND: "not_found",
    TagErrorCode.LAST_TAG: "conflict",
    TagErrorCode.TAG_IN_USE: "conflict",
    TagErrorCode.RESERVED_TAG: "conflict",
    TagErrorCode.INVARIANT_VIOLATION: "conflict",
}


def _tags_refusal(exc: TagStoreError) -> InternalApiError:
    """Project one classified store failure onto the refused envelope."""
    return InternalApiError(_TAG_STORE_WIRE_CODES[exc.code], str(exc))


# ---------------------------------------------------------------------------
# The composition builder
# ---------------------------------------------------------------------------


def build_history_tag_capabilities(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> InternalApiComposition:
    """Build the ``tags.*`` descriptors + executors.

    The current-target operations (``tags.assign``/``tags.remove``/
    ``tags.list_for``) address the execution context's own job run and
    go through the typed tag store: writes are idempotent and
    scope-checked, the read returns one deterministic bounded page. The
    catalog operations (``tags.create``/``tags.list_catalog``/
    ``tags.delete``) manage the global tag vocabulary through the same
    store's reviewed catalog functions. Every operation refuses
    ``unavailable`` only when no ``session_factory`` is wired.
    Descriptors are validated fail-closed at build time
    (``assert_agent_ready_descriptor`` via :func:`build_descriptor`).
    """
    operations: list[tuple[InternalApiSpec, InternalApiOperation]] = []

    # -- tags.assign ---------------------------------------------------------

    @internal_api_tool(
        capability_id=TAGS_ASSIGN_CAPABILITY,
        version="0.2.0",
        effect_class="mutate",
        resource_scope=["history_tags"],
        environments=["controller"],
        payload_model=TagsAssignPayload,
        description=(
            "Attach one existing history tag to the job run this "
            "execution belongs to. The tag is referenced by canonical "
            "UUID; another server's runs are invisible."
        ),
    )
    async def _tags_assign(
        call: CapabilityCall, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        if session_factory is None:
            raise InternalApiError("unavailable", "tag store is not configured")
        tag_id = uuid.UUID(str(payload["tag_id"]))
        try:
            await aassign_job_tag(
                session_factory,
                call.context.run_id,
                tag_id,
                server_id=call.context.server_id,
                assigned_by=call.context.principal,
                assignment_source="agent",
            )
        except TagStoreError as exc:
            raise _tags_refusal(exc) from exc
        return {
            "ok": True,
            "entity_id": str(call.context.run_id),
            "tag_id": str(tag_id),
        }

    operations.append(_tags_assign)

    # -- tags.remove ---------------------------------------------------------

    @internal_api_tool(
        capability_id=TAGS_REMOVE_CAPABILITY,
        version="0.2.0",
        effect_class="mutate",
        resource_scope=["history_tags"],
        environments=["controller"],
        payload_model=TagsRemovePayload,
        description=(
            "Detach one history tag from the job run this execution "
            "belongs to. Optionally names one replacement tag UUID "
            "attached in place of the removed one; another server's "
            "runs are invisible."
        ),
    )
    async def _tags_remove(
        call: CapabilityCall, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        if session_factory is None:
            raise InternalApiError("unavailable", "tag store is not configured")
        tag_id = uuid.UUID(str(payload["tag_id"]))
        raw_replacement = payload.get("replacement_tag_id")
        replacement_tag_id = (
            uuid.UUID(str(raw_replacement)) if raw_replacement is not None else None
        )
        try:
            await aremove_job_tag(
                session_factory,
                call.context.run_id,
                tag_id,
                server_id=call.context.server_id,
                replacement_tag_id=replacement_tag_id,
            )
        except TagStoreError as exc:
            raise _tags_refusal(exc) from exc
        return {
            "ok": True,
            "entity_id": str(call.context.run_id),
            "tag_id": str(tag_id),
            "replacement_tag_id": (
                str(replacement_tag_id) if replacement_tag_id is not None else None
            ),
        }

    operations.append(_tags_remove)

    # -- tags.list_for ---------------------------------------------------------

    @internal_api_tool(
        capability_id=TAGS_LIST_FOR_CAPABILITY,
        version="0.2.0",
        effect_class="read",
        resource_scope=["history_tags"],
        environments=["controller"],
        payload_model=TagsListForPayload,
        description=(
            "List the history tags currently attached to the job run "
            "this execution belongs to. Returns one bounded page "
            "(limit/offset) with the pre-slice total; another "
            "server's runs are invisible."
        ),
    )
    async def _tags_list_for(
        call: CapabilityCall, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        if session_factory is None:
            raise InternalApiError("unavailable", "tag store is not configured")
        limit = int(payload.get("limit", TAGS_LIST_DEFAULT_LIMIT))
        offset = int(payload.get("offset", 0))
        try:
            items, total = await atags_for_job(
                session_factory,
                call.context.run_id,
                server_id=call.context.server_id,
                limit=limit,
                offset=offset,
            )
        except TagStoreError as exc:
            raise _tags_refusal(exc) from exc
        has_more = offset + len(items) < total
        return {
            "ok": True,
            "entity_id": str(call.context.run_id),
            "items": [
                {
                    "tag_id": str(row.tag_id),
                    "name": row.name,
                    "assigned_by": row.assigned_by,
                    "assignment_source": row.assignment_source,
                    "created_at": row.created_at.isoformat(),
                }
                for row in items
            ],
            "limit": limit,
            "offset": offset,
            "total": total,
            "has_more": has_more,
            "next_offset": offset + len(items) if has_more else None,
        }

    operations.append(_tags_list_for)

    # -- tags.search_jobs ------------------------------------------------------

    @internal_api_tool(
        capability_id=TAGS_SEARCH_JOBS_CAPABILITY,
        version="0.2.0",
        effect_class="read",
        resource_scope=["history_tags"],
        environments=["controller"],
        payload_model=TagsSearchJobsPayload,
        description=(
            "Search this server's job runs by their history tags: "
            "give 1-50 catalog tag UUIDs and whether any one of them "
            "or all of them must be attached. Returns one bounded "
            "newest-first page of matching runs with the pre-slice "
            "total plus has_more/next_offset, and a per-run preview "
            "of attached tag names; other servers' runs are "
            "invisible."
        ),
    )
    async def _tags_search_jobs(
        call: CapabilityCall, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        if session_factory is None:
            raise InternalApiError("unavailable", "tag store is not configured")
        limit = int(payload.get("limit", TAGS_SEARCH_DEFAULT_LIMIT))
        offset = int(payload.get("offset", 0))
        match: Literal["any", "all"] = (
            "all" if payload.get("match") == "all" else "any"
        )
        tag_ids = [uuid.UUID(str(item)) for item in payload["tag_ids"]]
        try:
            rows, total = await asearch_jobs_by_tags(
                session_factory,
                tag_ids,
                server_id=call.context.server_id,
                match=match,
                limit=limit,
                offset=offset,
            )
        except TagStoreError as exc:
            raise _tags_refusal(exc) from exc
        has_more = offset + len(rows) < total
        return {
            "ok": True,
            "match": match,
            "items": [
                {
                    "run_id": str(row.run_id),
                    "submitted_at": row.submitted_at.isoformat(),
                    "tag_preview": {
                        "preview": list(row.tag_preview.preview),
                        "total": row.tag_preview.total,
                    },
                }
                for row in rows
            ],
            "limit": limit,
            "offset": offset,
            "total": total,
            "has_more": has_more,
            "next_offset": offset + len(rows) if has_more else None,
        }

    operations.append(_tags_search_jobs)

    # -- tags.read_job -------------------------------------------------------

    @internal_api_tool(
        capability_id=TAGS_READ_JOB_CAPABILITY,
        version="0.2.0",
        effect_class="read",
        resource_scope=["history_tags"],
        environments=["controller"],
        payload_model=TagsReadJobPayload,
        description=(
            "Read one job run's stored content on this server: "
            "parentage, the frozen submit label, the replayed "
            "lifecycle state, and error/evidence references; no raw "
            "events are exposed and another server's run is "
            "indistinguishable from a missing one."
        ),
    )
    async def _tags_read_job(
        call: CapabilityCall, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        if session_factory is None:
            raise InternalApiError(
                "unavailable", "history store is not configured"
            )
        run_id = uuid.UUID(str(payload["run_id"]))
        try:
            job = await aread_job_content(
                session_factory,
                run_id,
                server_id=call.context.server_id,
            )
        except ValueError as exc:
            # One fixed value-free refusal: a missing and another
            # server's run are indistinguishable on the wire.
            raise InternalApiError("not_found", "job not found") from exc
        return {
            "ok": True,
            "run_id": str(job.run_id),
            "parent_run_id": (
                str(job.parent_run_id)
                if job.parent_run_id is not None
                else None
            ),
            "label": job.label,
            "state": job.state,
            "submitted_at": job.submitted_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
            "error_ref": job.error_ref,
            "evidence_ref": job.evidence_ref,
        }

    operations.append(_tags_read_job)

    # -- tags.create ---------------------------------------------------------

    @internal_api_tool(
        capability_id=TAGS_CREATE_CAPABILITY,
        version="0.2.0",
        effect_class="mutate",
        resource_scope=["history_tags"],
        environments=["controller"],
        payload_model=TagsCreatePayload,
        description=(
            "Create one history tag in the global catalog, or return "
            "the existing tag unchanged when a tag with the same "
            "canonical (case-insensitive, whitespace-normalized) name "
            "already exists. This manages the vocabulary only; no "
            "history record is tagged by this tool."
        ),
    )
    async def _tags_create(
        call: CapabilityCall, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        if session_factory is None:
            raise InternalApiError("unavailable", "tag store is not configured")
        raw_description = payload.get("description")
        try:
            row = await acreate_tag(
                session_factory,
                name=str(payload["name"]),
                description=(
                    str(raw_description) if raw_description is not None else None
                ),
            )
        except TagStoreError as exc:
            raise _tags_refusal(exc) from exc
        return {
            "ok": True,
            "tag_id": str(row.tag_id),
            "name": row.name,
            "description": row.description,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }

    operations.append(_tags_create)

    # -- tags.list_catalog -----------------------------------------------------

    @internal_api_tool(
        capability_id=TAGS_LIST_CATALOG_CAPABILITY,
        version="0.2.0",
        effect_class="read",
        resource_scope=["history_tags"],
        environments=["controller"],
        payload_model=TagsListCatalogPayload,
        description=(
            "List the global history tag catalog: one deterministic "
            "bounded page ordered by canonical tag name, optionally "
            "filtered by a case-insensitive name substring. Returns "
            "the pre-slice total plus has_more/next_offset so further "
            "pages can be fetched honestly."
        ),
    )
    async def _tags_list_catalog(
        call: CapabilityCall, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        if session_factory is None:
            raise InternalApiError("unavailable", "tag store is not configured")
        limit = int(payload.get("limit", TAGS_LIST_DEFAULT_LIMIT))
        offset = int(payload.get("offset", 0))
        raw_q = payload.get("q")
        try:
            items, total = await alist_tags(
                session_factory,
                name_filter=str(raw_q) if raw_q is not None else None,
                limit=limit,
                offset=offset,
            )
        except TagStoreError as exc:
            raise _tags_refusal(exc) from exc
        has_more = offset + len(items) < total
        return {
            "ok": True,
            "items": [
                {
                    "tag_id": str(row.tag_id),
                    "name": row.name,
                    "description": row.description,
                    "created_at": row.created_at.isoformat(),
                    "updated_at": row.updated_at.isoformat(),
                }
                for row in items
            ],
            "limit": limit,
            "offset": offset,
            "total": total,
            "has_more": has_more,
            "next_offset": offset + len(items) if has_more else None,
        }

    operations.append(_tags_list_catalog)

    # -- tags.delete ---------------------------------------------------------

    @internal_api_tool(
        capability_id=TAGS_DELETE_CAPABILITY,
        version="0.2.0",
        effect_class="mutate",
        resource_scope=["history_tags"],
        environments=["controller"],
        payload_model=TagsDeletePayload,
        description=(
            "Delete one history tag from the global catalog. A tag "
            "that is still attached to history records is refused by "
            "default and may be deleted with force; the reserved "
            "'unclassified' tag is always refused, with or without "
            "force. A forced delete proceeds only while every "
            "affected record keeps another tag, so final tags are "
            "always preserved."
        ),
    )
    async def _tags_delete(
        call: CapabilityCall, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        if session_factory is None:
            raise InternalApiError("unavailable", "tag store is not configured")
        tag_id = uuid.UUID(str(payload["tag_id"]))
        force = bool(payload.get("force", False))
        try:
            removed_associations = await adelete_tag(
                session_factory, tag_id, force=force
            )
        except TagStoreError as exc:
            raise _tags_refusal(exc) from exc
        return {
            "ok": True,
            "tag_id": str(tag_id),
            "force": force,
            "removed_associations": removed_associations,
        }

    operations.append(_tags_delete)

    # -- composition -----------------------------------------------------------

    registry = CapabilityRegistry()
    executors: dict[str, Callable[[CapabilityCall], object]] = {}
    for spec, operation in operations:
        descriptor = build_descriptor(spec)
        registry.register(descriptor)
        executors[spec.capability_id] = build_executor(spec, operation)
    return InternalApiComposition(
        descriptors=tuple(registry.list_capabilities()),
        executors=executors,
    )


__all__ = [
    "TAGS_ASSIGN_CAPABILITY",
    "TAGS_CAPABILITY_IDS",
    "TAGS_CREATE_CAPABILITY",
    "TAGS_DELETE_CAPABILITY",
    "TAGS_LIST_CATALOG_CAPABILITY",
    "TAGS_LIST_FOR_CAPABILITY",
    "TAGS_READ_DEFAULT_LIMIT",
    "TAGS_READ_MAX_LIMIT",
    "TAGS_READ_JOB_CAPABILITY",
    "TAGS_REMOVE_CAPABILITY",
    "TAGS_SEARCH_JOBS_CAPABILITY",
    "TagsAssignPayload",
    "TagsCreatePayload",
    "TagsDeletePayload",
    "TagsListCatalogPayload",
    "TagsListForPayload",
    "TagsReadJobPayload",
    "TagsRemovePayload",
    "TagsSearchJobsPayload",
    "build_history_tag_capabilities",
]
