"""History-tag management HTTP surface (history-tags HT-W6B).

The user-facing half of the 0011 mandatory-tag schema over the ONE
storage owner (:mod:`mai.tag_store`): a global server-agnostic catalog
plus same-server job/conversation associations. This surface is a thin
typed seam — every write and read composes the accepted store functions,
whose reserved-tag and final-tag semantics are preserved verbatim; no
agent capability is duplicated here and no route re-queries around the
store for domain truth.

- ``GET  /api/v1/tags`` — one bounded catalog page
  (``limit|offset|total|has_more``; AGENTS.md mandatory-pagination
  invariant) with the store's case-insensitive ``name_filter``.
- ``POST /api/v1/tags`` — idempotent canonical create: the name is
  normalized (whitespace collapsed, trimmed, 64-char bound) and an
  existing canonical match is returned untouched (the stored
  description is never overwritten).
- ``DELETE /api/v1/tags/{tag_id}`` — the store's used/unused policy:
  unused deletes directly; a used tag refuses without ``force=True``;
  a forced delete proceeds only when every affected entity keeps
  another tag or receives ``replacement_tag_id`` in the same
  transaction. The reserved ``unclassified`` row can never be deleted.
- ``GET/POST/DELETE /api/v1/jobs/runs/{run_id}/tags`` — the run's tag
  page and idempotent user assignments/removals inside an explicit
  ``server_id`` scope. These routes serve durable job runs, which
  INCLUDE the legacy chat history runs (legacy conversations are job
  runs), so legacy chat history tagging happens here too. User-originated
  assignments carry ``assignment_source='user'`` and a bounded
  non-secret actor derived from the validated session (the session
  token's SHA-256 — the C01 principal convention); user input is never
  accepted as the actor.
- ``GET/POST/DELETE /api/v1/chat/conversations/{conversation_id}/tags``
  — the canonical conversation mirror with the same explicit-scope
  discipline. ONLY these conversation routes accept the zero UUID
  server scope (``00000000-...-0000`` — the General Consult sentinel
  from the chat surface); job routes never treat it as a valid scope.

Scope honesty (C8): a missing entity and another server's entity are
the SAME closed 404 — for conversations the exact scope-blind
``conversation_not_found`` envelope of the read surface, for runs the
exact ``not_found`` envelope of the run surface; existence never leaks
across scopes and there is no implicit inheritance between the job and
conversation association domains.

NOT in this slice: the legacy chat list (``GET /api/v1/chat/runs``) is
NOT filtered by tags — its schema and consumers are owned by the chat
surface; tag-based filtering there arrives with that surface's own
slice. This module's docstring is the explicit record of that boundary.

Boundary discipline (C01/C08): safe reads take the session layer only;
every mutation runs session → origin → CSRF before any effect; every
collection view is a bounded page; every rejection uses the C01
structured envelope with stable, value-free codes.
"""

from __future__ import annotations

import uuid
from datetime import timezone
from typing import Annotated, Final, Mapping

from fastapi import APIRouter, FastAPI, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.status import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_204_NO_CONTENT,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_422_UNPROCESSABLE_CONTENT,
)

from mai.api.auth import (
    ErrorEnvelope,
    _require_allowed_origin,
    _require_csrf,
    _require_session,
)
from mai.api.conversations import _conversation_not_found
from mai.api.jobs import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    _error,
    _not_found_run,
    _sessions,
    _validation_error,
)
from mai.auth.sessions import hash_token
from mai.contracts.conversations import compute_has_more
from mai.tag_store import (
    EntityTagRow,
    TagErrorCode,
    TagRow,
    TagStoreError,
    aassign_conversation_tag,
    aassign_job_tag,
    acreate_tag,
    adelete_tag,
    alist_tags,
    aremove_conversation_tag,
    aremove_job_tag,
    atags_for_conversation,
    atags_for_job,
)

__all__ = ["register_history_tags"]

#: ``assignment_source`` for user-originated HTTP assignments (the frozen
#: store vocabulary ``agent | user | system``; the store validates).
_USER_ASSIGNMENT_SOURCE: Final[str] = "user"

# ---------------------------------------------------------------------------
# Wire shapes (transport only; truth is the tag store)
# ---------------------------------------------------------------------------


class TagCreateBody(BaseModel):
    """Create body (strict: unknown fields are rejected, value-free 422).

    Bounds mirror the store's canonical limits (name ≤ 64, description
    ≤ 500); normalization truth is the store — the stored name is the
    canonical form of the submitted one.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=64,
        description="Tag display name; stored whitespace-normalized "
        "and canonically unique case-insensitively",
    )
    description: str | None = Field(
        default=None,
        max_length=500,
        description="Optional free-form description of the tag's meaning",
    )


class TagView(BaseModel):
    """One catalog row: the normalized display name IS the stored name."""

    model_config = ConfigDict(extra="forbid")

    tag_id: uuid.UUID = Field(description="Opaque stable tag identity")
    name: str = Field(description="Canonical stored display name")
    description: str | None = Field(
        description="Optional free-form description, when set",
    )
    created_at: str = Field(description="RFC3339 UTC creation timestamp")
    updated_at: str = Field(description="RFC3339 UTC last-update timestamp")


class TagPage(BaseModel):
    """One bounded catalog page (AGENTS.md §1 pagination invariant)."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(
        ge=1,
        le=MAX_PAGE_LIMIT,
        description="Page size used for this slice (1..200)",
    )
    offset: int = Field(ge=0, description="Slice start within the collection")
    total: int = Field(
        ge=0,
        description="Pre-slice matching count under the identical filter",
    )
    has_more: bool = Field(
        description="True when another page exists after this slice",
    )
    items: list[TagView] = Field(
        default_factory=list,
        description="Catalog rows in deterministic lower(name) order",
    )

    @model_validator(mode="after")
    def _page_is_consistent(self) -> "TagPage":
        if len(self.items) > self.limit:
            raise ValueError("page carries more items than its limit")
        if self.has_more != (self.offset + len(self.items) < self.total):
            raise ValueError("has_more must equal offset + len(items) < total")
        return self


class TagAssignBody(BaseModel):
    """Assign body (strict): the tag plus the explicit server scope."""

    model_config = ConfigDict(extra="forbid")

    tag_id: uuid.UUID = Field(description="Catalog tag to assign")
    server_id: uuid.UUID = Field(
        description="Explicit server scope (C01 rule 2); the entity is "
        "resolved inside exactly this scope",
    )


class EntityTagView(BaseModel):
    """One association row as seen from its owning entity."""

    model_config = ConfigDict(extra="forbid")

    tag_id: uuid.UUID = Field(description="Assigned catalog tag identity")
    name: str = Field(description="Canonical tag display name")
    assigned_by: str = Field(
        description="Bounded actor reference (empty for system writers)",
    )
    assignment_source: str = Field(
        description="Writer vocabulary: agent, user or system",
    )
    created_at: str = Field(description="RFC3339 UTC assignment timestamp")


class JobTagsPage(BaseModel):
    """One bounded tag page for one job run (same-server scope)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(description="The owning job run identity")
    limit: int = Field(
        ge=1,
        le=MAX_PAGE_LIMIT,
        description="Page size used for this slice (1..200)",
    )
    offset: int = Field(ge=0, description="Slice start within the collection")
    total: int = Field(ge=0, description="Pre-slice count for this run")
    has_more: bool = Field(
        description="True when another page exists after this slice",
    )
    items: list[EntityTagView] = Field(
        default_factory=list,
        description="Association rows in deterministic name order",
    )

    @model_validator(mode="after")
    def _page_is_consistent(self) -> "JobTagsPage":
        if len(self.items) > self.limit:
            raise ValueError("page carries more items than its limit")
        if self.has_more != (self.offset + len(self.items) < self.total):
            raise ValueError("has_more must equal offset + len(items) < total")
        return self


class ConversationTagsPage(BaseModel):
    """One bounded tag page for one canonical conversation (same-server)."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID = Field(description="Owning conversation identity")
    limit: int = Field(
        ge=1,
        le=MAX_PAGE_LIMIT,
        description="Page size used for this slice (1..200)",
    )
    offset: int = Field(ge=0, description="Slice start within the collection")
    total: int = Field(ge=0, description="Pre-slice count for this conversation")
    has_more: bool = Field(
        description="True when another page exists after this slice",
    )
    items: list[EntityTagView] = Field(
        default_factory=list,
        description="Association rows in deterministic name order",
    )

    @model_validator(mode="after")
    def _page_is_consistent(self) -> "ConversationTagsPage":
        if len(self.items) > self.limit:
            raise ValueError("page carries more items than its limit")
        if self.has_more != (self.offset + len(self.items) < self.total):
            raise ValueError("has_more must equal offset + len(items) < total")
        return self


# ---------------------------------------------------------------------------
# Rejections (classified store codes → the C01 envelope; scope-blind 404s)
# ---------------------------------------------------------------------------

_TAG_ERROR_MAP: Final[Mapping[TagErrorCode, tuple[int, str]]] = {
    TagErrorCode.TAG_NOT_FOUND: (HTTP_404_NOT_FOUND, "tag_not_found"),
    TagErrorCode.TAG_IN_USE: (HTTP_409_CONFLICT, "tag_in_use"),
    TagErrorCode.RESERVED_TAG: (HTTP_409_CONFLICT, "reserved_tag"),
    TagErrorCode.LAST_TAG: (HTTP_409_CONFLICT, "last_tag"),
    TagErrorCode.INVARIANT_VIOLATION: (HTTP_409_CONFLICT, "conflict"),
}

#: Wire timestamp timezone (every stored value renders in UTC ``…Z``).
_UTC: Final = timezone.utc


def _store_error_response(
    request: Request, exc: TagStoreError, *, entity_scope: str
) -> Response:
    """Map one classified store rejection onto the C01 envelope.

    ``entity_scope`` selects the scope-blind 404 for
    ``entity_not_found``: conversation routes reuse the read surface's
    exact ``conversation_not_found`` envelope, job routes the run
    surface's exact ``not_found`` envelope — missing and cross-server
    stay indistinguishable on both.
    """
    if exc.code is TagErrorCode.ENTITY_NOT_FOUND:
        if entity_scope == "conversation":
            return _conversation_not_found(request)
        return _not_found_run(request)
    if exc.code is TagErrorCode.VALIDATION:
        return _validation_error(request, exc.field or "request", str(exc))
    status, code = _TAG_ERROR_MAP[exc.code]
    return _error(request, status, code, str(exc))


def _tag_view(row: TagRow) -> TagView:
    """Project one store row onto the wire view (UTC ``…Z`` timestamps)."""
    return TagView(
        tag_id=row.tag_id,
        name=row.name,
        description=row.description,
        created_at=row.created_at.astimezone(_UTC).isoformat().replace("+00:00", "Z"),
        updated_at=row.updated_at.astimezone(_UTC).isoformat().replace("+00:00", "Z"),
    )


def _entity_tag_view(row: EntityTagRow) -> EntityTagView:
    """Project one association row onto the wire view."""
    return EntityTagView(
        tag_id=row.tag_id,
        name=row.name,
        assigned_by=row.assigned_by,
        assignment_source=row.assignment_source,
        created_at=row.created_at.astimezone(_UTC).isoformat().replace("+00:00", "Z"),
    )


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1", tags=["history-tags"])

_SERVER_ID_QUERY = Query(
    ...,
    description="Explicit server scope (C01 rule 2); other servers' "
    "entities are invisible, not forbidden",
)


# --- global catalog -----------------------------------------------------------


@router.get(
    "/tags",
    status_code=HTTP_200_OK,
    response_model=TagPage,
    summary="One bounded page of the global tag catalog",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Query parameters failed validation",
        },
    },
)
async def list_tags(
    request: Request,
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
    name_filter: str | None = Query(
        default=None,
        max_length=200,
        description="Case-insensitive substring filter over tag names",
    ),
) -> TagPage:
    """One bounded catalog page (safe read: session layer only).

    Ordering and the same-filter total are the store's deterministic
    contract (``ORDER BY lower(name), tag_id``; ``COUNT(*)`` under the
    identical predicate).
    """
    await _require_session(request)
    factory = _sessions(request)
    rows, total = await alist_tags(
        factory,
        name_filter=name_filter,
        limit=limit,
        offset=offset,
    )
    return TagPage(
        limit=limit,
        offset=offset,
        total=total,
        has_more=compute_has_more(offset, len(rows), total),
        items=[_tag_view(row) for row in rows],
    )


@router.post(
    "/tags",
    status_code=HTTP_201_CREATED,
    response_model=TagView,
    summary="Create one catalog tag (idempotent on the canonical name)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowed or CSRF token rejected",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation",
        },
    },
)
async def create_tag(request: Request, payload: TagCreateBody) -> TagView | Response:
    """Create (or canonically return) one catalog tag.

    Mutation: session → origin → CSRF (ADR §5 order) before any effect.
    The store normalizes the name and returns an existing canonical
    match untouched — a re-create is idempotent and never overwrites the
    stored description; the response is the canonical row either way.
    """
    _store, _token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    factory = _sessions(request)
    try:
        row = await acreate_tag(
            factory, name=payload.name, description=payload.description
        )
    except TagStoreError as exc:
        return _store_error_response(request, exc, entity_scope="catalog")
    return _tag_view(row)


@router.delete(
    "/tags/{tag_id}",
    status_code=HTTP_204_NO_CONTENT,
    summary="Delete one catalog tag under the used/unused policy",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowed or CSRF token rejected",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No such tag",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Tag in use without force, reserved, or an "
            "entity would lose its final tag",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Query parameters failed validation",
        },
    },
)
async def delete_tag(
    request: Request,
    tag_id: uuid.UUID,
    force: bool = Query(
        default=False,
        description="Required to delete a tag that still has associations",
    ),
    replacement_tag_id: uuid.UUID | None = Query(
        default=None,
        description="Re-classification target for entities whose ONLY tag "
        "is the deleted one (same-transaction swap)",
    ),
) -> Response:
    """Delete one catalog tag; the store owns the used/unused policy.

    Mutation: session → origin → CSRF (ADR §5 order) before any effect.
    Unused tags delete directly; used tags refuse without ``force``; a
    forced delete proceeds only when every affected entity keeps another
    tag or receives ``replacement_tag_id`` inside the store's single
    transaction. The reserved ``unclassified`` tag can never be deleted.
    """
    _store, _token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    factory = _sessions(request)
    try:
        await adelete_tag(
            factory,
            tag_id,
            force=force,
            replacement_tag_id=replacement_tag_id,
        )
    except TagStoreError as exc:
        return _store_error_response(request, exc, entity_scope="catalog")
    return Response(status_code=HTTP_204_NO_CONTENT)


# --- job run associations (durable runs; legacy chat history included) --------


@router.get(
    "/jobs/runs/{run_id}/tags",
    status_code=HTTP_200_OK,
    response_model=JobTagsPage,
    summary="One bounded tag page for one job run (explicit server scope)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No run in the requesting server scope",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Query parameters failed validation",
        },
    },
)
async def list_job_tags(
    request: Request,
    run_id: uuid.UUID,
    server_id: Annotated[uuid.UUID, _SERVER_ID_QUERY],
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> JobTagsPage | Response:
    """The run's bounded tag page (safe read: session layer only).

    The store resolves the run inside the requesting ``server_id`` scope;
    a missing and a cross-server run are the SAME closed 404 envelope as
    the run surface's, so existence never leaks across scopes.
    """
    await _require_session(request)
    factory = _sessions(request)
    try:
        items, total = await atags_for_job(
            factory,
            run_id,
            server_id=server_id,
            limit=limit,
            offset=offset,
        )
    except TagStoreError as exc:
        return _store_error_response(request, exc, entity_scope="job")
    return JobTagsPage(
        run_id=str(run_id),
        limit=limit,
        offset=offset,
        total=total,
        has_more=compute_has_more(offset, len(items), total),
        items=[_entity_tag_view(row) for row in items],
    )


@router.post(
    "/jobs/runs/{run_id}/tags",
    status_code=HTTP_204_NO_CONTENT,
    summary="Assign one tag to a job run (idempotent, user-sourced)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowed or CSRF token rejected",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No run or no tag in the requesting scope",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation",
        },
    },
)
async def assign_job_tag(
    request: Request,
    run_id: uuid.UUID,
    payload: TagAssignBody,
) -> Response:
    """Assign one catalog tag to one run (re-assignment is a no-op).

    Mutation: session → origin → CSRF (ADR §5 order) before any effect.
    The assignment is user-originated: ``assignment_source='user'`` and
    the bounded non-secret actor is the validated session's token hash —
    never client-supplied input.
    """
    _store, token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    factory = _sessions(request)
    try:
        await aassign_job_tag(
            factory,
            run_id,
            payload.tag_id,
            server_id=payload.server_id,
            assigned_by=hash_token(token),
            assignment_source=_USER_ASSIGNMENT_SOURCE,
        )
    except TagStoreError as exc:
        return _store_error_response(request, exc, entity_scope="job")
    return Response(status_code=HTTP_204_NO_CONTENT)


@router.delete(
    "/jobs/runs/{run_id}/tags/{tag_id}",
    status_code=HTTP_204_NO_CONTENT,
    summary="Remove one job-run tag (idempotent, final-tag protected)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowed or CSRF token rejected",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No run or no tag in the requesting scope",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "The run's final tag cannot be removed without "
            "a replacement",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Query parameters failed validation",
        },
    },
)
async def remove_job_tag(
    request: Request,
    run_id: uuid.UUID,
    tag_id: uuid.UUID,
    server_id: Annotated[uuid.UUID, _SERVER_ID_QUERY],
    replacement_tag_id: uuid.UUID | None = Query(
        default=None,
        description="Re-classification target committed in the same "
        "transaction when this is the run's final tag",
    ),
) -> Response:
    """Remove one run-tag association (removing an absent one is a no-op).

    Mutation: session → origin → CSRF (ADR §5 order) before any effect.
    Removing the run's final ordinary tag requires ``replacement_tag_id``:
    removal and replacement commit together, so the mandatory-tag
    invariant never drops below one tag.
    """
    _store, _token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    factory = _sessions(request)
    try:
        await aremove_job_tag(
            factory,
            run_id,
            tag_id,
            server_id=server_id,
            replacement_tag_id=replacement_tag_id,
        )
    except TagStoreError as exc:
        return _store_error_response(request, exc, entity_scope="job")
    return Response(status_code=HTTP_204_NO_CONTENT)


# --- canonical conversation associations --------------------------------------


@router.get(
    "/chat/conversations/{conversation_id}/tags",
    status_code=HTTP_200_OK,
    response_model=ConversationTagsPage,
    summary="One bounded tag page for one canonical conversation",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No conversation in the requesting server scope",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Query parameters failed validation",
        },
    },
)
async def list_conversation_tags(
    request: Request,
    conversation_id: uuid.UUID,
    server_id: Annotated[uuid.UUID, _SERVER_ID_QUERY],
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> ConversationTagsPage | Response:
    """The conversation's bounded tag page (safe read; session only).

    The store resolves the conversation inside the requesting
    ``server_id`` scope; a missing and a cross-server conversation are
    the SAME scope-blind 404 envelope as the conversation read surface.
    The zero UUID server scope (General Consult) is valid HERE ONLY.
    """
    await _require_session(request)
    factory = _sessions(request)
    try:
        items, total = await atags_for_conversation(
            factory,
            conversation_id,
            server_id=server_id,
            limit=limit,
            offset=offset,
        )
    except TagStoreError as exc:
        return _store_error_response(request, exc, entity_scope="conversation")
    return ConversationTagsPage(
        conversation_id=conversation_id,
        limit=limit,
        offset=offset,
        total=total,
        has_more=compute_has_more(offset, len(items), total),
        items=[_entity_tag_view(row) for row in items],
    )


@router.post(
    "/chat/conversations/{conversation_id}/tags",
    status_code=HTTP_204_NO_CONTENT,
    summary="Assign one tag to a conversation (idempotent, user-sourced)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowed or CSRF token rejected",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No conversation or no tag in the requesting scope",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation",
        },
    },
)
async def assign_conversation_tag(
    request: Request,
    conversation_id: uuid.UUID,
    payload: TagAssignBody,
) -> Response:
    """Assign one catalog tag to one conversation (re-assignment: no-op).

    Mutation: session → origin → CSRF (ADR §5 order) before any effect.
    The assignment is user-originated: ``assignment_source='user'`` and
    the bounded non-secret actor is the validated session's token hash —
    never client-supplied input. The zero UUID server scope (General
    Consult) is valid HERE ONLY.
    """
    _store, token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    factory = _sessions(request)
    try:
        await aassign_conversation_tag(
            factory,
            conversation_id,
            payload.tag_id,
            server_id=payload.server_id,
            assigned_by=hash_token(token),
            assignment_source=_USER_ASSIGNMENT_SOURCE,
        )
    except TagStoreError as exc:
        return _store_error_response(request, exc, entity_scope="conversation")
    return Response(status_code=HTTP_204_NO_CONTENT)


@router.delete(
    "/chat/conversations/{conversation_id}/tags/{tag_id}",
    status_code=HTTP_204_NO_CONTENT,
    summary="Remove one conversation tag (idempotent, final-tag protected)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowed or CSRF token rejected",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No conversation or no tag in the requesting scope",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "The conversation's final tag cannot be removed "
            "without a replacement",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Query parameters failed validation",
        },
    },
)
async def remove_conversation_tag(
    request: Request,
    conversation_id: uuid.UUID,
    tag_id: uuid.UUID,
    server_id: Annotated[uuid.UUID, _SERVER_ID_QUERY],
    replacement_tag_id: uuid.UUID | None = Query(
        default=None,
        description="Re-classification target committed in the same "
        "transaction when this is the conversation's final tag",
    ),
) -> Response:
    """Remove one conversation-tag association (absent removal: no-op).

    Mutation: session → origin → CSRF (ADR §5 order) before any effect.
    Removing the conversation's final ordinary tag requires
    ``replacement_tag_id``: removal and replacement commit together, so
    the mandatory-tag invariant never drops below one tag. The zero UUID
    server scope (General Consult) is valid HERE ONLY.
    """
    _store, _token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    factory = _sessions(request)
    try:
        await aremove_conversation_tag(
            factory,
            conversation_id,
            tag_id,
            server_id=server_id,
            replacement_tag_id=replacement_tag_id,
        )
    except TagStoreError as exc:
        return _store_error_response(request, exc, entity_scope="conversation")
    return Response(status_code=HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Registration (api-wiring exclusive resource)
# ---------------------------------------------------------------------------


def register_history_tags(app: FastAPI) -> None:
    """Register the history-tag management surface (C01/C08)."""
    app.include_router(router)
