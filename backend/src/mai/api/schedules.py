"""Schedules HTTP API: versioned scheduled-job configuration (C05/C08).

Ownership map (contracts.md **C05 rule 8** schedules, **C08** UI/API and
auth, **C01** envelope/identity/scope; SERVICES ``mai-api`` row; producer
``05-schedules`` (the durable facade this surface composes), sibling
``05-jobs-api``; consumers ``05-web-jobs``/``05-web-execution``):

- **HTTP persists configuration only** (acceptance 1). Create, pause/
  resume and delete go through the ONE reviewed facade
  (:class:`mai.jobs.schedules.ScheduleManager`, a control-plane
  ``DBOSClient`` — never a launched engine). The engine trigger stays
  derived from the domain row inside the facade's one transaction;
  firing itself is engine-owned and happens in the worker: no request
  ever dispatches, executes or catches up a fire (C05 rule 8 frozen
  misfire policy: skip missed, resume from now, no catch-up).
- **Update is pause/resume only** (facade ``set_enabled``). The schedule
  definition (cron, timezone, run_spec) is frozen at create time —
  mirroring the facade, which exposes no definition mutation; the
  available path is delete + re-create. The strict body accepts exactly
  ``{"enabled": bool}``; any definition key is a structured 422, never a
  silent rewrite of a frozen definition.
- **Target binding** (C01 rule 7, acceptance 2). Create names the target
  ``server_id`` explicitly and binds to an EXISTING canonical server —
  an unknown target is ``404 not_found``, exactly like job submit. The
  surface carries NO grant vocabulary at all: a schedule never expands
  grants for its later jobs — every fire's run re-checks the target
  server's configured policy/grants at execution (G2), and the body is
  schema-strict so no grant-shaped rider can be smuggled in.
- **Reads are honest and bounded** (C01 rule 5). List is a bounded,
  optionally server-scoped page; the fire ledger view is a bounded page
  over the facade-owned ``job_schedule_fires`` dedup ledger (the storage
  level of the C05 rule 8 exactly-once guarantee). ``run_id`` on a fire
  is the recorded link — ``null`` until the run-row owner fills it — and
  ``planned_run_id`` is the facade's deterministic single planned-fire
  identity (:func:`mai.jobs.schedules.planned_run_id`).
- **Failure mapping** is the C01 envelope everywhere: facade rejections
  keep their stable field-scoped ``validation_failed`` codes, unknown
  identities are ``404 not_found``, an unusable facade/engine dependency
  fails closed with ``503 unavailable`` — never a guessed success.

Route/doc listeners stay off (G1 minimal unauthenticated surface); the
schema is produced in-process via ``app.openapi()`` for the central
exporter owned by the ``openapi-client`` resource.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Final

from dbos import error as dbos_error
from fastapi import APIRouter, FastAPI, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.status import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_204_NO_CONTENT,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_500_INTERNAL_SERVER_ERROR,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from mai.api.auth import (
    BoundaryError,
    ErrorEnvelope,
    _require_allowed_origin,
    _require_csrf,
    _require_session,
    render_error_envelope,
)
from mai.api.jobs import _canonical_profile
from mai.jobs.schedules import (
    ScheduleError,
    ScheduleErrorCode,
    ScheduleManager,
    ScheduleRecord,
    planned_run_id,
)

__all__ = ["register_schedules"]

# ---------------------------------------------------------------------------
# Transport bounds (module-owned policy, explicitly bounded)
# ---------------------------------------------------------------------------

#: List page bounds (C01 rule 5: list endpoints are bounded/paginated).
DEFAULT_PAGE_LIMIT: Final[int] = 50
MAX_PAGE_LIMIT: Final[int] = 200

#: Schedule identity is a canonical UUID path parameter (C01 rule 1);
#: a malformed one is field-scoped validation, never a route miss.
_SCHEDULE_FIELD: Final[str] = "schedule_id"


# ---------------------------------------------------------------------------
# Wire shapes (transport only; truth is the facade producers)
# ---------------------------------------------------------------------------


class RunSpecBody(BaseModel):
    """The run intent template applied per fire (references only, C05 r3).

    Exactly the facade's ``run_spec`` shape: a registered handler name
    and its payload object. Secret VALUES are the submission boundary's
    problem (C03 rule 6) and never belong here; the payload is persisted
    canonically by the facade.
    """

    model_config = ConfigDict(extra="forbid")

    handler_name: str
    payload: dict[str, Any]


class ScheduleCreateBody(BaseModel):
    """Create body (strict: unknown fields are rejected, value-free 422).

    ``cron_timezone`` absent applies the frozen UTC default EXPLICITLY
    inside the facade (engine rule 7 — the default never lives in the
    wire layer). ``misfire_policy`` is deliberately not a field: the
    single frozen value ``skip_missed`` is pinned by the facade/storage.
    """

    model_config = ConfigDict(extra="forbid")

    server_id: str
    cron: str
    run_spec: RunSpecBody
    cron_timezone: str | None = None


class ScheduleUpdateBody(BaseModel):
    """Update body: pause/resume ONLY (the definition is frozen).

    Exactly ``{"enabled": bool}``. Attempts to change cron/timezone/
    run_spec are rejected by the strict schema — a frozen definition has
    no in-place mutation (delete + re-create is the available path), so
    a conflicting update can never half-apply.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class RunSpecView(BaseModel):
    """The persisted run intent template (facade-typed, references only)."""

    handler_name: str
    payload: dict[str, Any]


class ScheduleView(BaseModel):
    """One persisted schedule record (the facade-typed ``job_schedules`` row).

    ``misfire_policy`` is always the frozen ``skip_missed`` value — the
    facade fails closed on any drift, so the wire can state it plainly.
    """

    schedule_id: str
    server_id: str
    cron: str
    cron_timezone: str
    misfire_policy: str
    enabled: bool
    run_spec: RunSpecView
    created_at: str
    updated_at: str


class SchedulePage(BaseModel):
    """One bounded, optionally server-scoped list page (C01 rule 5/7)."""

    schedules: list[ScheduleView]
    limit: int
    offset: int
    total: int


class FireView(BaseModel):
    """One dispatched fire from the dedup ledger (C05 rule 8).

    ``run_id`` is the ledger's recorded link — ``null`` while the fire's
    run has no ``job_runs`` row yet (the deterministic identity below IS
    the run intent's identity). ``planned_run_id`` is the facade's
    restart-stable planned-fire identity; identical across replays and
    duplicate pollers, so a consumer can never count one fire twice.
    """

    schedule_id: str
    planned_fire_time: str
    fired_at: str
    run_id: str | None
    planned_run_id: str


class FirePage(BaseModel):
    """One bounded fire-ledger page for one explicitly named schedule."""

    schedule_id: str
    fires: list[FireView]
    limit: int
    offset: int
    total: int


# ---------------------------------------------------------------------------
# Facade access + small helpers
# ---------------------------------------------------------------------------


def _schedules_manager(request: Request) -> ScheduleManager:
    """The lifespan-composed facade; without it fail closed."""
    manager = getattr(request.app.state, "schedules", None)
    if manager is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "schedules service is unavailable",
        )
    return manager


def _sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    """The app-side session factory (present whenever the facade is)."""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "schedules service is unavailable",
        )
    return factory


#: Facade rejection code → (HTTP status, envelope code, wire field).
#: Facade messages are already value-free (C01 rule 3) and travel as-is.
_ERROR_MAP: Final[dict[ScheduleErrorCode, tuple[int, str, str]]] = {
    ScheduleErrorCode.INVALID_SERVER_ID: (
        HTTP_422_UNPROCESSABLE_CONTENT,
        "validation_failed",
        "server_id",
    ),
    ScheduleErrorCode.INVALID_SCHEDULE_ID: (
        HTTP_422_UNPROCESSABLE_CONTENT,
        "validation_failed",
        _SCHEDULE_FIELD,
    ),
    ScheduleErrorCode.INVALID_FIRE_TIME: (
        HTTP_422_UNPROCESSABLE_CONTENT,
        "validation_failed",
        "planned_fire_time",
    ),
    ScheduleErrorCode.INVALID_CRON: (
        HTTP_422_UNPROCESSABLE_CONTENT,
        "validation_failed",
        "cron",
    ),
    ScheduleErrorCode.INVALID_TIMEZONE: (
        HTTP_422_UNPROCESSABLE_CONTENT,
        "validation_failed",
        "cron_timezone",
    ),
    ScheduleErrorCode.INVALID_RUN_SPEC: (
        HTTP_422_UNPROCESSABLE_CONTENT,
        "validation_failed",
        "run_spec",
    ),
    ScheduleErrorCode.SCHEDULE_NOT_FOUND: (
        HTTP_404_NOT_FOUND,
        "not_found",
        _SCHEDULE_FIELD,
    ),
}


def _facade_error(request: Request, error: ScheduleError) -> Response:
    """Map one value-free facade rejection onto the C01 envelope."""
    mapped = _ERROR_MAP.get(error.code)
    if mapped is not None:
        status, code, field = mapped
        return render_error_envelope(
            request, status, code, error.detail, fields={field: "invalid"}
        )
    if error.code is ScheduleErrorCode.MISFIRE_POLICY_DRIFT:
        # Persisted state outside the frozen vocabulary: honest internal
        # failure, surfaced value-free — never silently repaired.
        return render_error_envelope(
            request,
            HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "persisted schedule carries an out-of-contract policy",
        )
    # ENGINE_REJECTED (and any future facade code): the engine dependency
    # refused/failed — fail closed instead of guessing an outcome.
    return render_error_envelope(
        request,
        HTTP_503_SERVICE_UNAVAILABLE,
        "unavailable",
        "schedule engine is unavailable",
    )


async def _apply(
    request: Request,
    operation: Callable[..., Awaitable[Response]],
    *args: object,
    **kwargs: object,
) -> Response:
    """Run one facade call, mapping dependency failures to the envelope.

    ``DBOSException``/``OSError`` here mean the engine/system dependency
    is unusable (the facade's own value-free rejections are
    :class:`ScheduleError` and mapped above) — never a guessed success.
    """
    try:
        return await operation(*args, **kwargs)
    except ScheduleError as error:
        return _facade_error(request, error)
    except dbos_error.DBOSException, OSError:
        return render_error_envelope(
            request,
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "schedule engine is unavailable",
        )


def _schedule_view(record: ScheduleRecord) -> ScheduleView:
    """Type one facade record into the wire view (frozen ``…Z`` times)."""
    return ScheduleView(
        schedule_id=record.schedule_id,
        server_id=record.server_id,
        cron=record.cron,
        cron_timezone=record.cron_timezone,
        misfire_policy=record.misfire_policy,
        enabled=record.enabled,
        run_spec=RunSpecView(
            handler_name=record.run_spec.handler_name,
            payload=dict(record.run_spec.payload),
        ),
        created_at=_wire_ts(record.created_at),
        updated_at=_wire_ts(record.updated_at),
    )


def _wire_ts(value: datetime) -> str:
    """Render one stored UTC timestamp in the frozen ``…Z`` wire form."""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Routes (frozen design targets; mutation order: session → origin → CSRF)
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/schedules", tags=["schedules"])


@router.post(
    "",
    status_code=HTTP_201_CREATED,
    response_model=ScheduleView,
    summary="Create a schedule (durable configuration, engine trigger derived)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowlisted or CSRF token mismatch",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No canonical profile stored for the target server id",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Schedules facade or canonical profile service unavailable",
        },
    },
)
async def create_schedule(request: Request, payload: ScheduleCreateBody) -> Response:
    """Persist one schedule record and derive its engine trigger.

    Mutation: session → origin → CSRF (ADR §5 order) before any effect.
    The target is bound to an EXISTING canonical server (C01 rule 7);
    the facade validates cron against the frozen field contract AND the
    engine's own grammar, applies the explicit timezone (default UTC),
    pins the frozen misfire policy and creates row + trigger inside one
    transaction — engine rejection rolls the row back.
    """
    _store, _token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    manager = _schedules_manager(request)
    try:
        server_id = str(uuid.UUID(payload.server_id))
    except ValueError, AttributeError:
        return render_error_envelope(
            request,
            HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_failed",
            "request body failed validation",
            fields={"server_id": "invalid"},
        )
    # Target binding (acceptance 2): an unauthorized/unknown target is
    # denied before any schedule row exists (same boundary as job submit).
    _canonical_profile(request, uuid.UUID(server_id))
    return await _apply(
        request,
        _create_record,
        manager,
        server_id=server_id,
        cron=payload.cron,
        cron_timezone=payload.cron_timezone,
        run_spec=payload.run_spec.model_dump(),
    )


async def _create_record(
    manager: ScheduleManager,
    *,
    server_id: str,
    cron: str,
    cron_timezone: str | None,
    run_spec: Mapping[str, object],
) -> ScheduleView:
    record = await manager.create(
        server_id=server_id,
        cron=cron,
        cron_timezone=cron_timezone,
        run_spec=run_spec,
    )
    return _schedule_view(record)


@router.get(
    "",
    status_code=HTTP_200_OK,
    response_model=SchedulePage,
    summary="List schedules (bounded page, optional explicit server scope)",
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
async def list_schedules(
    request: Request,
    server_id: uuid.UUID | None = None,
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> Response:
    """One bounded page of persisted schedules (C01 rule 5).

    Safe method: session layer only. The server scope is optional but
    EXPLICIT when present (C01 rule 7) — an unfiltered page is the
    single-admin control-plane view, the scoped one never leaks another
    server's schedules into a scoped consumer.
    """
    await _require_session(request)
    manager = _schedules_manager(request)
    scope = None if server_id is None else str(server_id)
    records = await manager.list(scope)
    total = len(records)
    page = records[offset : offset + limit]
    return SchedulePage(
        schedules=[_schedule_view(record) for record in page],
        limit=limit,
        offset=offset,
        total=total,
    )


@router.get(
    "/{schedule_id}",
    status_code=HTTP_200_OK,
    response_model=ScheduleView,
    summary="Read one persisted schedule record",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No schedule stored for this schedule id",
        },
    },
)
async def read_schedule(request: Request, schedule_id: uuid.UUID) -> Response:
    """One schedule's durable record; truth is the facade row, never the engine."""
    await _require_session(request)
    manager = _schedules_manager(request)
    return await _apply(request, _read_record, manager, str(schedule_id))


async def _read_record(manager: ScheduleManager, schedule_id: str) -> ScheduleView:
    record = await manager.get(schedule_id)
    if record is None:
        raise ScheduleError(
            ScheduleErrorCode.SCHEDULE_NOT_FOUND,
            "no schedule record for this schedule_id",
        )
    return _schedule_view(record)


@router.patch(
    "/{schedule_id}",
    status_code=HTTP_200_OK,
    response_model=ScheduleView,
    summary="Update a schedule: pause/resume ONLY (the definition is frozen)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowlisted or CSRF token mismatch",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No schedule stored for this schedule id",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Request body failed validation (definition keys "
            "are rejected — delete + re-create is the definition path)",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Schedules facade or engine unavailable",
        },
    },
)
async def update_schedule(
    request: Request, schedule_id: uuid.UUID, payload: ScheduleUpdateBody
) -> Response:
    """Pause/resume the schedule (facade ``set_enabled``).

    Mutation: session → origin → CSRF (ADR §5 order) before any effect.
    The engine trigger toggles first (fail fast, nothing written on
    rejection); the domain ``enabled`` flag stays the dispatch-time
    authority, so a paused schedule never dispatches even if its trigger
    fires once more before the engine notices (acceptance 2/feature
    scenario: pause across due time skips the missed fire — no ledger
    row, no run).
    """
    _store, _token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    manager = _schedules_manager(request)
    return await _apply(
        request,
        _update_record,
        manager,
        str(schedule_id),
        enabled=payload.enabled,
    )


async def _update_record(
    manager: ScheduleManager, schedule_id: str, *, enabled: bool
) -> ScheduleView:
    record = await manager.set_enabled(schedule_id, enabled=enabled)
    return _schedule_view(record)


@router.delete(
    "/{schedule_id}",
    status_code=HTTP_204_NO_CONTENT,
    summary="Delete a schedule (row + fires cascade, then the engine trigger)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowlisted or CSRF token mismatch",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No schedule stored for this schedule id",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Schedules facade or engine unavailable",
        },
    },
)
async def delete_schedule(request: Request, schedule_id: uuid.UUID) -> Response:
    """Remove the schedule; a deleted schedule can never fire again.

    Mutation: session → origin → CSRF (ADR §5 order) before any effect.
    The facade deletes the domain row first (fires cascade; a fire
    dispatching in the window finds no row and skips value-free), then
    removes the engine trigger.
    """
    _store, _token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    manager = _schedules_manager(request)
    return await _apply(request, _delete_record, manager, str(schedule_id))


async def _delete_record(manager: ScheduleManager, schedule_id: str) -> Response:
    await manager.delete(schedule_id)
    return Response(status_code=HTTP_204_NO_CONTENT)


@router.get(
    "/{schedule_id}/fires",
    status_code=HTTP_200_OK,
    response_model=FirePage,
    summary="List the schedule's dispatched fires (dedup ledger, bounded page)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No schedule stored for this schedule id",
        },
    },
)
async def list_fires(
    request: Request,
    schedule_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> Response:
    """One bounded page of the facade-owned fire ledger (C05 rule 8).

    Safe method: session layer only. One row per ACTUALLY dispatched
    ``(schedule_id, planned_fire_time)`` pair — the storage level of the
    exactly-once guarantee — oldest first. ``run_id`` is the recorded
    link (``null`` until the run-row owner fills it); ``planned_run_id``
    is the deterministic identity the fire's run intent carries.
    """
    await _require_session(request)
    manager = _schedules_manager(request)
    identity = str(schedule_id)
    factory = _sessions(request)
    # Explicit-scope read of the schedule FIRST: an unknown schedule has
    # no ledger page (C01 rule 7 — never an unfiltered empty success).
    known = await manager.get(identity)
    if known is None:
        return _facade_error(
            request,
            ScheduleError(
                ScheduleErrorCode.SCHEDULE_NOT_FOUND,
                "no schedule record for this schedule_id",
            ),
        )
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT schedule_id::text AS schedule_id, "
                        "planned_fire_time, fired_at, run_id::text AS run_id "
                        "FROM job_schedule_fires WHERE schedule_id = :s "
                        "ORDER BY planned_fire_time LIMIT :l OFFSET :o"
                    ),
                    {"s": identity, "l": limit, "o": offset},
                )
            )
            .mappings()
            .all()
        )
        total = (
            await session.execute(
                text("SELECT COUNT(*) FROM job_schedule_fires WHERE schedule_id = :s"),
                {"s": identity},
            )
        ).scalar_one()
    return FirePage(
        schedule_id=identity,
        fires=[
            FireView(
                schedule_id=str(row["schedule_id"]),
                planned_fire_time=_wire_ts(row["planned_fire_time"]),
                fired_at=_wire_ts(row["fired_at"]),
                run_id=None if row["run_id"] is None else str(row["run_id"]),
                planned_run_id=planned_run_id(
                    str(row["schedule_id"]), row["planned_fire_time"]
                ),
            )
            for row in rows
        ],
        limit=limit,
        offset=offset,
        total=total,
    )


def register_schedules(app: FastAPI) -> None:
    """Register the schedules surface under the shared boundary (C01/C08)."""
    app.include_router(router)
