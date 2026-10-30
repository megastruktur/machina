"""Canonical conversation history HTTP surface (08-history-api).

The read half of the ratified conversation contract (act_CONTRACTS §3–§4)
over the app-owned ``0008`` store (:mod:`mai.agents.conversation_store` —
the ONE storage owner; this surface composes it, never re-queries around
it for domain truth):

- ``GET /api/v1/chat/conversations`` — one bounded, explicitly
  server-scoped page of safe conversation metadata (``limit|offset|total|
  has_more`` per the AGENTS.md mandatory-pagination invariant; ``COUNT(*)``
  under the identical ``server_id`` predicate, deterministic ordering).
- ``GET /api/v1/chat/conversations/{conversation_id}`` — the safe
  singleton header plus honest legacy-gap metadata (§4): a conversation
  whose recovered history is ``partial``/``unavailable`` labels exactly
  what is missing (missing transcript range, proven-but-unrecoverable
  submission count) instead of inventing turns.
- ``GET /api/v1/chat/conversations/{conversation_id}/messages`` — the
  role-preserving sanitized transcript page ordered ``(seq, message_id)``
  with the same-filter ``total`` (C2: a fresh session restores the true
  server transcript; no browser cache and no placeholder ever needed).
- ``GET /api/v1/chat/conversations/{conversation_id}/events`` — bounded
  resumable metadata events with the conversation-local monotonic
  message sequence as the dedup cursor: frames are strictly
  cursor-exclusive (``after_seq``), ordered by ``seq``, so a reconnect
  with the last seen sequence deduplicates by construction (C2). Events
  carry content IDENTITIES only (never transcript text, C05), and the
  page envelope carries the current revision/lease/steering state so a
  reconnecting consumer can catch up honestly.

Boundary discipline (identical to the chat surface, C01/C08): the
principal comes from the validated session only (401 before any body);
every route takes its explicit ``server_id`` argument (C01 rule 2);
cross-server access fails CLOSED with the scope-blind
``conversation_not_found`` 404 — another scope's existence never leaks
through the error channel (C8). Legacy gap metadata is computed ONLY
over app-owned authoritative records (``job_events``/``job_runs``);
no route ever reads DBOS private tables (§4).
"""

from __future__ import annotations

import uuid
from datetime import timezone
from typing import Annotated, Final, Literal

from fastapi import APIRouter, FastAPI, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.status import (
    HTTP_200_OK,
    HTTP_401_UNAUTHORIZED,
    HTTP_404_NOT_FOUND,
    HTTP_422_UNPROCESSABLE_CONTENT,
)

from mai.agents.conversation_store import (
    ConversationRow,
    aget_conversation,
    alist_conversations,
    alist_messages,
)
from mai.api.auth import ErrorEnvelope, _require_session
from mai.api.jobs import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    _conflict,
    _error,
    _jobs_services,
    _sessions,
)
from mai.contracts.conversations import (
    ConversationContractError,
    ConversationDetail,
    ConversationErrorCode,
    ConversationSummary,
    ConversationTranscriptPage,
    EntityTagPreviewView,
    HistoryCompleteness,
    compute_has_more,
)
from mai.tag_store import EntityTagPreview, apreview_conversation_tags

__all__ = ["register_conversations"]

def _preview_view(found: EntityTagPreview | None) -> EntityTagPreviewView:
    """Project the store's preview dataclass onto the wire view.

    Every in-scope conversation carries at least one tag (the mandatory
    unclassified seed), so a missing preview after a successful scoped
    read is a bug — it fails loudly instead of fabricating an empty
    projection (``total=0`` is contract-invalid).
    """
    if found is None:
        raise RuntimeError(
            "mandatory tag preview missing after a successful scoped read"
        )
    return EntityTagPreviewView(preview=list(found.preview), total=found.total)

#: Upper bound on the proven-but-unrecoverable submission count (the same
#: bounded metadata cap ``mai.agents.conversation_context`` uses: the
#: count is gap METADATA, never content).
LEGACY_PROVEN_SUBMISSIONS_CAP: Final[int] = 10_000

#: The one event kind this surface owns (open slug grammar, frozen
#: envelope shape): one canonical transcript append, keyed by the
#: conversation-local monotonic message sequence. Content NEVER rides an
#: event frame (C05: generic streams do not broadcast raw prompts) —
#: consumers fetch pages from the messages route.
TRANSCRIPT_APPENDED_EVENT_KIND: Final[str] = "transcript.appended"


# ---------------------------------------------------------------------------
# Wire shapes (transport only; truth is the store + frozen contracts)
# ---------------------------------------------------------------------------


class LegacyHistoryGap(BaseModel):
    """One honestly-labelled legacy gap (§4; scenario C7).

    Gap metadata is derived ONLY from proved app-owned records; a gap is
    shown, never papered over with a fabricated turn or a placeholder.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["missing_range", "unrecoverable_submissions"] = Field(
        description="Which gap this row labels: a missing transcript "
        "sequence range, or proven submissions with no recoverable row",
    )
    from_sequence: int | None = Field(
        default=None,
        ge=1,
        description="First missing transcript sequence (missing_range only)",
    )
    to_sequence: int | None = Field(
        default=None,
        ge=1,
        description="Last missing transcript sequence, inclusive (missing_range only)",
    )
    count: int | None = Field(
        default=None,
        ge=0,
        description="Bounded count of the labelled records "
        "(unrecoverable_submissions only)",
    )
    description: str = Field(
        min_length=1,
        max_length=200,
        description="Value-free human-readable gap explanation",
    )

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> "LegacyHistoryGap":
        if self.kind == "missing_range":
            if self.from_sequence is None or self.to_sequence is None:
                raise ValueError("missing_range gap requires the sequence range")
            if self.to_sequence < self.from_sequence:
                raise ValueError(
                    "missing range must satisfy to_sequence >= from_sequence"
                )
        elif self.count is None:
            raise ValueError("unrecoverable_submissions gap requires a count")
        return self


class ConversationDetailView(ConversationDetail):
    """Safe singleton header plus explicit legacy-gap metadata (§3/§4).

    ``legacy_gaps`` is empty exactly when ``history_completeness`` is
    ``complete``; otherwise it names every gap the store proved.
    """

    model_config = ConfigDict(extra="forbid")

    legacy_gaps: list[LegacyHistoryGap] = Field(
        default_factory=list,
        description="Honest legacy-gap metadata (§4); empty when complete",
    )


class ConversationListPage(BaseModel):
    """One bounded, explicitly server-scoped list page (§3 ``GET`` list).

    Safe metadata only: rows never carry transcript content, so the page
    stays safe for generic list surfaces (C01 rule 5; AGENTS.md §1).
    """

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
    items: list[ConversationSummary] = Field(
        default_factory=list,
        description="Conversation rows in deterministic updated_at order",
    )

    @model_validator(mode="after")
    def _page_is_consistent(self) -> "ConversationListPage":
        if len(self.items) > self.limit:
            raise ValueError("page carries more items than its limit")
        if self.has_more != (self.offset + len(self.items) < self.total):
            raise ValueError("has_more must equal offset + len(items) < total")
        return self


class ConversationEvent(BaseModel):
    """One metadata-only transcript event (§3 ``GET`` events).

    The ``seq`` IS the dedup cursor: conversation-local, monotonic,
    gap-free (allocated under the conversation's write lock), so a
    reconnect with the last seen sequence replays strictly newer events
    without gaps or duplicates (C2). No content field exists to leak.
    """

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(
        ge=1,
        description="Conversation-local monotonic sequence (the dedup cursor)",
    )
    kind: Literal["transcript.appended"] = Field(
        description="The event kind (one canonical transcript append)",
    )
    message_id: uuid.UUID = Field(description="Opaque stable message identity")
    role: str = Field(
        min_length=1,
        max_length=16,
        description="Trusted producer-assigned role (vocabulary mirror)",
    )
    source_kind: str = Field(
        min_length=1,
        max_length=16,
        description="Origin of the message (vocabulary mirror)",
    )
    redacted: bool = Field(
        description="True when content was deliberately redacted",
    )
    has_content_ref: bool = Field(
        description="True when content lives behind the safe content_ref",
    )
    created_at: str = Field(description="RFC3339 UTC event timestamp")


class ConversationCurrentView(BaseModel):
    """Current-state catch-up block on every events page (§3).

    Bounded scalar state (identities + counts only), so a reconnecting
    consumer renders the lease/steering truth without fabricating a
    timeline entry for state that has no monotonic cursor.
    """

    model_config = ConfigDict(extra="forbid")

    latest_seq: int = Field(
        ge=0,
        description="Newest allocated transcript sequence (0 when empty)",
    )
    history_completeness: HistoryCompleteness = Field(
        description="Honest history marker for this conversation (§4)",
    )
    active_run_id: uuid.UUID | None = Field(
        description="Current lease-holding durable run, when one exists",
    )
    active_task_id: uuid.UUID | None = Field(
        description="Current active task episode, when one exists (§1)",
    )
    pending_steering: int = Field(
        ge=0,
        description="Bounded count of not-yet-applied durable control rows",
    )


class ConversationEventsPage(BaseModel):
    """One bounded, cursor-exclusive events page (§3 ``GET`` events).

    The cursor filters the set (``seq > after_seq``); ``total`` is the
    pre-slice count under that identical predicate and the slice serves
    the filtered set from its start, so ``has_more`` follows the
    canonical derivation. The page is deterministic and replayable: the
    same cursor always returns the same slice (C2 reconnect dedup).
    """

    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID = Field(description="Owning conversation identity")
    limit: int = Field(
        ge=1,
        le=MAX_PAGE_LIMIT,
        description="Page size used for this slice (1..200)",
    )
    offset: int = Field(
        ge=0,
        description="Items skipped within the cursor-filtered set; the "
        "cursor (after_seq) is the resume position, so the filtered set "
        "is served from its start",
    )
    total: int = Field(
        ge=0,
        description="Pre-slice matching count under the identical filter",
    )
    has_more: bool = Field(
        description="True when another page exists after this slice",
    )
    after_seq: int = Field(
        ge=0,
        description="The replay cursor: only strictly newer events are returned",
    )
    events: list[ConversationEvent] = Field(
        default_factory=list,
        description="Metadata events in strictly ascending seq order",
    )
    current: ConversationCurrentView = Field(
        description="Current-state catch-up block (identities and counts)",
    )

    @model_validator(mode="after")
    def _page_is_consistent(self) -> "ConversationEventsPage":
        if len(self.events) > self.limit:
            raise ValueError("page carries more events than its limit")
        seqs = [event.seq for event in self.events]
        if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
            raise ValueError("events page must be strictly seq-ordered")
        if any(seq <= self.after_seq for seq in seqs):
            raise ValueError("events page must be strictly cursor-exclusive")
        if self.has_more != (self.offset + len(self.events) < self.total):
            raise ValueError("has_more must equal offset + len(events) < total")
        return self


# ---------------------------------------------------------------------------
# Rejections (the scope-blind C01 envelope; mirrors the chat surface)
# ---------------------------------------------------------------------------


def _conversation_not_found(request: Request) -> Response:
    """The scope-blind 404 (C8): existence of another server's conversation
    never leaks through the error channel."""
    return _error(
        request,
        HTTP_404_NOT_FOUND,
        ConversationErrorCode.CONVERSATION_NOT_FOUND.value,
        "no conversation in the requesting server scope",
    )


def _conversation_read_error(
    request: Request, exc: ConversationContractError
) -> Response:
    """Map the store's closed read rejections onto the C01 envelope.

    Reads reject with exactly one closed code — ``conversation_not_found``
    (the store's single scope-mismatch/absent error); every other stored
    code is a write-path vocabulary, surfaced value-free as a 409.
    """
    codes = set(exc.fields.values())
    if ConversationErrorCode.CONVERSATION_NOT_FOUND.value in codes:
        return _conversation_not_found(request)
    return _conflict(request, "conversation contract rejected")


async def _achecked_read(
    request: Request,
    conversation_id: uuid.UUID,
    server_id: uuid.UUID,
) -> ConversationRow | Response:
    """Scope-checked singleton read for one GET route (C8 fails closed)."""
    factory = _sessions(request)
    try:
        return await aget_conversation(factory, conversation_id, server_id=server_id)
    except ConversationContractError as exc:
        return _conversation_read_error(request, exc)


# ---------------------------------------------------------------------------
# legacy gap metadata (§4; proved app records only, never DBOS tables)
# ---------------------------------------------------------------------------

_UNRECOVERABLE_SQL: Final[str] = """
    SELECT COUNT(*) FROM job_events je
    WHERE je.type = 'chat.submitted'
      AND je.data->>'conversation_id' = CAST(:cid AS text)
      AND (
            je.data->>'message_id' IS NULL
            OR NOT EXISTS (
                SELECT 1 FROM conversation_messages m
                WHERE m.conversation_id = :cid
                  AND m.message_id::text = je.data->>'message_id'
            )
          )
"""


async def _aunrecoverable_submissions(
    session: AsyncSession, conversation_id: uuid.UUID
) -> int:
    """Count proven submissions with no recoverable transcript row (C7).

    ``chat.submitted`` job-event records prove WHICH submissions happened
    for this conversation (identities/digests, never content). A proven
    submission with no canonical row is counted as one unrecoverable gap
    unit — it is never converted into a synthetic turn. Text comparison
    (``::text``) keeps the probe total against malformed payloads.
    """
    value = await session.execute(
        text(_UNRECOVERABLE_SQL),
        {"cid": conversation_id},
    )
    return min(int(value.scalar_one()), LEGACY_PROVEN_SUBMISSIONS_CAP)


def _legacy_gaps(
    conversation: ConversationRow, unrecoverable: int
) -> list[LegacyHistoryGap]:
    """The explicit gap list for one non-complete conversation (§4)."""
    gaps: list[LegacyHistoryGap] = []
    missing = conversation.missing_range
    if missing is not None:
        gaps.append(
            LegacyHistoryGap(
                kind="missing_range",
                from_sequence=missing.from_sequence,
                to_sequence=missing.to_sequence,
                description=(
                    "Transcript sequences "
                    f"{missing.from_sequence}-{missing.to_sequence} could not "
                    "be recovered from proved app records; they are missing, "
                    "not fabricated"
                ),
            )
        )
    if unrecoverable > 0:
        gaps.append(
            LegacyHistoryGap(
                kind="unrecoverable_submissions",
                count=unrecoverable,
                description=(
                    f"{unrecoverable} proven submission(s) have no "
                    "recoverable transcript row; no turn is invented for them"
                ),
            )
        )
    return gaps


# ---------------------------------------------------------------------------
# routes (safe reads: session layer only, explicit server scope, bounded)
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/chat", tags=["chat-conversations"])

_SERVER_ID_QUERY = Query(
    ...,
    description="Explicit server scope (C01 rule 2); other servers' "
    "conversations are invisible, not forbidden",
)


@router.get(
    "/conversations",
    status_code=HTTP_200_OK,
    response_model=ConversationListPage,
    summary="One bounded server-scoped page of canonical conversations",
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
async def list_canonical_conversations(
    request: Request,
    server_id: Annotated[uuid.UUID, _SERVER_ID_QUERY],
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> ConversationListPage:
    """One bounded page of safe conversation metadata (§3 ``GET`` list).

    The page is exactly the requested server's scope; ordering and the
    same-filter total are the storage owner's deterministic contract
    (``updated_at DESC, conversation_id``). Rows are safe metadata: no
    transcript content ever rides a list page.
    """
    await _require_session(request)
    _lifecycle, _recovery = _jobs_services(request)  # fail closed when absent
    factory = _sessions(request)
    items, total = await alist_conversations(
        factory, server_id=server_id, limit=limit, offset=offset
    )
    previews = await apreview_conversation_tags(
        factory,
        [row.conversation_id for row in items],
        server_id=server_id,
        preview_limit=3,
    )
    return ConversationListPage(
        limit=limit,
        offset=offset,
        total=total,
        has_more=compute_has_more(offset, len(items), total),
        items=[
            ConversationSummary(
                conversation_id=row.conversation_id,
                server_id=row.server_id,
                title=row.title,
                created_at=row.created_at,
                updated_at=row.updated_at,
                message_count=row.message_count,
                active_task_id=row.active_task_id,
                active_run_id=row.active_run_id,
                tag_preview=_preview_view(previews.get(row.conversation_id)),
            )
            for row in items
        ],
    )


@router.get(
    "/conversations/{conversation_id}",
    status_code=HTTP_200_OK,
    response_model=ConversationDetailView,
    summary="One conversation's safe header with honest legacy-gap metadata",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No conversation in the requesting server scope",
        },
    },
)
async def read_canonical_conversation(
    request: Request,
    conversation_id: uuid.UUID,
    server_id: Annotated[uuid.UUID, _SERVER_ID_QUERY],
) -> ConversationDetailView | Response:
    """The safe singleton header (§3 ``GET`` one) with §4 gap honesty.

    A ``partial``/``unavailable`` conversation labels its gaps explicitly
    (missing range + bounded proven-but-unrecoverable submission count);
    a complete conversation carries no gap metadata and pays no extra
    query. Nothing about another scope's identity can appear in any
    rejection (C8).
    """
    await _require_session(request)
    _lifecycle, _recovery = _jobs_services(request)
    checked = await _achecked_read(request, conversation_id, server_id)
    if isinstance(checked, Response):  # scope-blind 404 envelope
        return checked
    conversation = checked
    gaps: list[LegacyHistoryGap] = []
    if conversation.history_completeness is not HistoryCompleteness.COMPLETE:
        factory = _sessions(request)
        async with factory() as session:
            unrecoverable = await _aunrecoverable_submissions(
                session, conversation.conversation_id
            )
        gaps = _legacy_gaps(conversation, unrecoverable)
    previews = await apreview_conversation_tags(
        _sessions(request),
        [conversation.conversation_id],
        server_id=server_id,
        preview_limit=3,
    )
    return ConversationDetailView(
        conversation_id=conversation.conversation_id,
        server_id=conversation.server_id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        message_count=await _amessage_count(request, conversation.conversation_id),
        active_task_id=conversation.active_task_id,
        active_run_id=conversation.active_run_id,
        tag_preview=_preview_view(
            previews.get(conversation.conversation_id)
        ),
        history_completeness=conversation.history_completeness,
        missing_range=conversation.missing_range,
        legacy_gaps=gaps,
    )


async def _amessage_count(request: Request, conversation_id: uuid.UUID) -> int:
    """The conversation's persisted message count (bounded metadata)."""
    factory = _sessions(request)
    async with factory() as session:
        value = await session.execute(
            text(
                "SELECT COUNT(*) FROM conversation_messages "
                "WHERE conversation_id = :cid"
            ),
            {"cid": conversation_id},
        )
    return int(value.scalar_one())


@router.get(
    "/conversations/{conversation_id}/messages",
    status_code=HTTP_200_OK,
    response_model=ConversationTranscriptPage,
    summary="One bounded role-preserving transcript page (deterministic order)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No conversation in the requesting server scope",
        },
    },
)
async def list_canonical_messages(
    request: Request,
    conversation_id: uuid.UUID,
    server_id: Annotated[uuid.UUID, _SERVER_ID_QUERY],
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> ConversationTranscriptPage | Response:
    """The sanitized transcript page (§3 ``GET`` messages; C2).

    Ordered ``(seq, message_id)`` with the same-filter ``total``; the
    page carries the conversation's honest history-completeness marker.
    A fresh session restores the true server transcript — no browser
    cache dependency, no placeholder rows, no fabricated turns.
    """
    await _require_session(request)
    _lifecycle, _recovery = _jobs_services(request)
    factory = _sessions(request)
    try:
        return await alist_messages(
            factory,
            conversation_id,
            server_id=server_id,
            limit=limit,
            offset=offset,
        )
    except ConversationContractError as exc:
        return _conversation_read_error(request, exc)


@router.get(
    "/conversations/{conversation_id}/events",
    status_code=HTTP_200_OK,
    response_model=ConversationEventsPage,
    summary="One bounded cursor-exclusive metadata event page (resumable)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No conversation in the requesting server scope",
        },
    },
)
async def list_canonical_events(
    request: Request,
    conversation_id: uuid.UUID,
    server_id: Annotated[uuid.UUID, _SERVER_ID_QUERY],
    after_seq: int = Query(
        default=0,
        ge=0,
        description="Replay cursor: return only strictly newer events",
    ),
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
) -> ConversationEventsPage | Response:
    """Bounded resumable metadata events (§3 ``GET`` events; C2).

    The cursor is the conversation-local monotonic message sequence.
    Frames are strictly cursor-exclusive and ascending, so a reconnect
    with the last seen ``after_seq`` deduplicates by construction —
    without gaps and without duplicates. Events carry content
    IDENTITIES only (never transcript text, C05); the envelope's
    current-state block carries the bounded lease/steering/revision
    truth for honest catch-up rendering.
    """
    await _require_session(request)
    _lifecycle, _recovery = _jobs_services(request)
    checked = await _achecked_read(request, conversation_id, server_id)
    if isinstance(checked, Response):
        return checked
    conversation = checked
    factory = _sessions(request)
    params: dict[str, object] = {
        "cid": conversation.conversation_id,
        "after": after_seq,
        "l": limit,
    }
    async with factory() as session:
        total = int(
            (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM conversation_messages "
                        "WHERE conversation_id = :cid AND seq > :after"
                    ),
                    params,
                )
            ).scalar_one()
        )
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT message_id, seq, role, source_kind, redacted, "
                        "(content_ref IS NOT NULL) AS has_content_ref, created_at "
                        "FROM conversation_messages "
                        "WHERE conversation_id = :cid AND seq > :after "
                        "ORDER BY seq, message_id LIMIT :l"
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )
        latest_seq = int(
            (
                await session.execute(
                    text(
                        "SELECT COALESCE(MAX(seq), 0) FROM conversation_messages "
                        "WHERE conversation_id = :cid"
                    ),
                    {"cid": conversation.conversation_id},
                )
            ).scalar_one()
        )
        pending_steering = int(
            (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM conversation_steering "
                        "WHERE conversation_id = :cid "
                        "AND delivery_status = 'pending'"
                    ),
                    {"cid": conversation.conversation_id},
                )
            ).scalar_one()
        )
    events = [
        ConversationEvent(
            seq=int(row["seq"]),
            kind=TRANSCRIPT_APPENDED_EVENT_KIND,
            message_id=row["message_id"],  # type: ignore[arg-type]
            role=str(row["role"]),
            source_kind=str(row["source_kind"]),
            redacted=bool(row["redacted"]),
            has_content_ref=bool(row["has_content_ref"]),
            created_at=row["created_at"]
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        )
        for row in rows
    ]
    return ConversationEventsPage(
        conversation_id=conversation.conversation_id,
        limit=limit,
        offset=0,
        total=total,
        has_more=compute_has_more(0, len(events), total),
        after_seq=after_seq,
        events=events,
        current=ConversationCurrentView(
            latest_seq=latest_seq,
            history_completeness=conversation.history_completeness,
            active_run_id=conversation.active_run_id,
            active_task_id=conversation.active_task_id,
            pending_steering=pending_steering,
        ),
    )


# ---------------------------------------------------------------------------
# Registration (api-wiring exclusive resource)
# ---------------------------------------------------------------------------


def register_conversations(app: FastAPI) -> None:
    """Register the canonical conversation history surface (C01/C08)."""
    app.include_router(router)
