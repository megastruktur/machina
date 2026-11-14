"""Monitoring HTTP API: observations, findings and the notification inbox.

Ownership map (contracts.md **C10 rules 3/4**, **C01** envelope/identity/
scope, **C08 rule 2** boundary; SERVICES ``mai-api`` row; producers: the
0007 observation schema via bounded SQL reads and the reviewed
``mai.notifications.service`` evaluation pipeline; consumers
``10-web-dashboard``/``10-web-evidence`` and the ``10-verify`` gate
scenario):

- **Reads are bounded, server-scoped and honest** (C01 rules 5/7,
  C10 rule 4, C08 rule 6). Every list route names its ``server_id``
  explicitly in the path and pages with ``limit``/``offset`` bounds;
  a row owned by a different server can never appear in a page, and a
  single-entity read under the wrong server scope is the same value-free
  ``404`` as a missing row (scope mismatch never leaks existence).
  ``freshness`` on every observation view is DERIVED at request time
  through the frozen :func:`mai.contracts.observations.derive_freshness`
  (never a stored flag): ``fresh`` inside the collector-declared
  ``fresh_until`` horizon, ``stale`` past it, ``unknown`` when no sample
  was actually taken — the dashboard's loading/empty/stale/unknown states
  read this field directly.
- **Context refs usable in chat** (brief acceptance 1, 0007
  ``context_ref`` comments): observation views carry
  ``observation:<uuid>``, finding views ``finding:<uuid>`` and notice
  views the stored ``context_ref`` (``finding:<uuid>`` / ``run:<uuid>``
  written by the notification service) — bounded references, never
  payloads.
- **Ack is evidence-preserving** (brief acceptance 2, monitor-api rule
  in the 0007 header): ``POST .../notifications/{alert_id}/ack`` stamps
  ``acknowledged_at`` in one conditional UPDATE and deletes nothing —
  the notice, its finding and its producing observation all survive the
  ack, and an already-acknowledged notice is an idempotent ``200`` that
  never re-stamps. Session → origin → CSRF run before any state is
  touched; the consuming principal is the validated session only
  (C01 rule 2).
- **Re-evaluation composes the reviewed producer** (brief: findings
  re-evaluation): ``POST .../findings/evaluate`` re-runs
  :func:`mai.notifications.service.evaluate_findings` for one server's
  open findings under THIS surface's effective routing policy (below).
  Delivery-side effects stay owned by the notification service; the
  route never writes findings or observations itself.
- **The effective policy is stated, not hidden** (notification policy
  endpoint): v1 wires NO policy editor and NO policy storage — the
  surface evaluates under the frozen default policy (default cooldown,
  no quiet hours, no outbound channels ⇒ inbox-only delivery) and
  ``GET /api/v1/monitoring/notification-policy`` reports exactly that
  constant. A durable policy store is a later card's decision, not a
  silent invention here.
- **Severity on notice views is the real column**: since migration
  ``0017_notification_inbox`` every ``notification_alerts`` row carries
  ``severity`` and the notification service is its only writer; the
  live reads below select ``a.severity`` directly (the anchor join is
  gone). ``_alert_severity`` is RETAINED in this module as the frozen
  pre-0017 backfill oracle — the exact reader migration 0017's backfill
  reproduced — and ``tests/test_observation_schema.py`` imports it to
  pin that backfill, so it stays defined even though no live route
  calls it anymore.
- **AlertView is additive over the unified inbox**: global notices
  carry ``server_id IS NULL`` (no sentinel UUID), so ``AlertView
  .server_id`` is nullable; the external sender identity and message
  body arrive as the nullable ``source``/``body`` columns (0017), and
  finding/job_result notices keep them ``null``.

Every error renders the C01 envelope; an unmapped failure surfaces as
the generic ``internal_error`` (fail-closed), never a guessed success.

Route/doc listeners stay off (G1 minimal unauthenticated surface); the
schema is produced in-process via ``app.openapi()`` for the central
exporter owned by the ``openapi-client`` resource.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Final, Mapping

from fastapi import APIRouter, FastAPI, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.status import (
    HTTP_200_OK,
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
)
from mai.contracts.observations import (
    CONTEXT_REF_MAX_CHARS,
    SOURCE_MAX_CHARS,
    FindingSeverity,
    derive_freshness,
)
from mai.notifications.service import (
    NotificationError,
    RoutingPolicy,
    evaluate_findings,
)

__all__ = ["register_monitoring"]


# ---------------------------------------------------------------------------
# Transport bounds (module-owned policy, explicitly bounded)
# ---------------------------------------------------------------------------

#: List page bounds (C01 rule 5: list endpoints are bounded/paginated).
DEFAULT_PAGE_LIMIT: Final[int] = 50
MAX_PAGE_LIMIT: Final[int] = 200

#: The effective routing policy THIS surface evaluates under (see module
#: docstring "notification policy endpoint"): the reviewed service's frozen
#: defaults — default cooldown, no quiet hours, no outbound channels, i.e.
#: inbox-only delivery. One constant, reported verbatim by the policy
#: endpoint and used verbatim by the evaluate route: the reported policy
#: can never drift from the applied one.
_EFFECTIVE_POLICY: Final[RoutingPolicy] = RoutingPolicy()

#: Severity attention order for finding listing (frozen vocabulary; mirrors
#: the notification service's escalation rank — most severe first, oldest
#: next). Keys mirror :class:`mai.contracts.observations.FindingSeverity`.
_SEVERITY_ORDER: Final[dict[str, int]] = {
    FindingSeverity.CRITICAL.value: 0,
    FindingSeverity.WARNING.value: 1,
    FindingSeverity.INFO.value: 2,
}

#: Pre-0017 summary-tag grammar reader — RETAINED solely as part of the
#: frozen backfill oracle below (:func:`_alert_severity`, imported by
#: ``tests/test_observation_schema.py``); no live route reads tags
#: anymore (the real ``notification_alerts.severity`` column replaced
#: the writer/reader tag grammar in migration 0017).
_SEVERITY_TAG: Final[re.Pattern[str]] = re.compile(
    r"\[severity=(info|warning|critical)\]$"
)


# ---------------------------------------------------------------------------
# Wire shapes (transport only; truth is the 0007 rows and the service)
# ---------------------------------------------------------------------------


class ObservationView(BaseModel):
    """One collected health sample (0007 ``server_observations`` row).

    ``freshness`` is derived at request time (``fresh`` | ``stale`` |
    ``unknown``) — C08 rule 6 truthful states; ``context_ref`` is the
    chat-usable ``observation:<uuid>`` reference (bounded, never a
    payload). ``metric``/``value``/``observed_at`` are ``null`` exactly
    when the sample quality is ``unknown`` (schema CHECK).
    """

    model_config = ConfigDict(extra="forbid")

    observation_id: str
    server_id: str
    application_id: str | None
    source: str
    method: str
    quality: str
    metric: str | None
    value: str | None
    observed_at: str | None
    collected_at: str
    fresh_until: str | None
    freshness: str
    seq: int
    context_ref: str


class ObservationPage(BaseModel):
    """One bounded observation page for one explicitly named server."""

    model_config = ConfigDict(extra="forbid")

    server_id: str
    observations: list[ObservationView]
    limit: int
    offset: int
    total: int


class FindingView(BaseModel):
    """One derived condition (0007 ``observation_findings`` row).

    ``open`` is derived from ``resolved_at is null``; ``context_ref`` is
    the chat-usable ``finding:<uuid>`` reference the notification service
    anchors notices on.
    """

    model_config = ConfigDict(extra="forbid")

    finding_id: str
    server_id: str
    application_id: str | None
    observation_id: str | None
    kind: str
    dedup_key: str
    severity: str
    summary: str
    detail: str | None
    opened_at: str
    updated_at: str
    resolved_at: str | None
    open: bool
    context_ref: str


class FindingPage(BaseModel):
    """One bounded finding page for one explicitly named server."""

    model_config = ConfigDict(extra="forbid")

    server_id: str
    findings: list[FindingView]
    limit: int
    offset: int
    total: int


class AlertView(BaseModel):
    """One inbox notice (0007 ``notification_alerts`` row).

    ``severity`` is the real 0017 column (the notification service is
    its only writer); ``acked`` mirrors ``acknowledged_at is not null``
    so a consumer never compares timestamps for state. Global notices
    carry ``server_id: null`` (0017 dropped NOT NULL); ``source`` and
    ``body`` are the 0017 external-notice columns — ``null`` for
    finding/job_result notices, whose text is the ``summary``.
    """

    model_config = ConfigDict(extra="forbid")

    alert_id: str
    server_id: str | None
    origin: str
    dedup_key: str
    severity: str
    title: str
    summary: str | None
    source: str | None
    body: str | None
    context_ref: str | None
    delivery_status: str
    created_at: str
    delivered_at: str | None
    acknowledged_at: str | None
    acked: bool
    finding_id: str | None
    run_id: str | None


class AlertPage(BaseModel):
    """One bounded inbox page for one explicitly named server."""

    model_config = ConfigDict(extra="forbid")

    server_id: str
    alerts: list[AlertView]
    limit: int
    offset: int
    total: int


class AckResult(BaseModel):
    """The ack receipt: the notice view after the (idempotent) stamp."""

    model_config = ConfigDict(extra="forbid")

    alert: AlertView


class EvaluateOutcomeView(BaseModel):
    """What one re-evaluation decided about one condition (service view)."""

    model_config = ConfigDict(extra="forbid")

    alert_id: str
    dedup_key: str
    disposition: str
    delivery: str
    channels_attempted: list[str]


class EvaluateResult(BaseModel):
    """One bounded re-evaluation round over one server's open findings."""

    model_config = ConfigDict(extra="forbid")

    server_id: str
    evaluated: int
    outcomes: list[EvaluateOutcomeView]


class PolicyView(BaseModel):
    """The effective notification routing policy (see module docstring).

    ``cooldown_seconds`` is the re-delivery cooldown in whole seconds;
    ``quiet_hours`` is always ``null`` in v1 (no outbound delivery to
    defer); ``channels_by_severity`` maps the frozen severity vocabulary
    to its (empty) outbound routes — inbox-only delivery.
    """

    model_config = ConfigDict(extra="forbid")

    cooldown_seconds: int
    quiet_hours: None
    channels_by_severity: dict[str, list[str]]


# ---------------------------------------------------------------------------
# Accessors and small helpers
# ---------------------------------------------------------------------------


def _sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    """The lifespan-built session factory; without it fail closed."""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise BoundaryError(
            HTTP_503_SERVICE_UNAVAILABLE,
            "unavailable",
            "monitoring service is unavailable",
        )
    return factory


def _utcnow() -> datetime:
    """The request's derivation clock (timezone-aware UTC, C01 rule 5)."""
    return datetime.now(timezone.utc)


def _wire_ts(value: datetime) -> str:
    """Render one stored UTC timestamp in the frozen ``…Z`` wire form."""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _wire_ts_or_none(value: datetime | None) -> str | None:
    return None if value is None else _wire_ts(value)


def _context_ref(prefix: str, identity: uuid.UUID) -> str:
    """The bounded chat-usable reference for one domain row."""
    return f"{prefix}:{identity}"[:CONTEXT_REF_MAX_CHARS]


def _alert_severity(anchor_severity: object, summary: object) -> str:
    """Severity of one stored notice row (frozen pre-0017 oracle).

    RETAINED as the frozen pre-0017 backfill oracle: the exact reader
    migration 0017's severity backfill reproduced row-for-row (live
    anchor join first, the persisted ``[severity=<v>]`` summary tag
    second, ``info`` fallback). ``tests/test_observation_schema.py``
    imports this function to pin that backfill. Live routes no longer
    call it — they read ``notification_alerts.severity`` directly — so
    changing this function can only ever affect the pinned backfill
    proof, never the served views.
    """
    if isinstance(anchor_severity, str) and anchor_severity in _SEVERITY_ORDER:
        return anchor_severity
    if isinstance(summary, str):
        match = _SEVERITY_TAG.search(summary)
        if match is not None:
            return match.group(1)
    return FindingSeverity.INFO.value


# ---------------------------------------------------------------------------
# Routes (C01 prefix/envelope; C08 boundary; C10 rules 3/4)
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/monitoring", tags=["monitoring"])


@router.get(
    "/notification-policy",
    status_code=HTTP_200_OK,
    response_model=PolicyView,
    summary="Read the effective notification routing policy (frozen v1 default)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
    },
)
async def read_notification_policy(request: Request) -> PolicyView:
    """Report the exact policy the evaluate route applies.

    v1 wires no policy storage and no editor: the surface evaluates under
    one frozen module constant (:data:`_EFFECTIVE_POLICY`), so what this
    returns is verbatim what re-evaluation uses — the reported policy can
    never drift from the applied one.
    """
    await _require_session(request)  # layer 1
    policy = _EFFECTIVE_POLICY
    return PolicyView(
        cooldown_seconds=int(policy.cooldown.total_seconds()),
        quiet_hours=None,
        channels_by_severity={
            severity: list(policy.channels_for(severity))
            for severity in (severity.value for severity in FindingSeverity)
        },
    )


@router.get(
    "/servers/{server_id}/observations",
    status_code=HTTP_200_OK,
    response_model=ObservationPage,
    summary="List one server's health observations (bounded page, freshness derived)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Query parameters failed validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Monitoring service unavailable",
        },
    },
)
async def list_observations(
    request: Request,
    server_id: uuid.UUID,
    application_id: uuid.UUID | None = None,
    source: str | None = Query(default=None, max_length=SOURCE_MAX_CHARS),
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> ObservationPage:
    """One bounded page of ``server_observations``, newest sample first.

    Safe method: session layer only. The server scope is the storage
    boundary (C01 rule 7): rows of any other server can never appear.
    ``application_id`` narrows to one adopted application's samples,
    ``source`` to one collector identity. ``freshness`` is derived
    against the request clock — never a stored flag.
    """
    await _require_session(request)  # layer 1
    factory = _sessions(request)
    clauses = ["server_id = :server_id"]
    params: dict[str, object] = {"server_id": server_id}
    if application_id is not None:
        clauses.append("application_id = :application_id")
        params["application_id"] = application_id
    if source is not None:
        clauses.append("source = :source")
        params["source"] = source
    where = " AND ".join(clauses)
    now = _utcnow()
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT observation_id, server_id, application_id, source,"
                        " method, quality, metric, value, observed_at, collected_at,"
                        " fresh_until, seq"
                        f" FROM server_observations WHERE {where}"
                        " ORDER BY collected_at DESC, seq DESC, observation_id DESC"
                        " LIMIT :limit OFFSET :offset"
                    ),
                    params | {"limit": limit, "offset": offset},
                )
            )
            .mappings()
            .all()
        )
        total = (
            await session.execute(
                text(f"SELECT COUNT(*) FROM server_observations WHERE {where}"),
                params,
            )
        ).scalar_one()
    return ObservationPage(
        server_id=str(server_id),
        observations=[
            ObservationView(
                observation_id=str(row["observation_id"]),
                server_id=str(row["server_id"]),
                application_id=None
                if row["application_id"] is None
                else str(row["application_id"]),
                source=str(row["source"]),
                method=str(row["method"]),
                quality=str(row["quality"]),
                metric=row["metric"],
                value=row["value"],
                observed_at=_wire_ts_or_none(row["observed_at"]),
                collected_at=_wire_ts(row["collected_at"]),
                fresh_until=_wire_ts_or_none(row["fresh_until"]),
                freshness=derive_freshness(
                    str(row["quality"]),
                    row["observed_at"],
                    row["fresh_until"],
                    now,
                ).value,
                seq=int(row["seq"]),
                context_ref=_context_ref("observation", row["observation_id"]),
            )
            for row in rows
        ],
        limit=limit,
        offset=offset,
        total=int(total),
    )


@router.get(
    "/servers/{server_id}/observations/{observation_id}",
    status_code=HTTP_200_OK,
    response_model=ObservationView,
    summary="Read one health observation (server-scoped)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No observation for this id in this server scope",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Monitoring service unavailable",
        },
    },
)
async def read_observation(
    request: Request, server_id: uuid.UUID, observation_id: uuid.UUID
) -> ObservationView:
    """One sample row; a row owned by another server is a value-free 404."""
    await _require_session(request)  # layer 1
    factory = _sessions(request)
    async with factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT observation_id, server_id, application_id, source,"
                        " method, quality, metric, value, observed_at, collected_at,"
                        " fresh_until, seq"
                        " FROM server_observations"
                        " WHERE observation_id = :observation_id"
                        " AND server_id = :server_id"
                    ),
                    {"observation_id": observation_id, "server_id": server_id},
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise BoundaryError(
            HTTP_404_NOT_FOUND,
            "not_found",
            "no observation for this id in this server scope",
        )
    return ObservationView(
        observation_id=str(row["observation_id"]),
        server_id=str(row["server_id"]),
        application_id=None
        if row["application_id"] is None
        else str(row["application_id"]),
        source=str(row["source"]),
        method=str(row["method"]),
        quality=str(row["quality"]),
        metric=row["metric"],
        value=row["value"],
        observed_at=_wire_ts_or_none(row["observed_at"]),
        collected_at=_wire_ts(row["collected_at"]),
        fresh_until=_wire_ts_or_none(row["fresh_until"]),
        freshness=derive_freshness(
            str(row["quality"]), row["observed_at"], row["fresh_until"], _utcnow()
        ).value,
        seq=int(row["seq"]),
        context_ref=_context_ref("observation", row["observation_id"]),
    )


@router.get(
    "/servers/{server_id}/findings",
    status_code=HTTP_200_OK,
    response_model=FindingPage,
    summary="List one server's findings (bounded page, attention-ordered)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Query parameters failed validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Monitoring service unavailable",
        },
    },
)
async def list_findings(
    request: Request,
    server_id: uuid.UUID,
    open: bool | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> FindingPage:
    """One bounded finding page, most severe / oldest first.

    Safe method: session layer only; server scope is the storage boundary.
    ``open`` filters the derivation state (``true`` = unresolved
    conditions — the notification service's evaluation input; ``false`` =
    resolved history; omitted = all).
    """
    await _require_session(request)  # layer 1
    factory = _sessions(request)
    clauses = ["server_id = :server_id"]
    params: dict[str, object] = {"server_id": server_id}
    if open is not None:
        clauses.append("resolved_at IS NULL" if open else "resolved_at IS NOT NULL")
    where = " AND ".join(clauses)
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT finding_id, server_id, application_id,"
                        " observation_id, kind, dedup_key, severity, summary,"
                        " detail, opened_at, updated_at, resolved_at"
                        f" FROM observation_findings WHERE {where}"
                        " ORDER BY CASE severity"
                        " WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,"
                        " opened_at, finding_id"
                        " LIMIT :limit OFFSET :offset"
                    ),
                    params | {"limit": limit, "offset": offset},
                )
            )
            .mappings()
            .all()
        )
        total = (
            await session.execute(
                text(f"SELECT COUNT(*) FROM observation_findings WHERE {where}"),
                params,
            )
        ).scalar_one()
    return FindingPage(
        server_id=str(server_id),
        findings=[
            FindingView(
                finding_id=str(row["finding_id"]),
                server_id=str(row["server_id"]),
                application_id=None
                if row["application_id"] is None
                else str(row["application_id"]),
                observation_id=None
                if row["observation_id"] is None
                else str(row["observation_id"]),
                kind=str(row["kind"]),
                dedup_key=str(row["dedup_key"]),
                severity=str(row["severity"]),
                summary=str(row["summary"]),
                detail=row["detail"],
                opened_at=_wire_ts(row["opened_at"]),
                updated_at=_wire_ts(row["updated_at"]),
                resolved_at=_wire_ts_or_none(row["resolved_at"]),
                open=row["resolved_at"] is None,
                context_ref=_context_ref("finding", row["finding_id"]),
            )
            for row in rows
        ],
        limit=limit,
        offset=offset,
        total=int(total),
    )


@router.post(
    "/servers/{server_id}/findings/evaluate",
    status_code=HTTP_200_OK,
    response_model=EvaluateResult,
    summary="Re-evaluate one server's open findings into inbox notices",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_403_FORBIDDEN: {
            "model": ErrorEnvelope,
            "description": "Origin not allowed or CSRF token rejected",
        },
        HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorEnvelope,
            "description": "Notification evaluation failed",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Monitoring service unavailable",
        },
    },
)
async def evaluate_findings_route(
    request: Request, server_id: uuid.UUID
) -> EvaluateResult:
    """Re-run the notification evaluation for one server's open findings.

    Mutation: session → origin → CSRF (ADR §5 order) before any effect.
    The reviewed producer (:func:`mai.notifications.service.evaluate_findings`)
    owns every decision — dedup, cooldown, escalation, ack suppression —
    under this surface's frozen effective policy (inbox-only v1; see the
    policy endpoint). The route itself writes nothing: findings and
    observations stay collector-owned.
    """
    _store, _token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    factory = _sessions(request)
    try:
        outcomes = await evaluate_findings(
            factory,
            server_id,
            now=_utcnow(),
            policy=_EFFECTIVE_POLICY,
        )
    except NotificationError:
        # The producer's own rejections are value-free; this surface's
        # frozen policy cannot trip them (no channels, default cooldown),
        # so reaching here means persisted state outside the contract —
        # honest internal failure, never a guessed success.
        raise BoundaryError(
            HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "notification evaluation failed",
        ) from None
    return EvaluateResult(
        server_id=str(server_id),
        evaluated=len(outcomes),
        outcomes=[
            EvaluateOutcomeView(
                alert_id=str(outcome.alert_id),
                dedup_key=outcome.dedup_key,
                disposition=outcome.disposition,
                delivery=outcome.delivery,
                channels_attempted=list(outcome.channels_attempted),
            )
            for outcome in outcomes
        ],
    )


@router.get(
    "/servers/{server_id}/findings/{finding_id}",
    status_code=HTTP_200_OK,
    response_model=FindingView,
    summary="Read one finding (server-scoped)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No finding for this id in this server scope",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Monitoring service unavailable",
        },
    },
)
async def read_finding(
    request: Request, server_id: uuid.UUID, finding_id: uuid.UUID
) -> FindingView:
    """One condition row; a row owned by another server is a value-free 404."""
    await _require_session(request)  # layer 1
    factory = _sessions(request)
    async with factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT finding_id, server_id, application_id,"
                        " observation_id, kind, dedup_key, severity, summary,"
                        " detail, opened_at, updated_at, resolved_at"
                        " FROM observation_findings"
                        " WHERE finding_id = :finding_id"
                        " AND server_id = :server_id"
                    ),
                    {"finding_id": finding_id, "server_id": server_id},
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise BoundaryError(
            HTTP_404_NOT_FOUND,
            "not_found",
            "no finding for this id in this server scope",
        )
    return FindingView(
        finding_id=str(row["finding_id"]),
        server_id=str(row["server_id"]),
        application_id=None
        if row["application_id"] is None
        else str(row["application_id"]),
        observation_id=None
        if row["observation_id"] is None
        else str(row["observation_id"]),
        kind=str(row["kind"]),
        dedup_key=str(row["dedup_key"]),
        severity=str(row["severity"]),
        summary=str(row["summary"]),
        detail=row["detail"],
        opened_at=_wire_ts(row["opened_at"]),
        updated_at=_wire_ts(row["updated_at"]),
        resolved_at=_wire_ts_or_none(row["resolved_at"]),
        open=row["resolved_at"] is None,
        context_ref=_context_ref("finding", row["finding_id"]),
    )


@router.get(
    "/servers/{server_id}/notifications",
    status_code=HTTP_200_OK,
    response_model=AlertPage,
    summary="List one server's notification inbox (bounded page, newest first)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Query parameters failed validation",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Monitoring service unavailable",
        },
    },
)
async def list_notifications(
    request: Request,
    server_id: uuid.UUID,
    unacknowledged: bool | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> AlertPage:
    """One bounded inbox page of persisted notices, newest first.

    Safe method: session layer only; server scope is the storage boundary.
    The inbox is the durable record: acknowledged notices stay listed
    (evidence outlives the ack); ``unacknowledged=true`` is the attention
    view, ``false`` the acknowledged history.
    """
    await _require_session(request)  # layer 1
    factory = _sessions(request)
    clauses = ["a.server_id = :server_id"]
    params: dict[str, object] = {"server_id": server_id}
    if unacknowledged is not None:
        clauses.append(
            "a.acknowledged_at IS NULL"
            if unacknowledged
            else "a.acknowledged_at IS NOT NULL"
        )
    where = " AND ".join(clauses)
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT a.alert_id, a.server_id, a.origin, a.dedup_key,"
                        " a.title, a.summary, a.severity, a.source, a.body,"
                        " a.context_ref, a.delivery_status,"
                        " a.created_at, a.delivered_at, a.acknowledged_at,"
                        " a.finding_id, a.run_id"
                        " FROM notification_alerts a"
                        f" WHERE {where}"
                        " ORDER BY a.created_at DESC, a.alert_id DESC"
                        " LIMIT :limit OFFSET :offset"
                    ),
                    params | {"limit": limit, "offset": offset},
                )
            )
            .mappings()
            .all()
        )
        total = (
            await session.execute(
                text(f"SELECT COUNT(*) FROM notification_alerts a WHERE {where}"),
                params,
            )
        ).scalar_one()
    return AlertPage(
        server_id=str(server_id),
        alerts=[_alert_view(row) for row in rows],
        limit=limit,
        offset=offset,
        total=int(total),
    )


@router.get(
    "/servers/{server_id}/notifications/{alert_id}",
    status_code=HTTP_200_OK,
    response_model=AlertView,
    summary="Read one inbox notice (server-scoped)",
    responses={
        HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "No valid session",
        },
        HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "No notice for this id in this server scope",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Monitoring service unavailable",
        },
    },
)
async def read_notification(
    request: Request, server_id: uuid.UUID, alert_id: uuid.UUID
) -> AlertView:
    """One notice row; a row owned by another server is a value-free 404."""
    await _require_session(request)  # layer 1
    row = await _alert_row(_sessions(request), server_id, alert_id)
    if row is None:
        raise BoundaryError(
            HTTP_404_NOT_FOUND,
            "not_found",
            "no notice for this id in this server scope",
        )
    return _alert_view(row)


@router.post(
    "/servers/{server_id}/notifications/{alert_id}/ack",
    status_code=HTTP_200_OK,
    response_model=AckResult,
    summary="Acknowledge one inbox notice (evidence-preserving, idempotent)",
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
            "description": "No notice for this id in this server scope",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Monitoring service unavailable",
        },
    },
)
async def acknowledge_notification(
    request: Request, server_id: uuid.UUID, alert_id: uuid.UUID
) -> AckResult:
    """Stamp ``acknowledged_at`` on one notice; delete nothing.

    Mutation: session → origin → CSRF (ADR §5 order) before any state is
    touched. The conditional UPDATE matches the server scope (C01 rule 7)
    and an empty ``acknowledged_at`` (idempotency: the first stamp wins,
    a replay never re-stamps or shifts the timestamp). Ack is NOT a
    delete: the notice, its finding and its producing observation all
    survive — the inbox keeps the row and the evidence chain is intact.
    """
    _store, _token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    factory = _sessions(request)
    now = _utcnow()
    async with factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT alert_id FROM notification_alerts"
                        " WHERE alert_id = :alert_id AND server_id = :server_id"
                    ),
                    {"alert_id": alert_id, "server_id": server_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise BoundaryError(
                HTTP_404_NOT_FOUND,
                "not_found",
                "no notice for this id in this server scope",
            )
        await session.execute(
            text(
                "UPDATE notification_alerts SET acknowledged_at = :now"
                " WHERE alert_id = :alert_id AND acknowledged_at IS NULL"
            ),
            {"now": now, "alert_id": alert_id},
        )
        await session.commit()
    stamped = await _alert_row(factory, server_id, alert_id)
    if stamped is None:  # unreachable: the UNIQUE row raced away
        raise BoundaryError(
            HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "acknowledged notice vanished",
        )
    return AckResult(alert=_alert_view(stamped))


@router.post(
    "/servers/{server_id}/notifications/{alert_id}/unacknowledge",
    status_code=HTTP_200_OK,
    response_model=AckResult,
    summary="Unacknowledge one inbox notice (scope-checked, idempotent)",
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
            "description": "No notice for this id in this server scope",
        },
        HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorEnvelope,
            "description": "Monitoring service unavailable",
        },
    },
)
async def unacknowledge_notification(
    request: Request, server_id: uuid.UUID, alert_id: uuid.UUID
) -> AckResult:
    """Clear ``acknowledged_at`` on one notice; delete nothing.

    Mutation: session → origin → CSRF (ADR §5 order) before any state is
    touched. The value-free ``404`` mirrors the ack route exactly: a row
    owned by another server and a missing row are indistinguishable (no
    existence leak, C01 rule 7). Idempotent: unacknowledging an already
    unacknowledged notice changes nothing and answers ``200``. Unack is
    NOT a delete: the notice, its finding and its producing observation
    survive; for finding notices re-opening the notice re-arms the
    reminder logic (the notification service stops suppressing
    re-delivery once ``acknowledged_at`` is cleared again).
    """
    _store, _token = await _require_session(request)  # layer 1
    _require_allowed_origin(request)  # layer 2
    _require_csrf(request)  # layer 3
    factory = _sessions(request)
    async with factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT alert_id FROM notification_alerts"
                        " WHERE alert_id = :alert_id AND server_id = :server_id"
                    ),
                    {"alert_id": alert_id, "server_id": server_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise BoundaryError(
                HTTP_404_NOT_FOUND,
                "not_found",
                "no notice for this id in this server scope",
            )
        await session.execute(
            text(
                "UPDATE notification_alerts SET acknowledged_at = NULL"
                " WHERE alert_id = :alert_id"
            ),
            {"alert_id": alert_id},
        )
        await session.commit()
    cleared = await _alert_row(factory, server_id, alert_id)
    if cleared is None:  # unreachable: the UNIQUE row raced away
        raise BoundaryError(
            HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "unacknowledged notice vanished",
        )
    return AckResult(alert=_alert_view(cleared))


# ---------------------------------------------------------------------------
# Shared row loaders/views
# ---------------------------------------------------------------------------

_ALERT_ROW_SQL = text(
    "SELECT a.alert_id, a.server_id, a.origin, a.dedup_key,"
    " a.title, a.summary, a.severity, a.source, a.body, a.context_ref,"
    " a.delivery_status, a.created_at, a.delivered_at, a.acknowledged_at,"
    " a.finding_id, a.run_id"
    " FROM notification_alerts a"
    " WHERE a.alert_id = :alert_id AND a.server_id = :server_id"
)


async def _alert_row(
    factory: async_sessionmaker[AsyncSession],
    server_id: uuid.UUID,
    alert_id: uuid.UUID,
) -> Mapping[str, object] | None:
    """One notice row under its server scope; ``None`` = missing/foreign."""
    async with factory() as session:
        row = (
            (
                await session.execute(
                    _ALERT_ROW_SQL,
                    {"alert_id": alert_id, "server_id": server_id},
                )
            )
            .mappings()
            .first()
        )
    return row


def _alert_view(row: Mapping[str, object]) -> AlertView:
    """One inbox notice view from a row carrying the 0017 columns.

    The single row→view mapping for BOTH monitoring surfaces and the
    global notification surface (``mai.api.notifications`` imports this
    mapper), so the unified inbox can never render two shapes.
    """
    return AlertView(
        alert_id=str(row["alert_id"]),
        server_id=None if row["server_id"] is None else str(row["server_id"]),
        origin=str(row["origin"]),
        dedup_key=str(row["dedup_key"]),
        severity=str(row["severity"]),
        title=str(row["title"]),
        summary=row["summary"],
        source=row["source"],
        body=row["body"],
        context_ref=row["context_ref"],
        delivery_status=str(row["delivery_status"]),
        created_at=_wire_ts(row["created_at"]),
        delivered_at=_wire_ts_or_none(row["delivered_at"]),
        acknowledged_at=_wire_ts_or_none(row["acknowledged_at"]),
        acked=row["acknowledged_at"] is not None,
        finding_id=None if row["finding_id"] is None else str(row["finding_id"]),
        run_id=None if row["run_id"] is None else str(row["run_id"]),
    )


def register_monitoring(app: FastAPI) -> None:
    """Register the monitoring surface under the shared boundary (api-wiring)."""
    app.include_router(router)
