"""History-tag capability surface for the chat agent (W4A3 store wiring).

All three operations (``tags.assign``, ``tags.remove``, ``tags.list_for``)
are wired to the typed tag store — the current target is resolved per
execution (the run itself for ``job``; the ONE active same-server
conversation link for ``conversation``), every classified store failure
is projected onto a value-free refused envelope, mutations stay
idempotent with atomic replacement support on remove, and ``list_for``
returns one deterministic bounded page (limit/offset + honest total).
No registration, catalog, or history/UI surface belongs to this card.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from typing import Final, Literal

from pydantic import BaseModel, Field
from sqlalchemy import text
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
from mai.plugins.registry import CapabilityRegistry
from mai.policy.evaluate import ExecutionContext
from mai.tag_store import (
    TagErrorCode,
    TagStoreError,
    aassign_conversation_tag,
    aassign_job_tag,
    aremove_conversation_tag,
    aremove_job_tag,
    atags_for_conversation,
    atags_for_job,
)

TAGS_ASSIGN_CAPABILITY: Final[str] = "tags.assign"
TAGS_REMOVE_CAPABILITY: Final[str] = "tags.remove"
TAGS_LIST_FOR_CAPABILITY: Final[str] = "tags.list_for"

#: The frozen public vocabulary of the W4A1 scaffold.
TAGS_CAPABILITY_IDS: Final[tuple[str, ...]] = (
    TAGS_ASSIGN_CAPABILITY,
    TAGS_REMOVE_CAPABILITY,
    TAGS_LIST_FOR_CAPABILITY,
)

#: Bounded pagination vocabulary for ``tags.list_for`` (AGENTS.md §1).
TAGS_LIST_DEFAULT_LIMIT: Final[int] = 10
TAGS_LIST_MAX_LIMIT: Final[int] = 100


# ---------------------------------------------------------------------------
# Payload contracts (fail-closed at declaration; documented for LLM matching)
# ---------------------------------------------------------------------------


class TagsAssignPayload(BaseModel):
    """Attach an existing history tag to the current target."""

    target: Literal["job", "conversation"] = Field(
        description=(
            "Which current target the tag attaches to: the running job "
            "itself, or the conversation this run is actively bound to "
            "on this server."
        )
    )
    tag_id: uuid.UUID = Field(
        description=(
            "Canonical UUID of the existing history tag to attach; the "
            "tag must already exist in the tag store."
        ),
    )


class TagsRemovePayload(BaseModel):
    """Detach history tags from the current target."""

    target: Literal["job", "conversation"] = Field(
        description=(
            "Which current target the tags are detached from: the "
            "running job itself, or the conversation this run is "
            "actively bound to on this server."
        )
    )
    tag_id: uuid.UUID = Field(
        description=(
            "Canonical UUID of the history tag to detach from the "
            "current target."
        ),
    )
    replacement_tag_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Optional tag UUID attached in place of the removed ones; "
            "omit to remove without replacement."
        ),
    )


class TagsListForPayload(BaseModel):
    """List the history tags attached to the current target."""

    target: Literal["job", "conversation"] = Field(
        description=(
            "Which current target to list tags for: the running job "
            "itself, or the conversation this run is actively bound to "
            "on this server."
        )
    )
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


# ---------------------------------------------------------------------------
# Typed store failures → value-free wire refusals (W4A2)
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
# Current-target resolution (shared by assign/remove/list_for wiring)
# ---------------------------------------------------------------------------


async def _aresolve_current_target(
    sessions: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    target: Literal["job", "conversation"],
) -> uuid.UUID:
    """Resolve the id a tag operation addresses for THIS execution.

    ``job`` resolves to the execution context's own run id.
    ``conversation`` resolves to the ONE active same-server conversation
    link of this run: a conversation on another server, or a released
    (``released_at`` set) link, simply does not resolve — every failure
    is closed and value-free (``not_found``). No tag_store access here.
    """
    if target == "job":
        return context.run_id
    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    "SELECT cr.conversation_id AS cid "
                    "FROM conversation_runs cr "
                    "JOIN conversations c "
                    "ON c.conversation_id = cr.conversation_id "
                    "WHERE cr.run_id = :rid "
                    "AND cr.released_at IS NULL "
                    "AND c.server_id = :sid "
                    "LIMIT 1"
                ),
                {"rid": context.run_id, "sid": context.server_id},
            )
        ).first()
    if row is None:
        raise InternalApiError(
            "not_found",
            "no active conversation is bound to this run on this server",
        )
    return uuid.UUID(str(row[0]))


# ---------------------------------------------------------------------------
# The W4A1 composition builder (store wiring arrives in W4A2+)
# ---------------------------------------------------------------------------


def build_history_tag_capabilities(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> InternalApiComposition:
    """Build the ``tags.*`` descriptors + executors (W4A3 wiring).

    All three operations resolve the current target and go through the
    typed tag store: ``tags.assign``/``tags.remove`` write (idempotent,
    scope-checked), ``tags.list_for`` reads one deterministic bounded
    page. They refuse ``unavailable`` only when no ``session_factory``
    is wired. Descriptors are validated fail-closed at build time
    (``assert_agent_ready_descriptor`` via :func:`build_descriptor`).
    """
    operations: list[tuple[InternalApiSpec, InternalApiOperation]] = []

    # -- tags.assign ---------------------------------------------------------

    @internal_api_tool(
        capability_id=TAGS_ASSIGN_CAPABILITY,
        version="0.1.0",
        effect_class="mutate",
        resource_scope=["history_tags"],
        environments=["controller"],
        payload_model=TagsAssignPayload,
        description=(
            "Attach one existing history tag to the current target: "
            "the running job itself, or the conversation this run is "
            "actively bound to on this server. The tag is referenced "
            "by canonical UUID; another server's targets and released "
            "conversation links are invisible."
        ),
    )
    async def _tags_assign(
        call: CapabilityCall, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        if session_factory is None:
            raise InternalApiError("unavailable", "tag store is not configured")
        target: Literal["job", "conversation"] = (
            "job" if payload["target"] == "job" else "conversation"
        )
        tag_id = uuid.UUID(str(payload["tag_id"]))
        try:
            entity_id = await _aresolve_current_target(
                session_factory, call.context, target
            )
            if target == "job":
                await aassign_job_tag(
                    session_factory,
                    entity_id,
                    tag_id,
                    server_id=call.context.server_id,
                    assigned_by=call.context.principal,
                    assignment_source="agent",
                )
            else:
                await aassign_conversation_tag(
                    session_factory,
                    entity_id,
                    tag_id,
                    server_id=call.context.server_id,
                    assigned_by=call.context.principal,
                    assignment_source="agent",
                )
        except TagStoreError as exc:
            raise _tags_refusal(exc) from exc
        return {
            "ok": True,
            "target": target,
            "entity_id": str(entity_id),
            "tag_id": str(tag_id),
        }

    operations.append(_tags_assign)

    # -- tags.remove ---------------------------------------------------------

    @internal_api_tool(
        capability_id=TAGS_REMOVE_CAPABILITY,
        version="0.1.0",
        effect_class="mutate",
        resource_scope=["history_tags"],
        environments=["controller"],
        payload_model=TagsRemovePayload,
        description=(
            "Detach one history tag from the current target: the "
            "running job itself, or the conversation this run is "
            "actively bound to on this server. Optionally names one "
            "replacement tag UUID attached in place of the removed "
            "one; another server's targets and released links are "
            "invisible."
        ),
    )
    async def _tags_remove(
        call: CapabilityCall, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        if session_factory is None:
            raise InternalApiError("unavailable", "tag store is not configured")
        target: Literal["job", "conversation"] = (
            "job" if payload["target"] == "job" else "conversation"
        )
        tag_id = uuid.UUID(str(payload["tag_id"]))
        raw_replacement = payload.get("replacement_tag_id")
        replacement_tag_id = (
            uuid.UUID(str(raw_replacement)) if raw_replacement is not None else None
        )
        try:
            entity_id = await _aresolve_current_target(
                session_factory, call.context, target
            )
            if target == "job":
                await aremove_job_tag(
                    session_factory,
                    entity_id,
                    tag_id,
                    server_id=call.context.server_id,
                    replacement_tag_id=replacement_tag_id,
                )
            else:
                await aremove_conversation_tag(
                    session_factory,
                    entity_id,
                    tag_id,
                    server_id=call.context.server_id,
                    replacement_tag_id=replacement_tag_id,
                )
        except TagStoreError as exc:
            raise _tags_refusal(exc) from exc
        return {
            "ok": True,
            "target": target,
            "entity_id": str(entity_id),
            "tag_id": str(tag_id),
            "replacement_tag_id": (
                str(replacement_tag_id) if replacement_tag_id is not None else None
            ),
        }

    operations.append(_tags_remove)

    # -- tags.list_for ---------------------------------------------------------

    @internal_api_tool(
        capability_id=TAGS_LIST_FOR_CAPABILITY,
        version="0.1.0",
        effect_class="read",
        resource_scope=["history_tags"],
        environments=["controller"],
        payload_model=TagsListForPayload,
        description=(
            "List the history tags currently attached to the current "
            "target: the running job itself, or the conversation this "
            "run is actively bound to on this server. Returns one "
            "bounded page (limit/offset) with the pre-slice total; "
            "another server's targets and released links are invisible."
        ),
    )
    async def _tags_list_for(
        call: CapabilityCall, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        if session_factory is None:
            raise InternalApiError("unavailable", "tag store is not configured")
        target: Literal["job", "conversation"] = (
            "job" if payload["target"] == "job" else "conversation"
        )
        limit = int(payload.get("limit", TAGS_LIST_DEFAULT_LIMIT))
        offset = int(payload.get("offset", 0))
        try:
            entity_id = await _aresolve_current_target(
                session_factory, call.context, target
            )
            if target == "job":
                items, total = await atags_for_job(
                    session_factory,
                    entity_id,
                    server_id=call.context.server_id,
                    limit=limit,
                    offset=offset,
                )
            else:
                items, total = await atags_for_conversation(
                    session_factory,
                    entity_id,
                    server_id=call.context.server_id,
                    limit=limit,
                    offset=offset,
                )
        except TagStoreError as exc:
            raise _tags_refusal(exc) from exc
        has_more = offset + len(items) < total
        return {
            "ok": True,
            "target": target,
            "entity_id": str(entity_id),
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
    "TAGS_LIST_FOR_CAPABILITY",
    "TAGS_REMOVE_CAPABILITY",
    "TagsAssignPayload",
    "TagsListForPayload",
    "TagsRemovePayload",
    "build_history_tag_capabilities",
]
