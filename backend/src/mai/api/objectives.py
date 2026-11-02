"""Runtime v2 objective HTTP API (runtime-v2.md §3/§8/§10/§11; AC1–AC5).

Owned by the ``conversation-api`` slice: the objective families are the
v2 goal-episode surface over the ONE persistence seam
(``mai.agents.objective_store`` / ``journal_store`` / ``activity_store``
— never a second store) and the canonical ``objective.*`` ledger.

- **Explicit authority everywhere (AC2):** objectives are created,
  scope-expanded and checklist-edited by the authenticated operator
  session only — these routes pin ``ActorKind.OPERATOR`` and the agent
  capability surface never exposes them, so no model, child or tool
  call can reach a scope change. Every mutation carries the expected
  CAS revision; a stale one is the structured ``409 revision_conflict``,
  never last-writer-wins.
- **Bounded pages (AGENTS.md §1; §5.2):** every collection view is a
  ``PageEnvelope`` with ``limit``/``offset``/``total``/``has_more``,
  deterministic ordering and the same-filter ``COUNT(*)``; bounds are
  the frozen ``DEFAULT_PAGE_LIMIT``/``MAX_PAGE_LIMIT``.
- **Cursor-exclusive SSE (§5.2 / C08 rule 7):** the objective activity
  stream replays strictly after the ``Last-Event-ID`` (or ``after_seq``)
  cursor, stays authenticated and origin-checked, and self-closes once
  a settled episode is drained — a reconnect deduplicates by cursor
  instead of re-reading history.
- **Truthful drain inventory (§10.2; RV2-DEPLOY-DRAIN):** the
  ``drain-status`` route counts unsettled legacy-runtime runs (the
  ``agent.run``/``agent.delegate`` family — every run NOT bound in the
  v2-only ``objective_runs`` table) against unsettled v2 runs, from
  app-owned facade records only. Read-only; no engine tables.
- **C01 envelope (C8):** a foreign-scope objective reference is the
  scope-blind ``404 objective_not_found`` — existence never leaks; the
  rejections of the ONE stores ride their stable vocabulary verbatim.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from starlette.status import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from mai.agents.activity_store import alatest_seq, alist_events
from mai.agents.journal_store import (
    aput_checklist,
    aread_checklist,
    aread_journal,
)
from mai.agents.objective_store import (
    DEFAULT_PAGE_LIMIT,
    V2_HANDLER_NAME,
    achange_scope,
    acreate_objective,
    aget_objective,
    alist_objective_runs,
    alist_objectives,
)
from mai.api.auth import (
    BoundaryError,
    ErrorEnvelope,
    _require_allowed_origin,
    _require_csrf,
    _require_session,
)
from mai.api.jobs import _error, _sessions, _wire_ts
from mai.contracts.conversations import MAX_PAGE_LIMIT
from mai.contracts.objectives import (
    CHECKLIST_ITEM_MAX_CHARS,
    MAX_CHECKLIST_ITEMS,
    ORIGINAL_REQUEST_MAX_CHARS,
    ActorKind,
    ChecklistItem,
    ObjectiveBudget,
    ObjectiveContractError,
    ObjectiveDetail,
    ObjectiveErrorCode,
    ObjectiveEvent,
    ObjectiveEventPage,
    ObjectiveListPage,
    ObjectiveRunPage,
    ObjectiveStatus,
    WorkJournal,
)

__all__ = ["register_objectives"]

#: The frozen settled statuses (§3; a settled episode admits no new runs
#: and no further transitions — the store enforces the same set).
_TERMINAL_STATUSES: Final[frozenset[ObjectiveStatus]] = frozenset(
    {
        ObjectiveStatus.COMPLETED,
        ObjectiveStatus.FAILED,
        ObjectiveStatus.CANCELLED,
    }
)

#: SSE pacing (the jobs surface's reviewed cadence and bound).
SSE_REPLAY_BATCH: Final[int] = 200
SSE_POLL_SECONDS: Final[float] = 0.5
SSE_PING_SECONDS: Final[float] = 15.0
SSE_MAX_STREAM_SECONDS: Final[float] = 600.0

_TITLE_MAX_CHARS: Final[int] = 240
_CONSTRAINT_MAX_CHARS: Final[int] = 2_000


# ---------------------------------------------------------------------------
# Wire shapes (transport only; decision truth is the ONE stores)
# ---------------------------------------------------------------------------


class ObjectiveCreateBody(BaseModel):
    """One explicit operator objective episode (§3.1; AC1).

    ``original_request``/``constraints`` persist VERBATIM (projected for
    secret safety, never model-reworded); ``scope_server_ids`` is the
    operator-owned initial authorized set and must contain the
    initiating server. ``client_request_id`` makes the create idempotent:
    same key + same projected content returns the SAME objective;
    changed content is the structured conflict.
    """

    model_config = ConfigDict(extra="forbid")

    server_id: uuid.UUID = Field(description="Initiating (home) server identity")
    original_request: str = Field(
        min_length=1,
        max_length=ORIGINAL_REQUEST_MAX_CHARS,
        description="Verbatim operator request (provenance-carrying)",
    )
    constraints: list[Annotated[str, Field(min_length=1, max_length=_CONSTRAINT_MAX_CHARS)]] = Field(
        default_factory=list,
        max_length=12,
        description="Verbatim operator constraints (bounded list)",
    )
    scope_server_ids: list[uuid.UUID] = Field(
        min_length=1,
        max_length=16,
        description="The explicit operator-owned initial server scope",
    )
    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=_TITLE_MAX_CHARS,
        description="Operator-assigned title when set",
    )
    budget: ObjectiveBudget | None = Field(
        default=None,
        description="Bounded budget caps when set",
    )
    client_request_id: uuid.UUID | None = Field(
        default=None,
        description="Idempotency key deduplicating retransmitted creates",
    )

    @field_validator("original_request", "title")
    @classmethod
    def _not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value


class ObjectiveScopeBody(BaseModel):
    """One operator scope edit under the expected-revision CAS (§3.2; AC2).

    The route is the ONLY scope-change authority surface: it pins
    ``actor_kind=operator``; agents and children have no path here.
    ``add_server_ids``/``remove_server_ids`` are the explicit delta; the
    home server is immortal and the result stays non-empty and bounded
    (the ONE store's ``validate_scope_change`` decides, fail closed).
    """

    model_config = ConfigDict(extra="forbid")

    server_id: uuid.UUID = Field(
        description="Requesting in-scope server identity (C8 scope check)",
    )
    expected_revision: int = Field(
        ge=1,
        description="Expected objective revision; a stale value conflicts",
    )
    add_server_ids: list[uuid.UUID] = Field(
        default_factory=list,
        max_length=16,
        description="Servers the operator admits into the scope",
    )
    remove_server_ids: list[uuid.UUID] = Field(
        default_factory=list,
        max_length=16,
        description="Servers the operator removes from the scope",
    )


class ChecklistPutBody(BaseModel):
    """One advisory checklist replacement under its CAS revision (§8.2).

    The checklist is ADVISORY: no tick grants authority, completes
    anything, or gates a run. Revision ``0`` names the never-maintained
    state (the initial CAS pin); every later write pins the read
    revision.
    """

    model_config = ConfigDict(extra="forbid")

    server_id: uuid.UUID = Field(
        description="Requesting in-scope server identity (C8 scope check)",
    )
    expected_revision: int = Field(
        ge=0,
        description="Expected checklist revision (0 = never maintained)",
    )
    items: list[ChecklistItem] = Field(
        min_length=1,
        max_length=MAX_CHECKLIST_ITEMS,
        description="The full replacement advisory checklist",
    )


class RuntimeDrainCounters(BaseModel):
    """One runtime family's drain inventory (§10.2)."""

    model_config = ConfigDict(extra="forbid")

    runtime: Literal["legacy", "v2"] = Field(
        description="The runtime family the counters describe",
    )
    handler: str = Field(
        description="The family's durable handler identity (value-free label)",
    )
    unsettled: int = Field(
        description="Runs of this family whose replayed state is non-terminal",
    )
    total: int = Field(
        description="Runs of this family recorded in the facade",
    )


class DrainStatusView(BaseModel):
    """The deployment-drain inventory (§10.2; scenario RV2-DEPLOY-DRAIN).

    ``legacy`` counts every facade run NOT bound in the v2-only
    ``objective_runs`` table (the legacy ``agent.run`` roots and their
    ``agent.delegate`` subtrees share the drain window); ``v2`` counts
    the bound runs. ``ready_to_retire_legacy`` is honest derived truth:
    zero unsettled legacy runs — never a guess.
    """

    model_config = ConfigDict(extra="forbid")

    legacy: RuntimeDrainCounters = Field(
        description="Legacy-runtime counters (agent.run family)",
    )
    v2: RuntimeDrainCounters = Field(
        description="Runtime v2 counters (agent.run_v2 bindings)",
    )
    ready_to_retire_legacy: bool = Field(
        description="True when no legacy run remains unsettled",
    )


# ---------------------------------------------------------------------------
# Guards and error mapping (C01 envelope; boundary owned by mai.api.auth)
# ---------------------------------------------------------------------------

_NOT_FOUND_STATUS: Final[int] = HTTP_404_NOT_FOUND
_CONFLICT_CODES: Final[frozenset[str]] = frozenset(
    {
        ObjectiveErrorCode.REVISION_CONFLICT.value,
        ObjectiveErrorCode.OPERATOR_REQUIRED.value,
        ObjectiveErrorCode.SCOPE_VIOLATION.value,
        ObjectiveErrorCode.INVALID_STATUS_TRANSITION.value,
        ObjectiveErrorCode.LEGACY_RUN_NOT_CONVERTIBLE.value,
        ObjectiveErrorCode.INVALID_SCOPE_CHANGE.value,
        ObjectiveErrorCode.RUN_NOT_IN_SCOPE.value,
        ObjectiveErrorCode.CLIENT_REQUEST_CONFLICT.value,
    }
)


def _objective_error(request: Request, exc: ObjectiveContractError) -> Response:
    """Map the ONE store's closed rejections onto the C01 envelope.

    ``objective_not_found`` is the scope-blind 404 (existence never
    leaks, C8); state conflicts are 409 with the stable vocabulary;
    malformed payloads are 422. Every message is value-free by contract.
    """
    fields = exc.fields
    codes = set(fields.values())
    if ObjectiveErrorCode.OBJECTIVE_NOT_FOUND.value in codes:
        return _error(
            request,
            _NOT_FOUND_STATUS,
            ObjectiveErrorCode.OBJECTIVE_NOT_FOUND.value,
            "no objective in the requesting server scope",
            fields=fields,
        )
    if codes & _CONFLICT_CODES:
        return _error(
            request,
            HTTP_409_CONFLICT,
            next(iter(codes & _CONFLICT_CODES)),
            "objective contract rejected",
            fields=fields,
        )
    return _error(
        request,
        HTTP_422_UNPROCESSABLE_CONTENT,
        next(iter(codes)) if codes else "invalid_payload",
        "objective contract rejected",
        fields=fields,
    )


_SERVER_ID_QUERY = Query(
    ...,
    description="Requesting in-scope server identity (C8 scope check); "
    "other scopes' objectives are invisible, not forbidden",
)


# ---------------------------------------------------------------------------
# Drain inventory (§10.2; AC5)
# ---------------------------------------------------------------------------


async def _drain_status(request: Request) -> DrainStatusView:
    """Compute the honest drain inventory from app-owned records.

    One pass: the facade's run identities, each run's LATEST replayed
    transition target (``DISTINCT ON`` over the facade's own event log —
    no engine tables), and the v2-only ``objective_runs`` bindings. A run
    with no transition row is not yet terminal (it may never have
    started): counted unsettled, never guessed settled.
    """
    factory = _sessions(request)
    async with factory() as session:
        bound_rows = (
            await session.execute(text("SELECT run_id FROM objective_runs"))
        ).scalars().all()
        bound = {uuid.UUID(str(value)) for value in bound_rows}
        transition_rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT ON (run_id) run_id, data->>'to' AS target "
                    "FROM job_events WHERE type = 'run.transition' "
                    "ORDER BY run_id, seq DESC"
                )
            )
        ).mappings().all()
        terminal: set[uuid.UUID] = set()
        for row in transition_rows:
            target = row["target"]
            if target in ("succeeded", "failed", "cancelled"):
                terminal.add(uuid.UUID(str(row["run_id"])))
        total_rows = (
            await session.execute(text("SELECT run_id FROM job_runs"))
        ).scalars().all()
    all_runs = {uuid.UUID(str(value)) for value in total_rows}
    v2_runs = all_runs & bound
    legacy_runs = all_runs - bound
    v2_unsettled = len(v2_runs - terminal)
    legacy_unsettled = len(legacy_runs - terminal)
    return DrainStatusView(
        legacy=RuntimeDrainCounters(
            runtime="legacy",
            handler="agent.run",
            unsettled=legacy_unsettled,
            total=len(legacy_runs),
        ),
        v2=RuntimeDrainCounters(
            runtime="v2",
            handler=V2_HANDLER_NAME,
            unsettled=v2_unsettled,
            total=len(v2_runs),
        ),
        ready_to_retire_legacy=legacy_unsettled == 0,
    )


# ---------------------------------------------------------------------------
# Routes (C01 prefix/envelope; C08 boundary)
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/objectives", tags=["objectives"])


@router.get(
    "/drain-status",
    status_code=HTTP_200_OK,
    response_model=DrainStatusView,
    summary="Legacy drain inventory: unsettled legacy runs vs v2 runs (§10.2)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Persistence unavailable",
        },
    },
)
async def read_drain_status(request: Request) -> DrainStatusView:
    """The deployment-drain inventory (scenario RV2-DEPLOY-DRAIN).

    Safe method: session layer only. The counters derive ONLY from
    app-owned records (``job_runs``/``job_events``/``objective_runs``);
    the legacy/v2 split is the ``objective_runs`` binding itself (the
    DB CHECK admits only ``agent.run_v2``), so an unsettled legacy run
    can never masquerade as drained.
    """
    await _require_session(request)
    return await _drain_status(request)


@router.post(
    "",
    status_code=HTTP_201_CREATED,
    response_model=ObjectiveDetail,
    summary="Create one objective episode (operator authority; §3.1)",
    responses={
        HTTP_200_OK: {
            "model": ObjectiveDetail,
            "description": "Idempotent retransmission: the SAME objective "
            "returned for the same client request id and content",
        },
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowlisted or CSRF token missing/mismatched",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "The client request id was used with changed "
            "content, or the scope payload violates the frozen bounds",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Persistence unavailable",
        },
    },
)
async def create_objective(
    request: Request, payload: ObjectiveCreateBody
) -> Response:
    """Create one objective episode (§3.1; AC1/AC2).

    Operator authority only: the route runs behind the full session →
    origin → CSRF boundary and pins ``ActorKind.OPERATOR`` — there is no
    agent-reachable create path. (The canonical chat admission also
    starts an episode AUTOMATICALLY for a turn with no explicit or bound
    objective — the direct-agent default — through the same store seams
    and under the same submit-time operator authority; it never widens
    scope or authority beyond the selected server.) The ONE store
    persists the verbatim request/constraints (projected for secret
    safety), the initial scope, and the ``objective.created`` ledger
    event in ONE transaction.
    """
    _store, _token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    factory = _sessions(request)
    dedup = False
    if payload.client_request_id is not None:
        async with factory() as session:
            existing = (
                await session.execute(
                    text(
                        "SELECT objective_id FROM objectives "
                        "WHERE server_id = :sid AND client_request_id = :crid"
                    ),
                    {
                        "sid": payload.server_id,
                        "crid": payload.client_request_id,
                    },
                )
            ).first()
        dedup = existing is not None
    try:
        detail = await acreate_objective(
            factory,
            server_id=payload.server_id,
            original_request=payload.original_request,
            constraints=list(payload.constraints),
            scope_server_ids=list(payload.scope_server_ids),
            title=payload.title,
            budget=payload.budget,
            actor_kind=ActorKind.OPERATOR,
            client_request_id=payload.client_request_id,
        )
    except ObjectiveContractError as exc:
        return _objective_error(request, exc)
    except ValueError as exc:
        return _error(
            request,
            HTTP_422_UNPROCESSABLE_CONTENT,
            ObjectiveErrorCode.INVALID_PAYLOAD.value,
            "objective payload rejected",
            fields={"payload": ObjectiveErrorCode.INVALID_PAYLOAD.value},
        )
    body = detail.model_dump_json()
    if dedup:
        return Response(
            status_code=HTTP_200_OK, content=body, media_type="application/json"
        )
    return Response(
        status_code=HTTP_201_CREATED, content=body, media_type="application/json"
    )


@router.get(
    "",
    status_code=HTTP_200_OK,
    response_model=ObjectiveListPage,
    summary="One bounded page of in-scope objectives (AGENTS.md pagination)",
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
async def list_objectives(
    request: Request,
    server_id: Annotated[uuid.UUID, _SERVER_ID_QUERY],
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
    status: ObjectiveStatus | None = Query(default=None),
) -> ObjectiveListPage:
    """One bounded page of safe objective summaries (§3 ``GET`` list).

    The page is exactly the requested server's scope (a page row's scope
    contains ``server_id``); ordering is ``updated_at DESC,
    objective_id`` with the same-filter ``COUNT(*)`` and derived
    ``has_more``. Rows carry safe metadata only — no request text.
    """
    await _require_session(request)
    factory = _sessions(request)
    try:
        return await alist_objectives(
            factory,
            server_id=server_id,
            limit=limit,
            offset=offset,
            status=status,
        )
    except (ObjectiveContractError, ValueError) as exc:
        return _error(
            request,
            HTTP_422_UNPROCESSABLE_CONTENT,
            ObjectiveErrorCode.INVALID_PAYLOAD.value,
            "objective list rejected",
            fields={"page": ObjectiveErrorCode.INVALID_PAYLOAD.value},
        )


@router.get(
    "/{objective_id}",
    status_code=HTTP_200_OK,
    response_model=ObjectiveDetail,
    summary="One scope-checked objective read (§3)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No objective in the requesting server scope",
        },
    },
)
async def read_objective(
    request: Request,
    objective_id: uuid.UUID,
    server_id: Annotated[uuid.UUID, _SERVER_ID_QUERY],
) -> ObjectiveDetail | Response:
    """The scope-checked singleton read (§3; C8 fail closed).

    A foreign-scope reference is the scope-blind
    ``404 objective_not_found`` — the verbatim episode (request,
    constraints, scope membership) is visible only inside its scope.
    """
    await _require_session(request)
    factory = _sessions(request)
    try:
        return await aget_objective(factory, objective_id, server_id=server_id)
    except ObjectiveContractError as exc:
        return _objective_error(request, exc)


@router.get(
    "/{objective_id}/journal",
    status_code=HTTP_200_OK,
    response_model=WorkJournal,
    summary="The derived work journal (§8.1; trusted records only)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No objective in the requesting server scope",
        },
    },
)
async def read_journal(
    request: Request,
    objective_id: uuid.UUID,
    server_id: Annotated[uuid.UUID, _SERVER_ID_QUERY],
) -> WorkJournal | Response:
    """The bounded work-journal projection (§8.1).

    A fixed-shape read of the ledger fold: verbatim goal/constraints,
    action attempts, receipts, verification verdicts, unknown effects,
    delegation links — with explicit truncation flags. The cache
    catches up honestly over any ledger tail before answering (§8.4).
    """
    await _require_session(request)
    factory = _sessions(request)
    try:
        return await aread_journal(factory, objective_id, server_id=server_id)
    except ObjectiveContractError as exc:
        return _objective_error(request, exc)


@router.get(
    "/{objective_id}/events",
    status_code=HTTP_200_OK,
    response_model=ObjectiveEventPage,
    summary="One bounded objective-ledger page (§5.2 pagination)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No objective in the requesting server scope",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Query parameters failed validation",
        },
    },
)
async def list_objective_events(
    request: Request,
    objective_id: uuid.UUID,
    server_id: Annotated[uuid.UUID, _SERVER_ID_QUERY],
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
    type: str | None = Query(default=None),
    after_seq: int | None = Query(default=None, ge=0),
) -> ObjectiveEventPage | Response:
    """One deterministic ledger page (§5.2; AC3).

    Ordered by ``sequence`` with the same-filter ``total``; ``type``
    filters inside the ``objective.*`` family only; ``after_seq`` is the
    replay cursor returning strictly-newer rows. Payloads are the
    projected value-free documents (redaction and bounds applied at
    WRITE; this read never re-bounds silently).
    """
    await _require_session(request)
    factory = _sessions(request)
    try:
        return await alist_events(
            factory,
            objective_id,
            server_id=server_id,
            limit=limit,
            offset=offset,
            type=type,
            after_seq=after_seq,
        )
    except ObjectiveContractError as exc:
        return _objective_error(request, exc)
    except ValueError as exc:
        return _error(
            request,
            HTTP_422_UNPROCESSABLE_CONTENT,
            ObjectiveErrorCode.INVALID_PAYLOAD.value,
            "ledger page rejected",
            fields={"page": ObjectiveErrorCode.INVALID_PAYLOAD.value},
        )


@router.get(
    "/{objective_id}/events/stream",
    status_code=HTTP_200_OK,
    response_model=None,
    summary="Subscribe to the objective activity ledger (SSE, replay cursor)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session (answered before any stream byte)",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "An Origin header that is not allowlisted",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No objective in the requesting server scope",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Replay cursor is not a non-negative integer",
        },
    },
)
async def stream_objective_events(
    request: Request,
    objective_id: uuid.UUID,
    server_id: Annotated[uuid.UUID, _SERVER_ID_QUERY],
    after_seq: int | None = Query(default=None, ge=0),
) -> Response:
    """SSE subscription over the ``objective.*`` ledger (§5.2; AC3).

    The replay cursor is ``after_seq`` (query) or the standard SSE
    ``Last-Event-ID`` header (the query wins when both are present);
    absent means "from the beginning". Frames are strictly
    cursor-exclusive and ordered by ``seq``, so a reconnect with the
    last seen ``id`` deduplicates by construction. Authorization
    completes BEFORE the response starts; the stream is read-only and
    bounded — it self-closes once a settled episode is drained past the
    cursor (a settled objective can never emit another event).
    """
    await _require_session(request)  # 401 before any stream byte
    origin = request.headers.get("origin")
    if origin and origin not in request.app.state.public_origins:
        raise BoundaryError(
            HTTP_403_FORBIDDEN, "origin_rejected", "request origin is not allowed"
        )
    factory = _sessions(request)
    # Absent cursors replay from the run's beginning (the jobs surface's
    # reviewed semantics); a present ``after_seq`` query wins over the
    # ``Last-Event-ID`` header.
    cursor = 0
    if after_seq is not None:
        cursor = after_seq
    else:
        raw_cursor = request.headers.get("last-event-id")
        if raw_cursor not in (None, ""):
            try:
                cursor = int(raw_cursor)
            except ValueError:
                cursor = -1
            if cursor < 0:
                return _error(
                    request,
                    HTTP_422_UNPROCESSABLE_CONTENT,
                    "invalid_cursor",
                    "last-event-id must be a non-negative integer",
                    fields={"last-event-id": "invalid_cursor"},
                )
    try:
        # Authorization completes BEFORE the response starts: the
        # scope-check pins the stream (and validates the objective).
        await alatest_seq(factory, objective_id, server_id=server_id)
    except ObjectiveContractError as exc:
        return _objective_error(request, exc)
    return StreamingResponse(
        _objective_event_stream(factory, objective_id, server_id, cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


async def _objective_event_batch(
    session, objective_id: uuid.UUID, server_id: uuid.UUID, after_seq: int
) -> list[ObjectiveEvent]:
    """The next strictly-newer ledger batch, scope-verified, typed.

    The scope predicate rides INSIDE the read (an operator scope removal
    ends the stream honestly instead of leaking events past the
    revocation), and every row re-validates against the frozen
    ``ObjectiveEvent`` shape — drift fails the stream, never leaks.
    """
    rows = (
        (
            await session.execute(
                text(
                    "SELECT e.event_id, e.objective_id, e.seq, e.run_id, e.ts, "
                    "e.type, e.actor_kind, e.v, e.data "
                    "FROM objective_events e "
                    "WHERE e.objective_id = :oid AND e.seq > :c "
                    "AND EXISTS (SELECT 1 FROM objective_servers s "
                    "WHERE s.objective_id = e.objective_id "
                    "AND s.server_id = :sid) "
                    "ORDER BY e.seq LIMIT :b"
                ),
                {
                    "oid": objective_id,
                    "c": after_seq,
                    "sid": server_id,
                    "b": SSE_REPLAY_BATCH,
                },
            )
        )
        .mappings()
        .all()
    )
    events: list[ObjectiveEvent] = []
    for row in rows:
        events.append(
            ObjectiveEvent(
                event_id=row["event_id"],
                objective_id=row["objective_id"],
                sequence=int(row["seq"]),
                ts=_wire_ts(row["ts"]),
                type=str(row["type"]),
                actor_kind=ActorKind(str(row["actor_kind"])),
                v=int(row["v"]),
                data=dict(row["data"]),
                run_id=row["run_id"],
            )
        )
    return events


async def _objective_status(session, objective_id: uuid.UUID) -> ObjectiveStatus | None:
    """The objective's current status (``None`` when the row vanished)."""
    row = (
        await session.execute(
            text("SELECT status FROM objectives WHERE objective_id = :oid"),
            {"oid": objective_id},
        )
    ).first()
    return None if row is None else ObjectiveStatus(str(row[0]))


async def _objective_event_stream(
    factory,
    objective_id: uuid.UUID,
    server_id: uuid.UUID,
    cursor: int,
) -> AsyncIterator[str]:
    """The SSE body: cursor-exclusive replay, then tail, then self-close.

    A settled objective stops emitting (terminal states have no
    out-edges), so a drained settled stream closes itself; an active
    episode streams until its bounded deadline and the client reconnects
    from its cursor. A scope revocation starves the batch predicate and
    the status probe — the stream ends rather than leaking.
    """
    import time

    loop = asyncio.get_running_loop()
    deadline = loop.time() + SSE_MAX_STREAM_SECONDS
    last_frame = loop.time()
    while True:
        async with factory() as session:
            batch = await _objective_event_batch(
                session, objective_id, server_id, cursor
            )
            status = await _objective_status(session, objective_id)
        for event in batch:
            yield (
                f"id: {event.sequence}\n"
                f"data: {json.dumps(_wire_event(event), separators=(',', ':'))}\n\n"
            )
            cursor = event.sequence
            last_frame = loop.time()
        if batch:
            continue  # drain bursts before polling again
        if status is None or status in _TERMINAL_STATUSES:
            return  # settled (or vanished): nothing new can arrive
        now = loop.time()
        if now >= deadline:
            return  # bounded stream; the client reconnects from its cursor
        if now - last_frame >= SSE_PING_SECONDS:
            yield ": ping\n\n"  # keepalive comment; consumers ignore it
            last_frame = now
        await asyncio.sleep(SSE_POLL_SECONDS)


def _wire_event(event: ObjectiveEvent) -> dict[str, object]:
    """The frame document: the frozen envelope plus §4 provenance."""
    return {
        "event_id": str(event.event_id),
        "objective_id": str(event.objective_id),
        "sequence": event.sequence,
        "ts": event.ts if isinstance(event.ts, str) else _wire_ts(event.ts),
        "type": event.type,
        "actor_kind": event.actor_kind.value,
        "v": event.v,
        "data": event.data,
        **({"run_id": str(event.run_id)} if event.run_id is not None else {}),
    }


@router.get(
    "/{objective_id}/runs",
    status_code=HTTP_200_OK,
    response_model=ObjectiveRunPage,
    summary="One bounded page of the objective's run bindings (§3.3)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No objective in the requesting server scope",
        },
    },
)
async def list_objective_runs(
    request: Request,
    objective_id: uuid.UUID,
    server_id: Annotated[uuid.UUID, _SERVER_ID_QUERY],
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> ObjectiveRunPage | Response:
    """The objective's durable run/child bindings (§3.3; AC1).

    Every binding names a run of the frozen ``agent.run_v2`` handler (the
    DB CHECK is the §10 backstop), its initiating server and actor
    provenance — the truth the dual-ownership guarantee reports against.
    """
    await _require_session(request)
    factory = _sessions(request)
    try:
        return await alist_objective_runs(
            factory,
            objective_id,
            server_id=server_id,
            limit=limit,
            offset=offset,
        )
    except ObjectiveContractError as exc:
        return _objective_error(request, exc)


@router.post(
    "/{objective_id}/scope",
    status_code=HTTP_200_OK,
    response_model=ObjectiveDetail,
    summary="Operator scope edit under the expected-revision CAS (§3.2; AC2)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowlisted or CSRF token missing/mismatched",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No objective in the requesting server scope",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "A stale expected revision, a home-server removal, "
            "an add/remove overlap, or an empty/oversized result",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation",
        },
    },
)
async def change_objective_scope(
    request: Request,
    objective_id: uuid.UUID,
    payload: ObjectiveScopeBody,
) -> ObjectiveDetail | Response:
    """Expand or shrink the objective scope (§3.2; AC2/AC5).

    The ONLY scope-change authority surface: the authenticated operator
    session pins ``ActorKind.OPERATOR`` — no agent, child or model call
    can reach this route. The ONE store applies the delta under the row
    lock + CAS and appends ``objective.scope_changed`` with the exact
    effective delta in the SAME transaction; a rejected edit changes
    nothing.
    """
    _store, _token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    factory = _sessions(request)
    try:
        return await achange_scope(
            factory,
            objective_id,
            server_id=payload.server_id,
            expected_revision=payload.expected_revision,
            add_server_ids=list(payload.add_server_ids),
            remove_server_ids=list(payload.remove_server_ids),
            actor_kind=ActorKind.OPERATOR,
        )
    except ObjectiveContractError as exc:
        return _objective_error(request, exc)


@router.get(
    "/{objective_id}/checklist",
    status_code=HTTP_200_OK,
    response_model=None,
    summary="The advisory checklist projection (§8.2)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No objective in the requesting server scope, or "
            "the advisory checklist was never maintained",
        },
    },
)
async def read_checklist(
    request: Request,
    objective_id: uuid.UUID,
    server_id: Annotated[uuid.UUID, _SERVER_ID_QUERY],
) -> Response:
    """The bounded advisory checklist (§8.2; never authority).

    A never-maintained checklist answers ``404 checklist_absent`` — the
    honest absence marker, never a fabricated empty document.
    """
    await _require_session(request)
    factory = _sessions(request)
    try:
        document = await aread_checklist(factory, objective_id, server_id=server_id)
    except ObjectiveContractError as exc:
        return _objective_error(request, exc)
    if document is None:
        return _error(
            request,
            HTTP_404_NOT_FOUND,
            "checklist_absent",
            "the advisory checklist was never maintained for this objective",
            fields={"checklist": "checklist_absent"},
        )
    return Response(
        status_code=HTTP_200_OK,
        content=document.model_dump_json(),
        media_type="application/json",
    )


@router.put(
    "/{objective_id}/checklist",
    status_code=HTTP_200_OK,
    response_model=None,
    summary="Replace the advisory checklist under its CAS revision (§8.2)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowlisted or CSRF token missing/mismatched",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No objective in the requesting server scope",
        },
        HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "A stale expected checklist revision",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation",
        },
    },
)
async def put_checklist(
    request: Request,
    objective_id: uuid.UUID,
    payload: ChecklistPutBody,
) -> Response:
    """CAS-replace the advisory checklist (§8.2; §11).

    Operator authority only. The write appends
    ``objective.checklist_updated`` and bumps the revision in ONE
    transaction; a stale revision changes nothing (``409
    revision_conflict``). The checklist stays advisory: no tick grants
    authority or completes anything.
    """
    _store, _token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    factory = _sessions(request)
    try:
        document = await aput_checklist(
            factory,
            objective_id,
            server_id=payload.server_id,
            items=list(payload.items),
            expected_revision=payload.expected_revision,
            actor_kind=ActorKind.OPERATOR,
        )
    except ObjectiveContractError as exc:
        return _objective_error(request, exc)
    except ValueError as exc:
        return _error(
            request,
            HTTP_422_UNPROCESSABLE_CONTENT,
            ObjectiveErrorCode.INVALID_PAYLOAD.value,
            "checklist payload rejected",
            fields={"items": ObjectiveErrorCode.INVALID_PAYLOAD.value},
        )
    return Response(
        status_code=HTTP_200_OK,
        content=document.model_dump_json(),
        media_type="application/json",
    )


def register_objectives(app) -> None:
    """Register the objective surface under the shared boundary (C01/C08)."""
    app.include_router(router)
