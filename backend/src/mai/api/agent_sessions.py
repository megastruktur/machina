"""Session REST API + versioned cursor WebSocket channel (D06).

Surfaces over the D01–D05 session domain (the durable authorities stay
``session_store`` / ``session_runner`` — the HTTP layer persists INTENT,
the worker owns execution, and a channel command NEVER writes engine
state; the closed client vocabulary carries no such frame):

- ``GET  /api/v1/agent-sessions``            — bounded list page
  (``SessionListPage``; AGENTS.md §1 envelope: ``limit``/``offset``/
  ``total``/``has_more``; the store's deterministic ``ORDER BY`` with
  ``COUNT(*)`` under the identical predicate).
- ``POST /api/v1/agent-sessions``            — idempotent create
  (``SessionCreateRequest`` → ``SessionDetail``; the client idempotency
  key derives the session identity, so a retry converges on the same row).
- ``GET  /api/v1/agent-sessions/{id}``       — ``SessionDetail`` (scope-
  blind 404: a missing and a foreign session are the SAME failure).
- ``GET  /api/v1/agent-sessions/{id}/history`` — bounded cursor history
  page (``SessionHistoryPage``; never an unbounded transcript).
- ``POST /api/v1/agent-sessions/{id}/turns`` — ``SessionTurnSubmitRequest``
  → durable admission (run row + user entry) then the engine enqueue
  FOLLOWS the commit exactly like the reviewed chat seam (a crash leaves a
  re-armable gap; the deterministic run id makes the retry idempotent).
- ``POST /api/v1/agent-sessions/{id}/control`` — ``SessionControlRequest``
  delegated to ``session_runner.asubmit_session_control``.
- ``GET  /api/v1/agent-sessions/{id}/channel`` (WebSocket) — the versioned
  cursor channel. Admission mirrors ``api/terminal.py`` layer-for-layer
  (validated ``mai_session`` cookie → exact ``Origin`` allowlist → CSRF
  double-submit proof inside ``hello`` → session/scope check), with close
  codes ``1008 unauthenticated`` / ``1008 origin_rejected`` / ``1008
  csrf_rejected``. No query-string secrets: the URL carries only the
  session id and its non-secret scope pin.

Scope model (C01 rule 2, the reviewed ``conversations`` precedent): every
route takes its EXPLICIT ``server_id`` argument; a missing and a foreign
session collapse into one value-free 404.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

from fastapi import APIRouter, FastAPI, Query, Request, Response, WebSocket
from pydantic import BaseModel, ConfigDict, Field, model_validator
from fastapi import WebSocketDisconnect
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.status import (
    HTTP_204_NO_CONTENT,
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
)

from mai.agents.context import format_service_map_section
from mai.agents.session_runner import (
    HANDLER_NAME,
    SessionRunIntent,
    asubmit_session_control,
)
from mai.api.auth import (
    SESSION_COOKIE,
    _require_allowed_origin,
    _require_csrf,
    _require_session,
    render_error_envelope,
)
from mai.api.capability_defaults import (
    dedup_preserving_order,
    plugin_capability_ids,
    resolve_default_capabilities,
)
from mai.api.history_tags import EntityTagView
from mai.auth.sessions import hash_token
from mai.capabilities.history_tags import TAGS_CAPABILITY_IDS
from mai.capabilities.internal_api import INTERNAL_API_CAPABILITY_IDS
from mai.contracts.session_channel import (
    CHANNEL_PROTOCOL_VERSION,
    MAX_CHANNEL_SNAPSHOT_ENTRIES,
    ClientAckFrame,
    ClientHelloFrame,
    ClientPingFrame,
    ClientSubscribeFrame,
    ServerChannelErrorFrame,
    ServerEntriesFrame,
    ServerHelloOkFrame,
    ServerPongFrame,
    ServerResyncRequiredFrame,
    ServerSnapshotFrame,
    StreamIdentity,
    validate_client_frame,
)
from mai.contracts.sessions import (
    HISTORY_PAGE_MAX_ENTRIES,
    MAX_ENTRY_TEXT_CHARS,
    MAX_SERVICE_REF_NAME_CHARS,
    SESSION_LIST_DEFAULT_LIMIT,
    SESSION_LIST_MAX_LIMIT,
    EntryKind,
    ServiceRef,
    SessionContractError,
    SessionControlRequest,
    SessionControlResponse,
    SessionCreateRequest,
    SessionDetail,
    SessionErrorCode,
    SessionHistoryPage,
    SessionListPage,
    SessionRenameRequest,
    SessionRunState,
    SessionSourceKind,
    SessionStatus,
    SessionSummary,
    SessionTagAssignRequest,
    SessionTurnSubmitRequest,
    SessionTurnSubmitResponse,
    UserMessageEntry,
)
from mai.contracts.tags import EntityTagPreview, EntityTagPreviewView
from mai.policy.config import GrantConfigStore
from mai.policy.evaluate import ExecutionContext, PolicyMode
from mai.session_channel import (
    SessionChannelHub,
    SessionSink,
    notify_session_committed,
)
from mai.session_store import (
    aappend_entry,
    aattach_session_tag,
    acreate_root_run,
    acreate_session,
    aget_session,
    alist_sessions,
    apreview_session_tags,
    aread_session_history,
    arecord_admission,
    arecord_run_state,
    aremove_session_tag,
    arename_session,
    asession_tags_page,
)
from mai.tag_store import TagErrorCode, TagStoreError

__all__ = [
    "SESSIONS_API_PATH",
    "SESSION_CHANNEL_WS_PATH",
    "register_agent_sessions",
]

SESSIONS_API_PATH: Final[str] = "/api/v1/agent-sessions"
SESSION_CHANNEL_WS_PATH: Final[str] = "/api/v1/agent-sessions/{session_id}/channel"

router = APIRouter(prefix=SESSIONS_API_PATH, tags=["agent-sessions"])

#: The history route's default page bound (the D01 maximum stays the cap).
_HISTORY_PAGE_DEFAULT: Final[int] = 30

#: Bounded replay budget per subscription: pages fetched before the server
#: declares resync (a huge range arrives as bounded pages + resync, never
#: one transcript).
_REPLAY_PAGE_BUDGET: Final[int] = 20

#: General Consult: the zero server UUID has no profile binding.
_GENERAL_SERVER: Final[uuid.UUID] = uuid.UUID(int=0)

#: Capabilities the worker's grant bootstrap never auto-grants to the
#: General pseudo-server (``jobs/worker.py`` ``_read_grants``): host
#: effects, document retrieval and the HTTP service probe reach a server
#: only through its profile or an explicit grant-store entry, and the
#: pseudo-server has neither unless the grant store names it.
_GENERAL_EXCLUDED_AUTO_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        "host.shell",
        "host.file_read",
        "host.logs",
        "docs.retrieve",
        "service.http",
    }
)


def _general_consult_capabilities(request: Request) -> tuple[str, ...]:
    """The General Consult pseudo-server's admitted capability surface,
    kept in exact parity with the worker's auto-grant bootstrap
    (``jobs/worker.py`` ``_read_grants`` enrols the zero UUID):
    explicit grant-store entries naming the pseudo-server, enabled
    GLOBAL plugin bindings, the internal-API and history-tag
    vocabularies, and controller-side web research. Unlike
    :func:`resolve_default_capabilities` (the server-chat and runbook
    admission resolver), the built-in controller defaults
    (``docs.retrieve``/``service.http``/``host.logs``) and every host
    surface are withheld unless the grant store explicitly names the
    pseudo-server. Degradations mirror the worker: an unusable store
    contributes nothing — never an error, never a widening."""
    explicit: list[str] = []
    grants_store = getattr(request.app.state, "grants", None)
    if isinstance(grants_store, GrantConfigStore):
        try:
            for capability_id, servers in grants_store.read().grants.items():
                if _GENERAL_SERVER in servers:
                    explicit.append(capability_id)
        except Exception:
            pass
    resolved = list(explicit)
    resolved.extend(
        plugin_capability_ids(
            getattr(request.app.state, "plugins", None), _GENERAL_SERVER
        )
    )
    resolved.extend(INTERNAL_API_CAPABILITY_IDS)
    resolved.extend(TAGS_CAPABILITY_IDS)
    resolved.extend(("web.search", "web.read"))
    return tuple(
        capability_id
        for capability_id in dedup_preserving_order(resolved)
        if capability_id not in _GENERAL_EXCLUDED_AUTO_CAPABILITIES
        or capability_id in explicit
    )


# --- shared helpers ------------------------------------------------------------------


def _factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """The lifespan session factory; without it fail closed (503)."""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        from fastapi import HTTPException
        from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            detail="session domain service is unavailable",
        )
    return factory


def _not_found(request: Request) -> Response:
    return render_error_envelope(
        request,
        HTTP_404_NOT_FOUND,
        SessionErrorCode.SESSION_NOT_FOUND.value,
        "no session found for this identity",
    )


def _bad_request(request: Request, code: str, message: str) -> Response:
    return render_error_envelope(request, HTTP_400_BAD_REQUEST, code, message)


class SessionTagsPage(BaseModel):
    """One bounded tag page for one agent session (D07-fix1 F2; same-server
    scope; the ConversationTagsPage envelope shape with the session ids)."""

    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID = Field(description="Owning session identity")
    server_id: uuid.UUID = Field(description="Server scope pinning the session")
    limit: int = Field(
        ge=1,
        le=SESSION_LIST_MAX_LIMIT,
        description="Page size used for this slice (1.."
        f"{SESSION_LIST_MAX_LIMIT})",
    )
    offset: int = Field(ge=0, description="Slice start within the collection")
    total: int = Field(
        ge=0,
        description="Pre-slice count for this session under the same filter",
    )
    has_more: bool = Field(
        description="True when another page exists after this slice",
    )
    items: list[EntityTagView] = Field(
        default_factory=list,
        description="Association rows in deterministic assignment order",
    )

    @model_validator(mode="after")
    def _page_is_consistent(self) -> "SessionTagsPage":
        if len(self.items) > self.limit:
            raise ValueError("page carries more items than its limit")
        if self.has_more != (self.offset + len(self.items) < self.total):
            raise ValueError("has_more must equal offset + len(items) < total")
        return self


#: Tag-store rejection -> (HTTP status, value-free C01 code). The session
#: routes reuse the reviewed history-tags mapping verbatim.
_SESSION_TAG_ERROR_MAP: Final[Mapping[TagErrorCode, tuple[int, str]]] = {
    TagErrorCode.VALIDATION: (HTTP_400_BAD_REQUEST, "forbidden_field"),
    TagErrorCode.TAG_NOT_FOUND: (HTTP_404_NOT_FOUND, "tag_not_found"),
    TagErrorCode.TAG_IN_USE: (HTTP_409_CONFLICT, "tag_in_use"),
    TagErrorCode.RESERVED_TAG: (HTTP_409_CONFLICT, "reserved_tag"),
    TagErrorCode.LAST_TAG: (HTTP_409_CONFLICT, "last_tag"),
    TagErrorCode.INVARIANT_VIOLATION: (HTTP_409_CONFLICT, "conflict"),
}


def _tag_error(request: Request, exc: TagStoreError) -> Response:
    """Render one classified tag-store rejection as the C01 envelope."""
    status, code = _SESSION_TAG_ERROR_MAP.get(exc.code, (HTTP_400_BAD_REQUEST, "forbidden_field"))
    return render_error_envelope(request, status, code, str(exc))


def _summary_row(
    row: Any,
    tag_preview: EntityTagPreview | None = None,
    service: tuple[uuid.UUID | None, str | None] = (None, None),
) -> SessionSummary:
    """Project one ``SessionRow`` onto the safe summary (never content).
    ``tag_preview`` rides additively (D07-fix1 N5) so drawer session rows
    render HistoryTags without a second round trip; ``service`` is the
    ``(service_id, service_name)`` scope projection (SCOPE-1): the stored
    column identity, the legacy runbook_ref identity for NULL-service
    RUNBOOK rows, and the SAME-server map name — or None name when the map
    entry is missing/deleted (the client reports an unavailable name; it
    never borrows another server's label)."""
    service_id, service_name = service
    return SessionSummary(
        session_id=row.session_id,
        server_id=row.server_id,
        service_id=service_id,
        service_name=service_name,
        source=SessionSourceKind(str(row.source)),
        status=SessionStatus(str(row.status)),
        title=row.title,
        tag_preview=(
            None
            if tag_preview is None
            else EntityTagPreviewView(
                preview=list(tag_preview.preview),
                total=tag_preview.total,
            )
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _tag_previews(
    factory: async_sessionmaker[AsyncSession],
    session_ids: Sequence[uuid.UUID],
    *,
    server_id: uuid.UUID | None,
) -> dict[uuid.UUID, EntityTagPreview]:
    """Batch previews for one page; ``server_id=None`` previews the recent
    cross-server page (ids are already scope-produced)."""
    try:
        return await apreview_session_tags(
            factory, session_ids=session_ids, server_id=server_id
        )
    except SessionContractError:
        return {}


def _resolve_service_names(
    request: Request,
    pairs: Sequence[tuple[uuid.UUID, uuid.UUID]],
) -> dict[tuple[uuid.UUID, uuid.UUID], str | None]:
    """Current SAME-server Service Map names for ``(server, service)``
    pairs. One ``list_entries`` read per distinct server; an unreadable or
    missing map degrades to None names (unavailable), never to a borrowed
    label from another server."""
    store = getattr(request.app.state, "service_map", None)
    if store is None or not pairs:
        return {pair: None for pair in pairs}
    by_server: dict[uuid.UUID, set[uuid.UUID]] = {}
    for server_id, service_id in pairs:
        by_server.setdefault(server_id, set()).add(service_id)
    names: dict[tuple[uuid.UUID, uuid.UUID], str | None] = {}
    for server_id, service_ids in by_server.items():
        try:
            entries = store.list_entries(server_id)
        except Exception:
            names.update({(server_id, sid): None for sid in service_ids})
            continue
        index = {entry.application_id: entry.name for entry in entries}
        names.update(
            {(server_id, sid): index.get(str(sid)) for sid in service_ids}
        )
    return names


async def _last_service_ref_name(
    factory: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
) -> str | None:
    """The last persisted service_ref display name (SCOPE-2): ONE bounded
    newest-first query, used ONLY when the map entry was deleted, so the
    follow-on identity stays honest instead of borrowing another server's
    label. Names are admission-bounded; the read re-caps defensively."""
    async with factory() as session:
        row = (
            await session.execute(
                sql_text(
                    """
                    SELECT payload->'service_ref'->>'name' AS name
                    FROM session_entries
                    WHERE session_id = :sid
                      AND kind = 'user_message'
                      AND payload->'service_ref'->>'name' IS NOT NULL
                    ORDER BY seq DESC
                    LIMIT 1
                    """
                ),
                {"sid": session_id},
            )
        ).first()
    if row is None or row[0] is None:
        return None
    return str(row[0])[:MAX_SERVICE_REF_NAME_CHARS]


async def _is_first_user_entry(
    factory: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
) -> bool:
    """One bounded COUNT: does this session have ANY committed user entry
    yet? Decides the first-full vs compact follow-on service material."""
    async with factory() as session:
        count = (
            await session.execute(
                sql_text(
                    """
                    SELECT COUNT(*) FROM session_entries
                    WHERE session_id = :sid AND kind = 'user_message'
                    """
                ),
                {"sid": session_id},
            )
        ).scalar_one()
    return int(count) == 0


async def _service_scope_for_turn(
    factory: async_sessionmaker[AsyncSession],
    request: Request,
    *,
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    service_id: uuid.UUID | None,
) -> tuple[Any | None, ServiceRef | None]:
    """The turn's immutable session scope (SCOPE-2). The stored
    ``sessions.service_id`` is the ONLY authority — the turn body can never
    inject or rebind it. A deleted map entry ADMITS the turn with the
    persisted identity (last persisted name if safely available; the
    deterministic unavailable fallback renders at model context); it never
    borrows another server's name or injects a fresh runbook."""
    if service_id is None:
        return None, None
    store = getattr(request.app.state, "service_map", None)
    entry = None
    if store is not None:
        try:
            entry = store.get_entry(server_id, service_id)
        except Exception:
            entry = None  # an unreadable map degrades to identity-only
    if entry is not None:
        return entry, ServiceRef(service_id=service_id, name=entry.name)
    stored_name = await _last_service_ref_name(factory, session_id=session_id)
    return None, ServiceRef(service_id=service_id, name=stored_name)


async def _legacy_runbook_service_ids(
    factory: async_sessionmaker[AsyncSession],
    session_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, uuid.UUID]:
    """Backfill is PROJECTION-ONLY (chat-recovery §B): for legacy
    NULL-service RUNBOOK sessions, ONE bounded query reads each session's
    FIRST non-null ``runbook_ref`` user entry (``ORDER BY seq LIMIT 1`` per
    session via ``DISTINCT ON``; the page ids bound the scan). No writes, no
    guesses for interactive history."""
    if not session_ids:
        return {}
    async with factory() as session:
        rows = (
            await session.execute(
                sql_text(
                    """
                    SELECT DISTINCT ON (session_id)
                           session_id,
                           (payload->'runbook_ref'->>'service_id')
                               AS service_id
                    FROM session_entries
                    WHERE session_id = ANY(:sids)
                      AND kind = 'user_message'
                      AND payload->'runbook_ref'->>'service_id' IS NOT NULL
                    ORDER BY session_id, seq
                    """
                ),
                {"sids": list(session_ids)},
            )
        ).mappings().all()
    return {
        uuid.UUID(str(row["session_id"])): uuid.UUID(str(row["service_id"]))
        for row in rows
    }


async def _service_scope_projection(
    factory: async_sessionmaker[AsyncSession],
    request: Request,
    rows: Sequence[Any],
) -> dict[uuid.UUID, tuple[uuid.UUID | None, str | None]]:
    """One page's ``session_id -> (service_id, service_name)`` projection.

    Stored non-NULL ``sessions.service_id`` wins; a legacy NULL-service
    RUNBOOK row falls back to its first non-null ``runbook_ref`` user entry
    (the bounded projection-only query). Names resolve from the SAME-server
    map only; an unavailable or deleted entry projects a null name (wire
    honesty) and never mutates the session row."""
    stored = {
        row.session_id: (row.server_id, row.service_id)
        for row in rows
        if row.service_id is not None
    }
    legacy_candidates = [
        row.session_id
        for row in rows
        if row.service_id is None
        and str(row.source) == SessionSourceKind.RUNBOOK.value
    ]
    legacy = await _legacy_runbook_service_ids(factory, legacy_candidates)
    pairs: list[tuple[uuid.UUID, uuid.UUID]] = [
        *stored.values(),
        *(
            (row.server_id, legacy[row.session_id])
            for row in rows
            if row.session_id in legacy
        ),
    ]
    names = _resolve_service_names(request, pairs)
    projection: dict[uuid.UUID, tuple[uuid.UUID | None, str | None]] = {}
    for row in rows:
        service_id = row.service_id or legacy.get(row.session_id)
        projection[row.session_id] = (
            service_id,
            None if service_id is None else names.get((row.server_id, service_id)),
        )
    return projection


async def _detail_of(
    factory: async_sessionmaker[AsyncSession],
    request: Request,
    *,
    session_id: uuid.UUID,
    server_id: uuid.UUID,
) -> SessionDetail | None:
    """The scoped detail header plus bounded aggregates (one bounded
    aggregate query; never transcript content). ``aget_session`` raises
    the scope-blind not-found contract error — that IS the 404."""
    try:
        row = await aget_session(factory, session_id, server_id=server_id)
    except SessionContractError:
        return None
    if row is None:
        return None
    previews = await _tag_previews(factory, [session_id], server_id=server_id)
    service_scope = await _service_scope_projection(factory, request, [row])
    summary = _summary_row(
        row,
        previews.get(session_id),
        service_scope.get(session_id, (None, None)),
    )
    async with factory() as session:
        async with session.begin():
            aggregates = (
                await session.execute(
                    sql_text(
                        """
                        SELECT
                            COALESCE(MAX(seq), 0) AS latest_seq,
                            COUNT(*) AS total_entries,
                            COUNT(*) FILTER (WHERE kind = 'compaction')
                                AS compaction_count
                        FROM session_entries
                        WHERE session_id = :sid
                        """
                    ),
                    {"sid": session_id},
                )
            ).mappings().one()
    return SessionDetail(
        **summary.model_dump(mode="json"),
        latest_seq=(int(aggregates["latest_seq"]) or None),
        total_entries=int(aggregates["total_entries"]),
        compaction_count=int(aggregates["compaction_count"]),
    )


async def _active_run_id(
    factory: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
) -> uuid.UUID | None:
    """The session's active run, when any — one bounded query."""
    async with factory() as session:
        async with session.begin():
            row = (
                await session.execute(
                    sql_text(
                        """
                        SELECT run_id FROM session_runs
                        WHERE session_id = :sid
                          AND state IN ('running', 'waiting_approval',
                                        'waiting_child', 'reconciling',
                                        'outcome_unknown')
                        ORDER BY created_at DESC
                        LIMIT 1
                        """
                    ),
                    {"sid": session_id},
                )
            ).first()
    return uuid.UUID(str(row[0])) if row else None


def _profile_binding(
    request: Request, server_id: uuid.UUID
) -> tuple[int, int, tuple[str, ...]]:
    """``(profile_revision, prompt_revision, capabilities)`` from the
    canonical profile, resolved through the SAME reviewed default
    resolver as runbook admission (:func:`resolve_default_capabilities`):
    grant-store grants, the canonical profile's capabilities, enabled
    per-server AND global plugin bindings, the internal-API surface and
    the built-in controller defaults — deduplicated in that documented
    order. The profile-less General Consult scope pins revisions to 1
    and admits the worker pseudo-server grant surface instead
    (:func:`_general_consult_capabilities`). An unusable source degrades
    to fewer capabilities — never an error."""
    if server_id == _GENERAL_SERVER:
        return 1, 1, _general_consult_capabilities(request)
    store = getattr(request.app.state, "profiles", None)
    if store is None:
        return 1, 1, tuple(
            resolve_default_capabilities(request, server_id, ())
        )
    try:
        stored = store.read(server_id)
    except Exception:
        return 1, 1, tuple(
            resolve_default_capabilities(request, server_id, ())
        )
    from mai.contracts.server import parse_server_profile_text

    profile = parse_server_profile_text(stored.text)
    capabilities = tuple(
        resolve_default_capabilities(request, server_id, profile.capabilities)
    )
    return int(stored.revision), int(profile.prompt_revision), capabilities


async def _session_latest_seq(
    factory: async_sessionmaker[AsyncSession], session_id: uuid.UUID
) -> int:
    """One bounded aggregate: the session's highest committed sequence."""
    async with factory() as session:
        row = (
            await session.execute(
                sql_text(
                    "SELECT COALESCE(MAX(seq), 0) FROM session_entries "
                    "WHERE session_id = :sid"
                ),
                {"sid": session_id},
            )
        ).scalar_one()
    return int(row)


def _entry_page(frame_page: SessionHistoryPage) -> ServerEntriesFrame:
    return ServerEntriesFrame(
        entries=list(frame_page.items),
        latest_seq=frame_page.latest_seq or 0,
        has_more=frame_page.has_more,
        has_older=frame_page.has_older,
        next_before_seq=frame_page.next_before_seq,
    )


# --- REST routes ---------------------------------------------------------------------


@router.get(
    "",
    response_model=SessionListPage,
    summary="Bounded agent-session list page",
)
async def list_sessions(
    request: Request,
    server_id: uuid.UUID | None = Query(
        default=None,
        description="Explicit server scope the listing is pinned to "
        "(C01 rule 2); omit for the recent page across all enrolled "
        "servers + General Consult",
    ),
    status: SessionStatus | None = Query(
        default=None,
        description="Optional coarse status filter (SessionStatus vocabulary)",
    ),
    tag_id: uuid.UUID | None = Query(
        default=None,
        description="Optional catalog tag filter (session_tags association)",
    ),
    limit: int = Query(
        default=SESSION_LIST_DEFAULT_LIMIT,
        ge=1,
        le=SESSION_LIST_MAX_LIMIT,
        description="Page size used for this slice (1.."
        f"{SESSION_LIST_MAX_LIMIT})",
    ),
    offset: int = Query(
        default=0, ge=0, description="Slice start within the collection"
    ),
) -> SessionListPage:
    """One bounded, deterministic session page (newest-change first) with
    honest tag previews (D07-fix1: the drawer renders HistoryTags from the
    list page without a second round trip)."""
    await _require_session(request)
    factory = _factory(request)
    rows, total = await alist_sessions(
        factory,
        server_id=server_id,
        status=status,
        tag_id=tag_id,
        limit=limit,
        offset=offset,
    )
    previews = await _tag_previews(
        factory, [row.session_id for row in rows], server_id=server_id
    )
    service_scope = await _service_scope_projection(factory, request, rows)
    return SessionListPage(
        items=[
            _summary_row(
                row,
                previews.get(row.session_id),
                service_scope.get(row.session_id, (None, None)),
            )
            for row in rows
        ],
        limit=limit,
        offset=offset,
        total=int(total),
        has_more=offset + len(rows) < int(total),
    )


@router.post("", response_model=SessionDetail, status_code=201)
async def create_session(
    request: Request, payload: SessionCreateRequest
) -> Response | SessionDetail:
    """Idempotent session create: the client idempotency key derives the
    session identity, so a retry converges on the same row. Optional
    ``service_id`` (SCOPE-1) is validated against the SAME-server Service
    Map BEFORE the insert, refused for General Consult, and fixed at
    creation — an existing session is never rebound; a replayed key with a
    DIFFERENT scope is an honest 409."""
    await _require_session(request)
    _require_allowed_origin(request)
    _require_csrf(request)
    factory = _factory(request)
    try:
        validated_service_id = await _validated_service_id(request, payload)
    except SessionScopeValidationError as exc:
        return _bad_request(
            request,
            SessionErrorCode.FORBIDDEN_FIELD.value,
            str(exc),
        )
    derived_id = uuid.uuid5(
        uuid.UUID(int=0), f"agent-session:{payload.client_request_id}"
    )
    try:
        await acreate_session(
            factory,
            server_id=payload.server_id,
            source=payload.source,
            title=payload.title,
            session_id=derived_id,
            service_id=validated_service_id,
        )
    except SessionContractError:
        pass  # a replayed key: converge on the existing (scope-checked) row
    except Exception as error:
        # A replayed key surfaces as the schema unique violation; ANY other
        # integrity fault is a defect — never silently swallowed.
        if "unique" not in str(error).lower() and "duplicate" not in str(
            error
        ).lower():
            raise
    detail = await _detail_of(
        factory, request, session_id=derived_id, server_id=payload.server_id
    )
    if detail is None:
        return _not_found(request)
    # Idempotent replay scope conflict: the SAME client request id always
    # converges on the SAME service scope; anything else is rejected.
    if detail.service_id != validated_service_id:
        return render_error_envelope(
            request,
            HTTP_409_CONFLICT,
            SessionErrorCode.SESSION_SCOPE_CONFLICT.value,
            "this client request id was already admitted with a different "
            "service scope",
        )
    return detail


async def _validated_service_id(
    request: Request, payload: SessionCreateRequest
) -> uuid.UUID | None:
    """SCOPE-1 create-time validation: a service scope requires a
    non-General server and a SAME-server Service Map entry. Bounded
    value-free rejection — never a disclosure of foreign map contents, and
    never an arbitrary client string stored."""
    if payload.service_id is None:
        return None
    if payload.server_id == _GENERAL_SERVER:
        raise SessionScopeValidationError(
            "service scope requires a non-General server session"
        )
    store = getattr(request.app.state, "service_map", None)
    if store is not None:
        try:
            entry = store.get_entry(payload.server_id, payload.service_id)
        except Exception:
            entry = None  # an unreadable map admits nothing (fail closed)
        if entry is not None:
            return uuid.UUID(entry.application_id)
    raise SessionScopeValidationError(
        "service_id is not a mapped service on this server"
    )


class SessionScopeValidationError(ValueError):
    """Bounded create-time service-scope rejection (rendered as a 400
    ``forbidden_field`` on ``service_id``)."""


@router.get("/{session_id}", response_model=SessionDetail)
async def get_session_detail(
    request: Request,
    session_id: uuid.UUID,
    server_id: uuid.UUID = Query(
        description="Explicit server scope (C01 rule 2; scope-blind 404)"
    ),
) -> Response | SessionDetail:
    """Scoped session detail; a missing and a foreign session are the SAME
    value-free 404."""
    await _require_session(request)
    factory = _factory(request)
    detail = await _detail_of(
        factory, request, session_id=session_id, server_id=server_id
    )
    if detail is None:
        return _not_found(request)
    return detail


@router.get("/{session_id}/history", response_model=SessionHistoryPage)
async def get_history(
    request: Request,
    session_id: uuid.UUID,
    server_id: uuid.UUID = Query(
        description="Explicit server scope (C01 rule 2; scope-blind 404)"
    ),
    limit: int = Query(
        default=_HISTORY_PAGE_DEFAULT,
        ge=1,
        le=HISTORY_PAGE_MAX_ENTRIES,
        description=f"Bounded page size (1..{HISTORY_PAGE_MAX_ENTRIES})",
    ),
    before_seq: int | None = Query(
        default=None,
        ge=1,
        description="Exclusive keyset cursor (older pages)",
    ),
    after_seq: int | None = Query(
        default=None,
        ge=1,
        description="Exclusive keyset cursor (catch-up pages)",
    ),
) -> Response | SessionHistoryPage:
    """One bounded, byte-checked history page (the D01 contract)."""
    await _require_session(request)
    factory = _factory(request)
    try:
        return await aread_session_history(
            factory,
            session_id=session_id,
            server_id=server_id,
            limit=limit,
            before_seq=before_seq,
            after_seq=after_seq,
        )
    except SessionContractError as exc:
        codes = {item.code for item in exc.errors}
        if SessionErrorCode.SESSION_NOT_FOUND.value in codes:
            return _not_found(request)
        return _bad_request(request, "invalid_cursor", exc.args[0])


@router.post(
    "/{session_id}/turns", response_model=SessionTurnSubmitResponse
)
async def submit_turn(
    request: Request,
    session_id: uuid.UUID,
    payload: SessionTurnSubmitRequest,
    server_id: uuid.UUID = Query(
        description="Explicit server scope (C01 rule 2; scope-blind 404)"
    ),
) -> Response | SessionTurnSubmitResponse:
    """Admit one user turn: durable admission (run row + user entry), then
    the engine enqueue FOLLOWS the commit. HTTP persists INTENT only; the
    worker owns execution."""
    _, token = await _require_session(request)
    _require_allowed_origin(request)
    _require_csrf(request)
    if payload.session_id != session_id:
        return _bad_request(
            request,
            SessionErrorCode.FORBIDDEN_FIELD.value,
            "the body session id must match the path identity",
        )
    factory = _factory(request)
    try:
        row = await aget_session(factory, session_id, server_id=server_id)
    except SessionContractError:
        return _not_found(request)
    profile_revision, prompt_revision, capabilities = _profile_binding(
        request, server_id
    )

    # The deterministic run id: a replayed idempotency key converges on
    # the same run/entry (the store dedupes both) and the engine enqueue
    # stays idempotent (the run id IS the workflow id).
    run_id = uuid.uuid5(
        uuid.UUID(int=0),
        f"agent-session-run:{session_id}:{payload.client_request_id}",
    )
    context = ExecutionContext(
        principal=hash_token(token),
        server_id=server_id,
        run_id=run_id,
        parent_scope=None,
        profile_revision=str(profile_revision),
        prompt_revision=str(prompt_revision),
        credential_references=(),
        allowed_capabilities=tuple(capabilities),
        mode=PolicyMode.DELEGATED,
    )
    intent = SessionRunIntent(
        payload_version=1,
        session_id=str(session_id),
        run_id=str(run_id),
        server_id=str(server_id),
        client_request_id=str(payload.client_request_id),
        context=context,
        prompt=payload.text,
        parent_run_id=None,
        source=SessionSourceKind.API,
        connection=None,
        profile=payload.model_profile,
        max_rounds=90,
        approval_wait_seconds=3600.0,
        reconcile_wait_seconds=300.0,
        wait_poll_seconds=0.5,
        document_ref=None,
        runbook_ref=None,
    )
    # S2 (queued-chat hotfix): the reviewed root seam admits the run AND
    # the retained execution facade (job_runs depth 0 + the mandatory
    # classification) in ONE transaction BEFORE the enqueue, with the SAME
    # binding the frozen intent carries — no fabricated revisions, no
    # widened grants (requested_grants are exactly the resolved admission
    # capabilities the context authorizes).
    model_profile = payload.model_profile or "default"
    await acreate_root_run(
        factory,
        session_id=session_id,
        server_id=server_id,
        run_id=run_id,
        model_profile=model_profile,
        client_request_id=payload.client_request_id,
        profile_revision=profile_revision,
        prompt_revision=prompt_revision,
        requested_grants=capabilities,
    )
    await arecord_admission(
        factory,
        session_id=session_id,
        server_id=server_id,
        client_request_id=payload.client_request_id,
        source=SessionSourceKind.API,
        run_id=run_id,
    )
    # SCOPE-2: the session's stored service scope is the ONLY authority for
    # this turn (no per-turn client scope input). The FIRST service-bound
    # user entry persists the full validated Service Map section + prompt
    # (untrusted user content, never system/runbook execution; an over-
    # budget combination is an honest rejection, never clipped); follow-on
    # entries carry only the compact identity in their service_ref.
    service_entry, service_ref = await _service_scope_for_turn(
        factory,
        request,
        session_id=session_id,
        server_id=server_id,
        service_id=row.service_id,
    )
    entry_text = payload.text
    if (
        row.service_id is not None
        and service_entry is not None
        and await _is_first_user_entry(factory, session_id=session_id)
    ):
        entry_text = payload.text + format_service_map_section(
            [service_entry]
        )
        if len(entry_text) > MAX_ENTRY_TEXT_CHARS:
            return _bad_request(
                request,
                SessionErrorCode.BYTE_BUDGET_EXCEEDED.value,
                "the service context exceeds the bounded turn size; "
                "shorten the message",
            )
    user_entry = UserMessageEntry(
        kind=EntryKind.USER_MESSAGE,
        text=entry_text,
        client_request_id=payload.client_request_id,
        source=SessionSourceKind.API,
        service_ref=service_ref,
    )
    # aappend_entry returns the durable SessionEntryRow and is idempotent on
    # durable_source_id: a replayed client_request_id gets the ORIGINAL
    # entry_id/seq back (no duplicate row), so the response is built from the
    # append result itself — no confirmation re-read, which would silently
    # miss the newest entry on sessions with >=50 existing entries
    # (R06-fix1: the ascending after_seq=0 page returns the OLDEST 50).
    entry_row = await aappend_entry(
        factory,
        session_id=session_id,
        server_id=server_id,
        kind=EntryKind.USER_MESSAGE,
        payload=user_entry.model_dump(mode="json"),
        run_id=run_id,
        durable_source_id=f"turn:{payload.client_request_id}",
    )
    await arecord_run_state(
        factory,
        session_id=session_id,
        server_id=server_id,
        run_id=run_id,
        state=SessionRunState.QUEUED,
    )
    latest_seq = await _session_latest_seq(factory, session_id)
    # Wake the channel hub AFTER the commit (identity-only payload; a lost
    # wakeup costs one coarse tick, never correctness).
    engine = getattr(request.app.state, "engine", None)
    if engine is not None:
        with contextlib.suppress(Exception):
            await notify_session_committed(engine, session_id, latest_seq)
    # The engine enqueue FOLLOWS the commit exactly like the reviewed chat
    # seam: a crash leaves a re-armable gap (the retry re-enqueues
    # idempotently — the run id is the deterministic workflow id), never a
    # half-recorded turn.
    recovery = getattr(request.app.state, "job_recovery", None)
    deduplicated = False
    if recovery is not None:
        from mai.jobs.engine import RunIdInUseError

        try:
            await asyncio.to_thread(
                recovery.client.enqueue_run,
                str(run_id),
                HANDLER_NAME,
                json.dumps(intent.to_json(), sort_keys=True),
            )
        except RunIdInUseError:
            deduplicated = True
    return SessionTurnSubmitResponse(
        session_id=session_id,
        run_id=run_id,
        entry_id=entry_row.entry_id,
        seq=entry_row.seq,
        run_state=SessionRunState.QUEUED,
        deduplicated=deduplicated,
    )


@router.post(
    "/{session_id}/control", response_model=SessionControlResponse
)
async def submit_control(
    request: Request,
    session_id: uuid.UUID,
    payload: SessionControlRequest,
    server_id: uuid.UUID = Query(
        description="Explicit server scope (C01 rule 2; scope-blind 404)"
    ),
) -> Response | SessionControlResponse:
    """One durable control action (steer/pause/cancel/approve/deny/
    reconcile) delegated to the reviewed session-runner facade."""
    await _require_session(request)
    _require_allowed_origin(request)
    _require_csrf(request)
    if payload.session_id != session_id:
        return _bad_request(
            request,
            SessionErrorCode.FORBIDDEN_FIELD.value,
            "the body session id must match the path identity",
        )
    factory = _factory(request)
    try:
        row = await aget_session(factory, session_id, server_id=server_id)
    except SessionContractError:
        return _not_found(request)
    active = await _active_run_id(factory, session_id=session_id)
    database_url = getattr(request.app.state, "database_url", None)
    return await asubmit_session_control(
        factory,
        session_id=session_id,
        server_id=server_id,
        request=payload,
        active_run_id=active,
        database_url=database_url,
    )


# --- rename + tags surface (D07-fix1 F2) ---------------------------------------------
# The reviewed boundary order everywhere: session cookie (layer 1), exact
# Origin allowlist (layer 2), CSRF double submit (layer 3); every route is
# explicitly server-scoped and fails scope-blind 404 (C01 rule 2); every
# collection read is a bounded page (AGENTS.md §1).


@router.patch("/{session_id}", response_model=SessionDetail)
async def rename_session(
    request: Request,
    session_id: uuid.UUID,
    payload: SessionRenameRequest,
    server_id: uuid.UUID | None = Query(
        default=None,
        description="Explicit server scope (scope-blind 404 when it "
        "differs from the stored session)",
    ),
) -> Response | SessionDetail:
    """Rename one session (the only mutable header field); the trimmed
    title is durable under the session-row lock."""
    _ = await _require_session(request)
    _require_allowed_origin(request)
    _require_csrf(request)
    factory = _factory(request)
    try:
        row = await arename_session(
            factory,
            session_id=session_id,
            server_id=server_id,
            title=payload.title,
        )
    except SessionContractError as exc:
        # A title-bound rejection is a 400 (the client sent it); any scope
        # rejection stays the scope-blind 404 (C01 rule 2).
        if any(error.field == "title" for error in exc.errors):
            return _bad_request(
                request,
                SessionErrorCode.FORBIDDEN_FIELD.value,
                "title must be 1..120 characters when set",
            )
        return _not_found(request)
    detail = await _detail_of(
        factory, request, session_id=row.session_id, server_id=row.server_id
    )
    if detail is None:
        return _not_found(request)
    return detail


@router.get("/{session_id}/tags", response_model=SessionTagsPage)
async def list_session_tags(
    request: Request,
    session_id: uuid.UUID,
    server_id: uuid.UUID = Query(
        description="Explicit server scope (C01 rule 2)",
    ),
    limit: int = Query(
        default=SESSION_LIST_DEFAULT_LIMIT,
        ge=1,
        le=SESSION_LIST_MAX_LIMIT,
        description="Page size used for this slice (1.."
        f"{SESSION_LIST_MAX_LIMIT})",
    ),
    offset: int = Query(default=0, ge=0, description="Slice start"),
) -> Response | SessionTagsPage:
    """One bounded tag page of the session's associations (scope-blind
    refusal when the session is missing or foreign)."""
    _ = await _require_session(request)
    factory = _factory(request)
    try:
        rows, total = await asession_tags_page(
            factory, session_id=session_id, server_id=server_id, limit=limit, offset=offset
        )
    except SessionContractError:
        # The store's empty-vs-foreign probe raised the scope-blind
        # contract error: missing and cross-server stay the SAME 404.
        return _not_found(request)
    # Resolve the names through ONE bounded catalog probe for the page.
    names: dict[uuid.UUID, str] = {}
    if rows:
        async with factory() as session:
            tag_rows = (
                await session.execute(
                    sql_text(
                        "SELECT tag_id, name FROM tags "
                        "WHERE tag_id = ANY(:ids)"
                    ),
                    {"ids": [row.tag_id for row in rows]},
                )
            ).mappings().all()
        names = {uuid.UUID(str(row["tag_id"])): str(row["name"]) for row in tag_rows}
    items = [
        EntityTagView(
            tag_id=row.tag_id,
            name=names.get(row.tag_id, ""),
            assigned_by=row.assigned_by,
            assignment_source=row.assignment_source,
            created_at=row.created_at.isoformat().replace("+00:00", "Z"),
        )
        for row in rows
    ]
    return SessionTagsPage(
        session_id=session_id,
        server_id=server_id,
        limit=limit,
        offset=offset,
        total=int(total),
        has_more=offset + len(items) < int(total),
        items=items,
    )


@router.post("/{session_id}/tags", status_code=HTTP_204_NO_CONTENT)
async def assign_session_tag(
    request: Request,
    session_id: uuid.UUID,
    payload: SessionTagAssignRequest,
) -> Response:
    """Assign one catalog tag to the session (idempotent on the pair;
    ``assignment_source='user'``; optional create-if-missing name)."""
    _ = await _require_session(request)
    _require_allowed_origin(request)
    _require_csrf(request)
    factory = _factory(request)
    try:
        if payload.tag_name is not None and payload.tag_name.strip() != "":
            # Name-first resolution (idempotent submits): an existing
            # catalog tag with that canonical name wins (its id is the
            # association target regardless of the body's tag_id); a fresh
            # name with an unknown requested id mints the catalog row and
            # THAT id is the target.
            resolved = await _tag_id_by_name(factory, payload.tag_name)
            if resolved is not None:
                tag_id = resolved
            elif await _tag_exists(factory, payload.tag_id):
                tag_id = payload.tag_id
            else:
                from mai.tag_store import acreate_tag

                created = await acreate_tag(
                    factory,
                    name=payload.tag_name.strip(),
                    description=None,
                )
                tag_id = uuid.UUID(str(created.tag_id))
        else:
            tag_id = payload.tag_id
    except TagStoreError as exc:
        return _tag_error(request, exc)
    try:
        await aattach_session_tag(
            factory,
            session_id=session_id,
            server_id=payload.server_id,
            tag_id=tag_id,
            assigned_by="",
            assignment_source="user",
        )
    except SessionContractError:
        return _not_found(request)
    except TagStoreError as exc:
        return _tag_error(request, exc)


@router.delete(
    "/{session_id}/tags/{tag_id}", status_code=HTTP_204_NO_CONTENT
)
async def remove_session_tag(
    request: Request,
    session_id: uuid.UUID,
    tag_id: uuid.UUID,
    server_id: uuid.UUID = Query(
        description="Explicit server scope (C01 rule 2)",
    ),
    replacement_tag_id: uuid.UUID | None = Query(
        default=None,
        description="Mandatory on final-tag removal (last_tag 409 "
        "without it); attaches in the same transaction",
    ),
) -> Response:
    """Remove one session-tag association; the mandatory-at-least-one-tag
    invariant refuses the final removal without a replacement (409)."""
    _ = await _require_session(request)
    _require_allowed_origin(request)
    _require_csrf(request)
    factory = _factory(request)
    try:
        await aremove_session_tag(
            factory,
            session_id=session_id,
            server_id=server_id,
            tag_id=tag_id,
            replacement_tag_id=replacement_tag_id,
        )
    except TagStoreError as exc:
        return _tag_error(request, exc)


async def _tag_exists(
    factory: async_sessionmaker[AsyncSession], tag_id: uuid.UUID
) -> bool:
    """One bounded catalog probe (route-local helper)."""
    async with factory() as session:
        row = (
            await session.execute(
                sql_text("SELECT 1 FROM tags WHERE tag_id = :tid"),
                {"tid": tag_id},
            )
        ).first()
    return row is not None


async def _tag_id_by_name(
    factory: async_sessionmaker[AsyncSession], name: str
) -> uuid.UUID | None:
    """Canonical lower(name) catalog probe (route-local helper)."""
    async with factory() as session:
        row = (
            await session.execute(
                sql_text("SELECT tag_id FROM tags WHERE lower(name) = lower(:name)"),
                {"name": name.strip()},
            )
        ).first()
    return uuid.UUID(str(row[0])) if row else None


# --- the WebSocket channel ------------------------------------------------------------


def _origin_allowlist(app: FastAPI) -> frozenset[str]:
    """The exact-origin allowlist built by the lifespan (terminal
    precedent)."""
    origins = getattr(app.state, "allowed_origins", None) or getattr(
        app.state, "public_origins", frozenset()
    )
    return frozenset(origins)


@router.websocket("/{session_id}/channel")
async def session_channel_socket(
    websocket: WebSocket,
    session_id: uuid.UUID,
    server_id: uuid.UUID = Query(
        description="Explicit server scope (C01 rule 2; scope-blind refusal)"
    ),
) -> None:
    """The versioned cursor channel: admission (cookie → origin → CSRF in
    ``hello`` → scope), bounded snapshot/replay, live delivery."""
    app: FastAPI = websocket.app
    sessions_store = getattr(app.state, "sessions", None)
    factory = getattr(app.state, "session_factory", None)
    hub: SessionChannelHub | None = getattr(app.state, "session_channel", None)
    if sessions_store is None or factory is None or hub is None:
        with contextlib.suppress(Exception):
            await websocket.close(code=1011, reason="channel_unavailable")
        return
    # Layer 1: the validated mai_session cookie (revocation re-checked).
    token = websocket.cookies.get(SESSION_COOKIE, "")
    if not token:
        await websocket.close(code=1008, reason="unauthenticated")
        return
    try:
        await sessions_store.validate(token)
    except Exception:
        await websocket.close(code=1008, reason="unauthenticated")
        return
    # Layer 2: the exact Origin allowlist (Referer never counts).
    origin = websocket.headers.get("origin", "")
    if not origin or origin not in _origin_allowlist(app):
        await websocket.close(code=1008, reason="origin_rejected")
        return
    # Layer 4: the session must exist in the caller's declared scope
    # (scope-blind refusal; server_id is a non-secret scope pin).
    try:
        row = await aget_session(factory, session_id, server_id=server_id)
    except Exception:
        row = None
    if row is None:
        await websocket.close(code=1008, reason="scope_rejected")
        return

    await websocket.accept()
    connection_id = uuid.uuid4()
    stream_id = StreamIdentity.stream_id_for(session_id, connection_id)
    csrf_cookie = websocket.cookies.get("mai_csrf", "")
    sink = SessionSink(
        connection_id=connection_id,
        session_id=session_id,
        server_id=server_id,
    )

    async def send(frame: Any) -> bool:
        try:
            await websocket.send_text(
                json.dumps(frame.model_dump(mode="json"), separators=(",", ":"))
            )
            return True
        except Exception:
            return False

    subscribed = False
    try:
        # The handshake arrives FIRST and proves CSRF (layer 3,
        # constant-time against the mai_csrf cookie).
        frame = validate_client_frame(await websocket.receive_text())
        if not isinstance(frame, ClientHelloFrame):
            await send(_error_frame("handshake_required", "the first frame must be hello"))
            await websocket.close(code=1008, reason="handshake_required")
            return
        if not csrf_cookie or not hmac.compare_digest(frame.csrf_token, csrf_cookie):
            await send(_error_frame("csrf_rejected", "CSRF proof mismatched"))
            await websocket.close(code=1008, reason="csrf_rejected")
            return
        # THE RACE ORDERING RULE: subscribe BEFORE the snapshot read; the
        # snapshot carries latest_seq; anything committed above it arrives
        # as committed frames (no gap, no duplicate — the sink's
        # delivered-through cursor dedupes per connection).
        sink.delivered_through = frame.after_seq
        hub.subscribe(sink)
        subscribed = True
        await send(
            ServerHelloOkFrame(
                protocol_version=CHANNEL_PROTOCOL_VERSION,
                session_id=session_id,
                server_id=server_id,
                stream_id=stream_id,
                latest_seq=await _session_latest_seq(factory, session_id),
            )
        )
        await _snapshot_or_replay(
            send=send,
            factory=factory,
            session_id=session_id,
            server_id=server_id,
            sink=sink,
            after_seq=frame.after_seq,
        )

        client_task = asyncio.get_running_loop().create_task(
            _pump_client_frames(
                websocket, factory, session_id, server_id, sink, send
            )
        )
        flush_task = asyncio.get_running_loop().create_task(
            _flush_outbox(sink, send)
        )
        done, pending = await asyncio.wait(
            {client_task, flush_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for task in done:
            error = task.exception()
            if error is not None and not isinstance(
                error, WebSocketDisconnect
            ):
                raise error
    except WebSocketDisconnect:
        pass
    except SessionContractError:
        with contextlib.suppress(Exception):
            await send(_error_frame("frame_rejected", "the frame was refused"))
        with contextlib.suppress(Exception):
            await websocket.close(code=1008, reason="frame_rejected")
    except Exception as error:
        import sys as _sys

        print(
            f"session channel: unexpected failure: "
            f"{type(error).__name__}: {error}",
            file=_sys.stderr,
            flush=True,
        )
        with contextlib.suppress(Exception):
            await send(_error_frame("channel_failed", "the channel failed"))
    finally:
        # Dead clients are not retained: the fanout map shrinks here.
        if subscribed:
            hub.unsubscribe(sink)


def _error_frame(code: str, reason: str) -> ServerChannelErrorFrame:
    return ServerChannelErrorFrame(code=code[:64], reason=reason[:200])


def _resync_required(reason: str, latest_seq: int) -> ServerResyncRequiredFrame:
    return ServerResyncRequiredFrame(reason_code=reason, latest_seq=latest_seq)


async def _snapshot_or_replay(
    *,
    send: Any,
    factory: async_sessionmaker[AsyncSession],
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    sink: SessionSink,
    after_seq: int | None,
) -> None:
    """Bounded snapshot (no cursor) or bounded replay (cursor); a huge
    range arrives as bounded pages + resync_required, never a transcript.
    The sink's ``replaying`` flag keeps the hub catch-up off this outbox
    while the route owns it."""
    # TRUE for BOTH paths: the route owns the outbox while it reads/sends
    # the snapshot. A concurrent hub tick would otherwise read the newest
    # tail without a cursor (floor 0, limit 50) and duplicate a range the
    # 30-entry snapshot already covers — inflating the client cache past
    # the initial-load cap (D08: cached ≤ 30 before any older page).
    sink.replaying = True
    try:
        await _snapshot_or_replay_locked(
            send=send,
            factory=factory,
            session_id=session_id,
            server_id=server_id,
            sink=sink,
            after_seq=after_seq,
        )
    finally:
        sink.replaying = False
        # The replay advanced the cursor to its end: the hub resumes
        # catch-up from there (at_head False until the next page proves
        # the head).
        sink.at_head = False


async def _snapshot_or_replay_locked(
    *,
    send: Any,
    factory: async_sessionmaker[AsyncSession],
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    sink: SessionSink,
    after_seq: int | None,
) -> None:
    """The replay/snapshot body (``sink.replaying`` owned by the caller)."""
    if after_seq is None:
        page = await aread_session_history(
            factory,
            session_id=session_id,
            server_id=server_id,
            limit=MAX_CHANNEL_SNAPSHOT_ENTRIES,
        )
        await send(
            ServerSnapshotFrame(
                session_id=session_id,
                entries=list(page.items),
                latest_seq=page.latest_seq or 0,
                has_more=page.has_more,
                has_older=page.has_older,
                oldest_seq=page.oldest_seq,
                next_before_seq=page.next_before_seq,
                byte_size=page.byte_size,
                truncated_by_bytes=page.truncated_by_bytes,
            )
        )
        page_latest = page.items[-1].seq if page.items else 0
        sink.delivered_through = max(sink.delivered_through or 0, page_latest)
        return
    # Bounded replay: pages of ≤50 ascending from the cursor; over the
    # page budget → resync_required (the client re-subscribes/re-snapshots).
    # The FIRST page is ALWAYS sent — even when empty (a forged/absurd
    # cursor or an up-to-date cursor still gets its bounded reply; the
    # client's receive never hangs, R06 hang fix).
    cursor = after_seq
    for page_index in range(_REPLAY_PAGE_BUDGET):
        page = await aread_session_history(
            factory,
            session_id=session_id,
            server_id=server_id,
            limit=50,
            after_seq=cursor,
        )
        if page.items or page_index == 0:
            await send(_entry_page(page))
            sink.delivered_through = max(
                sink.delivered_through or 0, page.latest_seq or 0
            )
        if page.items:
            cursor = page.items[-1].seq
        if not page.has_more:
            return
    sink.resync = "replay_budget_exhausted"
    sink.offer(
        _resync_required(
            "replay_budget_exhausted",
            await _session_latest_seq(factory, session_id),
        )
    )


async def _pump_client_frames(
    websocket: WebSocket,
    factory: async_sessionmaker[AsyncSession],
    session_id: uuid.UUID,
    server_id: uuid.UUID,
    sink: SessionSink,
    send: Any,
) -> None:
    """Client-bound frames ONLY: subscribe (bounded replay), ack (the
    connection's own cursor), ping (pong). The closed vocabulary has NO
    engine-write frame — a channel command cannot mutate durable state."""
    while True:
        frame = validate_client_frame(await websocket.receive_text())
        if isinstance(frame, ClientPingFrame):
            sink.offer(ServerPongFrame())
        elif isinstance(frame, ClientAckFrame):
            # The ack advances ONLY this connection's cursor (the durable
            # store stays the only authority).
            sink.delivered_through = max(
                sink.delivered_through or 0, frame.after_seq
            )
        elif isinstance(frame, ClientSubscribeFrame):
            await _snapshot_or_replay(
                send=send,
                factory=factory,
                session_id=session_id,
                server_id=server_id,
                sink=sink,
                after_seq=frame.after_seq,
            )


async def _flush_outbox(sink: SessionSink, send: Any) -> None:
    """The bounded per-connection flusher: send queued frames in order."""
    while True:
        frame = await sink.outbox.get()
        if not await send(frame):
            return


def register_agent_sessions(app: FastAPI) -> None:
    """Register the session surface under the shared boundary."""
    app.include_router(router)
